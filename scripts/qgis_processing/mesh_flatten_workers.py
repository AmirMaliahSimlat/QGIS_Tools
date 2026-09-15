# -*- coding: utf-8 -*-
"""
Picklable road-surface snapshot + tile patch worker for flatten_road_mesh.

No QGIS imports — safe for ProcessPoolExecutor on Windows.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# Imported inside worker after scripts_root is on sys.path.
# Top-level imports of quantized_mesh work when scripts/ is on path.


def lonlat_to_utm(lon: float, lat: float, zone: int, northern: bool = True):
    """WGS84 geographic → UTM meters (WGS84 ellipsoid)."""
    # Krüger series — adequate for local road masks.
    a = 6378137.0
    f = 1 / 298.257223563
    e2 = f * (2 - f)
    ep2 = e2 / (1 - e2)
    n = f / (2 - f)
    # false easting / northing
    k0 = 0.9996
    lon0 = math.radians((zone - 1) * 6 - 180 + 3)
    lat_r = math.radians(lat)
    lon_r = math.radians(lon)
    sin_lat = math.sin(lat_r)
    cos_lat = math.cos(lat_r)
    tan_lat = math.tan(lat_r)
    N = a / math.sqrt(1 - e2 * sin_lat * sin_lat)
    T = tan_lat * tan_lat
    C = ep2 * cos_lat * cos_lat
    A = cos_lat * (lon_r - lon0)
    M = a * (
        (1 - e2 / 4 - 3 * e2 * e2 / 64 - 5 * e2 ** 3 / 256) * lat_r
        - (3 * e2 / 8 + 3 * e2 * e2 / 32 + 45 * e2 ** 3 / 1024)
        * math.sin(2 * lat_r)
        + (15 * e2 * e2 / 256 + 45 * e2 ** 3 / 1024) * math.sin(4 * lat_r)
        - (35 * e2 ** 3 / 3072) * math.sin(6 * lat_r)
    )
    easting = (
        k0
        * N
        * (
            A
            + (1 - T + C) * A ** 3 / 6
            + (5 - 18 * T + T * T + 72 * C - 58 * ep2) * A ** 5 / 120
        )
        + 500000.0
    )
    northing = k0 * (
        M
        + N
        * tan_lat
        * (
            A * A / 2
            + (5 - T + 9 * C + 4 * C * C) * A ** 4 / 24
            + (61 - 58 * T + T * T + 600 * C - 330 * ep2) * A ** 6 / 720
        )
    )
    if not northern:
        northing += 10000000.0
    return easting, northing


def _point_in_rings(x: float, y: float, rings) -> bool:
    inside = False
    for ring in rings:
        n = len(ring)
        if n < 3:
            continue
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if ((yi > y) != (yj > y)) and (
                x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi
            ):
                inside = not inside
            j = i
    return inside


def _point_segment_dist2(px, py, ax, ay, bx, by):
    abx = bx - ax
    aby = by - ay
    apx = px - ax
    apy = py - ay
    ab2 = abx * abx + aby * aby
    if ab2 <= 1e-18:
        dx = px - ax
        dy = py - ay
        return dx * dx + dy * dy
    t = max(0.0, min(1.0, (apx * abx + apy * aby) / ab2))
    dx = px - (ax + t * abx)
    dy = py - (ay + t * aby)
    return dx * dx + dy * dy


def _min_dist_to_rings(x, y, rings) -> float:
    best = None
    for ring in rings:
        n = len(ring)
        if n < 2:
            continue
        limit = n - 1 if ring[0] == ring[-1] else n
        for i in range(limit):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            d2 = _point_segment_dist2(x, y, ax, ay, bx, by)
            if best is None or d2 < best:
                best = d2
    if best is None:
        return 0.0
    return math.sqrt(best)


def _barycentric(px, py, ax, ay, bx, by, cx, cy):
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if den == 0.0:
        return None
    w1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
    w2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
    w3 = 1.0 - w1 - w2
    eps = -1e-9
    if w1 < eps or w2 < eps or w3 < eps:
        return None
    return w1, w2, w3


def _barycentric_clamped(px, py, ax, ay, bx, by, cx, cy):
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if den == 0.0:
        return None
    w1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
    w2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
    w3 = 1.0 - w1 - w2
    # Clamp to triangle
    w1 = max(0.0, w1)
    w2 = max(0.0, w2)
    w3 = max(0.0, w3)
    s = w1 + w2 + w3
    if s <= 0.0:
        return None
    return w1 / s, w2 / s, w3 / s


class SurfaceSnapshot:
    """Pure-Python road surface for worker processes."""

    __slots__ = (
        "triangles",
        "mask_rings",
        "mask_metric_rings",
        "mask_bboxes",
        "extent",
        "interior_drop_m",
        "edge_blend_m",
        "smooth_blend",
        "lowering_only",
        "utm_zone",
        "utm_northern",
        "_tri_grid",
        "_tri_cell",
    )

    def __init__(self, data: Dict[str, Any]):
        self.triangles = data["triangles"]
        self.mask_rings = data["mask_rings"]  # fid -> rings lon/lat
        self.mask_metric_rings = data.get("mask_metric_rings") or {}
        self.mask_bboxes = data.get("mask_bboxes") or {}
        self.extent = data.get("extent")
        self.interior_drop_m = float(data.get("interior_drop_m") or 0.0)
        self.edge_blend_m = float(data.get("edge_blend_m") or 0.0)
        self.smooth_blend = bool(data.get("smooth_blend", True))
        self.lowering_only = bool(data.get("lowering_only", False))
        self.utm_zone = int(data.get("utm_zone") or 14)
        self.utm_northern = bool(data.get("utm_northern", True))
        self._tri_cell = 0.001  # ~100 m at mid-latitudes in degrees
        self._tri_grid: Dict[Tuple[int, int], List[int]] = {}
        for i, tri in enumerate(self.triangles):
            w, s, e, n = tri["bbox"]
            x0 = int(math.floor(w / self._tri_cell))
            x1 = int(math.floor(e / self._tri_cell))
            y0 = int(math.floor(s / self._tri_cell))
            y1 = int(math.floor(n / self._tri_cell))
            for ix in range(x0, x1 + 1):
                for iy in range(y0, y1 + 1):
                    self._tri_grid.setdefault((ix, iy), []).append(i)

    def bounds_intersect(self, west, south, east, north) -> bool:
        if self.extent is None:
            return False
        ws, ss, es, ns = self.extent
        return not (east < ws or west > es or north < ss or south > ns)

    def tile_hits_mask(self, west, south, east, north) -> bool:
        for fid, bbox in self.mask_bboxes.items():
            mw, ms, me, mn = bbox
            if east < mw or west > me or north < ms or south > mn:
                continue
            rings = self.mask_rings.get(fid)
            if not rings:
                continue
            # Cheap: bbox overlap is enough to examine the tile.
            return True
        return False

    def _containing_mask_fid(self, lon, lat):
        for fid, bbox in self.mask_bboxes.items():
            mw, ms, me, mn = bbox
            if lon < mw or lon > me or lat < ms or lat > mn:
                continue
            rings = self.mask_rings.get(fid)
            if rings and _point_in_rings(lon, lat, rings):
                return fid
        return None

    def _blended_drop(self, lon, lat, mask_fid):
        if self.interior_drop_m <= 0.0:
            return 0.0
        if self.edge_blend_m <= 0.0:
            return self.interior_drop_m
        rings = self.mask_metric_rings.get(mask_fid)
        if not rings:
            return self.interior_drop_m
        mx, my = lonlat_to_utm(
            lon, lat, self.utm_zone, self.utm_northern
        )
        dist_m = _min_dist_to_rings(mx, my, rings)
        if self.smooth_blend:
            t = max(0.0, min(1.0, dist_m / self.edge_blend_m))
            t = t * t * (3.0 - 2.0 * t)
            return self.interior_drop_m * t
        if dist_m < self.edge_blend_m:
            return 0.0
        return self.interior_drop_m

    def _tin_z(self, lon, lat):
        if not self.triangles:
            return None
        cx = int(math.floor(lon / self._tri_cell))
        cy = int(math.floor(lat / self._tri_cell))
        candidates = self._tri_grid.get((cx, cy), [])
        best = None
        best_d = None
        for ti in candidates:
            tri = self.triangles[ti]
            w = _barycentric(
                lon,
                lat,
                tri["lon"][0],
                tri["lat"][0],
                tri["lon"][1],
                tri["lat"][1],
                tri["lon"][2],
                tri["lat"][2],
            )
            if w is not None:
                w1, w2, w3 = w
                return (
                    w1 * tri["z"][0]
                    + w2 * tri["z"][1]
                    + w3 * tri["z"][2]
                )
            tcx = sum(tri["lon"]) / 3.0
            tcy = sum(tri["lat"]) / 3.0
            d = (tcx - lon) ** 2 + (tcy - lat) ** 2
            if best_d is None or d < best_d:
                best_d = d
                best = tri
        if best is None:
            # Fallback: scan all (small sets / edge cells)
            for tri in self.triangles:
                w = _barycentric(
                    lon,
                    lat,
                    tri["lon"][0],
                    tri["lat"][0],
                    tri["lon"][1],
                    tri["lat"][1],
                    tri["lon"][2],
                    tri["lat"][2],
                )
                if w is not None:
                    w1, w2, w3 = w
                    return (
                        w1 * tri["z"][0]
                        + w2 * tri["z"][1]
                        + w3 * tri["z"][2]
                    )
            return None
        w = _barycentric_clamped(
            lon,
            lat,
            best["lon"][0],
            best["lat"][0],
            best["lon"][1],
            best["lat"][1],
            best["lon"][2],
            best["lat"][2],
        )
        if w is None:
            return None
        w1, w2, w3 = w
        return w1 * best["z"][0] + w2 * best["z"][1] + w3 * best["z"][2]

    def sample_z(self, lon, lat, current_z=None):
        mask_fid = self._containing_mask_fid(lon, lat)
        if mask_fid is None:
            return None
        drop = self._blended_drop(lon, lat, mask_fid)
        if self.lowering_only or not self.triangles:
            if current_z is None:
                return None
            return float(current_z) - drop
        z = self._tin_z(lon, lat)
        if z is None:
            return None
        return z - drop


_WORKER_SNAPSHOT: Optional[SurfaceSnapshot] = None


def init_tile_worker(scripts_root: str, snapshot_data: Dict[str, Any]) -> None:
    import sys

    if scripts_root and scripts_root not in sys.path:
        sys.path.insert(0, scripts_root)
    global _WORKER_SNAPSHOT
    _WORKER_SNAPSHOT = SurfaceSnapshot(snapshot_data)


def patch_one_tile(
    task: Tuple[str, int, int, int]
) -> Tuple[bool, int, Optional[str]]:
    """
    Patch one .terrain tile in-place under the worker snapshot.

    Returns (changed, n_changed_verts, error_or_None).
    """
    from quantized_mesh import (
        load_tile_altitudes_lonlat,
        replace_tile_altitudes,
        write_terrain_file,
    )

    path_str, level, tx, ty = task
    snap = _WORKER_SNAPSHOT
    if snap is None:
        return False, 0, "worker snapshot missing"
    path = Path(path_str)
    try:
        data, was_gzip, lons, lats, alts = load_tile_altitudes_lonlat(
            path, level, tx, ty
        )
    except Exception as exc:
        return False, 0, f"read {path}: {exc}"

    modified = False
    new_alts = list(alts)
    changed_verts = 0
    for vi, (lon, lat, old_z) in enumerate(zip(lons, lats, alts)):
        new_z = snap.sample_z(lon, lat, current_z=old_z)
        if new_z is None:
            continue
        if abs(new_z - old_z) > 1e-6:
            new_alts[vi] = new_z
            modified = True
            changed_verts += 1
    if not modified:
        return False, 0, None
    try:
        patched = replace_tile_altitudes(data, level, tx, ty, new_alts)
        write_terrain_file(path, patched, was_gzip)
    except Exception as exc:
        return False, 0, f"write {path}: {exc}"
    return True, changed_verts, None


def snapshot_from_road_surface(
    surface,
    utm_zone: int,
    utm_northern: bool = True,
) -> Dict[str, Any]:
    """Build a picklable dict from a live ``_RoadSurface``."""
    mask_bboxes = {}
    for fid, rings in surface._mask_rings.items():
        xs = [p[0] for ring in rings for p in ring]
        ys = [p[1] for ring in rings for p in ring]
        if xs and ys:
            mask_bboxes[fid] = (min(xs), min(ys), max(xs), max(ys))
    return {
        "triangles": list(surface.triangles),
        "mask_rings": dict(surface._mask_rings),
        "mask_metric_rings": dict(surface._mask_metric_rings),
        "mask_bboxes": mask_bboxes,
        "extent": surface._extent,
        "interior_drop_m": surface._interior_drop_m,
        "edge_blend_m": surface._edge_blend_m,
        "smooth_blend": surface._smooth_blend,
        "lowering_only": surface._lowering_only,
        "utm_zone": utm_zone,
        "utm_northern": utm_northern,
    }
