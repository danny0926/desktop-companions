"""Manifest-driven PNG frames, nearest-neighbor scaling, and shared anchors."""
from __future__ import annotations

import base64
from collections import OrderedDict
import json
import math
from pathlib import Path
import struct
import tkinter as tk
import zlib
from fractions import Fraction

from .gaze import apply_gaze_rgba, gaze_offset, infer_gaze_geometry


EXPRESSION_ANIMATION = {
    "neutral": "idle", "smile": "happy", "annoyed": "annoyed", "shy": "shy",
    "surprised": "surprised", "sad": "sad", "sleepy": "sleepy", "laughing": "laughing",
    "smug": "smug", "worried": "worried", "irritated": "irritated", "curious": "curious",
}


def _read_rgba(path: Path) -> tuple[int, int, bytes]:
    """Read RGBA8 PNG pixels and apply binary alpha only to the display copy."""
    data = path.read_bytes()
    header = None
    payload = bytearray()
    offset = 8
    while offset < len(data):
        length = struct.unpack_from(">I", data, offset)[0]
        kind, value = data[offset + 4:offset + 8], data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            header = value
        elif kind == b"IDAT":
            payload.extend(value)
        offset += length + 12
    width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", header)
    if (depth, color, compression, filtering, interlace) != (8, 6, 0, 0, 0):
        raise ValueError("The sprite manifest must use non-interlaced RGBA8 PNG frames.")
    raw, stride = zlib.decompress(payload), width * 4
    previous = bytearray(stride)
    pixels = bytearray()
    for row in range(height):
        start = row * (stride + 1)
        filter_type = raw[start]
        current = bytearray(raw[start + 1:start + 1 + stride])
        for index in range(stride):
            left = current[index - 4] if index >= 4 else 0
            up = previous[index]
            corner = previous[index - 4] if index >= 4 else 0
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = up
            elif filter_type == 3:
                predictor = (left + up) // 2
            elif filter_type == 4:
                base = left + up - corner
                distances = (abs(base - left), abs(base - up), abs(base - corner))
                predictor = (left, up, corner)[distances.index(min(distances))]
            elif filter_type == 0:
                predictor = 0
            else:
                raise ValueError("Invalid sprite PNG filter.")
            current[index] = (current[index] + predictor) & 255
        previous = current[:]
        current[3::4] = bytes(255 if alpha >= 128 else 0 for alpha in current[3::4])
        pixels.extend(current)
    return width, height, bytes(pixels)


def _rgba_png(width: int, height: int, pixels: bytes) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    stride = width * 4
    scanlines = b"".join(b"\0" + pixels[start:start + stride] for start in range(0, len(pixels), stride))
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(scanlines, 3)) + chunk(b"IEND", b"")


def color_key_png(path: Path) -> bytes:
    """Binary alpha for Tk's Windows color-key compositor, without changing art.

    Semi-transparent edge pixels otherwise blend with the chroma key and leave a
    pink halo. Pixel art has hard edges; preserve RGB and threshold alpha only in
    the in-memory display copy. The atlas and frame PNG files remain untouched.
    """
    return _rgba_png(*_read_rgba(path))


def bounded_rotation_angle(width: int, height: int, bounds, angle: float, grip) -> int:
    """Quantize lean and keep every occupied corner inside the fixed canvas."""
    angle = max(-20, min(20, round(angle / 2) * 2)) if math.isfinite(angle) else 0
    gx, gy = grip
    left, top, right, bottom = bounds
    while angle:
        cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        corners = [(gx + cosine * (x - gx) - sine * (y - gy),
                    gy + sine * (x - gx) + cosine * (y - gy))
                   for x in (left, right) for y in (top, bottom)]
        if all(0 <= x <= width and 0 <= y <= height for x, y in corners):
            break
        angle -= 2 if angle > 0 else -2
    return angle


