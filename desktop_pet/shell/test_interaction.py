"""Focused interaction contracts using fakes; no Tk window or model is started."""
from collections import deque
import json
from pathlib import Path
import queue
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_pet.agent.actions import parse_user_action
from desktop_pet.app import PetApplication
from desktop_pet.shell.interaction import HeadpatGesture, head_hit


class Value:
    def __init__(self, value):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class FakeController:
    def __init__(self):
        self.starts, self.deliveries = [], []
        self.stops = 0

    def start(self, text, **options):
        self.starts.append((text, options))
        return f"turn-{len(self.starts)}"

    def stop(self):
        self.stops += 1

    def record_delivery(self, *args):
        self.deliveries.append(args)

    def complete_delivery(self, turn_id):
        pass


def fake_app():
    app = PetApplication.__new__(PetApplication)
    app.config, app.closing, app.smoke = {}, False, False
    app.root = SimpleNamespace(state=lambda: "normal")
    app.native = SimpleNamespace(hwnd=1)
    app.atlas = SimpleNamespace(width=192, height=224, manifest={}, images={"idle": "idle", "headpat": "headpat"},
                                resolve_animation=lambda value: value, frame=lambda value, elapsed: "headpat" if value == "headpat" else "idle")
    app.frames, app.moves, app.logs = [], [], []
    app.canvas = SimpleNamespace(itemconfigure=lambda item, **options: app.frames.append(options["image"]),
                                 coords=lambda *args: None)
    app.sprite_item = 1
    app.behavior = SimpleNamespace(latest_snapshot=None, note_manual_interaction=lambda now: None,
                                  planner=SimpleNamespace(reset=lambda: None))
    app.controller = FakeController()
    app.player = SimpleNamespace(playing=False, item=None, stop=lambda: None)
    app.status, app.speak_var, app.profile_var = Value(""), Value(False), Value("quality_40")
    app.topmost_var, app.subtitle_var = Value(True), Value(True)
    app.size_var, app.skin_var = Value("small"), Value("pixel_v2")
    app.roaming_var, app.proactive_var = Value(True), Value("occasional")
    app.screen_enabled_var, app.screen_frequency_var = Value(True), Value("normal")
    app.screen_scope_var, app.idle_actions_var, app.return_idle_var = Value("active_window"), Value(True), Value(180)
    app.chat, app.transcript, app.input_box, app.bubble = None, None, None, None
    app._transcript_parts, app._heard, app.health = [], [], {}
    app._draft_text = ""
    app._log = lambda event, **fields: app.logs.append((event, fields))
    app._move = lambda x, y: app.moves.append((x, y))
    app._save = lambda: None
    app._animation, app._animation_start = "idle", 0
    app._expression_until, app._motion_start = 0, 0
    app._reply_action_until = 0
    app._motion, app._last_frame = "idle", None
    app._touch_gesture, app._drag = None, None
    app._dragged = False
    app._touch_animation, app._touch_reaction_until = "headpat", 0
    app._last_touch_reply_at = app._last_manual_at = app._last_typing_at = float("-inf")
    app._turn_is_touch = app._turn_is_proactive = app._chat_input_focused = False
    app._prewarm_pending = app._synthesis_pending = False
    app._prewarm_id = app.turn_id = app._reply = None
    app._turn_complete, app._voice_failed = True, False
    app._audio_queue = deque()
    return app


def pointer(x=108, y=65, *, shift=False):
    return SimpleNamespace(x=x, y=y, x_root=x + 100, y_root=y + 200, state=1 if shift else 0)


