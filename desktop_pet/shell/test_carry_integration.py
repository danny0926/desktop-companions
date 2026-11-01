"""Real pointer hooks and carry render dispatch, without Tk/audio/model startup."""
import queue
import unittest
from unittest.mock import patch

from desktop_pet.shell.test_interaction import fake_app, pointer


class CarryIntegrationTests(unittest.TestCase):
    def test_body_click_stays_passive_and_real_drag_keeps_original_grip(self):
        app = fake_app()
        opened = []
        app.open_chat = lambda: opened.append(True)
        with patch("desktop_pet.app.window_rect", return_value=(100, 200, 316, 444)):
            with patch("desktop_pet.app.time.monotonic", return_value=10):
                app._drag_start(pointer(y=175))
                self.assertFalse(app._carry.active)
                app._drag_end(pointer(y=175))
            self.assertEqual(opened, [True])
            self.assertEqual(app._last_manual_at, float("-inf"))
            with patch("desktop_pet.app.time.monotonic", return_value=20):
                app._drag_start(pointer(y=175))
            with patch("desktop_pet.app.time.monotonic", return_value=20.02):
                app._drag_move(pointer(x=128, y=180))
            self.assertTrue(app._carry.active)
            grip = app._carry_grip
            self.assertEqual(grip, (48, 82.5))
            with patch("desktop_pet.app.time.monotonic", return_value=20.05):
                app._drag_move(pointer(x=168, y=195))
            self.assertEqual(app._carry_grip, grip)
            self.assertEqual(app.moves[-1], (160, 220))
            with patch("desktop_pet.app.time.monotonic", return_value=20.08):
                app._drag_end(pointer(x=168, y=195))
        self.assertEqual(app._carry.sample(20.1).phase, "landing")
        self.assertEqual(app._carry_grip, grip)
        self.assertIsNone(app._carry.sample(21))

    def test_tick_uses_physics_image_and_seated_landing_over_speech_and_touch(self):
        app = fake_app()
        app.events = queue.Queue()
        app.root.after = lambda *args: None
        app._behavior_tick = lambda now: None
        app.seated_var.set(True)
        app.player.playing = True
        app._touch_reaction_until = 20
        app._motion, app._motion_start = "shake", 10
        app._carry_grip = (48, 55)
        app._carry.begin((0, 0), 10, scale=2)
        app._carry.update((60, 0), 10.02)
        calls, coordinates = [], []
        app.atlas.manifest = {"animations": {"sit": {}, "sit_down": {}}}
        app.atlas.frame = lambda animation, elapsed: animation + "_frame"
        app.atlas.animation_duration = lambda animation: .61

        def render(frame, angle, grip):
            calls.append((frame, angle, grip))
            return ("carry", frame, angle, grip), "physical-image"

        app.atlas.carry_image = render
        app.canvas.coords = lambda item, x, y: coordinates.append((x, y))
        with patch("desktop_pet.app.time.monotonic", return_value=10.04):
            app._tick()
        self.assertTrue(calls[-1][0].startswith("carried_"))
        self.assertEqual(calls[-1][2], app._carry_grip)
        self.assertEqual(app.frames[-1], "physical-image")
        self.assertEqual(coordinates[-1], (12, 10))
        app._carry.release(10.05)
        with patch("desktop_pet.app.time.monotonic", return_value=10.2):
            app._tick()
        self.assertEqual(calls[-1][0], "sit_down_frame")
        self.assertEqual(coordinates[-1], (12, 10 + round(app._carry_sample.offset_y * 2)))
        app._carry.reset()
        app.seated_var.set(False)
        app._touch_reaction_until = 0
        app._drag, app._dragged = (0, 0, 100, 200), True
        with patch("desktop_pet.app.time.monotonic", return_value=10.25):
            app._tick()
        self.assertEqual(calls[-1], ("carried_4", 0, app._carry_grip))
        self.assertEqual(coordinates[-1], (12, 10))


if __name__ == "__main__":
    unittest.main()
