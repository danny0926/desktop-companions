"""Manifest-driven PNG frames, nearest-neighbor scaling, and shared anchors."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import struct
import tkinter as tk
import zlib
from fractions import Fraction


EXPRESSION_ANIMATION = {
    "neutral": "idle", "smile": "happy", "annoyed": "annoyed", "shy": "shy",
    "surprised": "surprised", "sad": "sad", "sleepy": "sleepy", "laughing": "laughing",
    "smug": "smug", "worried": "worried", "irritated": "irritated", "curious": "curious",
}


def color_key_png(path: Path) -> bytes:
    """Binary alpha for Tk's Windows color-key compositor, without changing art.

    Semi-transparent edge pixels otherwise blend with the chroma key and leave a
    pink halo. Pixel art has hard edges; preserve RGB and threshold alpha only in
    the in-memory display copy. The atlas and frame PNG files remain untouched.
    """
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
    scanlines = bytearray()
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
        scanlines.extend(b"\0" + current)
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(scanlines, 3)) + chunk(b"IEND", b"")


class SpriteAtlas:
    def __init__(self, master, path: Path, scale: str = "small"):
        self.master = master
        self.path = path
        self.manifest = json.loads(path.read_text(encoding="utf-8-sig"))
        self.originals = {name: tk.PhotoImage(master=master, data=base64.b64encode(color_key_png(path.parent / info["file"])))
                          for name, info in self.manifest["frames"].items()}
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
