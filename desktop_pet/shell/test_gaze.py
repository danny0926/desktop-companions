"""Eye-only cursor following contracts; no windows, audio, or model calls."""
from fractions import Fraction
import json
from pathlib import Path
import queue
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from desktop_pet.shell.gaze import GazeTracker, apply_gaze_rgba, infer_gaze_geometry
from desktop_pet.shell.sprites import SpriteAtlas, _read_rgba


ASSETS = Path(__file__).resolve().parents[1] / "assets" / "nino-pixel-v3"
DIRECTIONS = tuple((x, y) for y in (-1, 0, 1) for x in (-1, 0, 1))


def settle(tracker, cursor, origin, scale, center, start=0):
    for index in range(25):
        offset = tracker.sample(cursor, origin, scale, center, start + index * .035)
    return offset


class GazeTrackerTests(unittest.TestCase):
    def test_physical_screen_direction_with_negative_origins_and_fractional_scales(self):
        center = (50.25, 34.25)
        for scale in (Fraction(1), Fraction(3, 2), Fraction(2), Fraction(4)):
            for origin in ((-1800, -350), (2400, -1200), (15, 70)):
                for dx, dy in DIRECTIONS:
                    cursor = tuple(origin[axis] + float(scale) * (center[axis] + delta * 250)
                                   for axis, delta in enumerate((dx, dy)))
                    with self.subTest(scale=scale, origin=origin, direction=(dx, dy)):
                        self.assertEqual(settle(GazeTracker(), cursor, origin, scale, center), (dx, dy))

    def test_smoothing_dead_zone_and_hysteresis_do_not_flicker(self):
        tracker = GazeTracker()
        self.assertEqual(settle(tracker, (300, 0), (0, 0), 1, (0, 0)), (1, 0))
        # Inside the exit/entry gap, tiny hand motion retains the current gaze.
        for index in range(60):
            self.assertEqual(tracker.sample((18 + index % 3, 0), (0, 0), 1, (0, 0), 1 + index * .035), (1, 0))
        self.assertEqual(settle(tracker, (0, 0), (0, 0), 1, (0, 0), 4), (0, 0))
        for index in range(60):
            self.assertEqual(tracker.sample(((-1) ** index * 2, index % 2), (0, 0), 1, (0, 0), 5 + index * .035), (0, 0))
        self.assertEqual(settle(tracker, (-500, 500), (0, 0), 1, (0, 0), 8), (-1, 1))
        self.assertEqual(tracker.sample((float("nan"), 0), (0, 0), 1, (0, 0), 10), (-1, 1))
        tracker.reset()
        self.assertEqual(tracker.offset, (0, 0))