def rotate_sprite_rgba(width: int, height: int, pixels: bytes, angle: float, grip) -> bytes:
    """Nearest-neighbor display transform about the actual grip; no new colors.

    Source art is never rewritten. The frame canvas and grip stay fixed, and
    inverse pixel-center mapping avoids holes between rotated source pixels.
    """
    if not angle:
        return pixels
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    gx, gy = grip
    result = bytearray(width * height * 4)
    for y in range(height):
        dy = y + 0.5 - gy
        for x in range(width):
            dx = x + 0.5 - gx
            sx = math.floor(gx + cosine * dx + sine * dy)
            sy = math.floor(gy - sine * dx + cosine * dy)
            if 0 <= sx < width and 0 <= sy < height:
                source, target = (sy * width + sx) * 4, (y * width + x) * 4
                result[target:target + 4] = pixels[source:source + 4]
    return bytes(result)


class SpriteAtlas:
    def __init__(self, master, path: Path, scale: str = "small"):
        self.master = master
        self.path = path
        self.manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        self.originals = {name: tk.PhotoImage(master=master, data=base64.b64encode(color_key_png(path.parent / info["file"])))
                          for name, info in self.manifest["frames"].items()}
        self._carry_sources = {}
        self._gaze_geometry = {}
        self.set_scale(scale)

    def set_scale(self, scale: str):
        size_multiplier = {"small": Fraction(1), "medium": Fraction(3, 2), "large": Fraction(2)}.get(scale, Fraction(1))
        self.scale = Fraction(str(self.manifest["scale"]["recommended"])) * size_multiplier
        numerator, denominator = self.scale.numerator, self.scale.denominator
        self.images = {}
        for name, image in self.originals.items():
            self.images[name] = image.zoom(numerator).subsample(denominator)
        self.width = round(self.manifest["frame_width"] * float(self.scale))
        self.height = round(self.manifest["frame_height"] * float(self.scale))
        anchor = self.manifest["anchor"]
        self.anchor = (round(anchor["x"] * float(self.scale)), round(anchor["y"] * float(self.scale)))
        self._carry_cache = OrderedDict()
        self._carry_grip = None
        self._gaze_cache = OrderedDict()

    def gaze_geometry(self, frame: str):
        """Return a frame's verified eye masks, or a safe legacy fallback."""
        if self.manifest.get("id") != "nino-pixel-v3":
            return None
        frame = self.resolve_frame(frame)
        if frame not in self._gaze_geometry:
            if frame not in self._carry_sources:
                self._carry_sources[frame] = _read_rgba(self.path.parent / self.manifest["frames"][frame]["file"])
            self._gaze_geometry[frame] = infer_gaze_geometry(*self._carry_sources[frame])
        return self._gaze_geometry[frame]

    def gaze_image(self, frame: str, offset):
        """Return a render key and a subtle eye-only display variant.

        The 32-entry LRU contains only requested, scaled variants. Original frame
        names stay separate from this key, so a still pose can follow the mouse.
        """
        frame = self.resolve_frame(frame)
        offset = gaze_offset(offset)
        geometry = self.gaze_geometry(frame)
        if geometry is None or offset == (0, 0):
            return frame, self.images[frame]
        key = ("gaze", frame, *offset)
        if key in self._gaze_cache:
            self._gaze_cache.move_to_end(key)
            return key, self._gaze_cache[key]
        width, height, pixels = self._carry_sources[frame]
        transformed = apply_gaze_rgba(width, height, pixels, geometry, offset)
        original = tk.PhotoImage(master=self.master, data=base64.b64encode(_rgba_png(width, height, transformed)))
        image = original.zoom(self.scale.numerator).subsample(self.scale.denominator)
        self._gaze_cache[key] = image
        while len(self._gaze_cache) > 32:
            self._gaze_cache.popitem(last=False)
        return key, image

    def carry_image(self, frame: str, angle: float, grip: tuple[float, float]):
        """Return ``(render_key, image)`` for a fixed-grip physical carry sample.

        ``grip`` is the original press position relative to the unscaled sprite
        top-left. The result has the normal frame dimensions and NW placement.
        Only a requested variant is built, with at most 24 small cached images;
        changing the grip or sprite scale clears the cache.
        """
        frame = self.resolve_frame(frame)
        width, height = self.manifest["frame_width"], self.manifest["frame_height"]
        gx, gy = (float(grip[0]), float(grip[1]))
        if not math.isfinite(gx) or not math.isfinite(gy):
            gx, gy = width / 2, height / 3
        grip = (max(0, min(width, gx)), max(0, min(height, gy)))
        if grip != self._carry_grip:
            self._carry_cache.clear()
            self._carry_grip = grip
        bounds = self.manifest["frames"][frame].get("opaque_bounds", (0, 0, width, height))
        angle = bounded_rotation_angle(width, height, bounds, angle, grip)
        key = ("carry", frame, angle, grip)
        if not angle:
            return key, self.images[frame]
        if key in self._carry_cache:
            self._carry_cache.move_to_end(key)
            return key, self._carry_cache[key]
        if frame not in self._carry_sources:
            self._carry_sources[frame] = _read_rgba(self.path.parent / self.manifest["frames"][frame]["file"])
        width, height, pixels = self._carry_sources[frame]
        transformed = rotate_sprite_rgba(width, height, pixels, angle, grip)
        original = tk.PhotoImage(master=self.master, data=base64.b64encode(_rgba_png(width, height, transformed)))
        image = original.zoom(self.scale.numerator).subsample(self.scale.denominator)
        self._carry_cache[key] = image
        while len(self._carry_cache) > 24:
            self._carry_cache.popitem(last=False)
        return key, image

    def resolve_frame(self, name: str) -> str:
        """Return a manifest frame, applying the small set of supported aliases."""
        frames = self.manifest["frames"]
        if name in frames:
            return name
        aliases = {"talk_small": "talk", "talk_open": "talk", "surprised": "idle", "sad": "idle", "smile": "happy"}
        candidate = aliases.get(name, "idle")
        return candidate if candidate in frames else "idle"

    def resolve_animation(self, name: str) -> str:
        """Return a manifest animation, including planner behavior aliases."""
        animations = self.manifest["animations"]
        if name in animations:
            return name
        aliases = {"walking_left": "walk_left", "walking_right": "walk_right", "walking_down": "walk_down", "walking_up": "walk_up", "rest": "sit", "headpat": "happy"}
        candidate = aliases.get(name, "idle")
        return candidate if candidate in animations else "idle"

    def available_behavior_animations(self) -> set[str]:
        """Return available animations with the planner's behavior aliases."""
        available = set(self.manifest["animations"])
        for alias, target in {"walking_left": "walk_left", "walking_right": "walk_right", "walking_down": "walk_down", "walking_up": "walk_up", "rest": "sit"}.items():
            if target in available:
                available.add(alias)
        return available

    def frame(self, animation: str, elapsed_ms: float) -> str:
        info = self.manifest["animations"][self.resolve_animation(animation)]
        frames = info["frames"]
        duration = sum(int(frame["duration_ms"]) for frame in frames)
        if info.get("loop", True):
            position = elapsed_ms % max(1, duration)
        else:
            position = min(max(0, elapsed_ms), max(0, duration - 1))
        for frame in frames:
            position -= int(frame["duration_ms"])
            if position < 0:
                return self.resolve_frame(frame["frame"])
        return self.resolve_frame(frames[-1]["frame"])

    def animation_duration(self, animation: str) -> float:
        info = self.manifest["animations"][self.resolve_animation(animation)]
        return max(.1, sum(int(frame["duration_ms"]) for frame in info["frames"]) / 1000)

    def write_icon(self, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        original = self.originals["idle"]
        factor = max(1, (original.width() + 31) // 32, (original.height() + 31) // 32)
        small = original.subsample(factor)
        icon = tk.PhotoImage(master=self.master, width=32, height=32)
        icon.tk.call(icon, "copy", small, "-to", (32 - small.width()) // 2, (32 - small.height()) // 2)
        png_path = folder / "tray-icon.png"
        icon.write(str(png_path), format="png")
        png = png_path.read_bytes()
        ico_path = folder / "tray-icon.ico"
        ico_path.write_bytes(struct.pack("<HHH", 0, 1, 1) +
                             struct.pack("<BBBBHHII", 32, 32, 0, 0, 1, 32, len(png), 22) + png)
        return ico_path
