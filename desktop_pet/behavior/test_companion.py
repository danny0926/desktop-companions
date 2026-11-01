"""New companion contracts with injected time/screens; no Tk, Codex or ML run."""
from dataclasses import replace
import json
from pathlib import Path
import random
import threading
import time
from types import SimpleNamespace
import unittest

from desktop_pet.agent.performance import build_reply, reply_schema
from desktop_pet.behavior import (
    ActivityCategory, ActivitySnapshot, BusyState, IdleActionConfig, IdleActionPlanner,
    MotionState, ObservationSource, ProactiveConfig, ProactiveController,
    ProactiveDecision, ProactiveEvent, ProactiveEventKind,
)
from desktop_pet.contracts import ATTITUDES, DELIVERIES, INTENSITIES, AgentReply
from desktop_pet.shell.behavior import BehaviorBridge
from desktop_pet.shell.controller import ConversationController, Turn


ROOT = Path(__file__).resolve().parents[2]


def sample(now, idle=0, **kwargs):
    return ActivitySnapshot(now, idle, ActivityCategory.CODING,
                            source=ObservationSource.INJECTED, **kwargs)


def config(**kwargs):
    return replace(ProactiveConfig(screen_min_seconds=10, screen_max_seconds=10,
                                    cooldown_seconds=1, max_events_per_hour=30,
                                    return_cooldown_seconds=1, focus_after_seconds=86400), **kwargs)


