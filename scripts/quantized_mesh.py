# -*- coding: utf-8 -*-
"""
Quantized-mesh terrain helpers for sampling altitudes.

Expects a single-LOD Cesium tileset on disk as:
  {root}/{x}/{y}.terrain

Defaults match Cesium quantized-mesh: EPSG:4326 (geographic) + TMS
(y increasing northward). Tiles are gzip-compressed.
"""

from __future__ import annotations

import gzip
import math
import struct
from pathlib import Path
from typing import Dict, List, Optional, Tuple


QUANTIZATION = 32767.0
HEADER_BYTES = 88


def zig_zag_decode(value: int) -> int:
    return (value >> 1) ^ (-(value & 1))


def decode_high_water_mark(indices: List[int]) -> List[int]:
    highest = 0
    decoded = []
    for code in indices:
        decoded.append(highest - code)
        if code == 0:
            highest += 1
    return decoded


def geographic_tile_count(level: int) -> Tuple[int, int]:
    return 2 ** (level + 1), 2 ** level


def tile_rectangle(level: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """Return (west, south, east, north) in degrees for EPSG:4326 TMS."""
    num_x, num_y = geographic_tile_count(level)
    west = -180.0 + x * (360.0 / num_x)
    east = -180.0 + (x + 1) * (360.0 / num_x)
    south = -90.0 + y * (180.0 / num_y)
    north = -90.0 + (y + 1) * (180.0 / num_y)
    return west, south, east, north


def lonlat_to_tile(level: int, lon: float, lat: float) -> Tuple[int, int]:
    num_x, num_y = geographic_tile_count(level)
    x = int(math.floor((lon + 180.0) / 360.0 * num_x))
    y = int(math.floor((lat + 90.0) / 180.0 * num_y))
    x = max(0, min(num_x - 1, x))
    y = max(0, min(num_y - 1, y))
    return x, y


def ecef_to_lon(x: float, y: float, _z: float) -> float:
    return math.degrees(math.atan2(y, x))


def list_tiles(root: Path) -> List[Tuple[int, int]]:
    tiles = []
    for x_dir in root.iterdir():
        if not x_dir.is_dir() or not x_dir.name.isdigit():
            continue
        x = int(x_dir.name)
        for terrain in x_dir.glob("*.terrain"):
            if terrain.stem.isdigit():
                tiles.append((x, int(terrain.stem)))
    return tiles


def detect_geographic_level(root: Path, tiles: List[Tuple[int, int]]) -> int:
    """Pick finest geographic level whose tile rect lon matches mesh header lon."""
    if not tiles:
        raise ValueError("No .terrain tiles found in quantized-mesh folder.")

    tx, ty = tiles[len(tiles) // 2]
    path = root / str(tx) / f"{ty}.terrain"
    data = _read_tile_bytes(path)
    cx, cy, cz = struct.unpack_from("<ddd", data, 0)
    tile_lon = ecef_to_lon(cx, cy, cz)

    for level in range(22, -1, -1):
        num_x, num_y = geographic_tile_count(level)
        if tx >= num_x or ty >= num_y:
            continue
        west, south, east, north = tile_rectangle(level, tx, ty)
        center_lon = 0.5 * (west + east)
        if abs(center_lon - tile_lon) <= (east - west):
            return level

    raise ValueError(
        "Could not detect geographic zoom level for quantized-mesh tiles."
    )


def _read_tile_bytes(path: Path) -> bytes:
    raw = path.read_bytes()
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


def _point_in_triangle(
    px: float,
    py: float,
    ax: float,
    ay: float,
    bx: float,
    by: float,
    cx: float,
    cy: float,
) -> Optional[Tuple[float, float, float]]:
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


class QuantizedMeshTile:
    __slots__ = ("lons", "lats", "altitudes", "triangles", "bounds")

    def __init__(
        self,
        lons: List[float],
        lats: List[float],
        altitudes: List[float],
        triangles: List[Tuple[int, int, int]],
        bounds: Tuple[float, float, float, float],
    ):
        self.lons = lons
        self.lats = lats
        self.altitudes = altitudes
        self.triangles = triangles
        self.bounds = bounds

    def sample(self, lon: float, lat: float) -> Optional[float]:
        for i0, i1, i2 in self.triangles:
            weights = _point_in_triangle(
                lon,
                lat,
                self.lons[i0],
                self.lats[i0],
                self.lons[i1],
                self.lats[i1],
                self.lons[i2],
                self.lats[i2],
            )
            if weights is None:
                continue
            w1, w2, w3 = weights
            return (
                w1 * self.altitudes[i0]
                + w2 * self.altitudes[i1]
                + w3 * self.altitudes[i2]
            )

        # Fallback: nearest vertex (edge cases / numerical misses)
        best_d = None
        best_alt = None
        for lo, la, alt in zip(self.lons, self.lats, self.altitudes):
            d = (lo - lon) ** 2 + (la - lat) ** 2
            if best_d is None or d < best_d:
                best_d = d
                best_alt = alt
        return best_alt


def load_tile(path: Path, level: int, x: int, y: int) -> QuantizedMeshTile:
    data = _read_tile_bytes(path)
    if len(data) < HEADER_BYTES + 4:
        raise ValueError(f"Tile too small: {path}")

    # Cesium header MinimumHeight / MaximumHeight (meters)
    min_alt, max_alt = struct.unpack_from("<ff", data, 24)
    vertex_count = struct.unpack_from("<I", data, HEADER_BYTES)[0]
    off = HEADER_BYTES + 4

    def read_u16_array(count: int):
        nonlocal off
        values = list(struct.unpack_from("<" + "H" * count, data, off))
        off += 2 * count
        return values

    u_buf = read_u16_array(vertex_count)
    v_buf = read_u16_array(vertex_count)
    alt_buf = read_u16_array(vertex_count)

    u = v = alt_q = 0
    for i in range(vertex_count):
        u += zig_zag_decode(u_buf[i])
        v += zig_zag_decode(v_buf[i])
        alt_q += zig_zag_decode(alt_buf[i])
        u_buf[i] = u
        v_buf[i] = v
        alt_buf[i] = alt_q

    west, south, east, north = tile_rectangle(level, x, y)
    lons = [
        west + (east - west) * (u_buf[i] / QUANTIZATION) for i in range(vertex_count)
    ]
    lats = [
        south + (north - south) * (v_buf[i] / QUANTIZATION)
        for i in range(vertex_count)
    ]
    altitudes = [
        min_alt + (max_alt - min_alt) * (alt_buf[i] / QUANTIZATION)
        for i in range(vertex_count)
    ]

    use32 = vertex_count > 65536
    align = 4 if use32 else 2
    if off % align:
        off += align - (off % align)

    triangle_count = struct.unpack_from("<I", data, off)[0]
    off += 4
    fmt = "I" if use32 else "H"
    raw_indices = list(
        struct.unpack_from("<" + fmt * (triangle_count * 3), data, off)
    )
    indices = decode_high_water_mark(raw_indices)
    triangles = [
        (indices[i], indices[i + 1], indices[i + 2])
        for i in range(0, len(indices), 3)
    ]
    return QuantizedMeshTile(
        lons, lats, altitudes, triangles, (west, south, east, north)
    )


class QuantizedMeshSampler:
    """Sample terrain altitude (meters) at WGS84 lon/lat from a tiles folder."""

    def __init__(self, root: Path, level: Optional[int] = None):
        self.root = Path(root)
        if not self.root.is_dir():
            raise ValueError(f"Quantized-mesh folder not found: {self.root}")
        self.tiles_index = set(list_tiles(self.root))
        if not self.tiles_index:
            raise ValueError(f"No .terrain tiles under: {self.root}")
        self.level = (
            level
            if level is not None
            else detect_geographic_level(self.root, sorted(self.tiles_index))
        )
        self._cache: Dict[Tuple[int, int], Optional[QuantizedMeshTile]] = {}

    def sample(self, lon: float, lat: float) -> Optional[float]:
        x, y = lonlat_to_tile(self.level, lon, lat)
        tile = self._get_tile(x, y)
        if tile is None:
            return None
        return tile.sample(lon, lat)

    def _get_tile(self, x: int, y: int) -> Optional[QuantizedMeshTile]:
        key = (x, y)
        if key in self._cache:
            return self._cache[key]
        if key not in self.tiles_index:
            self._cache[key] = None
            return None
        path = self.root / str(x) / f"{y}.terrain"
        try:
            tile = load_tile(path, self.level, x, y)
        except Exception:
            tile = None
        self._cache[key] = tile
        return tile
