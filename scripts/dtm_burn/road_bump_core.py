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
# Sample QM edges inside the road mask at this spacing (long edges can span
# tens of metres while bumps are ~1 m).
_EDGE_SAMPLE_M = 1.0
# Max XY snap when looking up road Z (metres). Keep tiny — a multi-metre
# search invents false curb bumps by borrowing road height from inward.
_ROAD_Z_SNAP_M = 0.15


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
    """Uniform grid of road triangles for overlap queries and Z sampling."""

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
        # A query covering hundreds of kilometres (or a bad triangle) must not
        # walk every empty cell. Occupied cells are the only ones that matter.
        if ix1 - ix0 > 400 or iy1 - iy0 > 400:
            for ix, iy in self.grid:
                if ix0 <= ix <= ix1 and iy0 <= iy <= iy1:
                    yield ix, iy
            return
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

    def road_z_at(self, x: float, y: float) -> Optional[float]:
        """Road-plane height at (lon, lat), or None if outside every road triangle."""
        cell = self.cell
        ix = int(math.floor(x / cell))
        iy = int(math.floor(y / cell))
        seen = set()
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for i in self.grid.get((ix + dx, iy + dy), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    z = _plane_z_inside(x, y, self.triangles[i])
                    if z is not None:
                        return z
        for i in self.overflow:
            if i in seen:
                continue
            z = _plane_z_inside(x, y, self.triangles[i])
            if z is not None:
                return z
        return None

    def road_z_near(
        self, x: float, y: float, max_m: float = 3.0
    ) -> Optional[float]:
        """
        Road height at (x, y): prefer a containing triangle, else the nearest
        road-plane sample within ``max_m`` (covers mask / mesh misalignment).
        """
        hit = self.road_z_at(x, y)
        if hit is not None:
            return hit
        lat = y
        mx = 111_320.0 * max(math.cos(math.radians(lat)), 1e-6)
        my = 111_320.0
        max_m2 = float(max_m) * float(max_m)
        best_d2 = max_m2
        best_z: Optional[float] = None
        cell = self.cell
        # Search a few cells around the point (~max_m in degrees).
        pad = max(1, int(math.ceil((max_m / min(mx, my)) / cell)) + 1)
        ix = int(math.floor(x / cell))
        iy = int(math.floor(y / cell))
        seen = set()
        for dx in range(-pad, pad + 1):
            for dy in range(-pad, pad + 1):
                for i in self.grid.get((ix + dx, iy + dy), ()):
                    if i in seen:
                        continue
                    seen.add(i)
                    tri = self.triangles[i]
                    cx = (tri[0][0] + tri[1][0] + tri[2][0]) / 3.0
                    cy = (tri[0][1] + tri[1][1] + tri[2][1]) / 3.0
                    dxm = (cx - x) * mx
                    dym = (cy - y) * my
                    d2 = dxm * dxm + dym * dym
                    if d2 > best_d2:
                        continue
                    z = plane_z(x, y, tri)
                    if z is None:
                        continue
                    best_d2 = d2
                    best_z = z
        for i in self.overflow:
            if i in seen:
                continue
            tri = self.triangles[i]
            cx = (tri[0][0] + tri[1][0] + tri[2][0]) / 3.0
            cy = (tri[0][1] + tri[1][1] + tri[2][1]) / 3.0
            dxm = (cx - x) * mx
            dym = (cy - y) * my
            d2 = dxm * dxm + dym * dym
            if d2 > best_d2:
                continue
            z = plane_z(x, y, tri)
            if z is None:
                continue
            best_d2 = d2
            best_z = z
        return best_z


def _plane_z_inside(px: float, py: float, tri: Triangle) -> Optional[float]:
    """Barycentric plane height only when (px, py) lies in the triangle."""
    ax, ay, az = tri[0]
    bx, by, bz = tri[1]
    cx, cy, cz = tri[2]
    v0x, v0y = cx - ax, cy - ay
    v1x, v1y = bx - ax, by - ay
    v2x, v2y = px - ax, py - ay
    dot00 = v0x * v0x + v0y * v0y
    dot01 = v0x * v1x + v0y * v1y
    dot02 = v0x * v2x + v0y * v2y
    dot11 = v1x * v1x + v1y * v1y
    dot12 = v1x * v2x + v1y * v2y
    denom = dot00 * dot11 - dot01 * dot01
    if abs(denom) < 1e-18:
        return None
    inv = 1.0 / denom
    u = (dot11 * dot02 - dot01 * dot12) * inv
    v = (dot00 * dot12 - dot01 * dot02) * inv
    w = 1.0 - u - v
    if u < -1e-12 or v < -1e-12 or w < -1e-12:
        return None
    return w * az + v * bz + u * cz


def _pip_ring(x: float, y: float, ring: Sequence[XY]) -> bool:
    """Ray-cast point-in-polygon for a closed or open exterior ring."""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / ((yj - yi) + 1e-30) + xi
        ):
            inside = not inside
        j = i
    return inside


