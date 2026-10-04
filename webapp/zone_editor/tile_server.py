# -*- coding: utf-8 -*-
"""On-demand XYZ imagery tiles from GeoTIFF / COG (and folders of GeoTIFFs)."""

from __future__ import annotations

import io
import math
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
from PIL import Image

try:
    import rasterio
    from rasterio.enums import Resampling
    from rasterio.warp import transform_bounds
    from rasterio.windows import from_bounds
except ImportError:  # pragma: no cover
    rasterio = None  # type: ignore

RASTER_EXTS = {".tif", ".tiff"}
_OPEN: Dict[str, object] = {}


def rasterio_available() -> bool:
    return rasterio is not None


def list_rasters(path: Path) -> List[Path]:
    path = Path(path)
    if path.is_file() and path.suffix.lower() in RASTER_EXTS:
        return [path]
    if path.is_dir():
        out: List[Path] = []
        for p in sorted(path.rglob("*")):
            if p.is_file() and p.suffix.lower() in RASTER_EXTS:
                out.append(p)
        return out
    return []


def _open_ds(path: Path):
    key = str(path.resolve())
    ds = _OPEN.get(key)
    if ds is None:
        ds = rasterio.open(key)
        _OPEN[key] = ds
    return ds


def mosaic_bounds_wgs84(paths: Sequence[Path]) -> Optional[Tuple[float, float, float, float]]:
    if not paths or rasterio is None:
        return None
    minx = miny = math.inf
    maxx = maxy = -math.inf
    for p in paths:
        ds = _open_ds(p)
        b = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
        minx, miny = min(minx, b[0]), min(miny, b[1])
        maxx, maxy = max(maxx, b[2]), max(maxy, b[3])
    if minx is math.inf:
        return None
    return (minx, miny, maxx, maxy)


def tile_bounds_wgs84(z: int, x: int, y: int) -> Tuple[float, float, float, float]:
    """Web Mercator XYZ tile → lon/lat bounds (west, south, east, north)."""
    n = 2.0 ** z
    west = x / n * 360.0 - 180.0
    east = (x + 1) / n * 360.0 - 180.0
    north = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    south = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * (y + 1) / n))))
    return west, south, east, north


def _intersects(
    a: Tuple[float, float, float, float],
    b: Tuple[float, float, float, float],
) -> bool:
    return not (a[2] <= b[0] or a[0] >= b[2] or a[3] <= b[1] or a[1] >= b[3])


def render_tile_png(
    raster_paths: Sequence[Path],
    z: int,
    x: int,
    y: int,
    *,
    size: int = 256,
) -> Optional[bytes]:
    """
    Render one XYZ tile as PNG (RGBA). Transparent where no data.
    Returns None if no raster intersects the tile.
    """
    if rasterio is None or not raster_paths:
        return None
    west, south, east, north = tile_bounds_wgs84(z, x, y)
    tile_bbox = (west, south, east, north)
    canvas = np.zeros((size, size, 4), dtype=np.uint8)
    hit = False

    for path in raster_paths:
        ds = _open_ds(path)
        try:
            rb = transform_bounds(ds.crs, "EPSG:4326", *ds.bounds, densify_pts=21)
        except Exception:
            continue
        if not _intersects(tile_bbox, rb):
            continue
        # Read window in dataset CRS covering the tile bbox.
        try:
            left, bottom, right, top = transform_bounds(
                "EPSG:4326", ds.crs, west, south, east, north, densify_pts=21
            )
        except Exception:
            continue
        window = from_bounds(left, bottom, right, top, transform=ds.transform)
        try:
            data = ds.read(
                out_shape=(min(ds.count, 3), size, size),
                window=window,
                resampling=Resampling.bilinear,
                boundless=True,
                fill_value=0,
            )
        except Exception:
            continue
        hit = True
        if data.ndim != 3:
            continue
        bands = data.shape[0]
        if bands >= 3:
            rgb = np.transpose(data[:3], (1, 2, 0))
        else:
            g = data[0]
            rgb = np.stack([g, g, g], axis=-1)
        # Alpha: any non-zero sample, or nodata mask if present
        if ds.nodata is not None:
            nodata = float(ds.nodata)
            valid = np.any(np.abs(data.astype(np.float32) - nodata) > 1e-6, axis=0)
        else:
            valid = np.any(data != 0, axis=0)
        alpha = np.where(valid, 255, 0).astype(np.uint8)
        # Overlay: keep existing pixels where already opaque
        empty = canvas[:, :, 3] == 0
        for c in range(3):
            channel = rgb[:, :, c]
            canvas[:, :, c] = np.where(empty & valid, channel, canvas[:, :, c])
        canvas[:, :, 3] = np.where(empty & valid, alpha, canvas[:, :, 3])

    if not hit:
        return None
    img = Image.fromarray(canvas, mode="RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def empty_png(size: int = 256) -> bytes:
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
