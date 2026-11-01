"""Local event detection and pacing. No LLM, network, speech, or history writes."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
import time
import uuid
import random

from .config import ProactiveConfig
from .models import (ActivityCategory, ActivitySnapshot, BusyState, ObservationSource,
                     ProactiveDecision, ProactiveEvent, ProactiveEventKind)


class ProactiveController:
    def __init__(self, config: ProactiveConfig | None = None, *,
                 on_decision: Callable[[ProactiveDecision], None] | None = None,
                 rng: random.Random | None = None):
        self.config = config or ProactiveConfig()
        self.on_decision = on_decision
        self._last_sample: float | None = None
        self._away_peak = 0.0
        self._focus_start: float | None = None
        self._pending: ProactiveEvent | None = None
        self._emitted: deque[float] = deque()
        self._per_kind: dict[ProactiveEventKind, float] = {}
        self._manual_until = float("-inf")
        self._generation = 0
        self._latest_decision: ProactiveDecision | None = None
        self._desktop_block: str | None = "no_observation"
        self.last_cancellation_reason: str | None = None
        self._rng = rng or random.Random()
        self._recent_topics: deque[str] = deque(maxlen=3)
        self._next_screen_at: float | None = None
        self._foreground_owned = False

    def _schedule_screen(self, now):
        self._next_screen_at = now + self._rng.uniform(self.config.screen_min_seconds, self.config.screen_max_seconds)

    def reset_screen_schedule(self):
        self._next_screen_at = None

    def _opening_topic(self, kind):
        # These are intentions, never fixed spoken greetings or invented memory.
        topics = (("resume_heard_topic", "casual_question", "light_teasing", "companion_aside",
                   "small_check_in", "food_or_daily_plan", "curious_thought")
                  if kind == ProactiveEventKind.RETURN_FROM_IDLE else
                  ("notice_visible_detail", "curious_visible_question", "soft_visible_comment")
                  if kind == ProactiveEventKind.SCREEN_OBSERVATION else
                  ("gentle_break_offer", "light_companion_aside", "small_stretch_offer"))
        choices = [topic for topic in topics if topic not in self._recent_topics] or list(topics)
        topic = self._rng.choice(choices)
        self._recent_topics.append(topic)
        return topic

    def cancel_pending(self, *, reason: str = "cancelled"):
        """Invalidate any queued/in-flight proactive reply; manual chat wins."""
        self._pending = None
        self._latest_decision = None
        self._generation += 1
        self.last_cancellation_reason = reason

    def note_manual_interaction(self, now: float | None = None):
        now = time.monotonic() if now is None else now
        self.cancel_pending(reason="manual_interaction")
        self._manual_until = now + self.config.manual_cooldown_seconds
        self._focus_start = None

    def should_deliver(self, decision: ProactiveDecision, *, busy: BusyState = BusyState(),
                       now: float | None = None) -> bool:
        """Recheck immediately before starting an optional generated utterance."""
        now = time.monotonic() if now is None else now
        return (self.config.enabled and decision.request_speech and decision == self._latest_decision
                and decision.generation == self._generation and now <= decision.expires_at
                and now >= self._manual_until and not self._desktop_block and not busy.speech_block_reason
                and (decision.event.kind != ProactiveEventKind.SCREEN_OBSERVATION or
                     self.config.screen_enabled and not self._foreground_owned and not busy.chat_active))

    def _event(self, kind: ProactiveEventKind, snapshot: ActivitySnapshot, *, idle=0, active=0) -> ProactiveEvent:
        return ProactiveEvent(uuid.uuid4().hex, kind, snapshot.captured_at, snapshot.category,
                              snapshot.source, idle, active, self._opening_topic(kind),
                              self._rng.randrange(2 ** 31), self.config.screen_scope)

    def observe(self, snapshot: ActivitySnapshot, *, busy: BusyState = BusyState()) -> ProactiveDecision | None:
        """Use a fresh, finite-frequency sample; duplicate timestamps do nothing.

        "Focus" means a span of recent input, not verified attention or work.
        Quiet mode emits visual actions only. Lively halves the global cooldown
        and doubles the hourly cap; per-event cooldowns still apply.
        """
        now, cfg = snapshot.captured_at, self.config
        if self._last_sample is not None and now <= self._last_sample:
            return None
        gap = None if self._last_sample is None else now - self._last_sample
        self._last_sample = now
        self._foreground_owned = snapshot.foreground_owned
        if self._next_screen_at is None:
            self._schedule_screen(now)
        self._desktop_block = ("fullscreen" if snapshot.foreground_fullscreen else
                               "desktop_unavailable" if snapshot.desktop_available is not True else
                               "observation_unavailable" if snapshot.source == ObservationSource.UNAVAILABLE else None)
        if not cfg.enabled:
            self.cancel_pending(reason="disabled")
            self._focus_start, self._away_peak = None, 0
            return None
        if gap is not None and gap > cfg.max_observation_gap_seconds:
            # A suspended process cannot establish continuous computer activity.
            self._focus_start = None
            self._pending = None
        idle = snapshot.idle_seconds
        candidate: ProactiveEvent | None = None
        if idle is not None and idle >= cfg.return_after_idle_seconds:
            self._away_peak = max(self._away_peak, idle)
        elif idle is not None and idle <= cfg.returned_input_seconds and self._away_peak:
            candidate = self._event(ProactiveEventKind.RETURN_FROM_IDLE, snapshot, idle=self._away_peak)
            self._away_peak = 0
        manual = busy.typing or busy.input_active or busy.manual_chat or busy.dragging
        if manual:
            self.note_manual_interaction(now)
        elif self._desktop_block or busy.action_block_reason:
            self.cancel_pending(reason=self._desktop_block or busy.action_block_reason or "busy")
        active = (idle is not None and idle <= cfg.focus_active_idle_seconds
                  and not self._desktop_block and not busy.action_block_reason
                  and snapshot.category in (ActivityCategory.CODING, ActivityCategory.BROWSER, ActivityCategory.OTHER))
        if not active:
            self._focus_start = None
        elif self._focus_start is None:
            self._focus_start = now
        elif now - self._focus_start >= cfg.focus_after_seconds:
            candidate = candidate or self._event(ProactiveEventKind.FOCUS_BREAK, snapshot, active=now - self._focus_start)
            self._focus_start = now
        if (candidate is None and self._pending is None and cfg.screen_enabled and cfg.mode != "quiet"
                and now >= self._next_screen_at and idle is not None and idle <= cfg.screen_max_idle_seconds
                and not snapshot.foreground_owned and not busy.chat_active and not busy.speech_block_reason
                and not self._desktop_block and now >= self._manual_until):
            candidate = self._event(ProactiveEventKind.SCREEN_OBSERVATION, snapshot)
        if candidate and not self._desktop_block and not busy.action_block_reason:
            self._pending = candidate
        event = self._pending
        if event is None:
            return None
        ttl = cfg.screen_event_ttl_seconds if event.kind == ProactiveEventKind.SCREEN_OBSERVATION else cfg.event_ttl_seconds
        if now - event.observed_at > ttl:
            self._pending = None
            return None
        if self._desktop_block or busy.action_block_reason or now < self._manual_until:
            return None
        while self._emitted and now - self._emitted[0] >= 3600:
            self._emitted.popleft()
        if event.kind == ProactiveEventKind.SCREEN_OBSERVATION and (not cfg.screen_enabled or snapshot.foreground_owned or busy.chat_active):
            self._pending = None
            return None
        per_kind_cooldown = (cfg.return_cooldown_seconds if event.kind == ProactiveEventKind.RETURN_FROM_IDLE else
                             cfg.screen_min_seconds if event.kind == ProactiveEventKind.SCREEN_OBSERVATION else
                             cfg.focus_cooldown_seconds)
        if (len(self._emitted) >= cfg.effective_max_events_per_hour
                or self._emitted and now - self._emitted[-1] < cfg.effective_cooldown_seconds
                or now - self._per_kind.get(event.kind, float("-inf")) < per_kind_cooldown):
            return None
        self._pending = None
        self._emitted.append(now)
        self._per_kind[event.kind] = now
        if event.kind == ProactiveEventKind.SCREEN_OBSERVATION:
            self._schedule_screen(now)
        self._generation += 1
        speech = cfg.mode != "quiet" and not busy.speech_block_reason
        reason = "quiet_mode" if cfg.mode == "quiet" else busy.speech_block_reason or event.kind.value
        action, animation = (("start_topic", "wave") if event.kind == ProactiveEventKind.RETURN_FROM_IDLE else
                             ("look_at_screen", "curious") if event.kind == ProactiveEventKind.SCREEN_OBSERVATION else
                             ("break_hint", "stretch"))
        decision = ProactiveDecision(event, action, animation,
                                     speech, reason, event.observed_at + ttl,
                                     self._generation, cfg.max_reply_chars)
        self._latest_decision = decision
        if self.on_decision is not None:
            self.on_decision(decision)
        return decision
