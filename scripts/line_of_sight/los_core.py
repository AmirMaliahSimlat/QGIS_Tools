# -*- coding: utf-8 -*-
"""
Line-of-sight against quantized-mesh terrain (and optional building prisms).

A straight line in 3D is the ECEF chord between the two endpoints.
A hit occurs if terrain or (when enabled) an extruded building intersects
that chord at any sample.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass
from typing import Dict, List, Optional, Sequence, Tuple

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_THIS_DIR)
for _p in (_THIS_DIR, _SCRIPTS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from quantized_mesh import QuantizedMeshSampler

# WGS84
_A = 6378137.0
_F = 1.0 / 298.257223563
_E2 = _F * (2.0 - _F)


def geodetic_to_ecef(lon_deg: float, lat_deg: float, height_m: float) -> Tuple[float, float, float]:
    lon = math.radians(lon_deg)
    lat = math.radians(lat_deg)
    sin_lat = math.sin(lat)
    cos_lat = math.cos(lat)
    sin_lon = math.sin(lon)
    cos_lon = math.cos(lon)
    n = _A / math.sqrt(1.0 - _E2 * sin_lat * sin_lat)
    x = (n + height_m) * cos_lat * cos_lon
    y = (n + height_m) * cos_lat * sin_lon
    z = (n * (1.0 - _E2) + height_m) * sin_lat
    return x, y, z


def ecef_to_geodetic(x: float, y: float, z: float) -> Tuple[float, float, float]:
    """Return (lon_deg, lat_deg, height_m)."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    lat = math.atan2(z, p * (1.0 - _E2))
    for _ in range(10):
        sin_lat = math.sin(lat)
        n = _A / math.sqrt(1.0 - _E2 * sin_lat * sin_lat)
        height = p / math.cos(lat) - n
        lat_new = math.atan2(z, p * (1.0 - _E2 * n / (n + height)))
        if abs(lat_new - lat) < 1e-12:
            lat = lat_new
            break
        lat = lat_new
    sin_lat = math.sin(lat)
    n = _A / math.sqrt(1.0 - _E2 * sin_lat * sin_lat)
    height = p / math.cos(lat) - n
    return math.degrees(lon), math.degrees(lat), height


@dataclass
class BuildingPrism:
    """Vertical extrusion of a footprint: [z_min, z_max] over exterior ring."""

    exterior: List[Tuple[float, float]]  # (lon, lat), closed or open
    z_min: float
    z_max: float
    min_lon: float
    min_lat: float
    max_lon: float
    max_lat: float


@dataclass
class LineOfSightResult:
    clear: bool
    sample_count: int
    distance_m: float
    start_altitude_m: float
    end_altitude_m: float
    min_clearance_m: Optional[float]
    first_hit_distance_m: Optional[float]
    first_hit_lon: Optional[float]
    first_hit_lat: Optional[float]
    hit_type: Optional[str] = None  # "terrain" | "building" | None


def _point_in_ring(lon: float, lat: float, ring: Sequence[Tuple[float, float]]) -> bool:
    """Ray-casting point-in-polygon on lon/lat exterior ring."""
    inside = False
    n = len(ring)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = ring[i]
        xj, yj = ring[j]
        if ((yi > lat) != (yj > lat)) and (
            lon < (xj - xi) * (lat - yi) / (yj - yi + 0.0) + xi
        ):
            inside = not inside
        j = i
    return inside


class BuildingIndex:
    """Simple lon/lat grid index for extruded building prisms."""

    def __init__(self, buildings: List[BuildingPrism], cell_size_deg: float = 0.0005):
        self.buildings = buildings
        self.cell = cell_size_deg
        self._grid: Dict[Tuple[int, int], List[int]] = {}
        for idx, b in enumerate(buildings):
            c0 = int(math.floor(b.min_lon / cell_size_deg))
            c1 = int(math.floor(b.max_lon / cell_size_deg))
            r0 = int(math.floor(b.min_lat / cell_size_deg))
            r1 = int(math.floor(b.max_lat / cell_size_deg))
            for c in range(c0, c1 + 1):
                for r in range(r0, r1 + 1):
                    self._grid.setdefault((c, r), []).append(idx)

    def hit(
        self, lon: float, lat: float, height_m: float, tol_m: float = 0.0
    ) -> bool:
        key = (
            int(math.floor(lon / self.cell)),
            int(math.floor(lat / self.cell)),
        )
        for idx in self._grid.get(key, ()):
            b = self.buildings[idx]
            if height_m < b.z_min - tol_m or height_m > b.z_max + tol_m:
                continue
            if lon < b.min_lon or lon > b.max_lon or lat < b.min_lat or lat > b.max_lat:
                continue
            if _point_in_ring(lon, lat, b.exterior):
                return True
        return False


