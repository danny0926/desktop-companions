"""Focused local contracts; all activity-event scenarios here are injected."""
from __future__ import annotations

from dataclasses import replace
import random
from pathlib import Path
import unittest

from desktop_pet.behavior import (ActivityCategory, ActivityConfig, ActivitySnapshot, BusyState,
                                  MotionState, ObservationSource, ProactiveConfig, ProactiveController,
                                  ProactiveEventKind, Rect, RoamingConfig, RoamingPlanner, SpriteGeometry,
                                  Win32ActivitySampler, load_behavior_config)


SPRITE = SpriteGeometry.from_frame(192, 224, (96, 216), padding=(12, 10, 12, 10))
ROOM = Rect(0, 0, 1920, 1040)
FLOOR = ROOM.bottom - SPRITE.height


def sample(now, idle=0, **kwargs):
    return ActivitySnapshot(now, idle, category=ActivityCategory.CODING,
                            source=ObservationSource.INJECTED, **kwargs)


class MotionContracts(unittest.TestCase):
    def planner(self, **kwargs):
        cfg = RoamingConfig(idle_min_seconds=0.01, idle_max_seconds=0.01, rest_probability=0)
        return RoamingPlanner(replace(cfg, **kwargs), sprite=SPRITE, rng=random.Random(7))

    def test_config_matches_outer_window_and_anchor(self):
        config = load_behavior_config(Path(__file__).resolve().parents[2] / "config" / "desktop_pet_behavior.json")
        self.assertEqual((SPRITE.width, SPRITE.height, SPRITE.anchor_x, SPRITE.anchor_y), (216, 244, 108, 226))
        self.assertTrue(config.roaming.enabled)
        with self.assertRaises(ValueError):
            RoamingConfig(speed_pixels_per_second=float("nan"))

    def test_roaming_uses_idle_until_walking_assets_exist(self):
        planner = self.planner()
        position = (400, FLOOR)
        moved = False
        for _ in range(80):
            result = planner.step(0.1, position=position, room=ROOM)
            position = result.position
            moved |= result.moved
            self.assertTrue(ROOM.left <= position[0] <= ROOM.right - SPRITE.width)
            self.assertEqual(position[1], FLOOR)
            self.assertEqual(result.animation, "idle")
            if result.state in (MotionState.WALKING_LEFT, MotionState.WALKING_RIGHT):
                self.assertTrue(result.animation_fallback)
        self.assertTrue(moved)
        planner = self.planner()
        position = (400, FLOOR)
        for _ in range(30):
            result = planner.step(0.1, position=position, room=ROOM,
                                  available_animations={"idle", "walking_left", "walking_right"})
            position = result.position
            if result.moved:
                break
        self.assertIn(result.animation, ("walking_left", "walking_right"))
        self.assertFalse(result.animation_fallback)

    def test_busy_states_hold_position_and_drag_is_not_clamped(self):
        for name in ("speaking", "typing", "input_active", "dragging", "thinking", "synthesizing",
                     "manual_chat", "fullscreen", "locked", "hidden"):
            planner = self.planner()
            position = (-400, 20)
            result = planner.step(0.1, position=position, room=ROOM, busy=BusyState(**{name: True}))
            self.assertEqual(result.position, position, name)
            self.assertFalse(result.moved, name)
        self.assertEqual(result.state, MotionState.PAUSED)
        planner = self.planner()
        result = planner.step(0.1, position=(400, FLOOR), room=ROOM, busy=BusyState(dragging=True))
        self.assertEqual(result.state, MotionState.DRAGGED)
        result = planner.step(0.1, position=(400, FLOOR), room=ROOM)
        self.assertFalse(result.moved)

    def test_pointer_in_path_stops_without_chasing_mouse(self):
        planner = self.planner()
        position = (400, FLOOR)
        for _ in range(30):
            first = planner.step(0.1, position=position, room=ROOM)
            position = first.position
            if first.moved:
                break
        self.assertIn(first.state, (MotionState.WALKING_LEFT, MotionState.WALKING_RIGHT))
        x, y = first.position
        right = first.state == MotionState.WALKING_RIGHT
        pointer_x = x + SPRITE.width + planner.config.mouse_clearance + 0.1 if right else x - planner.config.mouse_clearance - 0.1
        result = planner.step(0.1, position=first.position, room=ROOM, mouse=(pointer_x, y + SPRITE.height / 2))
        self.assertEqual(result.position, first.position)
        self.assertEqual(result.reason, "pointer_in_path")

    def test_negative_monitor_and_stalled_ui_stay_bounded(self):
        planner = self.planner()
        room = Rect(-1920, -120, 0, 920)
        start = (-1000, room.bottom - SPRITE.height)
        result = planner.step(60, position=start, room=room)
        self.assertLessEqual(abs(result.position[0] - start[0]), planner.config.speed_pixels_per_second * planner.config.max_step_seconds + 0.001)
        for _ in range(300):
            result = planner.step(0.1, position=result.position, room=room)
            self.assertTrue(room.left <= result.position[0] <= room.right - SPRITE.width)
            self.assertTrue(room.top <= result.position[1] <= room.bottom - SPRITE.height)

    def test_drop_settles_gradually_and_roaming_can_be_disabled(self):
        planner = self.planner()
        result = planner.step(0.1, position=(400, 100), room=ROOM)
        self.assertEqual(result.reason, "settling_to_floor")
        self.assertTrue(100 < result.position[1] <= 104)
        disabled = self.planner(enabled=False)
        result = disabled.step(1, position=(400, 100), room=ROOM)
        self.assertEqual(result.position, (400, 100))
        self.assertEqual(result.reason, "roaming_disabled")