class MaskIndex:
    """Spatial index of 2D rings for lon/lat point-in-polygon tests."""

    def __init__(self, rings: Sequence[Sequence[XY]], cell: Optional[float] = None):
        self.rings: List[List[XY]] = [list(r) for r in rings if len(r) >= 3]
        self.bboxes: List[Tuple[float, float, float, float]] = []
        self.grid = {}
        self._always: List[int] = []
        if not self.rings:
            self.cell = 1e-3
            return
        lat = self.rings[0][0][1]
        self.cell = float(cell) if cell else _cell_deg(lat, 75.0)
        for i, ring in enumerate(self.rings):
            xs = [p[0] for p in ring]
            ys = [p[1] for p in ring]
            west, east = min(xs), max(xs)
            south, north = min(ys), max(ys)
            self.bboxes.append((west, south, east, north))
            ix0 = int(math.floor(west / self.cell))
            ix1 = int(math.floor(east / self.cell))
            iy0 = int(math.floor(south / self.cell))
            iy1 = int(math.floor(north / self.cell))
            if ix1 - ix0 > 500 or iy1 - iy0 > 500:
                self._always.append(i)
                continue
            for ix in range(ix0, ix1 + 1):
                for iy in range(iy0, iy1 + 1):
                    self.grid.setdefault((ix, iy), []).append(i)

    def contains(self, x: float, y: float) -> bool:
        if not self.rings:
            return False
        cell = self.cell
        ix = int(math.floor(x / cell))
        iy = int(math.floor(y / cell))
        candidates = list(self.grid.get((ix, iy), ()))
        candidates.extend(self._always)
        seen = set()
        for i in candidates:
            if i in seen:
                continue
            seen.add(i)
            west, south, east, north = self.bboxes[i]
            if x < west or x > east or y < south or y > north:
                continue
            if _pip_ring(x, y, self.rings[i]):
                return True
        return False


def _mesh_adjacency(
    qm_triangles: Sequence[Tuple[int, int, int]], n_verts: int
) -> Tuple[List[set], List[List[int]]]:
    neighbors: List[set] = [set() for _ in range(n_verts)]
    incident: List[List[int]] = [[] for _ in range(n_verts)]
    for ti, (i0, i1, i2) in enumerate(qm_triangles):
        for a, b, c in ((i0, i1, i2), (i1, i2, i0), (i2, i0, i1)):
            if a < 0 or a >= n_verts:
                continue
            neighbors[a].add(b)
            neighbors[a].add(c)
            incident[a].append(ti)
    return neighbors, incident


def rings_overlapping_rect(
    rings: Sequence[Sequence[XY]],
    west: float,
    south: float,
    east: float,
    north: float,
    pad: float = 0.0,
) -> List[List[XY]]:
    """Keep mask rings whose bbox intersects the padded tile rectangle."""
    out: List[List[XY]] = []
    for ring in rings:
        if len(ring) < 3:
            continue
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        if (
            max(xs) < west - pad
            or min(xs) > east + pad
            or max(ys) < south - pad
            or min(ys) > north + pad
        ):
            continue
        out.append(list(ring))
    return out


def _edge_length_m(ax: float, ay: float, bx: float, by: float) -> float:
    lat = 0.5 * (ay + by)
    mx = 111_320.0 * max(math.cos(math.radians(lat)), 1e-6)
    my = 111_320.0
    dx = (bx - ax) * mx
    dy = (by - ay) * my
    return math.hypot(dx, dy)


