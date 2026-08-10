# -*- coding: utf-8 -*-
"""
Generate buildings GeoPackage with altitude, max_altitude, and height.

  altitude / max_altitude — min/max quantized-mesh samples on exterior-ring
                            vertices and edge midpoints
  height = Uniform(min, max) + (max_altitude - altitude)

Run via OSGeo4W / QGIS Python (needs GDAL/OGR):

  call "C:\\Program Files\\QGIS 3.44.12\\OSGeo4W.bat"
  python scripts/building_altitude/generate_altitude_shapefile.py --min 0 --max 10
"""

from __future__ import annotations

import argparse
import math
import os
import random
import subprocess
import sys
import time

ROOT = r"C:\Dev\QGIS Projects"
TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_ROOT = os.path.dirname(TOOL_DIR)
for _p in (TOOL_DIR, SCRIPTS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from quantized_mesh import QuantizedMeshSampler  # noqa: E402
from osgeo import ogr  # noqa: E402

INPUT_SHP = os.path.join(ROOT, "Nablus Data Layers", "Buildings", "B_BUILDINGS_A.shp")
MESH_DIR = os.path.join(ROOT, "Nablus Data Layers", "Quantize Mesh (DTM)")
OUT_DIR = os.path.join(ROOT, "Building Altitude Outputs")
OUT_GPKG = os.path.join(OUT_DIR, "B_BUILDINGS_A_with_altitude_precise.gpkg")

ALTITUDE_FIELD = "altitude"
MAX_ALTITUDE_FIELD = "max_altitude"
HEIGHT_FIELD = "height"

OGR2OGR = r"C:\Program Files\QGIS 3.44.12\bin\ogr2ogr.exe"


def exterior_rings(geom: ogr.Geometry):
    flat = ogr.GT_Flatten(geom.GetGeometryType())
    if flat == ogr.wkbPolygon:
        ring = geom.GetGeometryRef(0)
        if ring is not None:
            yield ring
    elif flat == ogr.wkbMultiPolygon:
        for i in range(geom.GetGeometryCount()):
            poly = geom.GetGeometryRef(i)
            if poly is None:
                continue
            ring = poly.GetGeometryRef(0)
            if ring is not None:
                yield ring


def _sample_point(sampler: QuantizedMeshSampler, x, y, min_z, max_z):
    z = sampler.sample(x, y)
    if z is None:
        return min_z, max_z
    if min_z is None or z < min_z:
        min_z = z
    if max_z is None or z > max_z:
        max_z = z
    return min_z, max_z


def altitude_range(geom: ogr.Geometry, sampler: QuantizedMeshSampler):
    """Min/max over exterior-ring vertices and every edge midpoint."""
    min_z = None
    max_z = None
    for ring in exterior_rings(geom):
        n = ring.GetPointCount()
        for i in range(n):
            x1, y1, *_ = ring.GetPoint(i)
            min_z, max_z = _sample_point(sampler, x1, y1, min_z, max_z)
            if i + 1 >= n:
                continue
            x2, y2, *_ = ring.GetPoint(i + 1)
            min_z, max_z = _sample_point(
                sampler, 0.5 * (x1 + x2), 0.5 * (y1 + y2), min_z, max_z
            )
    return min_z, max_z


def _ensure_real_field(layer, name: str) -> int:
    if layer.FindFieldIndex(name, 1) < 0:
        defn = ogr.FieldDefn(name, ogr.OFTReal)
        defn.SetWidth(24)
        defn.SetPrecision(15)
        if layer.CreateField(defn) != 0:
            raise SystemExit(f"Failed to create field '{name}'")
    return layer.FindFieldIndex(name, 1)


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description=(
            "Add altitude, max_altitude, and height = "
            "Uniform(min, max) + (max_altitude - altitude)."
        )
    )
    p.add_argument("--min", dest="min_v", type=float, required=True)
    p.add_argument("--max", dest="max_v", type=float, required=True)
    p.add_argument("--seed", type=int, default=None)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.min_v > args.max_v:
        raise SystemExit("--min must be <= --max.")
    if args.seed is not None:
        random.seed(args.seed)

    os.makedirs(OUT_DIR, exist_ok=True)
    if os.path.exists(OUT_GPKG):
        os.remove(OUT_GPKG)

    print("Copying buildings → GeoPackage with ogr2ogr…")
    subprocess.check_call(
        [OGR2OGR, "-f", "GPKG", OUT_GPKG, INPUT_SHP]
    )
    print("  copy done")

    print("Loading quantized-mesh…")
    sampler = QuantizedMeshSampler(MESH_DIR)
    print(f"  level={sampler.level} tiles={len(sampler.tiles_index)}")

    ds = ogr.Open(OUT_GPKG, 1)
    if ds is None:
        raise SystemExit(f"Could not open for update: {OUT_GPKG}")
    layer = ds.GetLayer(0)
    total = layer.GetFeatureCount()

    alt_idx = _ensure_real_field(layer, ALTITUDE_FIELD)
    max_idx = _ensure_real_field(layer, MAX_ALTITUDE_FIELD)
    height_idx = _ensure_real_field(layer, HEIGHT_FIELD)
    print(
        f"Filling '{ALTITUDE_FIELD}', '{MAX_ALTITUDE_FIELD}', "
        f"'{HEIGHT_FIELD}' for {total} features…"
    )
    print(
        f"  height = U({args.min_v}, {args.max_v}) + "
        f"({MAX_ALTITUDE_FIELD} - {ALTITUDE_FIELD})"
    )
    t0 = time.time()
    written = 0
    filled = 0

    layer.ResetReading()
    feat = layer.GetNextFeature()
    while feat is not None:
        geom = feat.GetGeometryRef()
        min_z, max_z = (
            altitude_range(geom, sampler) if geom is not None else (None, None)
        )
        if (
            min_z is not None
            and max_z is not None
            and math.isfinite(min_z)
            and math.isfinite(max_z)
        ):
            height = random.uniform(args.min_v, args.max_v) + (max_z - min_z)
            feat.SetField(alt_idx, float(min_z))
            feat.SetField(max_idx, float(max_z))
            feat.SetField(height_idx, float(height))
            filled += 1
        else:
            feat.SetFieldNull(alt_idx)
            feat.SetFieldNull(max_idx)
            feat.SetFieldNull(height_idx)
        layer.SetFeature(feat)
        written += 1
        if written % 1000 == 0 or written == total:
            elapsed = time.time() - t0
            rate = written / elapsed if elapsed else 0
            print(
                f"  {written}/{total} "
                f"({100.0 * written / max(total, 1):.1f}%) "
                f"filled={filled} "
                f"{elapsed:.1f}s ({rate:.0f} feat/s)"
            )
        feat = layer.GetNextFeature()

    ds = None
    print(f"Done: {OUT_GPKG}")
    print(f"Features={written}, filled={filled}, null={written - filled}")


if __name__ == "__main__":
    main()