class CompanionContracts(unittest.TestCase):
    def test_return_always_chooses_a_varied_intent_with_actual_source(self):
        planner = ProactiveController(config(screen_enabled=False), rng=random.Random(5))
        topics = []
        for index in range(7):
            now = 100 + index * 10
            self.assertIsNone(planner.observe(sample(now, 190)))
            result = planner.observe(sample(now + 1, 0))
            self.assertIsNotNone(result)
            self.assertTrue(result.request_speech)
            self.assertEqual(result.event.source, ObservationSource.INJECTED)
            self.assertNotIn(result.event.opener_topic, topics[-3:])
            self.assertEqual(result.action, "start_topic")
            topics.append(result.event.opener_topic)
        self.assertGreaterEqual(len(set(topics)), 5)

    def test_screen_schedule_is_eligible_only_after_due(self):
        planner = ProactiveController(config(), rng=random.Random(1))
        self.assertIsNone(planner.observe(sample(0)))
        self.assertIsNone(planner.observe(sample(9)))
        result = planner.observe(sample(10))
        self.assertEqual(result.event.kind, ProactiveEventKind.SCREEN_OBSERVATION)
        self.assertEqual(result.expires_at, 70)
        self.assertTrue(planner.should_deliver(result, now=11))
        self.assertFalse(planner.should_deliver(result, now=71))
        planner.note_manual_interaction(12)
        self.assertFalse(planner.should_deliver(result, now=13))

    def test_screen_blocks_own_foreground_locked_fullscreen_and_busy_states(self):
        scenarios = [({"foreground_owned": True}, BusyState()),
                     ({"desktop_available": False}, BusyState()),
                     ({"foreground_fullscreen": True}, BusyState()),
                     ({}, BusyState(input_active=True)), ({}, BusyState(typing=True)),
                     ({}, BusyState(dragging=True)), ({}, BusyState(speaking=True)),
                     ({}, BusyState(thinking=True)), ({}, BusyState(hidden=True))]
        for flags, busy in scenarios:
            with self.subTest(flags=flags, busy=busy):
                planner = ProactiveController(config())
                planner.observe(sample(0))
                self.assertIsNone(planner.observe(sample(10, **flags), busy=busy))
        for cfg in (config(screen_enabled=False), config(mode="quiet")):
            planner = ProactiveController(cfg)
            planner.observe(sample(0))
            self.assertIsNone(planner.observe(sample(10)))

    def test_visible_empty_chat_allows_screen_and_nonvisual_companionship(self):
        planner = ProactiveController(config(), rng=random.Random(1))
        planner.observe(sample(0), busy=BusyState(chat_active=True))
        result = planner.observe(sample(10), busy=BusyState(chat_active=True))
        self.assertEqual(result.event.kind, ProactiveEventKind.SCREEN_OBSERVATION)
        self.assertTrue(planner.should_deliver(result, now=11, busy=BusyState(chat_active=True)))
        planner = ProactiveController(config(screen_enabled=False, companion_min_seconds=10, companion_max_seconds=10))
        planner.observe(sample(0), busy=BusyState(chat_active=True))
        result = planner.observe(sample(10, foreground_owned=True), busy=BusyState(chat_active=True))
        self.assertEqual(result.event.kind, ProactiveEventKind.COMPANION_TOPIC)
        self.assertEqual((result.event.idle_seconds, result.event.active_seconds), (0, 0))
        self.assertTrue(planner.should_deliver(result, now=11, busy=BusyState(chat_active=True)))

    def test_companion_topic_yields_to_real_draft_manual_input_and_desktop_guards(self):
        for flags, busy in (({}, BusyState(input_active=True)), ({}, BusyState(typing=True)),
                            ({}, BusyState(speaking=True)), ({}, BusyState(dragging=True)),
                            ({"foreground_fullscreen": True}, BusyState()),
                            ({"desktop_available": False}, BusyState())):
            with self.subTest(flags=flags, busy=busy):
                planner = ProactiveController(config(screen_enabled=False, companion_min_seconds=10, companion_max_seconds=10))
                planner.observe(sample(0))
                self.assertIsNone(planner.observe(sample(10, **flags), busy=busy))
                self.assertIsNotNone(planner.status(10)["suppression_reason"])
        planner = ProactiveController(config(screen_enabled=False, companion_min_seconds=10, companion_max_seconds=10))
        planner.observe(sample(0))
        decision = planner.observe(sample(10))
        planner.note_manual_interaction(11)
        self.assertFalse(planner.should_deliver(decision, now=12))
        self.assertIsNone(planner.observe(sample(12)))
        self.assertEqual(planner.status(12)["suppression_reason"], "manual_cooldown")

    def test_seated_mode_holds_anchor_through_speech_touch_drag_and_resume(self):
        atlas = SimpleNamespace(width=192, height=224, anchor=(96, 216))
        bridge = BehaviorBridge(atlas, ROOT / "config/desktop_pet_behavior.json", lambda event: None)
        bridge.set_seated(True, now=0)
        options = dict(position=(300, 400), room=(0, 0, 1920, 1040), mouse=None,
                       animations={"idle", "sit", "sit_down", "sit_swing", "carried", "land", "stand_up"})
        for now, busy in ((1, BusyState()), (2, BusyState(speaking=True)),
                          (3, BusyState(interacting=True)), (4, BusyState(thinking=True))):
            result = bridge.step(now, busy=busy, **options)
            self.assertEqual(result.state, MotionState.SEATED)
            self.assertEqual(result.animation, "sit")
            self.assertEqual(result.anchor_position, (408, 626))
            self.assertFalse(result.moved)
        result = bridge.step(5, busy=BusyState(dragging=True), **options)
        self.assertEqual(result.animation, "carried")
        options["position"] = (700, 200)
        result = bridge.step(6, busy=BusyState(), **options)
        self.assertEqual(result.animation, "sit_down")
        result = bridge.step(7, busy=BusyState(), **options)
        self.assertTrue(bridge.seated)
        self.assertEqual(result.state, MotionState.SEATED)
        self.assertEqual(result.position, (700, 200))
        bridge.options(roaming=False, mode="occasional")
        self.assertTrue(bridge.seated)
        bridge.set_seated(False, now=8)
        result = bridge.step(9, busy=BusyState(), **options)
        self.assertEqual(result.reason, "roaming_disabled")

    def test_old_skin_seated_fallback_never_walks(self):
        atlas = SimpleNamespace(width=192, height=224, anchor=(96, 216))
        bridge = BehaviorBridge(atlas, ROOT / "config/desktop_pet_behavior.json", lambda event: None, seated=True)
        result = bridge.step(1, position=(300, 400), room=(0, 0, 1920, 1040), mouse=None,
                             busy=BusyState(speaking=True), animations={"idle"})
        self.assertEqual(result.animation, "idle")
        self.assertEqual(result.state, MotionState.SEATED)
        self.assertFalse(result.moved)

    def test_screen_shares_global_cooldown_and_hourly_budget(self):
        planner = ProactiveController(config(screen_min_seconds=1, screen_max_seconds=1,
                                             cooldown_seconds=10, max_events_per_hour=2))
        planner.observe(sample(0))
        self.assertIsNotNone(planner.observe(sample(1)))
        self.assertIsNone(planner.observe(sample(2)))
        self.assertIsNotNone(planner.observe(sample(11)))
        self.assertIsNone(planner.observe(sample(21)))

    def test_idle_action_requires_real_animation_and_yields_to_input(self):
        actions = IdleActionPlanner(IdleActionConfig(min_gap_seconds=1, max_gap_seconds=1), rng=random.Random(3))
        available = {"idle", "wave", "stretch", "yawn", "adjust_ribbon"}
        self.assertIsNone(actions.step(0, busy=BusyState(), available=available))
        self.assertIsNone(actions.step(1, busy=BusyState(), available=available, stationary=False))
        first = actions.step(2, busy=BusyState(), available=available)
        self.assertIn(first, available - {"idle"})
        self.assertEqual(actions.step(2.1, busy=BusyState(), available=available), first)
        self.assertIsNone(actions.step(2.2, busy=BusyState(dragging=True), available=available))
        self.assertIsNone(actions.step(4, busy=BusyState(), available={"idle"}))
        second = actions.step(5, busy=BusyState(), available=available)
        self.assertNotEqual(first, second)

    def test_stationary_action_holds_position_and_manual_preempts(self):
        atlas = SimpleNamespace(width=192, height=224, anchor=(96, 216))
        bridge = BehaviorBridge(atlas, ROOT / "config/desktop_pet_behavior.json", lambda event: None)
        bridge.idle_actions = IdleActionPlanner(IdleActionConfig(min_gap_seconds=1, max_gap_seconds=1))
        position, room = (300, 400), (0, 0, 1920, 1040)
        options = dict(position=position, room=room, mouse=None, animations={"idle", "stretch"})
        bridge.step(0, busy=BusyState(), **options)
        result = bridge.step(1, busy=BusyState(), **options)
        self.assertEqual(result.state, MotionState.IDLE_ACTION)
        self.assertEqual(result.position, position)
        self.assertFalse(result.moved)
        bridge.note_manual_interaction(1.1)
        result = bridge.step(1.2, busy=BusyState(dragging=True), **options)
        self.assertEqual(result.state, MotionState.DRAGGED)
        self.assertEqual(result.position, position)

    def test_acting_schema_is_stable_and_new_mapping_is_bounded(self):
        expressions, motions = set(), set()
        for attitude in ATTITUDES:
            for intensity in INTENSITIES:
                for delivery in DELIVERIES:
                    reply = build_reply({"speech_text_ja": "少し話してもいい？", "attitude": attitude,
                                         "intensity": intensity, "delivery": delivery},
                                        turn_id="injected", provider_mode="offline_demo")
                    expressions.add(reply.expression)
                    motions.add(reply.motion)
        self.assertTrue({"laughing", "smug", "worried", "irritated", "curious", "sleepy", "sad", "surprised"} <= expressions)
        self.assertTrue({"wave", "stretch", "yawn", "adjust_ribbon"} <= motions)
        self.assertEqual(set(reply_schema()["properties"]),
                         {"speech_text_ja", "subtitle_zh", "attitude", "intensity", "delivery"})

    def test_pack_exposes_real_actions_and_expressions(self):
        manifest = json.loads((ROOT / "desktop_pet/assets/nino-pixel-v2/manifest.json").read_text(encoding="utf-8-sig"))
        for key in ("wave", "stretch", "yawn", "adjust_ribbon"):
            records = manifest["animations"][key]["frames"]
            self.assertGreaterEqual(len({record["frame"] for record in records}), 4)
            self.assertTrue(all(record["frame"] in manifest["frames"] for record in records))
        for key in ("surprised", "sad", "sleepy", "laughing", "smug", "worried", "irritated", "curious"):
            self.assertIn(key, manifest["animations"])


