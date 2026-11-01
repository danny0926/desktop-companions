"""Regressions for presentation controls silencing an in-flight optional TTS turn."""
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_pet.behavior import (ActivityCategory, ActivitySnapshot, BusyState, ObservationSource,
                                  ProactiveConfig, ProactiveController, ProactiveEventKind)
from desktop_pet.contracts import AgentReply
from desktop_pet.shell.behavior import BehaviorBridge
from desktop_pet.shell.controller import ConversationController
from desktop_pet.shell.test_interaction import fake_app, pointer


ROOT = Path(__file__).resolve().parents[2]


def sample(now, idle=0, **flags):
    return ActivitySnapshot(now, idle, ActivityCategory.OTHER, source=ObservationSource.INJECTED, **flags)


class HeldVoice:
    config = {}

    def __init__(self):
        self.entered, self.resume = threading.Event(), threading.Event()

    def begin_turn(self, turn_id):
        pass

    def synthesize(self, request, **options):
        self.entered.set()
        if not self.resume.wait(3):
            raise TimeoutError("Test did not release the held synthesis")
        return {"status": "generated", "playable": True, "wav_path": "test-only.wav"}

    def cancel(self, **options):
        return {"status": "cancel_requested"}

    def close(self):
        self.resume.set()


@contextmanager
def held_proactive():
    app = fake_app()
    app.config = {"conversation_id": "test-only", "allow_injected_proactive": True}
    app.speak_var.set(True)
    app._refresh_status_panel = app._refresh_voice_library = lambda: None
    view = SimpleNamespace(winfo_exists=lambda: True, state=lambda: "normal", deiconify=lambda: None,
                           lift=lambda: None)
    app.settings = app.voice_library = app.companion_settings = app.chat = view
    app.input_box = SimpleNamespace(winfo_exists=lambda: True, get=lambda *args: "",
                                    focus_set=lambda: app._input_focus(True))
    app.menu = SimpleNamespace(entryconfigure=lambda *args, **kwargs: None,
                               tk_popup=lambda *args: None, grab_release=lambda: None)
    app._seat_menu_index = 1
    bridge = BehaviorBridge(SimpleNamespace(width=192, height=224, anchor=(96, 216)),
                            ROOT / "config/desktop_pet_behavior.json", lambda event: None)
    bridge.proactive = ProactiveController(replace(bridge.config.proactive, screen_enabled=False,
                                                   companion_min_seconds=1, companion_max_seconds=1))
    now = time.monotonic()
    bridge.proactive.observe(sample(now - 1))
    decision = bridge.proactive.observe(sample(now))
    bridge.latest_snapshot = sample(now)
    app.behavior = bridge
    app._turn_is_proactive = True
    app._active_proactive_decision = decision
    bridge.delivery_busy = app._busy_state(now, delivery=True)
    voice, events, audio_ready = HeldVoice(), [], threading.Event()

    def emit(event):
        events.append(event)
        if event["type"] == "audio":
            audio_ready.set()

    reply = AgentReply("test-only", "少し話してもいい？")
    agent = SimpleNamespace(generate_proactive_reply=lambda *args, **kwargs: reply)
    app.controller = controller = ConversationController(app.config, emit, agent=agent, voice=voice)
    app.turn_id = operation = controller.start_proactive(decision, should_deliver=app._proactive_guard)
    app._synthesis_pending, app._turn_complete, app._reply = True, False, reply.to_dict()
    try:
        if not voice.entered.wait(2):
            raise AssertionError(f"Optional turn never reached held synthesis: {events}")
        yield app, voice, events, audio_ready, operation
    finally:
        voice.resume.set()
        controller.close()
        controller._worker.join(3)
        if not controller.closed.wait(3):
            raise AssertionError("Fake controller did not close")


class ProactiveInterruptionRegressionTests(unittest.TestCase):
    def test_readonly_views_and_presentation_controls_preserve_nonvisual_tts(self):
        with held_proactive() as (app, voice, events, audio_ready, operation):
            for inspect in (app.open_settings, app.open_voice_library, app.open_companion_settings, app.open_chat,
                            lambda: app.show_menu(0, 0)):
                inspect()
            self.assertEqual(app._last_manual_at, float("-inf"))
            with patch("desktop_pet.app.window_rect", return_value=(100, 200, 316, 444)):
                app._drag_start(pointer(y=175))
                self.assertFalse(app._busy_state(time.monotonic(), delivery=True).dragging)
                app._drag_end(pointer(y=175))
            app.set_seated(True)
            app._behavior_options_changed()
            app.behavior.delivery_busy = app._busy_state(time.monotonic(), delivery=True)
            self.assertEqual(app.turn_id, operation)
            self.assertTrue(app.controller.allow_delivery(operation))
            self.assertGreater(app.behavior.proactive.status()["presentation_cooldown_remaining"], 0)
            voice.resume.set()
            self.assertTrue(audio_ready.wait(2), events)
            self.assertFalse(any(event["type"] == "proactive_discarded" for event in events))

    def test_explicit_stop_and_new_typing_still_cancel_nonvisual_tts(self):
        for interrupt in (lambda app: app.stop(), lambda app: app._input_activity()):
            with self.subTest(interrupt=interrupt), held_proactive() as (app, voice, events, audio_ready, operation):
                interrupt(app)
                self.assertIsNone(app.turn_id)
                self.assertFalse(app.controller.allow_delivery(operation))
                self.assertTrue(any(event["type"] == "proactive_discarded" for event in events))

    def test_nonvisual_reading_idle_is_eligible_until_five_minutes(self):
        cfg = ProactiveConfig(screen_enabled=False, companion_min_seconds=1, companion_max_seconds=1)
        self.assertEqual(cfg.companion_max_idle_seconds, 300)
        self.assertEqual(cfg.screen_max_idle_seconds, 60)
        planner = ProactiveController(cfg)
        planner.observe(sample(0, idle=120))
        decision = planner.observe(sample(2, idle=122))
        self.assertEqual(decision.event.kind, ProactiveEventKind.COMPANION_TOPIC)
        self.assertEqual((decision.event.idle_seconds, decision.event.active_seconds), (0, 0))
        planner = ProactiveController(cfg)
        planner.observe(sample(0, idle=301))
        self.assertIsNone(planner.observe(sample(2, idle=303)))
        self.assertEqual(planner.status(2)["suppression_reason"], "user_idle")

    def test_presentation_changes_do_not_lease_stale_screen_observations(self):
        cfg = ProactiveConfig(screen_min_seconds=1, screen_max_seconds=1)
        planner = ProactiveController(cfg)
        planner.observe(sample(0))
        decision = planner.observe(sample(2))
        self.assertEqual(decision.event.kind, ProactiveEventKind.SCREEN_OBSERVATION)
        planner.note_presentation_interaction(3)
        self.assertFalse(planner.should_deliver(decision, now=3))


if __name__ == "__main__":
    unittest.main()
