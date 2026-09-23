# -*- coding: utf-8 -*-
"""
Replace clip polygons with low-vertex shapes that still cover them.

A candidate is kept only when it covers the original bump and
``contains_fn`` says it lies inside the road mask. Rectangles are tried
first (road-aligned, then minimum-area). Nearby bumps merge when one
simpler shape covers both and still fits in the mask.
"""

from __future__ import annotations

import math
from typing import Callable, List, Optional, Sequence, Tuple

XY = Tuple[float, float]
Ring = List[XY]
ContainsFn = Callable[[Sequence[XY]], bool]
AngleFn = Callable[[Sequence[XY]], float]

_COVER_EPS_M = 1e-3


def _dedupe(ring: Sequence[XY], eps: float = 1e-6) -> Ring:
    out: Ring = []
    for p in ring:
        xy = (float(p[0]), float(p[1]))
        if out and math.hypot(out[-1][0] - xy[0], out[-1][1] - xy[1]) <= eps:
            continue
        out.append(xy)
    if (
        len(out) >= 2
        and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) <= eps
    ):
        out.pop()
    return out


def _signed_area(ring: Sequence[XY]) -> float:
    area = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    return area * 0.5


def _ccw(ring: Sequence[XY]) -> Ring:
    pts = _dedupe(ring)
    if len(pts) >= 3 and _signed_area(pts) < 0.0:
        pts.reverse()
    return pts


