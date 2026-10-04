# -*- coding: utf-8 -*-
"""
Points where quantized-mesh edges cross a polygon outline.

Points are placed every ``spacing`` meters along the outline. Mask vertices
between those samples are not copied. A mesh edge that crosses between two
samples can leave a short chord of the outline under the ground; inserting
the crossing keeps a sample on that mesh edge.
"""

from __future__ import annotations

import math
from typing import Callable, Iterable, List, Optional, Sequence, Tuple

Point = Tuple[float, float]
Edge = Tuple[Point, Point]

# Drop a crossing that lands on a sample already kept (outline vertex or
# another crossing). Spacing samples this close to a crossing are skipped.
_CROSS_SEP_M = 0.01
_SPACING_SEP_M = 0.02


def segment_cross_t(
    a: Point,
    b: Point,
    c: Point,
    d: Point,
) -> Optional[float]:
    """
    Parameter t along AB where AB crosses CD.

    t is in (0, 1): the crossing is on the open outline segment, so it is not
    an outline vertex. CD may be touched at an endpoint (a mesh vertex lying
    on the outline). Parallel and collinear edges return None.
    """
    ax, ay = a
    bx, by = b
    cx, cy = c
    dx, dy = d
    rx, ry = bx - ax, by - ay
    sx, sy = dx - cx, dy - cy
    den = rx * sy - ry * sx
    scale = math.hypot(rx, ry) * math.hypot(sx, sy)
    if scale == 0.0 or abs(den) <= 1e-12 * scale:
        return None
    qpx, qpy = cx - ax, cy - ay
    t = (qpx * sy - qpy * sx) / den
    u = (qpx * ry - qpy * rx) / den
    if t <= 1e-12 or t >= 1.0 - 1e-12:
        return None
    if u < -1e-9 or u > 1.0 + 1e-9:
        return None
    return t


class EdgeIndex:
    """Uniform grid of mesh edges in metric coordinates."""

    def __init__(self, edges: Sequence[Edge], cell_m: float = 80.0):
        self.edges = list(edges)
        self.cell = float(cell_m) if cell_m > 0 else 80.0
        self._bins = {}
        self.inserted = 0
        for index, (p, q) in enumerate(self.edges):
            for key in _cells_touched(p, q, self.cell):
                self._bins.setdefault(key, []).append(index)

    def cross_ts(self, a: Point, b: Point) -> List[float]:
        """Parameters along AB of mesh-edge crossings, sorted, 1 cm apart."""
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        if length < 1e-9:
            return []
        ts = []
        seen = set()
        for key in _cells_touched(a, b, self.cell):
            for index in self._bins.get(key, ()):
                if index in seen:
                    continue
                seen.add(index)
                p, q = self.edges[index]
                t = segment_cross_t(a, b, p, q)
                if t is None:
                    continue
                ts.append(t)
        if not ts:
            return []
        ts.sort()
        kept = [ts[0]]
        last = ts[0] * length
        for t in ts[1:]:
            dist = t * length
            if dist - last < _CROSS_SEP_M:
                continue
            kept.append(t)
            last = dist
        return kept


def mask_vertex_points(
    ring: Sequence[Point],
    edge_index: Optional[EdgeIndex] = None,
    on_edge: Optional[Callable[[int, int], None]] = None,
    min_sep_m: float = 0.0,
) -> Iterable[Point]:
    """
    Yield each mask vertex, in order.

    When ``edge_index`` is set, also yield mesh-edge crossings that fall
    between those vertices. A crossing within 1 cm of a vertex is omitted.
    No spacing samples are inserted. A closing vertex that repeats the
    first point is not duplicated. When ``min_sep_m`` is greater than 0,
    a point closer than that to the previous kept point, or to the ring
    start, is omitted (the later point).
    """
    if not ring:
        return

    pts = list(ring)
    if (
        len(pts) >= 2
        and abs(pts[0][0] - pts[-1][0]) < 1e-9
        and abs(pts[0][1] - pts[-1][1]) < 1e-9
    ):
        pts = pts[:-1]
    if len(pts) < 2:
        if pts:
            yield pts[0]
        return

    closed = pts + [pts[0]]
    n_edges = len(closed) - 1
    origin = closed[0]
    last_pt = origin
    yield origin
    cross_tol = max(_CROSS_SEP_M, min_sep_m)

    def _near(pt: Point, other: Point, tol: float) -> bool:
        if tol <= 0.0:
            return False
        return math.hypot(pt[0] - other[0], pt[1] - other[1]) < tol

    for i in range(n_edges):
        if on_edge is not None and (i % 4000 == 0 or i + 1 == n_edges):
            on_edge(i + 1, n_edges)
        x1, y1 = closed[i]
        x2, y2 = closed[i + 1]
        end = (x2, y2)
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length >= 1e-9 and edge_index is not None:
            for t in edge_index.cross_ts((x1, y1), end):
                pt = (x1 + t * dx, y1 + t * dy)
                if (
                    _near(pt, last_pt, cross_tol)
                    or _near(pt, end, cross_tol)
                    or _near(pt, origin, cross_tol)
                ):
                    continue
                edge_index.inserted += 1
                last_pt = pt
                yield pt
        if i < n_edges - 1 and not (
            _near(end, last_pt, min_sep_m) or _near(end, origin, min_sep_m)
        ):
            last_pt = end
            yield end