def _samples_along_edge(
    ax: float,
    ay: float,
    az: float,
    bx: float,
    by: float,
    bz: float,
    step_m: float,
) -> List[Tuple[float, float, float]]:
    """
    Points along AB at about ``step_m`` spacing (endpoints omitted).

    Short edges still get one midpoint so a sub-step bump is not skipped.
    """
    length = _edge_length_m(ax, ay, bx, by)
    if length < 1e-9:
        return []
    step = max(float(step_m), 1e-3)
    out: List[Tuple[float, float, float]] = []
    if length <= step:
        t = 0.5
        out.append((ax + t * (bx - ax), ay + t * (by - ay), az + t * (bz - az)))
        return out
    n = int(math.floor(length / step))
    for i in range(1, n + 1):
        t = (i * step) / length
        if t >= 1.0 - 1e-12:
            break
        out.append((ax + t * (bx - ax), ay + t * (by - ay), az + t * (bz - az)))
    # Ensure the geometric midpoint is included when it is not on the 1 m grid.
    mid_t = 0.5
    mid_on_grid = any(abs(i * step / length - mid_t) < 1e-9 for i in range(1, n + 1))
    if not mid_on_grid:
        out.append(
            (
                ax + mid_t * (bx - ax),
                ay + mid_t * (by - ay),
                az + mid_t * (bz - az),
            )
        )
    return out


def tile_bump_rings(
    lons: Sequence[float],
    lats: Sequence[float],
    alts: Sequence[float],
    qm_triangles: Sequence[Tuple[int, int, int]],
    road_tris: Sequence[Triangle],
    min_protrusion: float,
    min_piece_m2: float = 0.0,
    mask_rings: Optional[Sequence[Sequence[XY]]] = None,
    edge_sample_m: float = _EDGE_SAMPLE_M,
    sample_edges: bool = True,
    contour_exact: bool = True,
) -> List[List[XY]]:
    """
    Bump rings for one QM tile.

    When ``contour_exact`` is True (default), every QM triangle that overlaps
    a road triangle is clipped to the (ground - road) >= min_protrusion
    region. That preserves the height-contour meaning of each ring.

    When False, only triangles near protruding samples are clipped (faster,
    incomplete contour).
    """
    if not road_tris or not qm_triangles:
        return []
    n = len(lons)
    if n == 0 or len(lats) != n or len(alts) != n:
        return []

    roads = RoadGrid(road_tris)
    if roads.min_z:
        try:
            tile_max = max(float(a) for a in alts)
        except (TypeError, ValueError):
            tile_max = None
        if tile_max is not None and tile_max <= min(roads.min_z) + float(min_protrusion):
            return []

    protrusion = float(min_protrusion)
    min_piece = max(0.0, float(min_piece_m2))

    if contour_exact:
        return _contour_rings_all_triangles(
            lons, lats, alts, qm_triangles, roads, protrusion, min_piece
        )

    return _seeded_bump_rings(
        lons,
        lats,
        alts,
        qm_triangles,
        roads,
        protrusion,
        min_piece,
        mask_rings,
        edge_sample_m,
        sample_edges,
    )


def _contour_rings_all_triangles(
    lons: Sequence[float],
    lats: Sequence[float],
    alts: Sequence[float],
    qm_triangles: Sequence[Tuple[int, int, int]],
    roads: RoadGrid,
    protrusion: float,
    min_piece: float,
) -> List[List[XY]]:
    """Exact (ground-road)>=eps clips for every overlapping QM x road pair."""
    rings: List[List[XY]] = []
    n = len(lons)
    for i0, i1, i2 in qm_triangles:
        if not (0 <= i0 < n and 0 <= i1 < n and 0 <= i2 < n):
            continue
        try:
            qm = (
                (float(lons[i0]), float(lats[i0]), float(alts[i0])),
                (float(lons[i1]), float(lats[i1]), float(alts[i1])),
                (float(lons[i2]), float(lats[i2]), float(alts[i2])),
            )
        except (TypeError, ValueError):
            continue
        qm_max = max(qm[0][2], qm[1][2], qm[2][2])
        for ri in roads.candidates(qm):
            if qm_max <= roads.min_z[ri] + protrusion:
                continue
            for ring in bump_rings(qm, roads.triangles[ri], protrusion):
                if min_piece > 0.0 and ring_area_m2(ring) < min_piece:
                    continue
                rings.append(ring)
    return rings


