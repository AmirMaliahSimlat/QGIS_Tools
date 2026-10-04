# -*- coding: utf-8 -*-
"""
Road surface triangulation matching the Unreal RoadPlacer plugin.

XY only: 2D Delaunay of outline points in local meters, then keep a triangle
when its centroid lies inside the road mask. Height is not used to choose
edges. Max edge length 0 means no length cap, same as the plugin.
"""

from __future__ import annotations

import math
from typing import Iterable, List, Optional, Sequence, Tuple

XY = Tuple[float, float]
Tri = Tuple[int, int, int]

# Same constants as RoadTriangulate.cpp / RoadPlacerBPLibrary.cpp.
_METERS_LAT = 110540.0
_OUTLINE_SNAP_M = 15.0
_TILE_PAD_M = 80.0
_QUANT = 1.0e7


def quant_key(lon: float, lat: float) -> Tuple[int, int]:
    """Plugin dedup key: round lon/lat to 1e-7 degrees."""
    return (int(round(lon * _QUANT)), int(round(lat * _QUANT)))


def _meters_lon(lat: float) -> float:
    return 111320.0 * max(math.cos(math.radians(lat)), 0.05)


def dedupe_points(points: Sequence[XY]) -> List[XY]:
    seen = set()
    out: List[XY] = []
    for lon, lat in points:
        key = quant_key(lon, lat)
        if key in seen:
            continue
        seen.add(key)
        out.append((float(lon), float(lat)))
    return out


def _pip_ring(x: float, y: float, ring: Sequence[XY]) -> bool:
    """Even-odd test in lon/lat, matching RoadTriangulate::PointInRing."""
    n = len(ring)
    if n < 3:
        return False
    inside = False
    j = n - 1
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[j]
        if ((ay > y) != (by > y)) and (
            x < (bx - ax) * (y - ay) / (by - ay + 1.0e-30) + ax
        ):
            inside = not inside
        j = i
    return inside


def point_in_mask(lon: float, lat: float, outers: Sequence[Sequence[XY]], holes: Sequence[Sequence[Sequence[XY]]]) -> bool:
    for outer, hole_rings in zip(outers, holes):
        xs = [p[0] for p in outer]
        ys = [p[1] for p in outer]
        if lon < min(xs) or lon > max(xs) or lat < min(ys) or lat > max(ys):
            continue
        if not _pip_ring(lon, lat, outer):
            continue
        if any(_pip_ring(lon, lat, hole) for hole in hole_rings):
            continue
        return True
    return False


def build_tin(
    points: Sequence[XY],
    outers: Sequence[Sequence[XY]],
    holes: Sequence[Sequence[Sequence[XY]]],
    max_edge_m: float = 0.0,
    centroid_inside=None,
) -> List[Tuple[XY, XY, XY]]:
    """
    Delaunay triangles whose centroid is inside the mask.

    ``max_edge_m`` <= 0 disables the edge-length cap (plugin value 0).
    """
    from scipy.spatial import Delaunay

    pts = dedupe_points(points)
    if len(pts) < 3:
        return []
    origin_lon = sum(p[0] for p in pts) / len(pts)
    origin_lat = sum(p[1] for p in pts) / len(pts)
    mx = _meters_lon(origin_lat)
    xy = [((lon - origin_lon) * mx, (lat - origin_lat) * _METERS_LAT) for lon, lat in pts]
    tin = Delaunay(xy)
    kept: List[Tuple[XY, XY, XY]] = []
    cap = float(max_edge_m)
    for i0, i1, i2 in tin.simplices:
        a, b, c = pts[int(i0)], pts[int(i1)], pts[int(i2)]
        if cap > 0.0:
            if (
                _edge_m(a, b) > cap
                or _edge_m(b, c) > cap
                or _edge_m(c, a) > cap
            ):
                continue
        clon = (a[0] + b[0] + c[0]) / 3.0
        clat = (a[1] + b[1] + c[1]) / 3.0
        inside = (
            centroid_inside(clon, clat)
            if centroid_inside is not None
            else point_in_mask(clon, clat, outers, holes)
        )
        if inside:
            kept.append((a, b, c))
    return kept


def _edge_m(a: XY, b: XY) -> float:
    mid = 0.5 * (a[1] + b[1])
    dx = (a[0] - b[0]) * _meters_lon(mid)
    dy = (a[1] - b[1]) * _METERS_LAT
    return math.hypot(dx, dy)


def tri_key(tri: Sequence[XY]) -> Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int]]:
    return tuple(sorted(quant_key(p[0], p[1]) for p in tri))  # type: ignore[return-value]
