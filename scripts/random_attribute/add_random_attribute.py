# -*- coding: utf-8 -*-
"""
Add a Double attribute filled with uniform random values.

Requires GDAL/OGR (OSGeo4W / QGIS Python, or conda-forge gdal):

  call "C:\\Program Files\\QGIS 3.44.12\\OSGeo4W.bat"
  python scripts/random_attribute/add_random_attribute.py ^
    --input buildings.shp ^
    --output buildings_rand.shp ^
    --field rand_h ^
    --min 5 ^
    --max 20
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import subprocess
import sys
import time


def _find_ogr2ogr() -> str:
    candidates = [
        os.environ.get("OGR2OGR"),
        shutil.which("ogr2ogr"),
        r"C:\Program Files\QGIS 3.44.12\bin\ogr2ogr.exe",
        r"C:\Program Files\QGIS 3.40.0\bin\ogr2ogr.exe",
        r"C:\OSGeo4W\bin\ogr2ogr.exe",
        r"C:\OSGeo4W64\bin\ogr2ogr.exe",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path
    raise SystemExit(
        "ogr2ogr not found. Run inside OSGeo4W/QGIS shell, or set OGR2OGR."
    )


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Add a uniform-random Double attribute to a vector layer."
    )
    p.add_argument("--input", "-i", required=True, help="Input vector (e.g. .shp)")
    p.add_argument("--output", "-o", required=True, help="Output vector path")
    p.add_argument("--field", "-f", required=True, help="New attribute name")
    p.add_argument("--min", dest="min_v", type=float, required=True, help="Minimum value")
    p.add_argument("--max", dest="max_v", type=float, required=True, help="Maximum value")
    p.add_argument("--seed", type=int, default=None, help="Optional RNG seed")
    return p.parse_args(argv)


def main(argv=None):
    from osgeo import ogr  # noqa: WPS433 — requires GDAL env

    args = parse_args(argv)
    field = args.field.strip()
    if not field:
        raise SystemExit("Field name must not be empty.")
    if args.min_v > args.max_v:
        raise SystemExit("--min must be <= --max.")
    if not os.path.isfile(args.input) and not os.path.isdir(args.input):
        # shapefile is a file; gpkg may be file too
        if not os.path.exists(args.input):
            raise SystemExit(f"Input not found: {args.input}")

    out_dir = os.path.dirname(os.path.abspath(args.output))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)

    # Remove existing shapefile sidecars if writing .shp
    root, ext = os.path.splitext(args.output)
    if ext.lower() == ".shp":
        for side in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
            path = root + side
            if os.path.exists(path):
                os.remove(path)
    elif os.path.exists(args.output):
        os.remove(args.output)

    ogr2ogr = _find_ogr2ogr()
    fmt = "ESRI Shapefile" if ext.lower() == ".shp" else "GPKG"
    print(f"Copying input → output ({fmt})…")
    subprocess.check_call([ogr2ogr, "-f", fmt, args.output, args.input])
    print("  copy done")

    if args.seed is not None:
        random.seed(args.seed)

    ds = ogr.Open(args.output, 1)
    if ds is None:
        raise SystemExit(f"Could not open for update: {args.output}")
    layer = ds.GetLayer(0)
    total = layer.GetFeatureCount()

    # Shapefile field names max 10 chars
    stored_name = field[:10] if ext.lower() == ".shp" else field
    if stored_name != field:
        print(f"Warning: shapefile truncates field name to '{stored_name}'")

    if layer.FindFieldIndex(stored_name, 1) >= 0:
        ds = None
        raise SystemExit(f"Field '{stored_name}' already exists.")

    field_defn = ogr.FieldDefn(stored_name, ogr.OFTReal)
    field_defn.SetWidth(24)
    field_defn.SetPrecision(15)
    if layer.CreateField(field_defn) != 0:
        ds = None
        raise SystemExit("Failed to create field.")

    field_idx = layer.FindFieldIndex(stored_name, 1)
    print(
        f"Filling '{stored_name}' with U({args.min_v}, {args.max_v}) "
        f"for {total} features…"
    )
    t0 = time.time()
    written = 0
    layer.ResetReading()
    feat = layer.GetNextFeature()
    while feat is not None:
        feat.SetField(field_idx, random.uniform(args.min_v, args.max_v))
        layer.SetFeature(feat)
        written += 1
        if written % 5000 == 0 or written == total:
            elapsed = time.time() - t0
            rate = written / elapsed if elapsed else 0
            print(f"  {written}/{total} ({elapsed:.1f}s, {rate:.0f} feat/s)")
        feat = layer.GetNextFeature()

    ds = None
    print(f"Done: {args.output}")
    print(f"Features={written}")


if __name__ == "__main__":
    main()
