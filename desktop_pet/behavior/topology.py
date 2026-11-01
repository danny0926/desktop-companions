"""Sprite-sized routes over physical monitor unions, with work-area destinations."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import math

from .models import Rect, SpriteGeometry

EPS = 1e-6


def _subtract(rect: Rect, cover: Rect) -> list[Rect]:
    left, top = max(rect.left, cover.left), max(rect.top, cover.top)
    right, bottom = min(rect.right, cover.right), min(rect.bottom, cover.bottom)
    if right - left <= EPS or bottom - top <= EPS:
        return [rect]
    pieces = []
    if top > rect.top + EPS:
        pieces.append(Rect(rect.left, rect.top, rect.right, top))
    if bottom < rect.bottom - EPS:
        pieces.append(Rect(rect.left, bottom, rect.right, rect.bottom))
    if left > rect.left + EPS:
        pieces.append(Rect(rect.left, top, left, bottom))
    if right < rect.right - EPS:
        pieces.append(Rect(right, top, rect.right, bottom))
    return pieces


@dataclass(frozen=True)
class Portal:
    first: int
    second: int
    axis: str
    seam: float
    low: float
    high: float


class DesktopTopology:
    def __init__(self, areas, sprite: SpriteGeometry, *, work_areas=None):
        self.sprite = sprite
        bounds = tuple(area if isinstance(area, Rect) else Rect(*area) for area in areas)
        works = tuple(area if isinstance(area, Rect) else Rect(*area) for area in work_areas) if work_areas is not None else bounds
        if len(bounds) != len(works):
            raise ValueError("Monitor bounds/work areas must correspond.")
        pairs = tuple(dict.fromkeys(zip(bounds, works)))
        self.areas = tuple(pair[0] for pair in pairs)
        self.work_areas = tuple(pair[1] for pair in pairs)
        self.signature = (self.areas, self.work_areas, sprite)
        self.nodes = tuple(index for index, area in enumerate(self.areas)
                           if area.width + EPS >= sprite.width and area.height + EPS >= sprite.height)
        self.edges: dict[int, list[tuple[int, Portal]]] = {index: [] for index in self.nodes}
        for offset, first in enumerate(self.nodes):
            a = self.areas[first]
            for second in self.nodes[offset + 1:]:
                b = self.areas[second]
                portal = None
                if abs(a.right - b.left) <= EPS or abs(b.right - a.left) <= EPS:
                    low, high = max(a.top, b.top), min(a.bottom, b.bottom) - sprite.height
                    if high + EPS >= low:
                        portal = Portal(first, second, "x", a.right if abs(a.right - b.left) <= EPS else b.right, low, max(low, high))
                elif abs(a.bottom - b.top) <= EPS or abs(b.bottom - a.top) <= EPS:
                    low, high = max(a.left, b.left), min(a.right, b.right) - sprite.width
                    if high + EPS >= low:
                        portal = Portal(first, second, "y", a.bottom if abs(a.bottom - b.top) <= EPS else b.bottom, low, max(low, high))
                else:
                    left, top = max(a.left, b.left), max(a.top, b.top)
                    right, bottom = min(a.right, b.right), min(a.bottom, b.bottom)
                    if right - left + EPS >= sprite.width and bottom - top + EPS >= sprite.height:
                        portal = Portal(first, second, "overlap", left, top, bottom - sprite.height)
                if portal:
                    self.edges[first].append((second, portal))
                    self.edges[second].append((first, portal))

    def covers(self, rect: Rect) -> bool:
        remaining = [rect]
        for area in self.areas:
            remaining = [piece for part in remaining for piece in _subtract(part, area)]
            if not remaining:
                return True
        return False

    def window_visible(self, position) -> bool:
        x, y = position
        return self.covers(Rect(x, y, x + self.sprite.width, y + self.sprite.height))

    def destination_area(self, index: int):
        work = self.work_areas[index]
        return work if work.width + EPS >= self.sprite.width and work.height + EPS >= self.sprite.height else self.areas[index]

    def clamp_to_node(self, position, index: int):
        area = self.destination_area(index)
        return (min(max(position[0], area.left), area.right - self.sprite.width),
                min(max(position[1], area.top), area.bottom - self.sprite.height))

    def recover_position(self, position):
        """Manual/layout recovery may snap; an autonomous route never does."""
        if self.window_visible(position) or not self.nodes:
            return tuple(position)
        return min((self.clamp_to_node(position, index) for index in self.nodes), key=lambda point: math.dist(point, position))

    def safe_segment(self, start, end) -> bool:
        if abs(start[0] - end[0]) > EPS and abs(start[1] - end[1]) > EPS:
            return False
        return self.covers(Rect(min(start[0], end[0]), min(start[1], end[1]),
                                max(start[0], end[0]) + self.sprite.width, max(start[1], end[1]) + self.sprite.height))

    def axis_route(self, start, end):
        if math.dist(start, end) <= EPS:
            return []
        x_first, y_first = [(end[0], start[1]), tuple(end)], [(start[0], end[1]), tuple(end)]
        orders = (x_first, y_first) if abs(end[0] - start[0]) >= abs(end[1] - start[1]) else (y_first, x_first)
        for points in orders:
            route, current = [], tuple(start)
            for point in points:
                if math.dist(current, point) <= EPS:
                    continue
                if not self.safe_segment(current, point):
                    break
                route.append(point)
                current = point
            else:
                return route
        return None

    def entry(self, position):
        candidates = []
        for index in self.nodes:
            point = self.clamp_to_node(position, index)
            path = self.axis_route(position, point)
            if path is not None:
                candidates.append((sum(math.dist(a, b) for a, b in zip([position] + path, path)), index, point, path))
        return min(candidates, key=lambda item: item[0]) if candidates else None

    def component(self, start: int):
        reached, pending = {start}, deque([start])
        while pending:
            for neighbor, _ in self.edges[pending.popleft()]:
                if neighbor not in reached:
                    reached.add(neighbor); pending.append(neighbor)
        return tuple(sorted(reached))

    def node_path(self, first: int, last: int):
        pending, reached = deque([(first, [])]), {first}
        while pending:
            node, path = pending.popleft()
            if node == last:
                return path
            for neighbor, portal in self.edges[node]:
                if neighbor not in reached:
                    reached.add(neighbor); pending.append((neighbor, path + [(node, neighbor, portal)]))
        return None

    def route(self, position, destination: int, target):
        entry = self.entry(position)
        if entry is None:
            return None
        _, source, current, route = entry
        route = list(route)
        links = self.node_path(source, destination)
        if links is None:
            return None
        for first, second, portal in links:
            a, b = self.areas[first], self.areas[second]
            if portal.axis == "x":
                y = min(max(current[1], portal.low), portal.high)
                forward = a.left < b.left
                exit_point = (portal.seam - self.sprite.width if forward else portal.seam, y)
                enter_point = (portal.seam if forward else portal.seam - self.sprite.width, y)
            elif portal.axis == "y":
                x = min(max(current[0], portal.low), portal.high)
                forward = a.top < b.top
                exit_point = (x, portal.seam - self.sprite.height if forward else portal.seam)
                enter_point = (x, portal.seam if forward else portal.seam - self.sprite.height)
            else:
                overlap = Rect(max(a.left, b.left), max(a.top, b.top), min(a.right, b.right), min(a.bottom, b.bottom))
                exit_point = enter_point = (min(max(current[0], overlap.left), overlap.right - self.sprite.width),
                                            min(max(current[1], overlap.top), overlap.bottom - self.sprite.height))
            alignment = self.axis_route(current, exit_point)
            if alignment is None or not self.safe_segment(exit_point, enter_point):
                return None
            route.extend(alignment)
            if math.dist(exit_point, enter_point) > EPS:
                route.append(enter_point)
            current = enter_point
        final = self.axis_route(current, self.clamp_to_node(target, destination))
        return route + final if final is not None else None
