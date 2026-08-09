# -*- coding: utf-8 -*-
"""
Generate buildings shapefile with max-precision altitude from quantized-mesh.

Run via OSGeo4W / QGIS Python (needs GDAL/OGR):
  python scripts/building_altitude/generate_altitude_shapefile.py
"""

from __future__ import annotations

import os
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
OUT_SHP = os.path.join(OUT_DIR, "B_BUILDINGS_A_with_altitude_precise.shp")
ALT_FIELD = "altitude"
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


def min_altitude(geom: ogr.Geometry, sampler: QuantizedMeshSampler):
    min_z = None
    for ring in exterior_rings(geom):
        for i in range(ring.GetPointCount()):
            x, y, *_ = ring.GetPoint(i)
            z = sampler.sample(x, y)
            if z is None:
                continue
            if min_z is None or z < min_z:
                min_z = z
    return min_z


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    for ext in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        path = OUT_SHP[:-4] + ext
        if os.path.exists(path):
            os.remove(path)

    print("Copying buildings shapefile with ogr2ogr…")
    subprocess.check_call([OGR2OGR, "-f", "ESRI Shapefile", OUT_SHP, INPUT_SHP])
    print("  copy done")

    print("Loading quantized-mesh…")
    sampler = QuantizedMeshSampler(MESH_DIR)
    print(f"  level={sampler.level} tiles={len(sampler.tiles_index)}")

    ds = ogr.Open(OUT_SHP, 1)
    if ds is None:
        raise SystemExit(f"Could not open for update: {OUT_SHP}")
    layer = ds.GetLayer(0)
    total = layer.GetFeatureCount()

    if layer.FindFieldIndex(ALT_FIELD, 1) < 0:
        alt_defn = ogr.FieldDefn(ALT_FIELD, ogr.OFTReal)
        alt_defn.SetWidth(24)
        alt_defn.SetPrecision(15)
        if layer.CreateField(alt_defn) != 0:
            raise SystemExit("Failed to create altitude field")

    alt_idx = layer.FindFieldIndex(ALT_FIELD, 1)
    print(f"Filling '{ALT_FIELD}' (Real 24.15) for {total} features…")
    t0 = time.time()
    written = 0
    with_alt = 0

    layer.ResetReading()
    feat = layer.GetNextFeature()
    while feat is not None:
        geom = feat.GetGeometryRef()
        alt = min_altitude(geom, sampler) if geom is not None else None
        if alt is not None:
            feat.SetField(alt_idx, float(alt))
            with_alt += 1
        else:
            feat.SetFieldNull(alt_idx)
        layer.SetFeature(feat)
        written += 1
        if written % 1000 == 0 or written == total:
            elapsed = time.time() - t0
            rate = written / elapsed if elapsed else 0
            print(
                f"  {written}/{total} "
                f"({100.0 * written / max(total, 1):.1f}%) "
                f"with_altitude={with_alt} "
                f"{elapsed:.1f}s ({rate:.0f} feat/s)"
            )
        feat = layer.GetNextFeature()

    ds = None
    check_ds = ogr.Open(OUT_SHP)
    check_lyr = check_ds.GetLayer(0)
    i = check_lyr.GetLayerDefn().GetFieldIndex(ALT_FIELD)
    fd = check_lyr.GetLayerDefn().GetFieldDefn(i)
    sample = check_lyr.GetNextFeature().GetFieldAsDouble(i)
    check_ds = None

    print(f"Done: {OUT_SHP}")
    print(f"Features={written}, with altitude={with_alt}, null={written - with_alt}")
    print(
        f"altitude field: {fd.GetTypeName()} "
        f"width={fd.GetWidth()} precision={fd.GetPrecision()}"
    )
    print(f"sample value: {sample:.15f}")


if __name__ == "__main__":
    main()