class ScreenControllerContracts(unittest.TestCase):
    def test_single_capture_attaches_image_and_releases_after_delivery(self):
        finished = threading.Event()
        events = []
        image = SimpleNamespace(observed_at=time.monotonic(), metadata=lambda: {"source": "injected", "width": 1})
        capture = SimpleNamespace(calls=0, current=True, closed=False)
        def take(**options):
            capture.calls += 1
            return image
        capture.capture = take
        capture.is_current = lambda observation, **options: capture.current
        capture.close = lambda: setattr(capture, "closed", True)
        class Agent:
            screenshot = None
            def generate_proactive_reply(self, event, **options):
                self.screenshot = options["screenshot"]
                return AgentReply("test", "ここ、少し気になるわね。")
        agent = Agent()
        def emit(event):
            events.append(event)
            if event["type"] == "finished":
                finished.set()
        controller = ConversationController({"conversation_id": "injected"}, emit, agent=agent, screen_capture=capture)
        now = time.monotonic()
        event = ProactiveEvent("test", ProactiveEventKind.SCREEN_OBSERVATION, now,
                               ActivityCategory.CODING, ObservationSource.INJECTED)
        decision = ProactiveDecision(event, "look_at_screen", "curious", True, "injected", now + 60, 1)
        try:
            operation = controller.start_proactive(decision, should_deliver=lambda *args, **kwargs: True, speak=False)
            self.assertTrue(finished.wait(3))
            self.assertEqual(capture.calls, 1)
            self.assertIs(agent.screenshot, image)
            self.assertTrue(controller.allow_delivery(operation))
            controller._active.proactive_decision = replace(decision, expires_at=time.monotonic() - 1)
            controller.mark_playback(operation, True)
            self.assertFalse(controller.allow_delivery(operation))
            self.assertTrue(controller.allow_delivery(operation, continuing=True))
            capture.current = False
            self.assertFalse(controller.allow_delivery(operation))
            self.assertFalse(controller.allow_delivery(operation, continuing=True))
            controller.complete_delivery(operation)
            self.assertIsNone(controller._active.screen_observation)
            metadata = [item for item in events if item["type"] == "screen_observation"]
            self.assertEqual(metadata[0]["observation"], {"source": "injected", "width": 1})
        finally:
            controller.close()
            self.assertTrue(controller.closed.wait(3))
            self.assertTrue(capture.closed)

    def test_optional_observation_never_replaces_an_inflight_turn(self):
        controller = ConversationController.__new__(ConversationController)
        controller._condition = threading.Condition()
        controller._closing = False
        now = time.monotonic()
        event = ProactiveEvent("test", ProactiveEventKind.SCREEN_OBSERVATION, now,
                               ActivityCategory.OTHER, ObservationSource.INJECTED)
        decision = ProactiveDecision(event, "look_at_screen", "curious", True, "injected", now + 60, 1)
        for existing in (Turn("manual", "Hello", True), Turn("screen", "", True, proactive_decision=decision)):
            controller._active = existing
            self.assertIsNone(controller._schedule(Turn("new", "", True, proactive_decision=decision)))
            self.assertIs(controller._active, existing)


if __name__ == "__main__":
    unittest.main()