def _seeded_bump_rings(
    lons: Sequence[float],
    lats: Sequence[float],
    alts: Sequence[float],
    qm_triangles: Sequence[Tuple[int, int, int]],
    roads: RoadGrid,
    protrusion: float,
    min_piece: float,
    mask_rings: Optional[Sequence[Sequence[XY]]],
    edge_sample_m: float,
    sample_edges: bool,
) -> List[List[XY]]:
    """Legacy: seed protruding samples, then exact-clip only nearby triangles."""
    n = len(lons)
    mask = MaskIndex(mask_rings) if mask_rings else None
    neighbors, incident = _mesh_adjacency(qm_triangles, n)
    sample_m = max(float(edge_sample_m), 1e-3)

    def _sample_z(x: float, y: float) -> Optional[float]:
        z = roads.road_z_at(x, y)
        if z is not None:
            return z
        return roads.road_z_near(x, y, max_m=_ROAD_Z_SNAP_M)

    def _on_road(x: float, y: float) -> bool:
        if _sample_z(x, y) is None:
            return False
        if mask is not None:
            return mask.contains(x, y)
        return True

    def _ground_at_vertex(i: int) -> Optional[float]:
        try:
            return float(alts[i])
        except (TypeError, ValueError):
            return None

    on_mask: List[int] = []
    for i in range(n):
        if _on_road(float(lons[i]), float(lats[i])):
            on_mask.append(i)
    if not on_mask and mask is not None:
        if not sample_edges:
            return []
    elif not on_mask:
        return []

    flagged_verts: set = set()
    flagged_tris: set = set()

    def _flag_sample(
        x: float, y: float, gz: Optional[float], seed_verts: Sequence[int], tri: Optional[int]
    ) -> None:
        if gz is None:
            return
        if not _on_road(x, y):
            return
        rz = _sample_z(x, y)
        if rz is None:
            return
        if gz > rz + protrusion + _HEIGHT_EPS_M:
            for vi in seed_verts:
                if 0 <= vi < n:
                    flagged_verts.add(vi)
            if tri is not None:
                flagged_tris.add(tri)

    check = set(on_mask)
    for i in on_mask:
        for j in neighbors[i]:
            if 0 <= j < n and _on_road(float(lons[j]), float(lats[j])):
                check.add(j)
    for i in check:
        _flag_sample(
            float(lons[i]),
            float(lats[i]),
            _ground_at_vertex(i),
            (i,),
            None,
        )

    if sample_edges:
        seen_edge = set()
        for ti, (i0, i1, i2) in enumerate(qm_triangles):
            verts = (i0, i1, i2)
            cx = (float(lons[i0]) + float(lons[i1]) + float(lons[i2])) / 3.0
            cy = (float(lats[i0]) + float(lats[i1]) + float(lats[i2])) / 3.0
            if _on_road(cx, cy):
                g0 = _ground_at_vertex(i0)
                g1 = _ground_at_vertex(i1)
                g2 = _ground_at_vertex(i2)
                if g0 is not None and g1 is not None and g2 is not None:
                    _flag_sample(cx, cy, (g0 + g1 + g2) / 3.0, verts, ti)
            for a, b in ((i0, i1), (i1, i2), (i2, i0)):
                e = (a, b) if a < b else (b, a)
                if e in seen_edge:
                    continue
                seen_edge.add(e)
                ga = _ground_at_vertex(a)
                gb = _ground_at_vertex(b)
                if ga is None or gb is None:
                    continue
                ax, ay = float(lons[a]), float(lats[a])
                bx, by = float(lons[b]), float(lats[b])
                for sx, sy, sz in _samples_along_edge(
                    ax, ay, ga, bx, by, gb, sample_m
                ):
                    _flag_sample(sx, sy, sz, (a, b), ti)

    if not flagged_verts and not flagged_tris:
        return []

    tri_ids = set(flagged_tris)
    for i in flagged_verts:
        tri_ids.update(incident[i])

    rings: List[List[XY]] = []
    for ti in tri_ids:
        i0, i1, i2 = qm_triangles[ti]
        qm = (
            (float(lons[i0]), float(lats[i0]), float(alts[i0])),
            (float(lons[i1]), float(lats[i1]), float(alts[i1])),
            (float(lons[i2]), float(lats[i2]), float(alts[i2])),
        )
        qm_max = max(qm[0][2], qm[1][2], qm[2][2])
        for ri in roads.candidates(qm):
            if qm_max <= roads.min_z[ri] + protrusion:
                continue
            for ring in bump_rings(qm, roads.triangles[ri], protrusion):
                if min_piece > 0.0 and ring_area_m2(ring) < min_piece:
                    continue
                rings.append(ring)
    return rings