class ActivityAndEventContracts(unittest.TestCase):
    def test_sampler_limits_frequency_and_disabled_sampler_never_probes(self):
        calls = []
        def probe(now):
            calls.append(now)
            return sample(now)
        observer = Win32ActivitySampler(ActivityConfig(sample_interval_seconds=2), probe=probe)
        self.assertIsNotNone(observer.poll(0))
        self.assertIsNone(observer.poll(1.9))
        self.assertIsNotNone(observer.poll(2))
        self.assertEqual(calls, [0, 2])
        disabled = Win32ActivitySampler(ActivityConfig(enabled=False), probe=probe)
        self.assertIsNone(disabled.poll(100))
        self.assertEqual(calls, [0, 2])

    def test_return_event_requires_observed_idle_not_initial_active_state(self):
        decisions = []
        controller = ProactiveController(on_decision=decisions.append)
        self.assertIsNone(controller.observe(sample(0)))
        self.assertIsNone(controller.observe(sample(2, idle=200)))
        decision = controller.observe(sample(4, idle=1))
        self.assertEqual(decision.event.kind, ProactiveEventKind.RETURN_FROM_IDLE)
        self.assertEqual(decision.event.idle_seconds, 200)
        self.assertEqual(decision.event.source, ObservationSource.INJECTED)
        self.assertTrue(decision.request_speech)
        self.assertEqual(decisions, [decision])
        context = decision.event.model_context()
        self.assertEqual(context["source"], "local_activity_system_event")
        self.assertNotIn("user", context)

    def test_focus_span_resets_after_observation_gap_and_input_idle(self):
        cfg = ProactiveConfig(focus_after_seconds=6, max_observation_gap_seconds=3)
        controller = ProactiveController(cfg)
        for now in (0, 2, 4):
            self.assertIsNone(controller.observe(sample(now)))
        self.assertIsNone(controller.observe(sample(20)))
        self.assertIsNone(controller.observe(sample(22, idle=60)))
        for now in (24, 26, 28):
            self.assertIsNone(controller.observe(sample(now)))
        decision = controller.observe(sample(30))
        self.assertEqual(decision.event.kind, ProactiveEventKind.FOCUS_BREAK)
        self.assertEqual(decision.event.active_seconds, 6)

    def test_speech_is_separate_from_actions_when_quiet_or_voice_unavailable(self):
        cases = [(ProactiveConfig(mode="quiet"), BusyState(), "quiet_mode"),
                 (ProactiveConfig(), BusyState(voice_unavailable=True), "voice_unavailable"),
                 (ProactiveConfig(), BusyState(resource_waiting=True), "resource_waiting")]
        for config, busy, reason in cases:
            controller = ProactiveController(config)
            controller.observe(sample(0, idle=200), busy=busy)
            decision = controller.observe(sample(2), busy=busy)
            self.assertEqual(decision.action, "greet")
            self.assertFalse(decision.request_speech)
            self.assertEqual(decision.reason, reason)

    def test_cooldown_hourly_cap_and_expiration_are_enforced(self):
        cfg = ProactiveConfig(cooldown_seconds=5, return_cooldown_seconds=5, max_events_per_hour=2,
                              return_after_idle_seconds=10, returned_input_seconds=2)
        controller = ProactiveController(cfg)
        controller.observe(sample(0, idle=20))
        first = controller.observe(sample(2))
        controller.observe(sample(4, idle=20))
        self.assertIsNone(controller.observe(sample(6)))
        second = controller.observe(sample(8))
        self.assertIsNotNone(second)
        controller.observe(sample(10, idle=20))
        self.assertIsNone(controller.observe(sample(12)))
        self.assertFalse(controller.should_deliver(first, now=12))
        self.assertFalse(controller.should_deliver(second, now=second.expires_at + 1))

    def test_manual_interaction_cancels_queued_and_inflight_reply(self):
        controller = ProactiveController()
        controller.observe(sample(0, idle=200))
        decision = controller.observe(sample(2))
        self.assertTrue(controller.should_deliver(decision, now=3))
        controller.note_manual_interaction(3)
        self.assertFalse(controller.should_deliver(decision, now=4))
        controller.observe(sample(4, idle=200))
        self.assertIsNone(controller.observe(sample(6)))
        self.assertFalse(controller.should_deliver(decision, now=8))

    def test_fullscreen_and_unavailable_desktop_cancel_and_suppress(self):
        for snapshot_fields in ({"foreground_fullscreen": True}, {"desktop_available": False}, {"desktop_available": None}):
            controller = ProactiveController()
            controller.observe(sample(0, idle=200))
            decision = controller.observe(sample(2))
            self.assertIsNone(controller.observe(sample(4, idle=200, **snapshot_fields)))
            self.assertFalse(controller.should_deliver(decision, now=4))
            self.assertIsNone(controller.observe(sample(6, **snapshot_fields)))
            self.assertIsNone(controller.observe(sample(8)))
            self.assertFalse(controller.should_deliver(decision, now=8))


if __name__ == "__main__":
    unittest.main()
