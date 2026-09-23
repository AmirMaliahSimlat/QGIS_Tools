# -*- coding: utf-8 -*-
"""
Where a quantized-mesh triangle rises above a 3D road triangle.

Both surfaces are treated as planes in (lon, lat, height). The result is the
2D overlap where ground height exceeds the road by more than ``min_protrusion``
meters. No vertices are moved.
"""

from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

XYZ = Tuple[float, float, float]
XY = Tuple[float, float]
Triangle = Tuple[XYZ, XYZ, XYZ]

# Ignore sub-millimetre crossings so a road that sits on the mesh is not flagged.
_HEIGHT_EPS_M = 1e-4
_AREA_EPS_DEG2 = 1e-18


def _xy(p: Sequence[float]) -> XY:
    return (float(p[0]), float(p[1]))


def _orient_ccw(tri: Triangle) -> Triangle:
    ax, ay = tri[0][0], tri[0][1]
    bx, by = tri[1][0], tri[1][1]
    cx, cy = tri[2][0], tri[2][1]
    area2 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
    if area2 < 0.0:
        return (tri[0], tri[2], tri[1])
    return tri


def _tri_area2(tri: Triangle) -> float:
    ax, ay = tri[0][0], tri[0][1]
    bx, by = tri[1][0], tri[1][1]
    cx, cy = tri[2][0], tri[2][1]
    return abs((bx - ax) * (cy - ay) - (by - ay) * (cx - ax))


def plane_z(px: float, py: float, tri: Triangle) -> Optional[float]:
    """Height of the triangle plane at (px, py). Extrapolates outside the triangle."""
    ax, ay, az = tri[0]
    bx, by, bz = tri[1]
    cx, cy, cz = tri[2]
    denom = (bx - ax) * (cy - ay) - (cx - ax) * (by - ay)
    if abs(denom) < 1e-18:
        return None
    wb = ((px - ax) * (cy - ay) - (py - ay) * (cx - ax)) / denom
    wc = ((bx - ax) * (py - ay) - (by - ay) * (px - ax)) / denom
    wa = 1.0 - wb - wc
    return wa * az + wb * bz + wc * cz


def _is_left(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> bool:
    return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0]) >= -1e-14


def _segment_line_intersect(
    p: Sequence[float],
    q: Sequence[float],
    a: Sequence[float],
    b: Sequence[float],
) -> XY:
    """Intersection of segment pq with the infinite line through a-b."""
    px, py = float(p[0]), float(p[1])
    qx, qy = float(q[0]), float(q[1])
    ax, ay = float(a[0]), float(a[1])
    bx, by = float(b[0]), float(b[1])
    rx, ry = qx - px, qy - py
    sx, sy = bx - ax, by - ay
    denom = rx * sy - ry * sx
    if abs(denom) < 1e-18:
        return ((px + qx) * 0.5, (py + qy) * 0.5)
    t = ((ax - px) * sy - (ay - py) * sx) / denom
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return (px + t * rx, py + t * ry)


def clip_convex_polygon(subject: Sequence[XY], clip_tri: Triangle) -> List[XY]:
    """Sutherland–Hodgman clip of a convex ring to a CCW triangle."""
    output: List[XY] = [(_xy(p)) for p in subject]
    corners = (clip_tri[0], clip_tri[1], clip_tri[2], clip_tri[0])
    for i in range(3):
        if not output:
            return []
        a = corners[i]
        b = corners[i + 1]
        incoming = output
        output = []
        s = incoming[-1]
        s_in = _is_left(s, a, b)
        for e in incoming:
            e_in = _is_left(e, a, b)
            if e_in:
                if not s_in:
                    output.append(_segment_line_intersect(s, e, a, b))
                output.append(_xy(e))
            elif s_in:
                output.append(_segment_line_intersect(s, e, a, b))
            s = e
            s_in = e_in
    return output


def _interp_excess(
    p: Tuple[float, float, float],
    q: Tuple[float, float, float],
    threshold: float,
) -> Tuple[float, float, float]:
    ep = p[2]
    eq = q[2]
    den = eq - ep
    if abs(den) < 1e-12:
        t = 0.0
    else:
        t = (threshold - ep) / den
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return (
        p[0] + t * (q[0] - p[0]),
        p[1] + t * (q[1] - p[1]),
        threshold,
    )