def _bbox(points: Sequence[XY]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _expand_bbox(box, pad: float):
    return (box[0] - pad, box[1] - pad, box[2] + pad, box[3] + pad)


def _bbox_gap(a, b) -> float:
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return math.hypot(dx, dy)


def point_in_convex(p: XY, ring: Sequence[XY], eps_m: float = _COVER_EPS_M) -> bool:
    """True when ``p`` is inside or on the boundary of a CCW convex ring."""
    n = len(ring)
    if n < 3:
        return False
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        length = math.hypot(ex, ey)
        if length < 1e-12:
            continue
        cross = ex * (p[1] - ay) - ey * (p[0] - ax)
        if cross < -eps_m * length:
            return False
    return True


def convex_hull(points: Sequence[XY]) -> Ring:
    """Monotone-chain hull, CCW, open ring."""
    pts = sorted({(float(p[0]), float(p[1])) for p in points})
    if len(pts) <= 2:
        return pts

    def cross(o, a, b):
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    lower: Ring = []
    for p in pts:
        while len(lower) >= 2 and cross(lower[-2], lower[-1], p) <= 1e-12:
            lower.pop()
        lower.append(p)
    upper: Ring = []
    for p in reversed(pts):
        while len(upper) >= 2 and cross(upper[-2], upper[-1], p) <= 1e-12:
            upper.pop()
        upper.append(p)
    return _dedupe(lower[:-1] + upper[:-1])


def oriented_rect(points: Sequence[XY], angle: float) -> Ring:
    """Minimum rectangle of ``points`` with its long axis at ``angle`` radians."""
    if len(points) == 0:
        return []
    c = math.cos(angle)
    s = math.sin(angle)
    rot = [(p[0] * c + p[1] * s, -p[0] * s + p[1] * c) for p in points]
    minx, miny, maxx, maxy = _bbox(rot)
    if maxx - minx < 1e-6 and maxy - miny < 1e-6:
        return []
    # Keep a sliver so a collinear bump still becomes a thin quad.
    if maxx - minx < 1e-4:
        mid = (minx + maxx) * 0.5
        minx, maxx = mid - 5e-5, mid + 5e-5
    if maxy - miny < 1e-4:
        mid = (miny + maxy) * 0.5
        miny, maxy = mid - 5e-5, mid + 5e-5
    corners = ((minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy))
    # Inverse rotation.
    return _ccw([(x * c - y * s, x * s + y * c) for x, y in corners])


def _long_axis_angle(rect: Sequence[XY]) -> float:
    if len(rect) < 2:
        return 0.0
    best_d = -1.0
    best = 0.0
    n = len(rect)
    for i in range(n):
        ax, ay = rect[i]
        bx, by = rect[(i + 1) % n]
        d = (bx - ax) ** 2 + (by - ay) ** 2
        if d > best_d:
            best_d = d
            best = math.atan2(by - ay, bx - ax)
    return best


def min_area_rect(points: Sequence[XY]) -> Ring:
    hull = convex_hull(points)
    if len(hull) == 0:
        return []
    if len(hull) == 1:
        return []
    if len(hull) == 2:
        ang = math.atan2(hull[1][1] - hull[0][1], hull[1][0] - hull[0][0])
        return oriented_rect(hull, ang)
    best: Ring = []
    best_area = math.inf
    for i in range(len(hull)):
        ax, ay = hull[i]
        bx, by = hull[(i + 1) % len(hull)]
        ang = math.atan2(by - ay, bx - ax)
        rect = oriented_rect(hull, ang)
        area = abs(_signed_area(rect))
        if rect and area < best_area:
            best_area = area
            best = rect
    return best


def _clip_segment_to_rect(a: XY, b: XY, rect) -> Optional[Tuple[XY, XY]]:
    """Liang–Barsky clip. ``rect`` is (minx, miny, maxx, maxy)."""
    minx, miny, maxx, maxy = rect
    dx = b[0] - a[0]
    dy = b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in (
        (-dx, a[0] - minx),
        (dx, maxx - a[0]),
        (-dy, a[1] - miny),
        (dy, maxy - a[1]),
    ):
        if abs(p) < 1e-15:
            if q < 0.0:
                return None
            continue
        t = q / p
        if p < 0.0:
            if t > t1:
                return None
            if t > t0:
                t0 = t
        else:
            if t < t0:
                return None
            if t < t1:
                t1 = t
    if t1 < t0:
        return None
    return (a[0] + t0 * dx, a[1] + t0 * dy), (a[0] + t1 * dx, a[1] + t1 * dy)


class RoadEdges:
    """Local road direction from mask edges near a bump."""

    def __init__(self, edges: Sequence[Tuple[XY, XY]], cell: float = 40.0):
        self.cell = max(float(cell), 1.0)
        self.grid = {}
        for edge in edges:
            self._insert(edge)

    def _key(self, x: float, y: float):
        return (int(math.floor(x / self.cell)), int(math.floor(y / self.cell)))

    def _insert(self, edge: Tuple[XY, XY]) -> None:
        a, b = edge
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        steps = max(1, int(math.ceil(length / self.cell)))
        steps = min(steps, 8000)
        keys = set()
        for i in range(steps + 1):
            t = i / steps
            keys.add(
                self._key(
                    a[0] + t * (b[0] - a[0]),
                    a[1] + t * (b[1] - a[1]),
                )
            )
        for key in keys:
            self.grid.setdefault(key, []).append(edge)

    def _query(self, rect) -> List[Tuple[XY, XY]]:
        ix0, iy0 = self._key(rect[0], rect[1])
        ix1, iy1 = self._key(rect[2], rect[3])
        seen = set()
        out = []
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                for edge in self.grid.get((ix, iy), ()):
                    eid = id(edge)
                    if eid in seen:
                        continue
                    seen.add(eid)
                    out.append(edge)
        return out

    def angle_for(self, points: Sequence[XY], pad: float = 40.0) -> float:
        if len(points) == 0:
            return 0.0
        rect = _expand_bbox(_bbox(points), pad)
        local: Ring = []
        for a, b in self._query(rect):
            clipped = _clip_segment_to_rect(a, b, rect)
            if clipped is None:
                continue
            local.append(clipped[0])
            local.append(clipped[1])
        if len(local) < 2:
            obb = min_area_rect(points)
            return _long_axis_angle(obb) if obb else 0.0
        obb = min_area_rect(local)
        if not obb:
            ax, ay = local[0]
            bx, by = local[1]
            return math.atan2(by - ay, bx - ax)
        return _long_axis_angle(obb)


def _covers(shape: Sequence[XY], points: Sequence[XY]) -> bool:
    ring = _ccw(shape)
    if len(ring) < 3:
        return False
    return all(_pip(p, ring) for p in points)


def _turn(prev: XY, cur: XY, nxt: XY) -> float:
    return (cur[0] - prev[0]) * (nxt[1] - cur[1]) - (cur[1] - prev[1]) * (
        nxt[0] - cur[0]
    )


def _proper_cross(a: XY, b: XY, c: XY, d: XY, eps: float = 1e-8) -> bool:
    def orient(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    o1, o2 = orient(a, b, c), orient(a, b, d)
    o3, o4 = orient(c, d, a), orient(c, d, b)
    return ((o1 > eps and o2 < -eps) or (o1 < -eps and o2 > eps)) and (
        (o3 > eps and o4 < -eps) or (o3 < -eps and o4 > eps)
    )


def _shortcut_crosses(prev: XY, nxt: XY, ring: Sequence[XY], skip: int) -> bool:
    """True when edge prev→nxt hits a non-adjacent edge of ``ring``."""
    n = len(ring)
    share = {(skip - 1) % n, skip, (skip + 1) % n}
    for k in range(n):
        if k in share or (k + 1) % n in share:
            continue
        if _proper_cross(prev, nxt, ring[k], ring[(k + 1) % n]):
            return True
    return False


def _strictly_in_tri(p: XY, a: XY, b: XY, c: XY, eps: float = 1e-4) -> bool:
    def cross(u, v, w):
        return (v[0] - u[0]) * (w[1] - u[1]) - (v[1] - u[1]) * (w[0] - u[0])

    c1, c2, c3 = cross(a, b, p), cross(b, c, p), cross(c, a, p)
    if c1 > eps and c2 > eps and c3 > eps:
        return True
    if c1 < -eps and c2 < -eps and c3 < -eps:
        return True
    return False


def _ear_hits_rings(prev: XY, cur: XY, nxt: XY, rings: Sequence[Ring]) -> bool:
    """True when cutting off triangle prev-cur-nxt would uncover a bump."""
    for ring in rings:
        for p in ring:
            if _strictly_in_tri(p, prev, cur, nxt):
                return True
        n = len(ring)
        for i in range(n):
            if _proper_cross(ring[i], ring[(i + 1) % n], prev, nxt):
                return True
    return False


def erode_vertices(
    ring: Sequence[XY],
    cover_rings: Sequence[Ring],
    contains_fn: ContainsFn,
) -> Ring:
    """
    Drop corners while the ring still covers ``cover_rings`` and stays in the road.

    Convex corners are cut only when the ear misses the bump (the ring shrinks,
    so it stays in the road). Reflex corners are filled only when the growth
    stays in the road.
    """
    cur_ring = _ccw(ring)
    if len(cur_ring) < 4:
        return cur_ring
    limit = len(cur_ring)
    for _ in range(limit):
        if len(cur_ring) <= 3:
            break
        removed = False
        i = 0
        while i < len(cur_ring) and len(cur_ring) > 3:
            n = len(cur_ring)
            prev = cur_ring[(i - 1) % n]
            cur = cur_ring[i]
            nxt = cur_ring[(i + 1) % n]
            if _shortcut_crosses(prev, nxt, cur_ring, i):
                i += 1
                continue
            new_ring = cur_ring[:i] + cur_ring[i + 1 :]
            if _turn(prev, cur, nxt) >= -1e-8:
                if _ear_hits_rings(prev, cur, nxt, cover_rings):
                    i += 1
                    continue
                cur_ring = new_ring
                removed = True
            else:
                if not _covers(new_ring, [p for ring_ in cover_rings for p in ring_]):
                    i += 1
                    continue
                if not contains_fn(new_ring):
                    i += 1
                    continue
                cur_ring = new_ring
                removed = True
        if not removed:
            break
    return _ccw(_dedupe(cur_ring))


def _consider(best: Optional[Ring], shape: Sequence[XY], budget: int) -> Optional[Ring]:
    n = len(shape)
    if n >= budget or n < 3:
        return best
    if best is None or n < len(best):
        return list(shape)
    return best


def _best_shape(
    points: Sequence[XY],
    cover_rings: Sequence[Ring],
    contains_fn: ContainsFn,
    angle_fn: AngleFn,
    budget: int,
    intersect_fn: Optional[Callable[[Sequence[XY]], Sequence[Ring]]] = None,
) -> Optional[Ring]:
    """
    Fewest-vertex shape that covers ``points``, fits in the road, and beats ``budget``.

    A rectangle that lies fully inside the mask wins. Otherwise the rectangle is
    cut to the mask and corners are removed until dropping another one would
    uncover the bump or leave the road.
    """
    best: Optional[Ring] = None
    rects: List[Ring] = []
    aligned = oriented_rect(points, angle_fn(points))
    if len(aligned) >= 3:
        rects.append(aligned)
    obb = min_area_rect(points)
    if len(obb) >= 3:
        rects.append(obb)
    hull = convex_hull(points)
    for shape in rects + ([hull] if len(hull) >= 3 else []):
        if not _covers(shape, points) or not contains_fn(shape):
            continue
        best = _consider(best, shape, budget)
    if best is not None and len(best) <= 4:
        return best
    if intersect_fn is not None:
        flat = [p for ring in cover_rings for p in ring] or list(points)
        for rect in rects:
            try:
                parts = intersect_fn(rect) or []
            except Exception:
                parts = []
            for part in parts:
                if len(part) < 3 or not _covers(part, flat):
                    continue
                if not contains_fn(part):
                    continue
                eroded = erode_vertices(part, cover_rings or [list(points)], contains_fn)
                if not _covers(eroded, flat) or not contains_fn(eroded):
                    continue
                best = _consider(best, eroded, budget)
    return best


class _Group:
    def __init__(self, rings: Sequence[Ring], shape: Ring):
        self.rings = [list(ring) for ring in rings]
        self.points = [p for ring in self.rings for p in ring]
        self.shape = shape

    @property
    def verts(self) -> int:
        return len(self.shape)


def _shape_for_bump(
    bump: Ring,
    contains_fn: ContainsFn,
    angle_fn: AngleFn,
    intersect_fn: Optional[Callable[[Sequence[XY]], Sequence[Ring]]] = None,
) -> Ring:
    bump = _ccw(bump)
    chosen = _best_shape(
        bump, [bump], contains_fn, angle_fn, len(bump), intersect_fn
    )
    if chosen is not None:
        return chosen
    return bump


def _nearby_pairs(groups: Sequence[_Group], gap: float):
    cell = max(float(gap), 1.0)
    boxes = [_bbox(g.points) for g in groups]
    grid = {}
    for i, box in enumerate(boxes):
        exp = _expand_bbox(box, gap)
        ix0 = int(math.floor(exp[0] / cell))
        iy0 = int(math.floor(exp[1] / cell))
        ix1 = int(math.floor(exp[2] / cell))
        iy1 = int(math.floor(exp[3] / cell))
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                grid.setdefault((ix, iy), []).append(i)
    seen = set()
    for i, box in enumerate(boxes):
        exp = _expand_bbox(box, gap)
        ix0 = int(math.floor(exp[0] / cell))
        iy0 = int(math.floor(exp[1] / cell))
        ix1 = int(math.floor(exp[2] / cell))
        iy1 = int(math.floor(exp[3] / cell))
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                for j in grid.get((ix, iy), ()):
                    if j <= i or (i, j) in seen:
                        continue
                    seen.add((i, j))
                    if _bbox_gap(boxes[i], boxes[j]) <= gap + 1e-6:
                        yield i, j


def simplify_clips(
    bump_rings: Sequence[Sequence[XY]],
    merge_gap: float,
    contains_fn: ContainsFn,
    angle_fn: AngleFn,
    intersect_fn: Optional[Callable[[Sequence[XY]], Sequence[Ring]]] = None,
) -> List[Ring]:
    """
    One low-vertex ring per output part.

    ``contains_fn(ring)`` is true when that ring is inside the road mask.
    ``angle_fn(points)`` is the local road direction in radians.
    ``intersect_fn(ring)`` clips a candidate to the mask and returns exterior rings.
    """
    groups: List[_Group] = []
    for raw in bump_rings:
        bump = _ccw(raw)
        if len(bump) < 3 or abs(_signed_area(bump)) < 1e-8:
            continue
        groups.append(
            _Group(
                [bump],
                _shape_for_bump(bump, contains_fn, angle_fn, intersect_fn),
            )
        )

    gap = max(0.0, float(merge_gap))
    if gap > 0.0 and len(groups) >= 2:
        changed = True
        while changed:
            changed = False
            best = None
            for i, j in _nearby_pairs(groups, gap):
                a = groups[i]
                b = groups[j]
                budget = a.verts + b.verts
                points = a.points + b.points
                shape = _best_shape(
                    points,
                    a.rings + b.rings,
                    contains_fn,
                    angle_fn,
                    budget,
                    intersect_fn,
                )
                if shape is None:
                    continue
                savings = budget - len(shape)
                dist = _bbox_gap(_bbox(a.points), _bbox(b.points))
                rank = (savings, -dist)
                if best is None or rank > best[0]:
                    best = (rank, i, j, shape)
            if best is None:
                break
            _rank, i, j, shape = best
            if j < i:
                i, j = j, i
            merged_rings = groups[i].rings + groups[j].rings
            nxt = [g for k, g in enumerate(groups) if k != i and k != j]
            nxt.append(_Group(merged_rings, shape))
            groups = nxt
            changed = True

    return [g.shape for g in groups]


def self_test() -> None:
    def circle(cx, cy, r, n=16):
        return [
            (cx + r * math.cos(2.0 * math.pi * i / n), cy + r * math.sin(2.0 * math.pi * i / n))
            for i in range(n)
        ]

    def rect(x0, y0, x1, y1):
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    road = rect(0.0, 0.0, 100.0, 12.0)

    def contains(ring):
        return all(point_in_convex(p, _ccw(road)) for p in ring)

    def angle(_points):
        return 0.0

    blob = circle(30.0, 6.0, 3.0)
    out = simplify_clips([blob], 100.0, contains, angle)
    assert len(out) == 1, out
    assert len(out[0]) == 4, out[0]
    assert all(point_in_convex(p, out[0]) for p in blob)
    assert contains(out[0])

    # World-aligned square of a diagonal blob sticks out; road-aligned quad fits.
    ang = math.pi / 4.0
    c, s = math.cos(ang), math.sin(ang)

    def rot(p):
        return (p[0] * c - p[1] * s, p[0] * s + p[1] * c)

    wide = [rot(p) for p in rect(-40.0, -4.0, 40.0, 4.0)]
    # Shift onto positive coordinates.
    wide = [(p[0] + 50.0, p[1] + 50.0) for p in wide]
    blob_d = [rot(p) for p in circle(0.0, 0.0, 2.0, 12)]
    blob_d = [(p[0] + 50.0, p[1] + 50.0) for p in blob_d]
    edges = list(zip(wide, wide[1:] + wide[:1]))
    roads = RoadEdges(edges)
    out = simplify_clips([blob_d], 50.0, lambda ring: all(point_in_convex(p, _ccw(wide)) for p in ring), roads.angle_for)
    assert len(out) == 1 and len(out[0]) == 4, out
    assert all(point_in_convex(p, _ccw(wide)) for p in out[0])
    # The quad's long axis follows the road, not the world axes.
    axis = _long_axis_angle(out[0])
    delta = abs((axis - ang + math.pi / 2) % math.pi - math.pi / 2)
    assert delta < 0.2, delta

    a = circle(20.0, 6.0, 2.0, 12)
    b = circle(70.0, 6.0, 2.0, 12)
    merged = simplify_clips([a, b], 100.0, contains, angle)
    assert len(merged) == 1, len(merged)
    assert len(merged[0]) == 4
    assert all(point_in_convex(p, merged[0]) for p in a + b)

    # L mask: bumps on different arms must not become one rectangle.
    ell = _ccw([(0, 0), (50, 0), (50, 8), (8, 8), (8, 50), (0, 50)])

    def contains_ell(ring):
        # Convex candidates: vertices inside the L and the centroid inside the L.
        if not all(_pip(p, ell) for p in ring):
            return False
        cx = sum(p[0] for p in ring) / len(ring)
        cy = sum(p[1] for p in ring) / len(ring)
        return _pip((cx, cy), ell)

    left = circle(4.0, 30.0, 2.0, 10)
    right = circle(30.0, 4.0, 2.0, 10)
    parts = simplify_clips([left, right], 100.0, contains_ell, angle)
    assert len(parts) == 2, len(parts)

    tri = [(10.0, 4.0), (14.0, 4.0), (12.0, 8.0)]
    kept = simplify_clips([tri], 20.0, contains, angle)
    assert len(kept) == 1 and len(kept[0]) == 3, kept

    # Rectangle corners stick out of a tight disk, so the cut shape is eroded.
    blob = circle(0.0, 0.0, 3.0, 16)
    shell = circle(0.0, 0.0, 3.9, 20)

    def contains_disk(ring):
        return all(p[0] * p[0] + p[1] * p[1] <= 4.05 ** 2 for p in ring)

    def intersect_disk(_ring):
        return [list(shell)]

    fitted = simplify_clips([blob], 0.0, contains_disk, angle, intersect_disk)
    assert len(fitted) == 1, fitted
    assert 3 <= len(fitted[0]) < len(shell), len(fitted[0])
    assert all(_pip(p, _ccw(fitted[0])) for p in blob)
    assert contains_disk(fitted[0])

    print("road_clip_simplify_core self_test ok")


def _pip(p: XY, ring: Sequence[XY]) -> bool:
    # General ray cast, boundary counts as inside. Not the convex test:
    # a concave road would otherwise accept points in the dent.
    x, y = p
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        length = math.hypot(ex, ey)
        if length < 1e-12:
            continue
        cross = ex * (y - ay) - ey * (x - ax)
        if abs(cross) <= _COVER_EPS_M * length:
            dot = (x - ax) * ex + (y - ay) * ey
            if -1e-6 <= dot <= length * length + 1e-6:
                return True
    inside = False
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = (x2 - x1) * (y - y1) / ((y2 - y1) or 1e-30) + x1
            if x < xint:
                inside = not inside
    return inside


if __name__ == "__main__":
    self_test()