def densify_ring(
    ring: Sequence[Point],
    spacing: float,
    edge_index: Optional[EdgeIndex] = None,
    on_edge: Optional[Callable[[int, int], None]] = None,
    min_sep_m: float = 0.0,
) -> Iterable[Point]:
    """
    Yield (x, y) along a ring in meter coordinates.

    Places one point every ``spacing`` meters of outline length, starting at
    the ring's first vertex. Mask vertices between those samples are not
    copied. When ``edge_index`` is set, also yields mesh-edge crossings.
    A spacing sample within 2 cm of a kept point is omitted, and a crossing
    within 1 cm. ``min_sep_m`` raises that gap when it is larger. The walk
    back to the first vertex does not duplicate it.
    """
    if not ring or spacing <= 0:
        return

    pts = list(ring)
    if (
        len(pts) >= 2
        and abs(pts[0][0] - pts[-1][0]) < 1e-9
        and abs(pts[0][1] - pts[-1][1]) < 1e-9
    ):
        pts = pts[:-1]
    if len(pts) < 2:
        if pts:
            yield pts[0]
        return

    closed = pts + [pts[0]]
    n_edges = len(closed) - 1
    origin = closed[0]
    last_pt = origin
    yield origin
    along = 0.0
    next_at = spacing

    def _separated(pt: Point, tol: float) -> bool:
        if math.hypot(pt[0] - last_pt[0], pt[1] - last_pt[1]) < tol:
            return False
        if math.hypot(pt[0] - origin[0], pt[1] - origin[1]) < tol:
            return False
        return True

    for i in range(n_edges):
        if on_edge is not None and (i % 4000 == 0 or i + 1 == n_edges):
            on_edge(i + 1, n_edges)
        x1, y1 = closed[i]
        x2, y2 = closed[i + 1]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length < 1e-9:
            continue
        marks = []
        end = along + length
        while next_at <= end + 1e-6:
            dist = next_at - along
            next_at += spacing
            if dist <= 1e-6:
                continue
            if dist >= length - 1e-6:
                # Station lands on this edge's end. The closing vertex is
                # the origin, already emitted.
                if i < n_edges - 1:
                    marks.append((length, False))
                continue
            marks.append((dist, False))
        if edge_index is not None:
            for t in edge_index.cross_ts((x1, y1), (x2, y2)):
                marks.append((t * length, True))
        marks.sort(key=lambda item: (item[0], not item[1]))
        last_d = -1.0e9
        for dist, is_cross in marks:
            if is_cross:
                tol = max(_CROSS_SEP_M, min_sep_m)
            else:
                tol = max(_SPACING_SEP_M, min_sep_m)
            if dist - last_d < tol:
                continue
            t = 1.0 if dist >= length else dist / length
            pt = (x1 + t * dx, y1 + t * dy)
            if not _separated(pt, tol):
                continue
            if is_cross:
                edge_index.inserted += 1
            last_d = dist
            last_pt = pt
            yield pt
        along = end


