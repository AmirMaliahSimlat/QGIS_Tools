# -*- coding: utf-8 -*-
"""Convert raster samples to 0–255 RGB integers; discover GeoTIFFs."""

from __future__ import annotations

import os
from typing import List, Optional, Tuple

R_FIELD = "R"
G_FIELD = "G"
B_FIELD = "B"
TIFF_EXTENSIONS = (".tif", ".tiff")


def list_tiff_files(root: str) -> List[str]:
    """Recursively list .tif/.tiff files under root (files in subfolders too)."""
    if not root:
        return []
    root = os.path.abspath(root)
    if os.path.isfile(root):
        if os.path.splitext(root)[1].lower() in TIFF_EXTENSIONS:
            return [root]
        return []
    if not os.path.isdir(root):
        return []
    found = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            if os.path.splitext(name)[1].lower() in TIFF_EXTENSIONS:
                found.append(os.path.join(dirpath, name))
    found.sort()
    return found



def to_byte(value, nodata=None, data_kind: str = "byte") -> Optional[int]:
    """
    Map a raster sample to 0–255.

    data_kind:
      byte   — already 0–255
      float  — 0–1 scaled, otherwise clamped
      uint16 — scaled from 0–65535
      other  — clamped to 0–255
    """
    if value is None:
        return None
    try:
        fv = float(value)
    except (TypeError, ValueError):
        return None
    if fv != fv:  # NaN
        return None
    if nodata is not None:
        try:
            if fv == float(nodata):
                return None
        except (TypeError, ValueError):
            pass
    kind = (data_kind or "byte").lower()
    if kind == "float":
        if 0.0 <= fv <= 1.0000001:
            return int(round(fv * 255.0))
        return int(max(0, min(255, round(fv))))
    if kind in ("uint16", "int16"):
        return int(max(0, min(255, round(fv * 255.0 / 65535.0))))
    return int(max(0, min(255, round(fv))))


def rgb_from_bands(
    r, g, b, nodata=None, data_kind: str = "byte"
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    return (
        to_byte(r, nodata, data_kind),
        to_byte(g, nodata, data_kind),
        to_byte(b, nodata, data_kind),
    )