def _dedupe_ring(ring: Sequence[Sequence[float]], eps: float = 1e-12) -> List[XY]:
    out: List[XY] = []
    for p in ring:
        xy = (float(p[0]), float(p[1]))
        if out and abs(out[-1][0] - xy[0]) <= eps and abs(out[-1][1] - xy[1]) <= eps:
            continue
        out.append(xy)
    if (
        len(out) >= 2
        and abs(out[0][0] - out[-1][0]) <= eps
        and abs(out[0][1] - out[-1][1]) <= eps
    ):
        out.pop()
    return out


def _ring_area2(ring: Sequence[XY]) -> float:
    area2 = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        area2 += x1 * y2 - x2 * y1
    return area2


def ring_area_m2(ring: Sequence[XY]) -> float:
    """Equirectangular square meters. Enough to drop numerical specks."""
    n = len(ring)
    if n < 3:
        return 0.0
    lat = sum(p[1] for p in ring) / n
    mx = 111_320.0 * max(math.cos(math.radians(lat)), 1e-6)
    my = 111_320.0
    area2 = 0.0
    for i in range(n):
        x1, y1 = ring[i][0] * mx, ring[i][1] * my
        x2, y2 = ring[(i + 1) % n][0] * mx, ring[(i + 1) % n][1] * my
        area2 += x1 * y2 - x2 * y1
    return abs(area2) * 0.5


def clip_above(
    samples: Sequence[Tuple[float, float, float]],
    threshold: float,
) -> List[XY]:
    """
    Keep the part of a convex ring where excess height is above ``threshold``.

    ``samples`` are (lon, lat, ground_z - road_z). Excess is linear, so one
    half-plane clip is exact.
    """
    if not samples:
        return []
    if max(s[2] for s in samples) <= threshold + _HEIGHT_EPS_M:
        return []
    incoming = list(samples)
    output: List[Tuple[float, float, float]] = []
    s = incoming[-1]
    s_in = s[2] >= threshold
    for e in incoming:
        e_in = e[2] >= threshold
        if e_in:
            if not s_in:
                output.append(_interp_excess(s, e, threshold))
            output.append(e)
        elif s_in:
            output.append(_interp_excess(s, e, threshold))
        s = e
        s_in = e_in
    return _dedupe_ring(output)


def bump_rings(
    qm: Triangle,
    road: Triangle,
    min_protrusion: float,
) -> List[List[XY]]:
    """Overlap of ``qm`` and ``road`` where the ground plane is above the road."""
    qm = _orient_ccw(qm)
    road = _orient_ccw(road)
    if _tri_area2(qm) < 1e-20 or _tri_area2(road) < 1e-20:
        return []
    overlap = clip_convex_polygon(
        [(qm[0][0], qm[0][1]), (qm[1][0], qm[1][1]), (qm[2][0], qm[2][1])],
        road,
    )
    overlap = _dedupe_ring(overlap)
    if len(overlap) < 3 or abs(_ring_area2(overlap)) < _AREA_EPS_DEG2:
        return []
    samples: List[Tuple[float, float, float]] = []
    for x, y in overlap:
        gz = plane_z(x, y, qm)
        rz = plane_z(x, y, road)
        if gz is None or rz is None:
            return []
        samples.append((x, y, gz - rz))
    above = clip_above(samples, float(min_protrusion))
    if len(above) < 3 or abs(_ring_area2(above)) < _AREA_EPS_DEG2:
        return []
    return [above]


def _cell_deg(lat: float, meters: float = 25.0) -> float:
    dlat = meters / 111_320.0
    dlon = meters / (111_320.0 * max(math.cos(math.radians(lat)), 1e-3))
    return max(dlat, dlon, 1e-8)


