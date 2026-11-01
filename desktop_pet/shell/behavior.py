"""Adapt pure behavior planners to shell geometry and main-loop snapshots."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import time

from desktop_pet.behavior import (ActivityCategory, ActivitySnapshot, BusyState, ObservationSource,
                                 IdleActionPlanner, MotionDecision, MotionState, ProactiveController,
                                 Rect, RoamingPlanner, SpriteGeometry,
                                 Win32ActivitySampler, load_behavior_config)
from desktop_pet.behavior.topology import DesktopTopology


class BehaviorBridge:
    def __init__(self, atlas, config_path: Path, emit, *, roaming=True, mode="occasional",
                 screen_enabled=None, screen_frequency="normal", screen_scope=None, idle_actions=None,
                 return_after_idle_seconds=None, seated=False):
        self.config = load_behavior_config(config_path)
        self.emit = emit
        self.roaming_enabled, self.mode = bool(roaming), mode
        self.screen_enabled = self.config.proactive.screen_enabled if screen_enabled is None else bool(screen_enabled)
        self.screen_frequency = screen_frequency if screen_frequency in {"less", "normal", "more"} else "normal"
        self.screen_scope = screen_scope if screen_scope in {"active_window", "active_screen"} else self.config.proactive.screen_scope
        self.return_after_idle_seconds = return_after_idle_seconds if return_after_idle_seconds in {180, 300} else self.config.proactive.return_after_idle_seconds
        self.idle_actions_enabled = self.config.idle_actions.enabled if idle_actions is None else bool(idle_actions)
        self.sampler = Win32ActivitySampler(self.config.activity)
        self.proactive = ProactiveController(self._proactive_config(), on_decision=self._decision)
        self.idle_actions = IdleActionPlanner(replace(self.config.idle_actions, enabled=self.idle_actions_enabled))
        self.latest_snapshot = None
        self.delivery_busy = BusyState(desktop_available=None)
        self._last_tick = time.monotonic()
        self.last_motion = None
        self.monitors = ()
        self.topology = None
        self._latest_action = None
        self._manual_until = float("-inf")
        self.seated = bool(seated)
        self._posture_transition = None
        self._posture_until = 0.0
        self._was_dragging = False
        self.reset_geometry(atlas)

    def _decision(self, decision):
        self._latest_action = decision
        self.emit({"type": "proactive_decision", "decision": decision})

    def _proactive_config(self):
        frequency = {"less": 2, "normal": 1, "more": .6}[self.screen_frequency]
        return replace(self.config.proactive, mode=self.mode, screen_enabled=self.screen_enabled,
                       screen_min_seconds=self.config.proactive.screen_min_seconds * frequency,
                       screen_max_seconds=self.config.proactive.screen_max_seconds * frequency,
                       screen_scope=self.screen_scope, return_after_idle_seconds=self.return_after_idle_seconds)

    def reset_geometry(self, atlas):
        self.geometry = SpriteGeometry.from_frame(atlas.width, atlas.height, atlas.anchor, padding=(12, 10, 12, 10))
        self.action_durations = ({name: atlas.animation_duration(name)
                                  for name in ("wave", "stretch", "yawn", "adjust_ribbon", "crossed_arms", "head_tilt",
                                               "sit_down", "sit_swing", "stand_up", "carried", "land")
                                  if name in atlas.manifest["animations"]}
                                 if hasattr(atlas, "animation_duration") else {})
        self.planner = RoamingPlanner(replace(self.config.roaming, enabled=self.roaming_enabled), sprite=self.geometry)
        if self.monitors:
            self.topology = DesktopTopology([monitor["bounds"] for monitor in self.monitors], self.geometry,
                                             work_areas=[monitor["work_area"] for monitor in self.monitors])

    def update_monitors(self, monitors) -> bool:
        monitors = tuple(monitors)
        if monitors == self.monitors:
            return False
        self.monitors = monitors
        self.topology = DesktopTopology([monitor["bounds"] for monitor in monitors], self.geometry,
                                         work_areas=[monitor["work_area"] for monitor in monitors])
        self.planner.reset()
        return True

    def constrain_position(self, position):
        return self.topology.recover_position(position) if self.topology else tuple(position)

    def options(self, *, roaming: bool, mode: str, screen_enabled=None, screen_frequency=None,
                screen_scope=None, idle_actions=None, return_after_idle_seconds=None):
        self.roaming_enabled, self.mode = bool(roaming), mode
        if screen_enabled is not None:
            self.screen_enabled = bool(screen_enabled)
        if screen_frequency in {"less", "normal", "more"}:
            self.screen_frequency = screen_frequency
        if screen_scope in {"active_window", "active_screen"}:
            self.screen_scope = screen_scope
        if return_after_idle_seconds in {180, 300}:
            self.return_after_idle_seconds = return_after_idle_seconds
        if idle_actions is not None:
            self.idle_actions_enabled = bool(idle_actions)
        self.planner.config = replace(self.config.roaming, enabled=self.roaming_enabled)
        self.planner.reset()
        self.idle_actions.config = replace(self.config.idle_actions, enabled=self.idle_actions_enabled)
        self.idle_actions.cancel(time.monotonic())
        self.proactive.config = self._proactive_config()
        self.proactive.reset_screen_schedule()
        self.proactive.note_presentation_interaction(time.monotonic())
        self._latest_action = None

    def note_manual_interaction(self, now: float):
        self._latest_action = None
        self._manual_until = now + self.config.proactive.manual_cooldown_seconds
        self.proactive.note_manual_interaction(now)
        self.idle_actions.cancel(now)

    def note_presentation_interaction(self, now: float):
        self._latest_action = None
        self._manual_until = now + self.config.proactive.manual_cooldown_seconds
        self.proactive.note_presentation_interaction(now)
        self.idle_actions.cancel(now)

    def set_seated(self, seated: bool, *, now: float | None = None):
        now = time.monotonic() if now is None else now
        self.seated = bool(seated)
        self.planner.reset()
        self.idle_actions.cancel(now)
        self._posture_transition = "sit_down" if self.seated else "stand_up"
        self._posture_until = now + self.action_durations.get(self._posture_transition, .1)

    def _stationary(self, position, state, requested, reason, animations, *fallbacks):
        animation = next((name for name in (requested, *fallbacks, "idle") if name in animations), "idle")
        self.last_motion = MotionDecision(tuple(position), state, animation, reason, False,
                                          (position[0] + self.geometry.anchor_x, position[1] + self.geometry.anchor_y),
                                          animation != requested)
        return self.last_motion

    def accepts_action(self, decision):
        now = time.monotonic()
        return (decision == self._latest_action and now <= decision.expires_at and now >= self._manual_until
                and not self.delivery_busy.action_block_reason)

    def observe(self, now: float, busy: BusyState):
        snapshot = self.sampler.poll(now=now)
        if snapshot is not None:
            self.latest_snapshot = snapshot
            if busy.action_block_reason or snapshot.desktop_available is not True or snapshot.foreground_fullscreen:
                self._latest_action = None
            self.proactive.observe(snapshot, busy=busy)
        return snapshot

    def should_deliver(self, decision, *, audio_prepared=False, continuing=False) -> bool:
        # delivery_busy is an immutable main-thread snapshot, so this callback
        # is safe for the dialogue worker and performs no Tk/window operation.
        busy = self.delivery_busy
        if audio_prepared:
            # A maintenance unload cannot invalidate an already returned WAV.
            # Source, expiry, manual/fullscreen/lock eligibility still applies.
            busy = replace(busy, resource_waiting=False, voice_unavailable=False)
        now = time.monotonic()
        if continuing:
            # A lease to finish the current file, never to begin a new segment.
            # Generation/manual cooldown and current desktop/busy checks remain.
            now = min(now, decision.expires_at)
        return self.proactive.should_deliver(decision, busy=busy, now=now)

    def step(self, now: float, *, position, room, mouse, busy, animations, allow_idle_actions=True):
        dt, self._last_tick = max(0, now - self._last_tick), now
        if busy.dragging:
            self._was_dragging = True
            self.planner.reset()
            self.idle_actions.cancel(now)
            return self._stationary(position, MotionState.DRAGGED, "carried", "carrying", animations, "dragged")
        if self._was_dragging:
            self._was_dragging = False
            # The ordinary landing ends standing. An explicitly seated pet
            # lowers back into its seat instead of briefly popping upright.
            self._posture_transition = "sit_down" if self.seated else "land"
            self._posture_until = now + self.action_durations.get(self._posture_transition, .1)
        if self._posture_transition and now < self._posture_until:
            state = MotionState.SEATED if self.seated else MotionState.PAUSED
            return self._stationary(position, state, self._posture_transition, "posture_transition", animations,
                                    "sit" if self.seated else "idle")
        self._posture_transition = None
        if self.seated:
            action = self.idle_actions.step(now, busy=busy, available=animations, allow_start=allow_idle_actions,
                                            durations=self.action_durations, actions=("sit_swing",))
            return self._stationary(position, MotionState.SEATED, action or "sit", "seated", animations, "rest")
        previous_action = self.last_motion and self.last_motion.state == MotionState.IDLE_ACTION
        stationary = self.last_motion is None or not self.last_motion.moved
        action = self.idle_actions.step(now, busy=busy, available=animations, stationary=stationary,
                                        allow_start=allow_idle_actions, durations=self.action_durations,
                                        actions=("wave", "stretch", "yawn", "adjust_ribbon", "crossed_arms", "head_tilt"))
        if action:
            if not previous_action:
                self.planner.reset()
            self.last_motion = MotionDecision(tuple(position), MotionState.IDLE_ACTION, action, "idle_performance", False,
                                              (position[0] + self.geometry.anchor_x, position[1] + self.geometry.anchor_y))
            return self.last_motion
        self.last_motion = self.planner.step(dt, position=position, room=Rect(*room), mouse=mouse,
                                             busy=busy, available_animations=animations, topology=self.topology)
        return self.last_motion

    def simulate_return(self):
        """Explicit supplied scenario; no claim of actual three-minute idle."""
        now = time.monotonic()
        self.proactive = ProactiveController(self._proactive_config(), on_decision=self._decision)
        self._manual_until = float("-inf")
        self.proactive.observe(ActivitySnapshot(now - 2, self.proactive.config.return_after_idle_seconds + 1, ActivityCategory.OTHER,
                                                source=ObservationSource.INJECTED))
        return self.proactive.observe(ActivitySnapshot(now, 0, ActivityCategory.OTHER,
                                                       source=ObservationSource.INJECTED))
