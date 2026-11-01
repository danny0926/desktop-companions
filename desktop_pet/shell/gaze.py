"""Small, local eye movements inferred from the existing pixel sprite.

All geometry and offsets are logical sprite pixels. Screen coordinates enter
only through GazeTracker, so a monitor's origin or display scale cannot reverse
the gaze. No source images are changed and no imaging package is required.
"""
from __future__ import annotations

from dataclasses import dataclass
import math


_SKIN = bytes((255, 219, 193, 255))
_IRIS = frozenset(bytes(color) for color in (
    (23, 40, 62, 255), (40, 80, 132, 255), (76, 137, 188, 255),
    (88, 161, 167, 255), (51, 97, 106, 255), (134, 194, 193, 255),
))
_BLUE = frozenset(bytes(color) for color in (
    (23, 40, 62, 255), (40, 80, 132, 255), (76, 137, 188, 255),
))
_WHITES = frozenset(bytes(color) for color in (
    (255, 242, 231, 255), (243, 233, 228, 255),
    (170, 166, 188, 255), (212, 208, 221, 255), (103, 93, 92, 255),
))
_SCLERA = bytes((255, 242, 231, 255))
_OUTLINE = bytes((33, 23, 39, 255))
_NEIGHBORS = ((-1, -1), (0, -1), (1, -1), (-1, 0), (1, 0), (-1, 1), (0, 1), (1, 1))


@dataclass(frozen=True)
class EyeMask:
    aperture: frozenset[tuple[int, int]]
    ink: tuple[tuple[int, int, bytes], ...]


@dataclass(frozen=True)
class GazeGeometry:
    eyes: tuple[EyeMask, EyeMask]
    center: tuple[float, float]


def _components(points):
    remaining = set(points)
    while remaining:
        first = remaining.pop()
        component, todo = {first}, [first]
        while todo:
            x, y = todo.pop()
            for dx, dy in _NEIGHBORS:
                point = x + dx, y + dy
                if point in remaining:
                    remaining.remove(point)
                    component.add(point)
                    todo.append(point)
        yield component


def _bounds(points):
    return min(x for x, _ in points), min(y for _, y in points), max(x for x, _ in points), max(y for _, y in points)


