"""Pointer-driven, damped pickup motion in logical sprite pixels.

This module has no Tk dependency. The window follows the pointer directly;
only the body rotates around its fixed grip. A stationary pointer supplies no
energy, so the body always settles instead of playing a carried-pose loop.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


def _clamp(value: float, limit: float) -> float:
    return min(limit, max(-limit, value))


@dataclass(frozen=True)
class CarrySample:
    frame: str
    angle_degrees: float
    offset_y: float
    phase: str
    progress: float = 0.0


class CarryMotion:
    """Bounded pendulum response to actual pointer velocity and acceleration.

    ``begin`` and ``update`` receive screen coordinates and monotonic seconds.
    ``scale`` is displayed pixels per logical atlas pixel, keeping the response
    consistent across sprite sizes and monitor DPI. The caller owns the window
    position and the grip; neither is changed by this model. Positive rotation
    is clockwise in image coordinates: legs below a grip trail to the left when
    the pointer accelerates right.
    """

    MAX_ANGLE = 20.0
    MAX_SPEED = 2200.0
    MAX_ACCELERATION = 12000.0
    MAX_ANGULAR_SPEED = 240.0

    def __init__(self):
        self.reset()

    @property
    def active(self) -> bool:
        return self._phase is not None

    def reset(self):
        self._phase = None
        self._time = self._input_at = 0.0
        self._pointer = (0.0, 0.0)
        self._scale = 1.0
        self._vx = self._vy = self._ax = self._ay = 0.0
        self._event_vx = self._event_vy = 0.0
        self._angle = self._angular_speed = 0.0
        self._landing_at = self._landing_duration = 0.0
        self._offset_y = self._vertical_speed = 0.0

    def begin(self, pointer: tuple[float, float], now: float, *, scale: float = 1.0):
        """Start a fresh grab; no motion or momentum survives a previous grab."""
        self.reset()
        x, y, now = float(pointer[0]), float(pointer[1]), float(now)
        if not all(math.isfinite(value) for value in (x, y, now)):
            return
        self._phase = "carried"
        self._pointer = (x, y)
        self._time = self._input_at = now
        self._scale = float(scale) if math.isfinite(float(scale)) and scale > 0 else 1.0

    def update(self, pointer: tuple[float, float], now: float):
        """Supply an actual pointer event, independently of render frequency."""
        if self._phase != "carried":
            return
        x, y, now = float(pointer[0]), float(pointer[1]), float(now)
        if not all(math.isfinite(value) for value in (x, y, now)) or now < self._input_at:
            return
        elapsed = now - self._input_at
        self._advance(now)
        # Coalesced events with the same timestamp have no measurable speed.
        if elapsed <= 0.00001:
            return
        dt = min(0.12, max(1 / 240, elapsed))
        velocity = tuple(_clamp((value - previous) / self._scale / dt, self.MAX_SPEED)
                         for value, previous in zip((x, y), self._pointer))
        smoothing = 1 - math.exp(-dt / 0.045)
        vx = self._event_vx + smoothing * (velocity[0] - self._event_vx)
        vy = self._event_vy + smoothing * (velocity[1] - self._event_vy)
        self._ax = _clamp((vx - self._event_vx) / dt, self.MAX_ACCELERATION)
        self._ay = _clamp((vy - self._event_vy) / dt, self.MAX_ACCELERATION)
        self._vx = self._event_vx = vx
        self._vy = self._event_vy = vy
        self._pointer, self._input_at = (x, y), now

    def release(self, now: float):
        """Let the body settle once; completion returns control to its posture."""
        if self._phase != "carried" or not math.isfinite(now):
            return
        self._advance(now)
        self._phase = "landing"
        self._landing_at = self._time
        impact = min(1.0, (abs(self._vy) + abs(self._vx) * 0.15) / 900)
        self._landing_duration = 0.58 + impact * 0.12
        self._vertical_speed = _clamp(self._vy * 0.05, 60.0)
        self._offset_y = 0.0
        self._vx = self._vy = self._ax = self._ay = 0.0

    def sample(self, now: float) -> CarrySample | None:
        if self._phase is None or not math.isfinite(now):
            return None
        self._advance(now)
        if self._phase is None:
            return None
        if self._phase == "landing":
            progress = min(1.0, max(0.0, (self._time - self._landing_at) / self._landing_duration))
            index = 1 + sum(progress >= threshold for threshold in (0.18, 0.43, 0.72))
            return CarrySample(f"land_{index}", self._angle, self._offset_y, "landing", progress)
        # Pose choice follows the physical response, never elapsed time. The
        # forward term makes the legs respond before a large body lean develops.
        lean = self._angle + self._angular_speed * 0.03 + self._vx * 0.006
        if abs(lean) > 2.6:
            frame = "carried_3" if lean > 0 else "carried_2"
        elif abs(self._vy) > 450 or abs(self._ay) > 5000:
            frame = "carried_1"
        else:
            frame = "carried_4"
        return CarrySample(frame, self._angle, 0.0, "carried")

    def _advance(self, now: float):
        if self._phase is None or now <= self._time:
            return
        elapsed = now - self._time
        if self._phase == "landing" and now - self._landing_at >= self._landing_duration:
            self.reset()
            return
        # A paused UI or resumed computer cannot inject a huge integration step.
        if elapsed > 0.3:
            self._angle = self._angular_speed = 0.0
            self._vx = self._vy = self._ax = self._ay = 0.0
            self._event_vx = self._event_vy = 0.0
            self._offset_y = self._vertical_speed = 0.0
            self._time = now
            return
        steps = max(1, math.ceil(elapsed * 120))
        dt = elapsed / steps
        for _ in range(steps):
            self._time += dt
            if self._phase == "carried":
                if self._time - self._input_at > 0.04:
                    decay = math.exp(-dt / 0.055)
                    self._vx *= decay
                    self._vy *= decay
                acceleration_decay = math.exp(-dt / 0.045)
                self._ax *= acceleration_decay
                self._ay *= acceleration_decay
                target = _clamp(self._vx * 0.016 + self._ax * 0.00085, self.MAX_ANGLE)
                acceleration = 12.5 ** 2 * (target - self._angle) - 16 * self._angular_speed
            else:
                acceleration = -18 ** 2 * self._angle - 25 * self._angular_speed
                self._vertical_speed += (-25 ** 2 * self._offset_y - 35 * self._vertical_speed) * dt
                self._offset_y = _clamp(self._offset_y + self._vertical_speed * dt, 4.0)
            self._angular_speed = _clamp(self._angular_speed + acceleration * dt, self.MAX_ANGULAR_SPEED)
            self._angle = _clamp(self._angle + self._angular_speed * dt, self.MAX_ANGLE)
            if abs(self._angle) == self.MAX_ANGLE and self._angle * self._angular_speed > 0:
                self._angular_speed = 0.0
        self._time = now
        if (abs(self._angle) < 0.04 and abs(self._angular_speed) < 0.2
                and abs(self._vx) < 1 and abs(self._ax) < 10):
            self._angle = self._angular_speed = 0.0
