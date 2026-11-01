"""Gentle defaults; roaming and proactive behavior can be disabled separately."""
from __future__ import annotations

from dataclasses import dataclass, field, fields
import json
import math
from pathlib import Path


def _number(name: str, value: float, minimum: float, maximum: float):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}.")


@dataclass(frozen=True)
class RoamingConfig:
    enabled: bool = True
    speed_pixels_per_second: float = 34
    idle_min_seconds: float = 12
    idle_max_seconds: float = 28
    rest_seconds: float = 50
    rest_probability: float = 0.2
    min_walk_pixels: float = 72
    max_walk_pixels: float = 260
    side_padding: float = 8
    bottom_padding: float = 0
    mouse_clearance: float = 36
    mouse_pause_seconds: float = 2
    resume_delay_seconds: float = 2
    max_step_seconds: float = 0.15

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("roaming.enabled must be boolean.")
        for name in ("speed_pixels_per_second", "idle_min_seconds", "idle_max_seconds", "rest_seconds",
                     "min_walk_pixels", "max_walk_pixels", "mouse_pause_seconds", "resume_delay_seconds"):
            _number(name, getattr(self, name), 0.01, 3600)
        for name in ("side_padding", "bottom_padding", "mouse_clearance"):
            _number(name, getattr(self, name), 0, 1000)
        _number("rest_probability", self.rest_probability, 0, 1)
        _number("max_step_seconds", self.max_step_seconds, 0.01, 0.5)
        if self.idle_min_seconds > self.idle_max_seconds or self.min_walk_pixels > self.max_walk_pixels:
            raise ValueError("Roaming minimums must not exceed maximums.")


def _process_categories() -> dict[str, tuple[str, ...]]:
    return {"coding": ("code.exe", "devenv.exe", "pycharm64.exe", "idea64.exe", "rider64.exe", "cursor.exe"),
            "browser": ("chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe"),
            "communication": ("slack.exe", "discord.exe", "teams.exe", "ms-teams.exe", "line.exe", "telegram.exe"),
            "game": ("cs2.exe", "dota2.exe", "valorant-win64-shipping.exe", "genshinimpact.exe", "minecraft.windows.exe"),
            "media": ("vlc.exe", "spotify.exe", "mpv.exe")}


@dataclass(frozen=True)
class ActivityConfig:
    enabled: bool = True
    sample_interval_seconds: float = 2
    process_cache_seconds: float = 15
    process_categories: dict[str, tuple[str, ...]] = field(default_factory=_process_categories)

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("activity.enabled must be boolean.")
        _number("sample_interval_seconds", self.sample_interval_seconds, 1, 60)
        _number("process_cache_seconds", self.process_cache_seconds, 1, 3600)
        seen: set[str] = set()
        for category, names in self.process_categories.items():
            if category not in {"coding", "browser", "communication", "game", "media", "other"} or not isinstance(names, (list, tuple)):
                raise ValueError("Invalid process category mapping.")
            for name in names:
                if not isinstance(name, str) or not name or any(c in name for c in "/\\") or name.lower() in seen:
                    raise ValueError("Use unique executable basenames in process_categories.")
                seen.add(name.lower())


@dataclass(frozen=True)
class ProactiveConfig:
    enabled: bool = True
    mode: str = "occasional"
    cooldown_seconds: float = 600
    max_events_per_hour: int = 3
    return_after_idle_seconds: float = 180
    returned_input_seconds: float = 8
    return_cooldown_seconds: float = 900
    focus_after_seconds: float = 1800
    focus_active_idle_seconds: float = 30
    focus_cooldown_seconds: float = 1800
    manual_cooldown_seconds: float = 90
    event_ttl_seconds: float = 60
    max_observation_gap_seconds: float = 10
    max_reply_chars: int = 36
    screen_enabled: bool = True
    screen_min_seconds: float = 300
    screen_max_seconds: float = 720
    screen_event_ttl_seconds: float = 60
    screen_max_idle_seconds: float = 60
    screen_scope: str = "active_window"

    def __post_init__(self):
        if not isinstance(self.enabled, bool) or self.mode not in ("quiet", "occasional", "lively"):
            raise ValueError("Use proactive.enabled boolean and mode quiet/occasional/lively.")
        if not isinstance(self.screen_enabled, bool) or self.screen_scope not in ("active_window", "active_screen"):
            raise ValueError("Use screen_enabled boolean and screen_scope active_window/active_screen.")
        for item in fields(self):
            if item.name not in ("enabled", "mode", "max_events_per_hour", "max_reply_chars", "screen_enabled", "screen_scope"):
                _number(item.name, getattr(self, item.name), 0.01, 86400)
        for name, maximum in (("max_events_per_hour", 30), ("max_reply_chars", 120)):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}.")
        if self.returned_input_seconds >= self.return_after_idle_seconds:
            raise ValueError("returned_input_seconds must be below return_after_idle_seconds.")
        if self.screen_min_seconds > self.screen_max_seconds:
            raise ValueError("screen_min_seconds must not exceed screen_max_seconds.")

    @property
    def effective_cooldown_seconds(self) -> float:
        return self.cooldown_seconds * (0.5 if self.mode == "lively" else 1)

    @property
    def effective_max_events_per_hour(self) -> int:
        return min(30, self.max_events_per_hour * (2 if self.mode == "lively" else 1))


@dataclass(frozen=True)
class IdleActionConfig:
    enabled: bool = True
    min_gap_seconds: float = 10
    max_gap_seconds: float = 30
    min_duration_seconds: float = 2.2
    max_duration_seconds: float = 3.4

    def __post_init__(self):
        if not isinstance(self.enabled, bool):
            raise ValueError("idle_actions.enabled must be boolean.")
        for item in fields(self):
            if item.name != "enabled":
                _number(item.name, getattr(self, item.name), 0.1, 3600)
        if self.min_gap_seconds > self.max_gap_seconds or self.min_duration_seconds > self.max_duration_seconds:
            raise ValueError("Idle action minimums must not exceed maximums.")


@dataclass(frozen=True)
class BehaviorConfig:
    roaming: RoamingConfig = field(default_factory=RoamingConfig)
    activity: ActivityConfig = field(default_factory=ActivityConfig)
    proactive: ProactiveConfig = field(default_factory=ProactiveConfig)
    idle_actions: IdleActionConfig = field(default_factory=IdleActionConfig)


def load_behavior_config(path: str | Path | None = None) -> BehaviorConfig:
    if path is None:
        return BehaviorConfig()
    raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.get("schema") != "desktop-pet-behavior-v1":
        raise ValueError("Expected desktop-pet-behavior-v1 config.")
    if set(raw) - {"schema", "roaming", "activity", "proactive", "idle_actions"}:
        raise ValueError("Unknown behavior config section.")
    return BehaviorConfig(**{name: cls(**raw.get(name, {})) for name, cls in
                            (("roaming", RoamingConfig), ("activity", ActivityConfig), ("proactive", ProactiveConfig),
                             ("idle_actions", IdleActionConfig))})