class GazePixelsTests(unittest.TestCase):
    def test_all_v3_variants_only_change_verified_eye_pixels_and_keep_palette_alpha(self):
        manifest = json.loads((ASSETS / "manifest.json").read_text(encoding="utf-8"))
        tracked = 0
        for name, info in manifest["frames"].items():
            width, height, pixels = _read_rgba(ASSETS / info["file"])
            geometry = infer_gaze_geometry(width, height, pixels)
            palette = {pixels[index:index + 4] for index in range(0, len(pixels), 4)}
            allowed = {y * width + x for eye in geometry.eyes for x, y in eye.aperture} if geometry else set()
            tracked += geometry is not None
            for offset in DIRECTIONS:
                with self.subTest(frame=name, offset=offset):
                    result = apply_gaze_rgba(width, height, pixels, geometry, offset)
                    changed = {index // 4 for index in range(0, len(pixels), 4) if result[index:index + 4] != pixels[index:index + 4]}
                    self.assertTrue(changed <= allowed)
                    self.assertEqual(result[3::4], pixels[3::4])
                    self.assertTrue({result[index:index + 4] for index in range(0, len(result), 4)} <= palette)
                    if geometry and offset != (0, 0):
                        self.assertTrue(changed)
        self.assertGreater(tracked, 45)

    def test_blinks_closed_eyes_and_rear_views_are_never_opened(self):
        for name in ("blink", "happy", "sleepy", "laughing", "sit_blink", "sit_happy", "crossed_arms_3", "walk_up_1"):
            width, height, pixels = _read_rgba(ASSETS / "frames" / (name + ".png"))
            with self.subTest(frame=name):
                geometry = infer_gaze_geometry(width, height, pixels)
                self.assertIsNone(geometry)
                self.assertIs(apply_gaze_rgba(width, height, pixels, geometry, (1, 1)), pixels)

    def test_seated_talking_and_tilted_faces_use_their_own_eye_positions(self):
        geometry = {}
        for name in ("idle", "talk", "sit_down_4", "sit_talk_small", "sit_talk_open", "head_tilt_2"):
            geometry[name] = infer_gaze_geometry(*_read_rgba(ASSETS / "frames" / (name + ".png")))
            self.assertIsNotNone(geometry[name], name)
        self.assertEqual(geometry["idle"], geometry["talk"])
        self.assertEqual(geometry["sit_down_4"], geometry["sit_talk_small"])
        self.assertEqual(geometry["sit_down_4"], geometry["sit_talk_open"])
        self.assertGreater(geometry["sit_down_4"].center[1] - geometry["idle"].center[1], 25)
        self.assertGreater(geometry["head_tilt_2"].center[0] - geometry["idle"].center[0], 8)
        # These exact idle bounds exclude the hair ribbons, top lashes and mouth.
        for eye, bounds in zip(geometry["idle"].eyes, ((40, 30, 48, 37), (54, 32, 62, 38))):
            self.assertTrue(all(bounds[0] <= x <= bounds[2] and bounds[1] <= y <= bounds[3] for x, y in eye.aperture))

    def test_runtime_inference_imports_only_standard_library(self):
        result = subprocess.run([sys.executable, "-c", "import sys; import desktop_pet.shell.gaze; print([n for n in sys.modules if n.split('.')[0] in {'PIL', 'numpy', 'torch', 'cv2'}])"],
                                cwd=ASSETS.parents[2], capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.strip(), "[]")


class GazeAtlasTests(unittest.TestCase):
    def test_cache_is_bounded_reuses_variants_and_clears_after_scale_change(self):
        class Image:
            def __init__(self, **options):
                pass

            def zoom(self, *args):
                return self

            def subsample(self, *args):
                return self

        with patch("desktop_pet.shell.sprites.tk.PhotoImage", Image):
            atlas = SpriteAtlas(None, ASSETS / "manifest.json")
            key, image = atlas.gaze_image("idle", (-1, 0))
            self.assertEqual(key, ("gaze", "idle", -1, 0))
            self.assertIs(atlas.gaze_image("idle", (-1, 0))[1], image)
            for frame in ("idle", "talk", "annoyed", "sit", "curious", "surprised"):
                for offset in DIRECTIONS:
                    atlas.gaze_image(frame, offset)
            self.assertEqual(len(atlas._gaze_cache), 32)
            self.assertEqual(atlas.gaze_image("blink", (1, 1)), ("blink", atlas.images["blink"]))
            atlas.set_scale("medium")
            self.assertEqual(atlas._gaze_cache, {})
            atlas.manifest["id"] = "nino-pixel-v2"
            self.assertEqual(atlas.gaze_image("idle", (1, 1)), ("idle", atlas.images["idle"]))
            atlas.manifest["id"] = "nino-q-v1"
            self.assertEqual(atlas.gaze_image("idle", (-1, -1)), ("idle", atlas.images["idle"]))


class GazeTickTests(unittest.TestCase):
    @staticmethod
    def app():
        from desktop_pet.shell.test_interaction import fake_app
        app = fake_app()
        app.events = queue.Queue()
        app.scheduled = []
        app.root.after = lambda *args: app.scheduled.append(args)
        app._behavior_tick = lambda now: None
        app._gaze = GazeTracker()
        app._gaze_failed_frames = set()
        app._gaze_failure_logged = False
        app.atlas.gaze_geometry = lambda frame: SimpleNamespace(center=(50, 34))
        app.atlas.gaze_image = lambda frame, offset: (("gaze", frame, *offset), ("eyes", *offset))
        return app

    def test_still_frame_follows_mouse_and_keeps_actual_frame_name(self):
        app = self.app()
        with patch("desktop_pet.app.window_rect", return_value=(-1500, -300, -1284, -56)):
            for direction in (-1, 1):
                with patch("desktop_pet.app.cursor_position", return_value=(-1388 + direction * 700, -222)):
                    for index in range(15):
                        with patch("desktop_pet.app.time.monotonic", return_value=10 + direction + index * .035):
                            app._tick()
                self.assertEqual(app.frames[-1], ("eyes", direction, 0))
                self.assertEqual(app._last_frame, "idle")
        self.assertEqual(len(app.scheduled), 30)

    def test_optional_render_failure_returns_base_logs_once_and_schedules_next_tick(self):
        app = self.app()
        failures = []

        def broken(frame):
            failures.append(frame)
            raise ValueError("bad optional eye mask")

        app.atlas.gaze_geometry = broken
        with patch("desktop_pet.app.cursor_position", return_value=(0, 0)), patch("desktop_pet.app.time.monotonic", return_value=10):
            app._tick()
            app._tick()
        self.assertEqual(failures, ["idle"])
        self.assertEqual(app.frames[-1], "idle")
        self.assertEqual(len([entry for entry in app.logs if entry[0] == "gaze_fallback"]), 1)
        self.assertEqual(len(app.scheduled), 2)

    def test_existing_carry_renderer_owns_the_image_while_carried(self):
        app = self.app()
        app._carry_grip = (48, 55)
        app._carry.begin((0, 0), 10, scale=2)
        app.atlas.carry_image = lambda frame, angle, grip: (("carry", frame, angle, grip), "carry-image")
        app.atlas.gaze_geometry = lambda frame: self.fail("Gaze must hold while the carry transform owns the image")
        with patch("desktop_pet.app.cursor_position", return_value=(100, 0)), patch("desktop_pet.app.time.monotonic", return_value=10.035):
            app._tick()
        self.assertEqual(app.frames[-1], "carry-image")
        self.assertEqual(app._last_frame, "carried_4")


if __name__ == "__main__":
    unittest.main()
