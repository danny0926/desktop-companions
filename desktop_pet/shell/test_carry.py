"""Physical pickup contracts, without a window, audio, or model calls."""
import math
import unittest

from desktop_pet.shell.carry import CarryMotion
from desktop_pet.shell.sprites import bounded_rotation_angle, rotate_sprite_rgba


def move_at_speed(speed, *, scale=1):
    motion = CarryMotion()
    motion.begin((0, 0), 0, scale=scale)
    for index in range(1, 16):
        now = index / 30
        motion.update((speed * now * scale, 0), now)
        motion.sample(now)
    return motion


class CarryMotionTests(unittest.TestCase):
    def test_still_hold_settles_into_one_neutral_pose(self):
        motion = move_at_speed(800)
        for index in range(1, 91):
            sample = motion.sample(0.5 + index / 30)
        self.assertEqual(sample.frame, "carried_4")
        self.assertEqual(sample.angle_degrees, 0)
        self.assertEqual(sample.offset_y, 0)
        for now in (4, 8, 20):
            self.assertEqual(motion.sample(now), sample)

    def test_speed_and_reversal_change_the_body_and_leg_response(self):
        slow = move_at_speed(100).sample(0.5)
        fast = move_at_speed(800)
        right = fast.sample(0.5)
        self.assertGreater(right.angle_degrees, slow.angle_degrees * 3)
        self.assertEqual(right.frame, "carried_3")
        for index in range(1, 16):
            now = 0.5 + index / 30
            fast.update((400 - 800 * index / 30, 0), now)
            left = fast.sample(now)
        self.assertLess(left.angle_degrees, -5)
        self.assertEqual(left.frame, "carried_2")
        scaled = move_at_speed(800, scale=3).sample(0.5)
        self.assertAlmostEqual(scaled.angle_degrees, right.angle_degrees)

    def test_vertical_jolt_landing_and_regrab(self):
        motion = CarryMotion()
        motion.begin((20, 20), 0)
        motion.update((20, 70), 0.03)
        self.assertEqual(motion.sample(0.03).frame, "carried_1")
        motion.release(0.04)
        landing = motion.sample(0.06)
        self.assertEqual(landing.phase, "landing")
        self.assertEqual(landing.frame, "land_1")
        self.assertLessEqual(abs(landing.offset_y), 4)
        motion.begin((500, -100), 0.08)
        regrab = motion.sample(0.08)
        self.assertEqual(regrab.frame, "carried_4")
        self.assertEqual(regrab.angle_degrees, 0)
        self.assertEqual(regrab.offset_y, 0)
        motion.release(0.09)
        self.assertIsNone(motion.sample(1))
        self.assertFalse(motion.active)

    def test_extreme_input_and_long_ui_pause_are_bounded(self):
        motion = CarryMotion()
        motion.begin((0, 0), 0)
        for index in range(1, 50):
            now = index / 240
            motion.update(((-1) ** index * 100000, 100000 * index), now)
            sample = motion.sample(now)
            self.assertTrue(math.isfinite(sample.angle_degrees))
            self.assertLessEqual(abs(sample.angle_degrees), motion.MAX_ANGLE)
        settled = motion.sample(600)
        self.assertEqual(settled.angle_degrees, 0)
        self.assertEqual(settled.frame, "carried_4")
        motion.update((0, 0), float("nan"))
        self.assertEqual(motion.sample(601), settled)


class CarryRenderingTests(unittest.TestCase):
    def test_nearest_rotation_preserves_palette_alpha_canvas_and_grip(self):
        width = height = 9
        pixels = bytearray(width * height * 4)
        center = (4 * width + 4) * 4
        pixels[center:center + 4] = bytes((173, 68, 112, 255))
        for angle in (-20, -12, 12, 20):
            result = rotate_sprite_rgba(width, height, bytes(pixels), angle, (4.5, 4.5))
            self.assertEqual(len(result), len(pixels))
            self.assertEqual(result[center:center + 4], pixels[center:center + 4])
            self.assertTrue(set(result[3::4]).issubset({0, 255}))
            colors = {result[index:index + 4] for index in range(0, len(result), 4)}
            self.assertTrue(colors.issubset({bytes(4), bytes((173, 68, 112, 255))}))

    def test_arbitrary_grip_clamps_lean_before_any_occupied_corner_clips(self):
        bounds = (16, 4, 79, 95)
        for grip in ((48, 28), (48, 85), (23, 102), (75, 102)):
            for requested in (-20, 20):
                actual = bounded_rotation_angle(96, 112, bounds, requested, grip)
                self.assertLessEqual(abs(actual), abs(requested))
                cosine, sine = math.cos(math.radians(actual)), math.sin(math.radians(actual))
                for x in (bounds[0], bounds[2]):
                    for y in (bounds[1], bounds[3]):
                        rx = grip[0] + cosine * (x - grip[0]) - sine * (y - grip[1])
                        ry = grip[1] + sine * (x - grip[0]) + cosine * (y - grip[1])
                        self.assertTrue(0 <= rx <= 96 and 0 <= ry <= 112)


if __name__ == "__main__":
    unittest.main()