def _cells_touched(p: Point, q: Point, cell: float):
    """Grid cells the segment actually enters (not the whole bounding box)."""
    x0, y0 = p
    x1, y1 = q
    ix = math.floor(x0 / cell)
    iy = math.floor(y0 / cell)
    ix_end = math.floor(x1 / cell)
    iy_end = math.floor(y1 / cell)
    yield (ix, iy)
    dx = x1 - x0
    dy = y1 - y0
    step_x = 1 if dx > 0.0 else (-1 if dx < 0.0 else 0)
    step_y = 1 if dy > 0.0 else (-1 if dy < 0.0 else 0)
    inf = float("inf")
    t_max_x = inf if step_x == 0 else ((ix + (step_x > 0)) * cell - x0) / dx
    t_max_y = inf if step_y == 0 else ((iy + (step_y > 0)) * cell - y0) / dy
    t_dx = inf if step_x == 0 else abs(cell / dx)
    t_dy = inf if step_y == 0 else abs(cell / dy)
    # Safety cap: a segment longer than this is not a real outline or mesh edge.
    for _ in range(100000):
        if ix == ix_end and iy == iy_end:
            return
        if t_max_x < t_max_y:
            ix += step_x
            t_max_x += t_dx
        elif t_max_y < t_max_x:
            iy += step_y
            t_max_y += t_dy
        else:
            ix += step_x
            iy += step_y
            t_max_x += t_dx
            t_max_y += t_dy
        yield (ix, iy)


