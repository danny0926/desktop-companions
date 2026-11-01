"""Small local contracts shared by movement, observation, and event pacing."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import math


@dataclass(frozen=True)
class Rect:
    """Absolute virtual-desktop coordinates; right/bottom are exclusive."""

    left: float
    top: float
    right: float
    bottom: float

    def __post_init__(self):
        if not all(math.isfinite(v) for v in (self.left, self.top, self.right, self.bottom)):
            raise ValueError("Rectangle coordinates must be finite.")
        if self.right <= self.left or self.bottom <= self.top:
            raise ValueError("Rectangle must have positive width and height.")

    @property
    def width(self) -> float:
        return self.right - self.left

    @property
    def height(self) -> float:
        return self.bottom - self.top

    def contains(self, point: tuple[float, float], margin: float = 0) -> bool:
        x, y = point
        return self.left - margin <= x < self.right + margin and self.top - margin <= y < self.bottom + margin


@dataclass(frozen=True)
class SpriteGeometry:
    """Outer window size and the foot anchor inside that window, in UI pixels.

    Supply the same outer size used by the shell's clamp, not only the PNG size.
    For a 192x224 frame, anchor (96,216), and 12/10 padding on each side,
    from_frame() yields window 216x244 and anchor (108,226).
    """

    width: float
    height: float
    anchor_x: float
    anchor_y: float

    def __post_init__(self):
        if not all(math.isfinite(v) for v in (self.width, self.height, self.anchor_x, self.anchor_y)):
            raise ValueError("Sprite geometry must be finite.")
        if self.width <= 0 or self.height <= 0 or not 0 <= self.anchor_x <= self.width or not 0 <= self.anchor_y <= self.height:
            raise ValueError("Sprite size/anchor is invalid.")

    @classmethod
    def from_frame(cls, width: float, height: float, anchor: tuple[float, float], *,
                   padding: tuple[float, float, float, float] = (0, 0, 0, 0)) -> SpriteGeometry:
        left, top, right, bottom = padding
        if any(not math.isfinite(v) or v < 0 for v in padding):
            raise ValueError("Frame padding must be finite and nonnegative.")
        return cls(width + left + right, height + top + bottom, anchor[0] + left, anchor[1] + top)


@dataclass(frozen=True)
class BusyState:
    """UI-owned activity flags. No keyboard hooks are installed to infer these."""

    speaking: bool = False
    typing: bool = False
    input_active: bool = False
    dragging: bool = False
    thinking: bool = False
    synthesizing: bool = False
    manual_chat: bool = False
    fullscreen: bool = False
    locked: bool = False
    hidden: bool = False
    desktop_available: bool | None = True
    resource_waiting: bool = False
    voice_unavailable: bool = False
    interacting: bool = False
    chat_active: bool = False

    @property
    def action_block_reason(self) -> str | None:
        for name in ("hidden", "locked", "fullscreen", "dragging", "interacting", "typing", "input_active",
                     "manual_chat", "speaking", "thinking", "synthesizing"):
            if getattr(self, name):
                return name
        if self.desktop_available is not True:
            return "desktop_unavailable"
        return None

    @property
    def speech_block_reason(self) -> str | None:
        return self.action_block_reason or ("resource_waiting" if self.resource_waiting else
                                            "voice_unavailable" if self.voice_unavailable else None)


class MotionState(str, Enum):
    IDLE = "idle"
    IDLE_ACTION = "idle_action"
    WALKING_LEFT = "walking_left"
    WALKING_RIGHT = "walking_right"
    WALKING_DOWN = "walking_down"
    WALKING_UP = "walking_up"
    REST = "rest"
    SEATED = "seated"
    SPEAKING = "speaking"
    DRAGGED = "dragged"
    PAUSED = "paused"


@dataclass(frozen=True)
class MotionDecision:
    position: tuple[float, float]
    state: MotionState
    animation: str
    reason: str
    moved: bool
    anchor_position: tuple[float, float]
    animation_fallback: bool = False


class ActivityCategory(str, Enum):
    CODING = "coding"
    BROWSER = "browser"
    COMMUNICATION = "communication"
    GAME = "game"
    MEDIA = "media"
    OTHER = "other"
    UNKNOWN = "unknown"


class ObservationSource(str, Enum):
    WIN32 = "win32"
    INJECTED = "injected"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ActivitySnapshot:
    captured_at: float
    idle_seconds: float | None
    category: ActivityCategory = ActivityCategory.UNKNOWN
    foreground_fullscreen: bool = False
    desktop_available: bool | None = True
    work_area: Rect | None = None
    mouse: tuple[int, int] | None = None
    source: ObservationSource = ObservationSource.INJECTED
    diagnostics: tuple[str, ...] = ()
    foreground_owned: bool = False

    def __post_init__(self):
        if not math.isfinite(self.captured_at):
            raise ValueError("Snapshot time must be finite.")
        if self.idle_seconds is not None and (not math.isfinite(self.idle_seconds) or self.idle_seconds < 0):
            raise ValueError("Idle duration must be finite and nonnegative.")


class ProactiveEventKind(str, Enum):
    RETURN_FROM_IDLE = "return_from_idle"
    FOCUS_BREAK = "focus_break"
    SCREEN_OBSERVATION = "screen_observation"
    COMPANION_TOPIC = "companion_topic"


@dataclass(frozen=True)
class ProactiveEvent:
    event_id: str
    kind: ProactiveEventKind
    observed_at: float
    category: ActivityCategory
    source: ObservationSource
    idle_seconds: float = 0
    active_seconds: float = 0
    opener_topic: str = ""
    topic_seed: int = 0
    screen_scope: str = "active_window"

    def model_context(self) -> dict:
        """A system-event payload, never a fabricated user message.

        Only these coarse facts should be sent to an optional dedicated agent
        event path. They do not establish what the person read, typed, or did.
        """
        return {"source": "local_activity_system_event", "event_id": self.event_id,
                "kind": self.kind.value, "observation_source": self.source.value,
                "category": self.category.value, "idle_seconds": round(self.idle_seconds),
                "active_seconds": round(self.active_seconds), "opener_topic": self.opener_topic,
                "topic_seed": self.topic_seed, "screen_scope": self.screen_scope,
                "instruction": "Optional brief companion remark. Do not claim task completion or knowledge of screen/input content."}


@dataclass(frozen=True)
class ProactiveDecision:
    event: ProactiveEvent
    action: str
    animation: str
    request_speech: bool
    reason: str
    expires_at: float
    generation: int
    max_reply_chars: int = 36
