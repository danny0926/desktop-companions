"""Full desktop XY wandering over visible monitor unions; no GUI side effects."""
from __future__ import annotations

import math
import random
from collections.abc import Collection

from .config import RoamingConfig
from .models import BusyState, MotionDecision, MotionState, Rect, SpriteGeometry
from .topology import DesktopTopology, EPS


class RoamingPlanner:
    def __init__(self, config: RoamingConfig | None = None, *, sprite: SpriteGeometry, rng: random.Random | None = None):
        self.config = config or RoamingConfig()
        self.sprite = sprite
        self._rng = rng or random.Random()
        self._wait = self._idle_duration()
        self._state = MotionState.IDLE
        self._was_busy = False
        self._layout_key = None
        self._layout = None
        self._route: list[tuple[float, float]] = []
        self._last_output = None

    def _idle_duration(self):
        return self._rng.uniform(self.config.idle_min_seconds, self.config.idle_max_seconds)

    def reset(self):
        self._route = []
        self._wait = self.config.resume_delay_seconds
        self._state = MotionState.IDLE
        self._last_output = None

    def _result(self, position, original, state, requested, reason, animations):
        animation = requested if requested in animations else "idle"
        self._last_output = tuple(position)
        return MotionDecision(tuple(position), state, animation, reason, math.dist(position, original) > EPS,
                              (position[0] + self.sprite.anchor_x, position[1] + self.sprite.anchor_y), requested != animation)

    def _choose_route(self, layout, position):
        entry = layout.entry(position)
        if entry is None:
            return []
        _, source, point, _ = entry
        component = layout.component(source)
        for _ in range(12):
            destination = self._rng.choice(component)
            if destination == source:
                angle = self._rng.uniform(0, math.tau)
                distance = self._rng.uniform(self.config.min_walk_pixels, self.config.max_walk_pixels)
                target = layout.clamp_to_node((point[0] + math.cos(angle) * distance,
                                               point[1] + math.sin(angle) * distance), destination)
            else:
                area = layout.destination_area(destination)
                target = (self._rng.uniform(area.left, area.right - self.sprite.width),
                          self._rng.uniform(area.top, area.bottom - self.sprite.height))
            route = layout.route(position, destination, target)
            if route and sum(math.dist(a, b) for a, b in zip([position] + route, route)) >= min(16, self.config.min_walk_pixels):
                return route
        return []

    def step(self, dt: float, *, position: tuple[float, float], room: Rect,
             mouse=None, busy: BusyState = BusyState(), available_animations: Collection[str] = ("idle",),
             topology: DesktopTopology | None = None) -> MotionDecision:
        if not math.isfinite(dt) or dt < 0 or not all(math.isfinite(v) for v in position):
            raise ValueError("Position/dt must be finite and dt nonnegative.")
        original = tuple(position)
        if busy.dragging:
            self.reset(); self._was_busy = True
            return self._result(original, original, MotionState.DRAGGED, "dragged", "dragging", available_animations)
        if busy.action_block_reason:
            self._route = []; self._was_busy = True
            return self._result(original, original, MotionState.SPEAKING if busy.speaking else MotionState.PAUSED,
                                "speaking" if busy.speaking else "idle", busy.action_block_reason, available_animations)
        if not self.config.enabled:
            self._route = []
            return self._result(original, original, MotionState.IDLE, "idle", "roaming_disabled", available_animations)
        if topology is None:
            if self._layout is None or self._layout.areas != (room,) or self._layout.sprite != self.sprite:
                self._layout = DesktopTopology((room,), self.sprite)
            topology = self._layout
        if not topology.nodes:
            self.reset()
            return self._result(original, original, MotionState.PAUSED, "idle", "no_fitting_work_area", available_animations)
        if self._was_busy or topology.signature != self._layout_key:
            self._route = []
            self._wait = max(self._wait, self.config.resume_delay_seconds)
            self._was_busy = False
            self._layout_key = topology.signature
            self._state = MotionState.IDLE
        current = original
        if self._last_output and all(abs(original[i] - self._last_output[i]) <= .51 for i in (0, 1)):
            current = self._last_output
        elif self._last_output and math.dist(original, self._last_output) > 1:
            self.reset()
        if not topology.window_visible(current):
            self.reset()
            return self._result(topology.recover_position(current), original, MotionState.PAUSED, "idle", "monitor_layout_recovery", available_animations)
        current_bounds = Rect(current[0], current[1], current[0] + self.sprite.width, current[1] + self.sprite.height)
        if mouse is not None and current_bounds.contains(mouse, self.config.mouse_clearance):
            self._route = []; self._state = MotionState.IDLE
            self._wait = max(self._wait, self.config.mouse_pause_seconds)
            return self._result(current, original, self._state, "idle", "pointer_nearby", available_animations)
        dt = min(dt, self.config.max_step_seconds)
        while self._route and math.dist(current, self._route[0]) <= EPS:
            self._route.pop(0)
        if not self._route:
            self._wait = max(0, self._wait - dt)
            if self._wait > 0:
                return self._result(current, original, self._state, "rest" if self._state == MotionState.REST else "idle",
                                    "resting" if self._state == MotionState.REST else "idle_wait", available_animations)
            self._route = self._choose_route(topology, current)
            if not self._route:
                self._wait = self._idle_duration()
                return self._result(current, original, MotionState.IDLE, "idle", "no_visible_route", available_animations)
        target = self._route[0]
        dx, dy = target[0] - current[0], target[1] - current[1]
        distance = abs(dx) + abs(dy)
        step = min(distance, self.config.speed_pixels_per_second * dt)
        next_position = (current[0] + math.copysign(step, dx), current[1]) if abs(dx) > EPS else (current[0], current[1] + math.copysign(step, dy))
        if not topology.safe_segment(current, next_position):
            self.reset()
            return self._result(current, original, MotionState.PAUSED, "idle", "visible_route_changed", available_animations)
        swept = Rect(min(current[0], next_position[0]), min(current[1], next_position[1]),
                     max(current[0], next_position[0]) + self.sprite.width, max(current[1], next_position[1]) + self.sprite.height)
        if mouse is not None and swept.contains(mouse, self.config.mouse_clearance):
            self._route = []; self._wait = self.config.mouse_pause_seconds; self._state = MotionState.IDLE
            return self._result(current, original, self._state, "idle", "pointer_in_path", available_animations)
        direction = (MotionState.WALKING_LEFT if dx < 0 else MotionState.WALKING_RIGHT) if abs(dx) > EPS else (MotionState.WALKING_UP if dy < 0 else MotionState.WALKING_DOWN)
        self._state = direction
        if math.dist(next_position, target) <= EPS:
            self._route.pop(0)
            if not self._route:
                resting = self._rng.random() < self.config.rest_probability
                self._state = MotionState.REST if resting else MotionState.IDLE
                self._wait = self.config.rest_seconds if resting else self._idle_duration()
        return self._result(next_position, original, direction, direction.value, "desktop_roaming", available_animations)