def _self_test() -> None:
    t = segment_cross_t((0.0, 0.0), (10.0, 0.0), (5.0, -1.0), (5.0, 1.0))
    assert t is not None and abs(t - 0.5) < 1e-9
    assert segment_cross_t((0.0, 0.0), (10.0, 0.0), (5.0, 1.0), (6.0, 2.0)) is None
    # Touching an endpoint of the outline segment is not a new point.
    assert segment_cross_t((0.0, 0.0), (10.0, 0.0), (0.0, -1.0), (0.0, 1.0)) is None
    # Mesh vertex lying on the open outline segment.
    t_mid = segment_cross_t((0.0, 0.0), (10.0, 0.0), (4.0, 0.0), (4.0, 3.0))
    assert t_mid is not None and abs(t_mid - 0.4) < 1e-9

    edges = [((5.0, -1.0), (5.0, 1.0)), ((7.0, -2.0), (7.0, -1.0))]
    index = EdgeIndex(edges, cell_m=10.0)
    pts = list(densify_ring([(0.0, 0.0), (10.0, 0.0), (10.0, 2.0), (0.0, 2.0)], 4.0, index))
    # Bottom edge length 10: stations at 4 and 8, plus the crossing at 5.
    # The corner (10, 0) is not a 4 m sample, so it is not copied.
    rounded = [(round(x, 6), round(y, 6)) for x, y in pts]
    assert (5.0, 0.0) in rounded
    assert (4.0, 0.0) in rounded and (8.0, 0.0) in rounded
    assert (10.0, 0.0) not in rounded
    assert index.inserted == 1
    # Flag off: spacing only, no crossing, no in-between vertices.
    plain = list(
        densify_ring([(0.0, 0.0), (10.0, 0.0), (10.0, 2.0), (0.0, 2.0)], 4.0)
    )
    plain_r = [(round(x, 6), round(y, 6)) for x, y in plain]
    assert (5.0, 0.0) not in plain_r
    assert (0.0, 0.0) in plain_r
    assert (4.0, 0.0) in plain_r and (8.0, 0.0) in plain_r
    assert (10.0, 0.0) not in plain_r
    # A long diagonal still finds a mesh edge stored in a distant grid cell.
    long_index = EdgeIndex([((500.0, 499.0), (500.0, 501.0))], cell_m=80.0)
    long_pts = list(
        densify_ring(
            [(0.0, 0.0), (1000.0, 1000.0), (1000.0, 2000.0), (0.0, 2000.0)],
            1000.0,
            long_index,
        )
    )
    assert any(abs(x - 500.0) < 1e-6 and abs(y - 500.0) < 1e-6 for x, y in long_pts)
    assert long_index.inserted == 1
    # Samples stay on the 5 m cadence. A vertex a few millimeters past a
    # sample is not copied.
    slim = [
        (round(x, 3), round(y, 3))
        for x, y in densify_ring(
            [(0.0, 0.0), (10.007, 0.0), (10.007, 2.0), (0.0, 2.0)], 5.0
        )
    ]
    assert (5.0, 0.0) in slim and (10.0, 0.0) in slim
    assert (10.007, 0.0) not in slim
    # Vertices every 1 m, each with a 0.7 cm twin, do not become points.
    bottom = []
    for k in range(20):
        bottom.append((float(k), 0.0))
        bottom.append((float(k) + 0.007, 0.0))
    bottom.append((20.0, 0.0))
    twins = list(densify_ring(bottom + [(20.0, 3.0), (0.0, 3.0)], 5.0))
    on_bottom = [round(x, 3) for x, y in twins if abs(y) < 1e-9]
    assert on_bottom == [0.0, 5.0, 10.0, 15.0, 20.0]
    # A mesh crossing between the 5 m samples is still inserted.
    cross = EdgeIndex([((3.5, -1.0), (3.5, 1.0))], cell_m=10.0)
    crossed = list(densify_ring(bottom + [(20.0, 3.0), (0.0, 3.0)], 5.0, cross))
    assert any(abs(x - 3.5) < 1e-6 and abs(y) < 1e-6 for x, y in crossed)
    assert cross.inserted == 1
    assert not any(abs(x - 1.0) < 1e-6 and abs(y) < 1e-6 for x, y in crossed)
    # Mask vertices are kept, and a crossing between them is added.
    vertex_index = EdgeIndex([((5.0, -1.0), (5.0, 1.0))], cell_m=10.0)
    vertex_pts = [
        (round(x, 6), round(y, 6))
        for x, y in mask_vertex_points(
            [(0.0, 0.0), (10.0, 0.0), (10.0, 2.0), (0.0, 2.0)],
            vertex_index,
        )
    ]
    assert (0.0, 0.0) in vertex_pts and (10.0, 0.0) in vertex_pts
    assert (5.0, 0.0) in vertex_pts
    assert (4.0, 0.0) not in vertex_pts
    assert vertex_index.inserted == 1
    # A crossing sitting on a mask vertex is not a second point.
    near_index = EdgeIndex([((9.995, -1.0), (9.995, 1.0))], cell_m=10.0)
    near_pts = list(
        mask_vertex_points(
            [(0.0, 0.0), (10.0, 0.0), (10.0, 2.0), (0.0, 2.0)],
            near_index,
        )
    )
    assert near_index.inserted == 0
    assert not any(abs(x - 9.995) < 1e-4 and abs(y) < 1e-6 for x, y in near_pts)
    # The 0.7 cm mask pair is kept; nothing is invented between them.
    pair = list(
        mask_vertex_points([(0.0, 0.0), (0.007, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)])
    )
    assert any(abs(x - 0.007) < 1e-9 and abs(y) < 1e-9 for x, y in pair)
    assert len(pair) == 5
    # A minimum separation drops the later point of a close pair.
    separated = list(
        mask_vertex_points(
            [(0.0, 0.0), (0.002, 0.0), (0.004, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)],
            min_sep_m=0.010,
        )
    )
    assert [(round(x, 3), round(y, 3)) for x, y in separated] == [
        (0.0, 0.0),
        (1.0, 0.0),
        (1.0, 1.0),
        (0.0, 1.0),
    ]
    # 3 cm past a 5 m sample is kept at the 1 cm crossing rule, and dropped
    # when the caller asks for 5 cm.
    close_cross = EdgeIndex([((5.03, -1.0), (5.03, 1.0))], cell_m=10.0)
    box = [(0.0, 0.0), (20.0, 0.0), (20.0, 3.0), (0.0, 3.0)]
    assert any(
        abs(x - 5.03) < 1e-6 and abs(y) < 1e-6
        for x, y in densify_ring(box, 5.0, close_cross)
    )
    wide = EdgeIndex([((5.03, -1.0), (5.03, 1.0))], cell_m=10.0)
    listed = list(densify_ring(box, 5.0, wide, min_sep_m=0.05))
    assert not any(abs(x - 5.03) < 1e-4 and abs(y) < 1e-6 for x, y in listed)
    assert wide.inserted == 0
    print("outline_qm_crossings self_test ok")


if __name__ == "__main__":
    _self_test()