def process_tile_task(task: Tuple) -> Tuple[List[List[XY]], Optional[str]]:
    """
    Worker entry: load one .terrain tile and return bump rings.

    ``task`` is
    (path, level, x, y, road_triangles, mask_rings, min_protrusion,
     min_piece_m2, sample_edges[, contour_exact]).
    Failures return ``([], message)`` so one bad tile does not stop the run.
    """
    path = task[0]
    level = task[1]
    x = task[2]
    y = task[3]
    road_tris = task[4]
    mask_rings = task[5]
    min_protrusion = task[6]
    min_piece_m2 = task[7]
    sample_edges = task[8]
    contour_exact = bool(task[9]) if len(task) > 9 else True
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
            mask_rings=mask_rings,
            sample_edges=bool(sample_edges),
            contour_exact=contour_exact,
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

    mask = [[(-0.1, -0.1), (2.1, -0.1), (2.1, 2.1), (-0.1, 2.1)]]
    rings = tile_bump_rings(
        [0.0, 2.0, 0.0],
        [0.0, 0.0, 2.0],
        [0.0, 0.0, 0.0],
        [(0, 1, 2)],
        [road_below],
        0.0,
        0.0,
        mask_rings=mask,
    )
    assert len(rings) == 1, rings

    # Peak vertex on mask with road below → flagged.
    rings = tile_bump_rings(
        [0.0, 2.0, 1.0],
        [0.0, 0.0, 2.0],
        [0.0, 0.0, 4.0],
        [(0, 1, 2)],
        [road],
        0.0,
        0.0,
        mask_rings=mask,
    )
    assert len(rings) == 1, rings

    rings = tile_bump_rings(
        [0.0, 2.0, 0.0],
        [0.0, 0.0, 2.0],
        [0.0, 0.0, 0.0],
        [(0, 1, 2)],
        [road_above],
        0.0,
        0.0,
        mask_rings=mask,
    )
    assert rings == []

    assert MaskIndex(mask).contains(1.0, 1.0)
    assert not MaskIndex(mask).contains(10.0, 10.0)

    # Point beside the road (mask-wide) must not borrow road Z from metres away.
    grid = RoadGrid([road])
    assert grid.road_z_at(5.0, 1.0) is None
    assert grid.road_z_near(5.0, 1.0, max_m=0.15) is None

    # ~10 m edge → about one sample per metre (endpoints omitted).
    bx = 10.0 / 111_320.0
    spaced = _samples_along_edge(0.0, 0.0, 0.0, bx, 0.0, 0.0, 1.0)
    assert 9 <= len(spaced) <= 11, len(spaced)

    # Verts-only mode skips edge densify (still finds vertex peaks).
    rings = tile_bump_rings(
        [0.0, 2.0, 1.0],
        [0.0, 0.0, 2.0],
        [0.0, 0.0, 4.0],
        [(0, 1, 2)],
        [road],
        0.0,
        0.0,
        mask_rings=mask,
        sample_edges=False,
    )
    assert len(rings) == 1, rings

    # Mesh face with every vertex outside the road still bumps where its plane
    # is above the road triangle it covers.
    rings = tile_bump_rings(
        [0.0, 4.0, 0.0],
        [0.0, 0.0, 4.0],
        [5.0, 5.0, 5.0],
        [(0, 1, 2)],
        [road_small],
        0.0,
        0.0,
    )
    assert len(rings) == 1, rings

    # High ground outside the 3D road XY (but inside a huge mask) must not flag.
    wide_mask = [[(-1.0, -1.0), (20.0, -1.0), (20.0, 20.0), (-1.0, 20.0)]]
    rings = tile_bump_rings(
        [5.0, 6.0, 5.5],
        [5.0, 5.0, 6.0],
        [10.0, 10.0, 10.0],
        [(0, 1, 2)],
        [road],
        0.0,
        0.0,
        mask_rings=wide_mask,
        sample_edges=True,
    )
    assert rings == [], rings

    print("road_bump_core self_test ok")


if __name__ == "__main__":
    self_test()
