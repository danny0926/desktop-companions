"""Local pointer gestures, independent of dialogue and the native window."""
from __future__ import annotations

from dataclasses import dataclass
import math


def head_hit(x: float, y: float, width: float, height: float, region: dict | None = None) -> bool:
    """Hit-test an ellipse in sprite coordinates, without the canvas padding.

    Packs can supply normalized head geometry. The fallback covers the large
    head of the existing two chibi packs, rather than stealing body dragging.
    """
    region = region or {"cx": 0.5, "cy": 0.29, "rx": 0.30, "ry": 0.23}
    cx, cy = float(region.get("cx", 0.5)), float(region.get("cy", 0.29))
    rx, ry = float(region.get("rx", 0.30)), float(region.get("ry", 0.23))
    if width <= 0 or height <= 0 or rx <= 0 or ry <= 0:
        return False
    return ((x / width - cx) / rx) ** 2 + ((y / height - cy) / ry) ** 2 <= 1


@dataclass
class HeadpatGesture:
    started_at: float
    last_x: float
    last_y: float
    distance: float = 0
    movements: int = 0

    def move(self, x: float, y: float):
        distance = math.hypot(x - self.last_x, y - self.last_y)
        if distance >= 1:
            self.distance += distance
            self.movements += 1
        self.last_x, self.last_y = x, y

    def description(self, now: float, sprite_width: float) -> str:
        if self.movements >= 2 and self.distance >= sprite_width * 0.08:
            return "輕輕來回摸頭"
        return "輕輕摸著頭" if now - self.started_at >= 0.8 else "輕輕摸頭"