def check_line_of_sight(
    sampler: QuantizedMeshSampler,
    lon1: float,
    lat1: float,
    lon2: float,
    lat2: float,
    *,
    start_offset_m: float = 0.0,
    end_offset_m: float = 0.0,
    sample_spacing_m: float = 1.0,
    clearance_tol_m: float = 0.5,
    start_absolute_altitude_m: Optional[float] = None,
    end_absolute_altitude_m: Optional[float] = None,
    buildings: Optional[BuildingIndex] = None,
) -> LineOfSightResult:
    """
    Return whether the ECEF straight line between the points is clear.

    If ``buildings`` is provided, also treat footprint extrusions as obstacles.
    """
    if sample_spacing_m <= 0:
        raise ValueError("sample_spacing_m must be positive.")

    terrain1 = sampler.sample(lon1, lat1)
    terrain2 = sampler.sample(lon2, lat2)
    if start_absolute_altitude_m is None and terrain1 is None:
        raise ValueError("Start point is outside the quantized-mesh coverage.")
    if end_absolute_altitude_m is None and terrain2 is None:
        raise ValueError("End point is outside the quantized-mesh coverage.")

    h1 = (
        float(start_absolute_altitude_m)
        if start_absolute_altitude_m is not None
        else float(terrain1) + start_offset_m
    )
    h2 = (
        float(end_absolute_altitude_m)
        if end_absolute_altitude_m is not None
        else float(terrain2) + end_offset_m
    )

    a = geodetic_to_ecef(lon1, lat1, h1)
    b = geodetic_to_ecef(lon2, lat2, h2)
    dx, dy, dz = b[0] - a[0], b[1] - a[1], b[2] - a[2]
    distance = math.sqrt(dx * dx + dy * dy + dz * dz)
    if distance == 0.0:
        return LineOfSightResult(
            clear=True,
            sample_count=1,
            distance_m=0.0,
            start_altitude_m=h1,
            end_altitude_m=h2,
            min_clearance_m=None,
            first_hit_distance_m=None,
            first_hit_lon=None,
            first_hit_lat=None,
            hit_type=None,
        )

    steps = max(1, int(math.ceil(distance / sample_spacing_m)))
    min_clearance = None
    sample_count = 0

    for i in range(1, steps):
        t = i / steps
        x = a[0] + t * dx
        y = a[1] + t * dy
        z = a[2] + t * dz
        lon, lat, line_h = ecef_to_geodetic(x, y, z)
        sample_count += 1

        if buildings is not None and buildings.hit(lon, lat, line_h, tol_m=0.0):
            return LineOfSightResult(
                clear=False,
                sample_count=sample_count,
                distance_m=distance,
                start_altitude_m=h1,
                end_altitude_m=h2,
                min_clearance_m=min_clearance,
                first_hit_distance_m=t * distance,
                first_hit_lon=lon,
                first_hit_lat=lat,
                hit_type="building",
            )

        terrain = sampler.sample(lon, lat)
        if terrain is None:
            continue
        clearance = line_h - float(terrain)
        if min_clearance is None or clearance < min_clearance:
            min_clearance = clearance
        if clearance < clearance_tol_m:
            return LineOfSightResult(
                clear=False,
                sample_count=sample_count,
                distance_m=distance,
                start_altitude_m=h1,
                end_altitude_m=h2,
                min_clearance_m=min_clearance,
                first_hit_distance_m=t * distance,
                first_hit_lon=lon,
                first_hit_lat=lat,
                hit_type="terrain",
            )

    return LineOfSightResult(
        clear=True,
        sample_count=sample_count,
        distance_m=distance,
        start_altitude_m=h1,
        end_altitude_m=h2,
        min_clearance_m=min_clearance,
        first_hit_distance_m=None,
        first_hit_lon=None,
        first_hit_lat=None,
        hit_type=None,
    )