def infer_gaze_geometry(width: int, height: int, pixels: bytes) -> GazeGeometry | None:
    """Identify two open eyes from face skin and the existing iris palette.

    The face's connected skin region excludes bows, shoes, and clothing, even
    though those can share iris colors. Each pose supplies its own geometry;
    closed eyes, rear views, and uncertain partial profiles simply return None.
    SpriteAtlas limits this palette-specific inference to the v3 pixel atlas.
    """
    if (width, height) != (96, 112) or len(pixels) != width * height * 4:
        return None
    colors = [pixels[index:index + 4] for index in range(0, len(pixels), 4)]
    skin = {(index % width, index // width) for index, color in enumerate(colors) if color == _SKIN}
    faces = []
    for component in _components(skin):
        left, top, right, bottom = _bounds(component)
        if len(component) >= 45 and 8 <= right - left <= 36 and 5 <= bottom - top <= 25 and top < height * .72:
            faces.append(component)
    if not faces:
        return None
    face = min(faces, key=lambda points: _bounds(points)[1])
    left, top, right, bottom = _bounds(face)
    candidates = []
    colored = {(x, y) for y in range(max(0, top - 8), min(height, bottom + 1))
               for x in range(max(0, left - 8), min(width, right + 9))
               if colors[y * width + x] in _IRIS}
    for component in _components(colored):
        x0, y0, x1, y1 = _bounds(component)
        if not (4 <= len(component) <= 48 and 2 <= y1 - y0 <= 8 and 1 <= x1 - x0 <= 9):
            continue
        if sum(colors[y * width + x] in _BLUE for x, y in component) < 3:
            continue
        if y1 > bottom - 2 or not any((x + dx, y + dy) in face for x, y in component
                                    for dx in range(-2, 3) for dy in range(-2, 3)):
            continue
        candidates.append(component)
    pairs = []
    for index, first in enumerate(candidates):
        a = _bounds(first)
        for second in candidates[index + 1:]:
            b = _bounds(second)
            separation = abs((a[0] + a[2] - b[0] - b[2]) / 2)
            vertical = abs((a[1] + a[3] - b[1] - b[3]) / 2)
            if 7 <= separation <= 26 and vertical <= 5:
                pairs.append((first, second))
    if len(pairs) != 1:
        return None
    eyes = []
    for iris in sorted(pairs[0], key=lambda points: _bounds(points)[0]):
        x0, y0, x1, y1 = _bounds(iris)
        aperture = set(iris)
        # Extend into existing sclera only. Skin, eyelashes, hair, and the mouth
        # are outside this mask; no rectangular replacement face is painted.
        whites = {(x, y) for y in range(max(0, y0 - 1), min(height, y1 + 2))
                  for x in range(max(0, x0 - 1), min(width, x1 + 2))
                  if colors[y * width + x] in _WHITES}
        for component in _components(whites):
            if any((x + dx, y + dy) in iris for x, y in component for dx, dy in _NEIGHBORS):
                aperture.update(component)
        ink = set(iris)
        # A few pupils contain the same dark pixel as the outline. Only enclosed
        # pixels move with the iris, leaving the top lash line untouched.
        for y in range(y0, y1 + 1):
            for x in range(x0, x1 + 1):
                color = colors[y * width + x]
                neighbors = sum((x + dx, y + dy) in iris for dx, dy in _NEIGHBORS)
                if color in (_OUTLINE, *_WHITES) and neighbors >= 5:
                    ink.add((x, y))
                    aperture.add((x, y))
        eyes.append(EyeMask(frozenset(aperture), tuple((x, y, colors[y * width + x]) for x, y in sorted(ink))))
    centers = [((min(x for x, _ in eye.aperture) + max(x for x, _ in eye.aperture)) / 2,
                (min(y for _, y in eye.aperture) + max(y for _, y in eye.aperture)) / 2) for eye in eyes]
    return GazeGeometry(tuple(eyes), ((centers[0][0] + centers[1][0]) / 2, (centers[0][1] + centers[1][1]) / 2))


def gaze_offset(offset) -> tuple[int, int]:
    """Keep raster variants bounded to the nine subtle logical directions."""
    try:
        x, y = float(offset[0]), float(offset[1])
    except (TypeError, ValueError, IndexError):
        return 0, 0
    if not math.isfinite(x) or not math.isfinite(y):
        return 0, 0
    return max(-1, min(1, round(x))), max(-1, min(1, round(y)))


def apply_gaze_rgba(width: int, height: int, pixels: bytes, geometry: GazeGeometry | None, offset) -> bytes:
    """Move the original iris ink inside its original eye aperture only."""
    dx, dy = gaze_offset(offset)
    if geometry is None or not (dx or dy):
        return pixels
    result = bytearray(pixels)
    for eye in geometry.eyes:
        for x, y, _ in eye.ink:
            start = (y * width + x) * 4
            result[start:start + 4] = _SCLERA
        for x, y, color in eye.ink:
            target = x + dx, y + dy
            if target in eye.aperture:
                start = (target[1] * width + target[0]) * 4
                result[start:start + 4] = color
    return bytes(result)


class GazeTracker:
    """Smooth a physical cursor target, then quantize with a quiet dead zone."""
    def __init__(self):
        self.reset()

    def reset(self):
        self._last_at = None
        self._smooth = (0.0, 0.0)
        self.offset = (0, 0)

    @staticmethod
    def _quantize(value, previous):
        if value >= .42:
            return 1
        if value <= -.42:
            return -1
        if previous > 0 and value >= .20:
            return 1
        if previous < 0 and value <= -.20:
            return -1
        return 0

    def sample(self, cursor, origin, scale, center, now: float) -> tuple[int, int]:
        """Use native screen coordinates, including negative monitor origins."""
        values = (*cursor, *origin, float(scale), *center, now)
        if not all(math.isfinite(value) for value in values) or scale <= 0:
            return self.offset
        delta = tuple((cursor[axis] - origin[axis]) / float(scale) - center[axis] for axis in (0, 1))
        target = tuple(value / (32 + abs(value)) for value in delta)
        elapsed = .035 if self._last_at is None else max(0, min(.15, now - self._last_at))
        self._last_at = now
        blend = -math.expm1(-elapsed / .105)
        self._smooth = tuple(previous + (value - previous) * blend for previous, value in zip(self._smooth, target))
        self.offset = tuple(self._quantize(value, previous) for value, previous in zip(self._smooth, self.offset))
        return self.offset
