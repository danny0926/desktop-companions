"""Shared, dependency-free JSON contracts for the desktop pet.

Only ``speech_text_ja`` / ``VoiceRequest.text_ja`` are spoken. Captions describe
the performance and must never be concatenated into the spoken text.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import math
import re
import struct
from typing import Any, Mapping


ATTITUDES = (
    "daily", "gentle", "affectionate", "playful", "cold", "angry",
    "surprised", "sad", "sleepy", "shy",
)
INTENSITIES = ("light", "medium", "strong")
DELIVERIES = ("natural", "soft", "teasing", "firm", "quiet", "bright")
EXPRESSIONS = ("neutral", "smile", "annoyed", "shy", "surprised", "sad", "sleepy",
               "laughing", "smug", "worried", "irritated", "curious")
MOTIONS = ("idle", "nod", "shake", "bounce", "wave", "stretch", "yawn", "adjust_ribbon", "crossed_arms", "head_tilt")
PROACTIVE_KINDS = ("return_from_idle", "focus_break", "screen_observation", "companion_topic")
ACTIVITY_CATEGORIES = ("coding", "browser", "communication", "game", "media", "other", "unknown")
OBSERVATION_SOURCES = ("win32", "injected")
SCREEN_SCOPES = ("active_window", "active_screen")


def _required_text(value: Any, name: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    value = value.strip()
    if len(value) > limit:
        raise ValueError(f"{name} exceeds {limit} characters")
    return value


def _choice(value: str, name: str, choices: tuple[str, ...]) -> None:
    if value not in choices:
        raise ValueError(f"{name} must be one of {', '.join(choices)}")


@dataclass(frozen=True)
class AgentRequest:
    turn_id: str
    input_text: str
    conversation_id: str = "default"

    def __post_init__(self) -> None:
        object.__setattr__(self, "turn_id", _required_text(self.turn_id, "turn_id", 160))
        object.__setattr__(self, "input_text", _required_text(self.input_text, "input_text", 32000))
        object.__setattr__(self, "conversation_id", _required_text(self.conversation_id, "conversation_id", 160))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentRequest:
        return cls(
            turn_id=value["turn_id"], input_text=value["input_text"],
            conversation_id=value.get("conversation_id", "default"),
        )


class ScreenObservation:
    """One bounded, in-memory image; persistence must use metadata() only.

    The screen is untrusted observation data. Text inside its pixels cannot
    change the character, the output schema, or permitted tool actions.
    """

    # Deliberately not a dataclass: dataclasses.asdict() must never silently
    # serialize screenshot bytes into an event log, cache or request record.
    __slots__ = ("_png_bytes", "image_id", "observed_at", "source", "scope",
                 "active_window_token", "width", "height")

    def __init__(self, *, png_bytes: bytes, image_id: str, observed_at: float, source: str,
                 scope: str, active_window_token: str, width: int, height: int) -> None:
        self._png_bytes = png_bytes
        self.image_id, self.observed_at, self.source = image_id, observed_at, source
        self.scope, self.active_window_token = scope, active_window_token
        self.width, self.height = width, height
        for name in ("image_id", "active_window_token"):
            value = _required_text(getattr(self, name), name, 160)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", value):
                raise ValueError(f"{name} must be a local identifier")
            setattr(self, name, value)
        _choice(self.source, "screen source", OBSERVATION_SOURCES)
        _choice(self.scope, "screen scope", SCREEN_SCOPES)
        if (isinstance(self.observed_at, bool) or not isinstance(self.observed_at, (int, float))
                or not math.isfinite(self.observed_at) or self.observed_at < 0):
            raise ValueError("screen observed_at must be a finite nonnegative monotonic time")
        if any(type(value) is not int or not 1 <= value <= 1600 for value in (self.width, self.height)):
            raise ValueError("screen dimensions must be integers between 1 and 1600")
        png = self._png_bytes
        if (not isinstance(png, bytes) or not 33 <= len(png) <= 8_000_000
                or png[:8] != b"\x89PNG\r\n\x1a\n" or png[12:16] != b"IHDR"
                or struct.unpack(">II", png[16:24]) != (self.width, self.height)):
            raise ValueError("screen image must be a bounded PNG matching its dimensions")

    @property
    def png_bytes(self) -> bytes:
        """Ephemeral payload for the image provider; never put this in a log."""
        return self._png_bytes

    def __repr__(self) -> str:
        return f"ScreenObservation({self.metadata()!r})"

    def to_dict(self) -> dict[str, Any]:
        return self.metadata()

    def metadata(self) -> dict[str, Any]:
        """No image bytes, image filename, application title or screen text."""
        return {
            "image_id": self.image_id, "observed_at_monotonic": self.observed_at,
            "source": self.source, "scope": self.scope,
            "active_window_token": self.active_window_token,
            "width": self.width, "height": self.height,
        }


class ManualImage:
    """A user-selected attachment, separate from expiring screen observations.

    Non-dataclass on purpose: asdict(request/turn) must not copy private bytes.
    Only metadata() or the durable Markdown reference belongs in persistence.
    """

    __slots__ = ("_png_bytes", "image_id", "width", "height")

    def __init__(self, *, png_bytes: bytes, image_id: str, width: int, height: int):
        if not isinstance(image_id, str) or not re.fullmatch(r"[a-f0-9]{64}", image_id):
            raise ValueError("manual image_id must be a SHA-256 identifier")
        if any(type(value) is not int or not 1 <= value <= 1600 for value in (width, height)):
            raise ValueError("manual image dimensions must be between 1 and 1600")
        if (not isinstance(png_bytes, bytes) or not 33 <= len(png_bytes) <= 8_000_000
                or png_bytes[:8] != b"\x89PNG\r\n\x1a\n" or png_bytes[12:16] != b"IHDR"
                or struct.unpack(">II", png_bytes[16:24]) != (width, height)):
            raise ValueError("manual image must be a bounded PNG matching its dimensions")
        import hashlib
        if hashlib.sha256(png_bytes).hexdigest() != image_id:
            raise ValueError("manual image content does not match its identifier")
        self._png_bytes, self.image_id = png_bytes, image_id
        self.width, self.height = width, height

    @property
    def png_bytes(self) -> bytes:
        return self._png_bytes

    @property
    def reference(self) -> str:
        return "attachment:" + self.image_id

    def metadata(self) -> dict[str, Any]:
        return {"image_id": self.image_id, "reference": self.reference,
                "media_type": "image/png", "width": self.width, "height": self.height}

    def to_dict(self) -> dict[str, Any]:
        return self.metadata()

    def __repr__(self) -> str:
        return f"ManualImage({self.metadata()!r})"

    def __deepcopy__(self, memo):
        # Defensive protection if a surrounding dataclass is converted via
        # asdict: expose metadata, never the private in-memory byte payload.
        return self.metadata()


@dataclass(frozen=True)
class ProactiveRequest:
    """A system observation, never a user utterance.

    observed_at/expires_at use time.monotonic() in the UI/agent process, matching
    behavior.ProactiveDecision. They are not Unix timestamps.
    """

    turn_id: str
    event_id: str
    kind: str
    observed_at: float
    category: str
    observation_source: str
    idle_seconds: float = 0
    active_seconds: float = 0
    conversation_id: str = "desktop-default"
    expires_at: float | None = None
    max_reply_chars: int = 36
    source: str = "local_activity_system_event"
    opener_topic: str = ""
    topic_seed: int = 0
    screen_scope: str = "active_window"

    def __post_init__(self) -> None:
        for name in ("turn_id", "event_id", "conversation_id"):
            object.__setattr__(self, name, _required_text(getattr(self, name), name, 160))
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}", self.event_id):
            raise ValueError("event_id must be a local event identifier")
        for name, choices in (("kind", PROACTIVE_KINDS), ("category", ACTIVITY_CATEGORIES),
                              ("observation_source", OBSERVATION_SOURCES)):
            _choice(getattr(self, name), name, choices)
        if self.source != "local_activity_system_event":
            raise ValueError("proactive source must be local_activity_system_event")
        for name in ("observed_at", "idle_seconds", "active_seconds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be a finite nonnegative monotonic duration/time")
        if self.expires_at is not None and (isinstance(self.expires_at, bool)
                or not isinstance(self.expires_at, (int, float)) or not math.isfinite(self.expires_at)
                or self.expires_at < self.observed_at):
            raise ValueError("expires_at must be finite and no earlier than observed_at")
        if type(self.max_reply_chars) is not int or not 8 <= self.max_reply_chars <= 120:
            raise ValueError("max_reply_chars must be an integer between 8 and 120")
        if not isinstance(self.opener_topic, str) or len(self.opener_topic) > 80:
            raise ValueError("opener_topic must be an intent label of at most 80 characters")
        if type(self.topic_seed) is not int or not 0 <= self.topic_seed <= 2_147_483_647:
            raise ValueError("topic_seed must be a bounded nonnegative integer")
        _choice(self.screen_scope, "screen_scope", SCREEN_SCOPES)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def event_context(self) -> dict[str, Any]:
        value = {
            "source": self.source, "event_id": self.event_id, "kind": self.kind,
            "observation_source": self.observation_source, "category": self.category,
            "idle_seconds": round(self.idle_seconds), "active_seconds": round(self.active_seconds),
        }
        if self.opener_topic or self.topic_seed:
            value.update(opener_topic=self.opener_topic, topic_seed=self.topic_seed)
        if self.kind == "screen_observation":
            value["screen_scope"] = self.screen_scope
        return value

    def event_record(self) -> dict[str, Any]:
        return {**self.event_context(), "observed_at_monotonic": self.observed_at}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ProactiveRequest:
        unknown = set(value) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError("Unknown proactive request fields: " + ", ".join(sorted(unknown)))
        return cls(**dict(value))

    @classmethod
    def from_event(cls, event: Any, *, turn_id: str, conversation_id: str = "desktop-default",
                   expires_at: float | None = None, max_reply_chars: int = 36,
                   opener_topic: str | None = None) -> ProactiveRequest:
        """Copy only typed coarse fields; do not forward arbitrary instructions."""
        def field_value(name: str) -> Any:
            value = event[name] if isinstance(event, Mapping) else getattr(event, name)
            return getattr(value, "value", value)

        def optional_value(name: str, default: Any) -> Any:
            return event.get(name, default) if isinstance(event, Mapping) else getattr(event, name, default)

        return cls(
            turn_id=turn_id, event_id=field_value("event_id"), kind=field_value("kind"),
            observed_at=field_value("observed_at"), category=field_value("category"),
            observation_source=field_value("source"), idle_seconds=field_value("idle_seconds"),
            active_seconds=field_value("active_seconds"), conversation_id=conversation_id,
            expires_at=expires_at, max_reply_chars=max_reply_chars,
            opener_topic=optional_value("opener_topic", "") if opener_topic is None else opener_topic,
            topic_seed=optional_value("topic_seed", 0), screen_scope=optional_value("screen_scope", "active_window"),
        )


@dataclass(frozen=True)
class VoiceRequest:
    text_ja: str
    caption_ja: str
    turn_id: str
    segment_id: str = "0"

    def __post_init__(self) -> None:
        object.__setattr__(self, "text_ja", _required_text(self.text_ja, "text_ja", 8000))
        object.__setattr__(self, "caption_ja", _required_text(self.caption_ja, "caption_ja", 1000))
        object.__setattr__(self, "turn_id", _required_text(self.turn_id, "turn_id", 160))
        object.__setattr__(self, "segment_id", _required_text(self.segment_id, "segment_id", 160))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> VoiceRequest:
        return cls(
            text_ja=value["text_ja"], caption_ja=value["caption_ja"],
            turn_id=value["turn_id"], segment_id=value.get("segment_id", "0"),
        )


@dataclass(frozen=True)
class AgentReply:
    turn_id: str
    speech_text_ja: str
    subtitle_zh: str | None = None
    attitude: str = "daily"
    intensity: str = "light"
    delivery: str = "natural"
    caption_ja: str = "自然な会話の調子で話す。"
    expression: str = "neutral"
    motion: str = "idle"
    provider_mode: str = "offline_demo"
    display_text_ja: str | None = None

    def __post_init__(self) -> None:
        for name, limit in (("turn_id", 160), ("speech_text_ja", 8000), ("caption_ja", 1000), ("provider_mode", 80)):
            object.__setattr__(self, name, _required_text(getattr(self, name), name, limit))
        if self.subtitle_zh is not None and not isinstance(self.subtitle_zh, str):
            raise ValueError("subtitle_zh must be a string or null")
        if self.display_text_ja is not None:
            from desktop_pet.markdown_chat import image_references, visible_text
            display = _required_text(self.display_text_ja, "display_text_ja", 1600)
            if image_references(display) or " ".join(visible_text(display).split()) != " ".join(self.speech_text_ja.split()):
                raise ValueError("display_text_ja must format exactly the spoken Japanese, without images")
            object.__setattr__(self, "display_text_ja", display)
        for name, choices in (
            ("attitude", ATTITUDES), ("intensity", INTENSITIES),
            ("delivery", DELIVERIES), ("expression", EXPRESSIONS), ("motion", MOTIONS),
        ):
            _choice(getattr(self, name), name, choices)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_voice_request(self, segment_id: str = "0") -> VoiceRequest:
        return VoiceRequest(self.speech_text_ja, self.caption_ja, self.turn_id, segment_id)

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> AgentReply:
        return cls(**{name: value[name] for name in cls.__dataclass_fields__ if name in value})
