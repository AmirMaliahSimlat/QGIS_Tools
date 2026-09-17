# -*- coding: utf-8 -*-
"""
Burn Unreal road-triangle Z into quantized-mesh vertices.

For each terrain vertex inside the burn region, take the minimum road-plane
height among dense samples within ~half the local vertex spacing, then apply
offset down and never raise terrain (only lower when needed).
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

Triangle = Tuple[
    Tuple[float, float, float],
    Tuple[float, float, float],
    Tuple[float, float, float],
]
Sample = Tuple[float, float, float]  # lon, lat, z


def point_in_triangle_2d(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    cx: float,
    cy: float,
    *,
    eps: float = 1e-12,
) -> Optional[Tuple[float, float, float]]:
    v0x, v0y = cx - ax, cy - ay
    v1x, v1y = bx - ax, by - ay
    v2x, v2y = px - ax, py - ay
    dot00 = v0x * v0x + v0y * v0y
    dot01 = v0x * v1x + v0y * v1y
    dot02 = v0x * v2x + v0y * v2y
    dot11 = v1x * v1x + v1y * v1y
    dot12 = v1x * v2x + v1y * v2y
    denom = dot00 * dot11 - dot01 * dot01
    if abs(denom) < eps:
        return None
    inv = 1.0 / denom
    u = (dot11 * dot02 - dot01 * dot12) * inv
    v = (dot00 * dot12 - dot01 * dot02) * inv
    w = 1.0 - u - v
    if u < -eps or v < -eps or w < -eps:
        return None
    return w, v, u


def plane_z_at(px: float, py: float, tri: Triangle) -> Optional[float]:
    a, b, c = tri
    weights = point_in_triangle_2d(
        px, py, a[0], a[1], b[0], b[1], c[0], c[1]
    )
    if weights is None:
        return None
    wa, wb, wc = weights
    return wa * a[2] + wb * b[2] + wc * c[2]


def fan_triangles_from_ring(
    ring_xyz: Sequence[Tuple[float, float, float]],
) -> List[Triangle]:
    pts: List[Tuple[float, float, float]] = []
    for p in ring_xyz:
        if pts and abs(pts[0][0] - p[0]) < 1e-12 and abs(pts[0][1] - p[1]) < 1e-12:
            continue
        if pts and abs(pts[-1][0] - p[0]) < 1e-12 and abs(pts[-1][1] - p[1]) < 1e-12:
            continue
        pts.append((float(p[0]), float(p[1]), float(p[2])))
    if len(pts) >= 2 and abs(pts[0][0] - pts[-1][0]) < 1e-12 and abs(
        pts[0][1] - pts[-1][1]
    ) < 1e-12:
        pts = pts[:-1]
    if len(pts) < 3:
        return []
    if len(pts) == 3:
        return [(pts[0], pts[1], pts[2])]
    out: List[Triangle] = []
    a = pts[0]
    for i in range(1, len(pts) - 1):
        out.append((a, pts[i], pts[i + 1]))
    return out


def _meters_to_deg(lat: float, spacing_m: float) -> Tuple[float, float]:
    lat_rad = math.radians(lat)
    dlat = spacing_m / 111_320.0
    cos_lat = max(math.cos(lat_rad), 1e-6)
    dlon = spacing_m / (111_320.0 * cos_lat)
    return dlon, dlat


def dense_samples_on_triangle(
    tri: Triangle,
    spacing_m: float,
) -> List[Sample]:
    """
    Sample the triangle plane on a barycentric grid (~spacing_m).

    Always includes the three vertices.
    """
    a, b, c = tri
    samples: List[Sample] = [
        (a[0], a[1], a[2]),
        (b[0], b[1], b[2]),
        (c[0], c[1], c[2]),
    ]
    if spacing_m <= 0:
        return samples

    lat_ref = (a[1] + b[1] + c[1]) / 3.0
    dlon, dlat = _meters_to_deg(lat_ref, spacing_m)
    # Edge lengths in deg (approx)
    def edge_len(p, q):
        return math.hypot((p[0] - q[0]) / dlon, (p[1] - q[1]) / dlat)

    max_edge = max(edge_len(a, b), edge_len(b, c), edge_len(c, a))
    steps = max(1, int(math.ceil(max_edge)))
    for i in range(steps + 1):
        for j in range(steps + 1 - i):
            # skip pure vertices already added
            if (i, j) in ((0, 0), (steps, 0), (0, steps)):
                continue
            k = steps - i - j
            w_a = i / steps
            w_b = j / steps
            w_c = k / steps
            lon = w_a * a[0] + w_b * b[0] + w_c * c[0]
            lat = w_a * a[1] + w_b * b[1] + w_c * c[1]
            z = w_a * a[2] + w_b * b[2] + w_c * c[2]
            samples.append((lon, lat, z))
    return samples


def dense_samples_on_triangles(
    triangles: Sequence[Triangle],
    spacing_m: float,
) -> List[Sample]:
    out: List[Sample] = []
    for tri in triangles:
        out.extend(dense_samples_on_triangle(tri, spacing_m))
    return out


def _build_vertex_grid(
    lons: Sequence[float],
    lats: Sequence[float],
    cell: float,
) -> Tuple[Dict[Tuple[int, int], List[int]], float]:
    cell = max(float(cell), 1e-12)
    grid: Dict[Tuple[int, int], List[int]] = {}
    for i, (lon, lat) in enumerate(zip(lons, lats)):
        key = (int(math.floor(lon / cell)), int(math.floor(lat / cell)))
        grid.setdefault(key, []).append(i)
    return grid, cell


def _nearest_vertex(
    lon: float,
    lat: float,
    lons: Sequence[float],
    lats: Sequence[float],
    eligible: Sequence[bool],
    grid: Dict[Tuple[int, int], List[int]],
    cell: float,
) -> Optional[int]:
    gx = int(math.floor(lon / cell))
    gy = int(math.floor(lat / cell))
    best_i = None
    best_d = None
    for dx in (-1, 0, 1):
        for dy in (-1, 0, 1):
            for i in grid.get((gx + dx, gy + dy), ()):
                if not eligible[i]:
                    continue
                d = (lons[i] - lon) ** 2 + (lats[i] - lat) ** 2
                if best_d is None or d < best_d:
                    best_d = d
                    best_i = i
    if best_i is not None:
        return best_i
    # Fallback: scan all eligible (rare: empty hash neighborhood)
    for i, ok in enumerate(eligible):
        if not ok:
            continue
        d = (lons[i] - lon) ** 2 + (lats[i] - lat) ** 2
        if best_d is None or d < best_d:
            best_d = d
            best_i = i
    return best_i


def _nearest_vertex_any(
    lon: float,
    lat: float,
    lons: Sequence[float],
    lats: Sequence[float],
    grid: Dict[Tuple[int, int], List[int]],
    cell: float,
    *,
    skip: Optional[int] = None,
) -> Optional[int]:
    gx = int(math.floor(lon / cell))
    gy = int(math.floor(lat / cell))
    best_i = None
    best_d = None
    for dx in (-1, 0, 1, -2, 2):
        for dy in (-1, 0, 1, -2, 2):
            for i in grid.get((gx + dx, gy + dy), ()):
                if skip is not None and i == skip:
                    continue
                d = (lons[i] - lon) ** 2 + (lats[i] - lat) ** 2
                if best_d is None or d < best_d:
                    best_d = d
                    best_i = i
    if best_i is not None:
        return best_i
    for i in range(len(lons)):
        if skip is not None and i == skip:
            continue
        d = (lons[i] - lon) ** 2 + (lats[i] - lat) ** 2
        if best_d is None or d < best_d:
            best_d = d
            best_i = i
    return best_i


def local_half_spacing_deg(
    vert_lons: Sequence[float],
    vert_lats: Sequence[float],
    *,
    grid_cell_deg: float,
    fallback_deg: float,
) -> List[float]:
    """
    Per-vertex search radius ≈ half distance to the nearest other vertex.

    Caps how far road samples may influence a coarse QM vertex so a long
    downhill stretch cannot dump its min Z into a distant cell.
    """
    n = len(vert_lons)
    if n == 0:
        return []
    grid, cell = _build_vertex_grid(vert_lons, vert_lats, grid_cell_deg)
    radii: List[float] = []
    fb = max(float(fallback_deg), 1e-12)
    for i in range(n):
        j = _nearest_vertex_any(
            vert_lons[i],
            vert_lats[i],
            vert_lons,
            vert_lats,
            grid,
            cell,
            skip=i,
        )
        if j is None:
            radii.append(fb)
            continue
        dist = math.hypot(
            vert_lons[i] - vert_lons[j], vert_lats[i] - vert_lats[j]
        )
        radii.append(max(0.5 * dist, 1e-12))
    return radii


def aggregate_min_road_z_per_vertex(
    vert_lons: Sequence[float],
    vert_lats: Sequence[float],
    vert_in_burn: Sequence[bool],
    samples: Sequence[Sample],
    *,
    grid_cell_deg: float,
    search_radii_deg: Optional[Sequence[float]] = None,
) -> List[Optional[float]]:
    """
    For each burn-zone vertex, keep the minimum sample Z within its local radius.

    If ``search_radii_deg`` is omitted, falls back to nearest-burn-vertex
    ownership (legacy; can over-lower on coarse LODs).
    """
    n = len(vert_lons)
    mins: List[Optional[float]] = [None] * n
    if n == 0 or not samples:
        return mins
    if not any(vert_in_burn):
        return mins

    if search_radii_deg is not None:
        # Spatial hash of samples for radius queries
        cell = max(float(grid_cell_deg), 1e-12)
        sample_grid: Dict[Tuple[int, int], List[int]] = {}
        for si, (lon, lat, _z) in enumerate(samples):
            key = (int(math.floor(lon / cell)), int(math.floor(lat / cell)))
            sample_grid.setdefault(key, []).append(si)

        for i, ok in enumerate(vert_in_burn):
            if not ok:
                continue
            radius = float(search_radii_deg[i])
            r2 = radius * radius
            gx = int(math.floor(vert_lons[i] / cell))
            gy = int(math.floor(vert_lats[i] / cell))
            # cells to cover the radius
            span = max(1, int(math.ceil(radius / cell)) + 1)
            best: Optional[float] = None
            for dx in range(-span, span + 1):
                for dy in range(-span, span + 1):
                    for si in sample_grid.get((gx + dx, gy + dy), ()):
                        lon, lat, z = samples[si]
                        d2 = (lon - vert_lons[i]) ** 2 + (lat - vert_lats[i]) ** 2
                        if d2 > r2:
                            continue
                        if best is None or z < best:
                            best = z
            mins[i] = best
        return mins

    # Legacy nearest-burn-vertex ownership
    grid, cell = _build_vertex_grid(vert_lons, vert_lats, grid_cell_deg)
    for lon, lat, z in samples:
        idx = _nearest_vertex(
            lon, lat, vert_lons, vert_lats, vert_in_burn, grid, cell
        )
        if idx is None:
            continue
        cur = mins[idx]
        if cur is None or z < cur:
            mins[idx] = z
    return mins


def resolve_burn_altitudes(
    vert_lons: Sequence[float],
    vert_lats: Sequence[float],
    vert_alts: Sequence[float],
    vert_in_burn: Sequence[bool],
    sample_mins: Sequence[Optional[float]],
    triangles: Sequence[Triangle],
    *,
    offset_down: float,
    only_lower: bool = True,
) -> Tuple[List[float], int]:
    """
    Burn-zone verts → road Z (local min or plane) minus offset.

    When ``only_lower`` is True (default), never raise a vertex above its
    current altitude: ``new = min(old, road_z - offset)``.
    """
    out = list(vert_alts)
    changed = 0
    offset = float(offset_down)
    for i, in_burn in enumerate(vert_in_burn):
        if not in_burn:
            continue
        z = sample_mins[i]
        if z is None:
            for tri in triangles:
                z = plane_z_at(vert_lons[i], vert_lats[i], tri)
                if z is not None:
                    break
        if z is None:
            continue
        target = float(z) - offset
        new_z = min(float(out[i]), target) if only_lower else target
        if new_z != out[i]:
            out[i] = new_z
            changed += 1
    return out, changed