class InteractionContractTests(unittest.TestCase):
    def test_hit_region_and_consolidated_rub(self):
        self.assertTrue(head_hit(96, 60, 192, 224))
        self.assertFalse(head_hit(96, 170, 192, 224))
        self.assertFalse(head_hit(5, 5, 192, 224))
        gesture = HeadpatGesture(10, 0, 0)
        self.assertEqual(gesture.description(10.1, 192), "輕輕摸頭")
        self.assertEqual(gesture.description(11, 192), "輕輕摸著頭")
        app = fake_app()
        with patch("desktop_pet.app.time.monotonic", return_value=100):
            app._drag_start(pointer())
            self.assertEqual(app.frames[-1], "headpat")
            self.assertEqual(app._busy_state(100).action_block_reason, "interacting")
            for x in (118, 98, 118, 98):
                app._drag_move(pointer(x=x))
            self.assertEqual(app.controller.starts, [])
            self.assertEqual(app.moves, [])
            app._drag_end(pointer())
        self.assertEqual(len(app.controller.starts), 1)
        action = parse_user_action(app.controller.starts[0][0])
        self.assertEqual((action.kind, action.source, action.description), ("headpat", "mouse", "輕輕來回摸頭"))
        self.assertEqual(len([row for row in app.logs if row[0] == "user_action"]), 1)
        self.assertEqual(app._transcript_parts[0][1], "你 · 滑鼠互動")

    def test_body_drag_shift_drag_and_body_click(self):
        app = fake_app()
        app.open_chat = lambda: app.logs.append(("chat", {}))
        with patch("desktop_pet.app.window_rect", return_value=(100, 200, 316, 444)):
            app._drag_start(pointer(y=175))
            app._drag_move(pointer(x=128, y=185))
            app._drag_end(pointer())
            app._drag_start(pointer(shift=True))
            app._drag_move(pointer(x=138, y=85, shift=True))
            app._drag_end(pointer())
            app._drag_start(pointer(y=175))
            app._drag_end(pointer(y=175))
        self.assertEqual(app.moves, [(120, 210), (130, 220)])
        self.assertEqual(len([row for row in app.logs if row[0] == "chat"]), 1)
        self.assertEqual(app.controller.starts, [])

    def test_touch_never_interrupts_manual_audio_and_obeys_cooldown(self):
        app = fake_app()
        app.turn_id, app._turn_complete, app._synthesis_pending = "manual", False, True
        app.player.playing = True
        with patch("desktop_pet.app.time.monotonic", return_value=100):
            app._drag_start(pointer())
            app._drag_end(pointer())
        self.assertEqual(app.turn_id, "manual")
        self.assertEqual(app.controller.stops, 0)
        self.assertEqual(app.controller.starts, [])
        app.player.playing = app._synthesis_pending = False
        app._turn_complete = True
        with patch("desktop_pet.app.time.monotonic", return_value=101):
            app._drag_start(pointer())
            app._drag_end(pointer())
        self.assertEqual(len(app.controller.starts), 1)
        app._synthesis_pending, app._turn_complete = False, True
        with patch("desktop_pet.app.time.monotonic", return_value=120):
            app._drag_start(pointer())
            app._drag_end(pointer())
        self.assertEqual(len(app.controller.starts), 1)
        with patch("desktop_pet.app.time.monotonic", return_value=132):
            app._drag_start(pointer())
            app._drag_end(pointer())
        self.assertEqual(len(app.controller.starts), 2)

    def test_new_typing_discards_touch_and_late_reply(self):
        app = fake_app()
        app.turn_id, app._turn_is_touch, app._synthesis_pending = "old-touch", True, True
        app._audio_queue.append({"turn_id": "old-touch"})
        app._input_activity()
        self.assertIsNone(app.turn_id)
        self.assertFalse(app._synthesis_pending)
        self.assertEqual(app.controller.stops, 1)
        self.assertEqual(len(app._audio_queue), 0)
        app._handle_event({"type": "reply", "turn_id": "old-touch"})
        self.assertEqual(app._transcript_parts, [])
        self.assertEqual(app.logs[-1][0], "stale_ui_event_discarded")

    def test_explicit_actions_preserve_input_and_manual_priority(self):
        app = fake_app()
        app.turn_id, app._turn_complete, app._synthesis_pending = "manual", False, True
        self.assertTrue(app.send_text("*輕輕拍肩*"))
        self.assertEqual(app.controller.starts[-1][0], "*輕輕拍肩*")
        self.assertEqual(app._transcript_parts[-1], ("action", "你 · 輸入動作", "*輕輕拍肩*"))
        self.assertFalse(app._turn_is_touch)
        app._menu_action("cheek_poke", "輕輕戳臉")
        self.assertEqual(parse_user_action(app.controller.starts[-1][0]).source, "menu")
        self.assertEqual(app.controller.stops, 2)
        self.assertTrue(app.send_text("你喜歡摸頭嗎？"))
        self.assertEqual(app._transcript_parts[-1], ("user", "你", "你喜歡摸頭嗎？"))

    def test_draft_save_keeps_unicode_and_never_sends_it(self):
        app = fake_app()
        draft = "還沒寫完的話\n（想輕輕握手）" + "待" * 21000
        app.input_box = SimpleNamespace(winfo_exists=lambda: True, get=lambda *args: draft)
        with TemporaryDirectory() as directory:
            app.state_path = Path(directory) / "window.json"
            with patch("desktop_pet.app.window_rect", return_value=(100, 200, 316, 444)):
                PetApplication._save(app)
            value = json.loads(app.state_path.read_text(encoding="utf-8"))
        self.assertEqual(value["chat_draft"], draft)
        self.assertEqual(value["skin"], "pixel_v2")
        self.assertEqual(app.controller.starts, [])
        self.assertEqual(app._touch_reply_block_reason(100), "chat_input_active")

    def test_release_expiry_preserves_first_walking_animation(self):
        app = fake_app()
        app.events = queue.Queue()
        app.root.after = lambda *args: None
        app._touch_reaction_until = 99
        app._expression_until = 200
        app._behavior_tick = lambda now: app._set_animation("walk_right")
        with patch("desktop_pet.app.time.monotonic", return_value=100):
            app._tick()
        self.assertEqual(app._animation, "walk_right")
        self.assertEqual(app._touch_reaction_until, 0)


if __name__ == "__main__":
    unittest.main()