class RoadGrid:
    """Uniform grid of road triangles for overlap queries."""

    def __init__(self, triangles: Sequence[Triangle], cell: Optional[float] = None):
        self.triangles: List[Triangle] = list(triangles)
        self.min_z: List[float] = []
        self.overflow: List[int] = []
        if not self.triangles:
            self.cell = 1e-4
            self.grid = {}
            return
        lat = self.triangles[0][0][1]
        self.cell = float(cell) if cell else _cell_deg(lat, 25.0)
        self.grid = {}
        for i, tri in enumerate(self.triangles):
            zs = (tri[0][2], tri[1][2], tri[2][2])
            self.min_z.append(min(zs))
            self._insert(i, tri)

    def _insert(self, index: int, tri: Triangle) -> None:
        xs = (tri[0][0], tri[1][0], tri[2][0])
        ys = (tri[0][1], tri[1][1], tri[2][1])
        cell = self.cell
        ix0 = int(math.floor(min(xs) / cell))
        ix1 = int(math.floor(max(xs) / cell))
        iy0 = int(math.floor(min(ys) / cell))
        iy1 = int(math.floor(max(ys) / cell))
        # A huge triangle is tested directly instead of filling millions of cells.
        if ix1 - ix0 > 400 or iy1 - iy0 > 400:
            self.overflow.append(index)
            return
        grid = self.grid
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                grid.setdefault((ix, iy), []).append(index)

    def _cells_for_rect(self, west: float, south: float, east: float, north: float):
        cell = self.cell
        ix0 = int(math.floor(west / cell))
        ix1 = int(math.floor(east / cell))
        iy0 = int(math.floor(south / cell))
        iy1 = int(math.floor(north / cell))
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                yield ix, iy

    def triangles_in_rect(
        self, west: float, south: float, east: float, north: float
    ) -> List[Triangle]:
        seen = set()
        out: List[Triangle] = []
        grid = self.grid
        tris = self.triangles
        for key in self._cells_for_rect(west, south, east, north):
            for i in grid.get(key, ()):
                if i in seen:
                    continue
                seen.add(i)
                tri = tris[i]
                xs = (tri[0][0], tri[1][0], tri[2][0])
                ys = (tri[0][1], tri[1][1], tri[2][1])
                if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
                    continue
                out.append(tri)
        for i in self.overflow:
            if i in seen:
                continue
            tri = tris[i]
            xs = (tri[0][0], tri[1][0], tri[2][0])
            ys = (tri[0][1], tri[1][1], tri[2][1])
            if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
                continue
            seen.add(i)
            out.append(tri)
        return out

    def candidates(self, tri: Triangle) -> List[int]:
        xs = (tri[0][0], tri[1][0], tri[2][0])
        ys = (tri[0][1], tri[1][1], tri[2][1])
        seen = set()
        out: List[int] = []
        grid = self.grid
        for key in self._cells_for_rect(min(xs), min(ys), max(xs), max(ys)):
            for i in grid.get(key, ()):
                if i in seen:
                    continue
                seen.add(i)
                out.append(i)
        qxs = (tri[0][0], tri[1][0], tri[2][0])
        qys = (tri[0][1], tri[1][1], tri[2][1])
        qwest, qeast = min(qxs), max(qxs)
        qsouth, qnorth = min(qys), max(qys)
        tris = self.triangles
        for i in self.overflow:
            if i in seen:
                continue
            other = tris[i]
            xs = (other[0][0], other[1][0], other[2][0])
            ys = (other[0][1], other[1][1], other[2][1])
            if max(xs) < qwest or min(xs) > qeast or max(ys) < qsouth or min(ys) > qnorth:
                continue
            seen.add(i)
            out.append(i)
        return out


def tile_bump_rings(
    lons: Sequence[float],
    lats: Sequence[float],
    alts: Sequence[float],
    qm_triangles: Sequence[Tuple[int, int, int]],
    road_tris: Sequence[Triangle],
    min_protrusion: float,
    min_piece_m2: float = 0.0,
) -> List[List[XY]]:
    """Bump rings for one quantized-mesh tile against a local set of road triangles."""
    if not road_tris or not qm_triangles:
        return []
    grid = RoadGrid(road_tris)
    rings: List[List[XY]] = []
    min_piece = max(0.0, float(min_piece_m2))
    protrusion = float(min_protrusion)
    for i0, i1, i2 in qm_triangles:
        qm = (
            (float(lons[i0]), float(lats[i0]), float(alts[i0])),
            (float(lons[i1]), float(lats[i1]), float(alts[i1])),
            (float(lons[i2]), float(lats[i2]), float(alts[i2])),
        )
        qm_max = max(qm[0][2], qm[1][2], qm[2][2])
        for ri in grid.candidates(qm):
            if qm_max <= grid.min_z[ri] + protrusion:
                continue
            for ring in bump_rings(qm, grid.triangles[ri], protrusion):
                if min_piece > 0.0 and ring_area_m2(ring) < min_piece:
                    continue
                rings.append(ring)
    return rings


