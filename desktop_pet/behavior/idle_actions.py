"""Small stationary sprite performances; real animation keys, no window tricks."""
from __future__ import annotations

from collections import deque
import random

from .config import IdleActionConfig
from .models import BusyState


IDLE_ACTIONS = ("wave", "stretch", "yawn", "adjust_ribbon")


class IdleActionPlanner:
    def __init__(self, config: IdleActionConfig | None = None, *, rng=None):
        self.config = config or IdleActionConfig()
        self._rng = rng or random.Random()
        self._due = None
        self._until = 0.0
        self._active = None
        self._recent = deque(maxlen=2)

    def _schedule(self, now):
        self._due = now + self._rng.uniform(self.config.min_gap_seconds, self.config.max_gap_seconds)

    def cancel(self, now):
        self._active, self._until = None, 0.0
        self._schedule(now)

    def step(self, now, *, busy: BusyState, available, stationary=True, allow_start=True, durations=None):
        if not self.config.enabled or busy.action_block_reason:
            if self._active or self._due is None:
                self.cancel(now)
            return None
        if self._active:
            if self._active in available and now < self._until:
                return self._active
            self.cancel(now)
            return None
        if self._due is None:
            self._schedule(now)
        if now < self._due or not stationary or not allow_start:
            return None
        choices = [name for name in IDLE_ACTIONS if name in available and name not in self._recent]
        if not choices:
            choices = [name for name in IDLE_ACTIONS if name in available]
        if not choices:
            self._schedule(now)
            return None
        self._active = self._rng.choice(choices)
        self._recent.append(self._active)
        duration = (durations or {}).get(self._active)
        if duration is None:
            duration = self._rng.uniform(self.config.min_duration_seconds, self.config.max_duration_seconds)
        self._until = now + duration
        return self._active