def process_tile_task(task: Tuple) -> Tuple[List[List[XY]], Optional[str]]:
    """
    Worker entry: load one .terrain tile and return bump rings.

    ``task`` is (path, level, x, y, road_triangles, min_protrusion, min_piece_m2).
    Failures return ``([], message)`` so one bad tile does not stop the run.
    """
    path, level, x, y, road_tris, min_protrusion, min_piece_m2 = task
    try:
        from pathlib import Path

        from quantized_mesh import load_tile

        tile = load_tile(Path(path), int(level), int(x), int(y))
        rings = tile_bump_rings(
            tile.lons,
            tile.lats,
            tile.altitudes,
            tile.triangles,
            road_tris,
            float(min_protrusion),
            float(min_piece_m2),
        )
        return rings, None
    except Exception as exc:
        return [], f"{path}: {exc}"


def self_test() -> None:
    """Geometry checks that do not need QGIS."""
    flat_ground = ((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0))
    road_below = ((0.0, 0.0, -1.0), (2.0, 0.0, -1.0), (0.0, 2.0, -1.0))
    rings = bump_rings(flat_ground, road_below, 0.0)
    assert len(rings) == 1, rings
    assert abs(ring_area_m2(rings[0]) - ring_area_m2([(0, 0), (2, 0), (0, 2)])) < 1.0

    road_above = ((0.0, 0.0, 5.0), (2.0, 0.0, 5.0), (0.0, 2.0, 5.0))
    assert bump_rings(flat_ground, road_above, 0.0) == []

    # One QM vertex at z=4, road plane at z=1. Crossing is a straight cut.
    qm = ((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (1.0, 2.0, 4.0))
    road = ((0.0, 0.0, 1.0), (2.0, 0.0, 1.0), (1.0, 2.0, 1.0))
    rings = bump_rings(qm, road, 0.0)
    assert len(rings) == 1, rings
    area = abs(_ring_area2(rings[0])) * 0.5
    assert abs(area - 1.125) < 1e-6, area
    assert any(abs(p[0] - 1.0) < 1e-6 and abs(p[1] - 2.0) < 1e-6 for p in rings[0])
    assert not any(abs(p[0]) < 1e-6 and abs(p[1]) < 1e-6 for p in rings[0])

    # Only the road triangle's footprint is returned when the mesh covers more.
    qm_wide = ((0.0, 0.0, 5.0), (4.0, 0.0, 5.0), (0.0, 4.0, 5.0))
    road_small = ((0.5, 0.5, 1.0), (1.5, 0.5, 1.0), (0.5, 1.5, 1.0))
    rings = bump_rings(qm_wide, road_small, 0.0)
    assert len(rings) == 1
    area = abs(_ring_area2(rings[0])) * 0.5
    assert abs(area - 0.5) < 1e-6, area

    # Coplanar road is not a bump.
    coplanar = ((0.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 2.0, 0.0))
    assert bump_rings(coplanar, coplanar, 0.0) == []

    # No XY overlap.
    other = ((10.0, 10.0, -5.0), (12.0, 10.0, -5.0), (10.0, 12.0, -5.0))
    assert bump_rings(flat_ground, other, 0.0) == []

    rings = tile_bump_rings(
        [0.0, 2.0, 0.0],
        [0.0, 0.0, 2.0],
        [0.0, 0.0, 0.0],
        [(0, 1, 2)],
        [road_below],
        0.0,
        0.0,
    )
    assert len(rings) == 1, rings

    print("road_bump_core self_test ok")


if __name__ == "__main__":
    self_test()
