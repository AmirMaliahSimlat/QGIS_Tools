# -*- coding: utf-8 -*-
"""
Uniformly thin a point shapefile (no polygons). Writes a new file.

  call "C:\\Program Files\\QGIS 3.44.12\\OSGeo4W.bat"
  python -u scripts/tree_points/thin_tree_points_cli.py ^
    --points "Fort Riley Data Layers\\Trees\\tree_points.shp" ^
    --output "Fort Riley Data Layers\\Trees\\tree_points_1M.shp" ^
    --keep-count 1000000 --seed 42
"""

from __future__ import annotations

import argparse
import os
import sys
import time

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

from thin_core import sample_uniform_indices  # noqa: E402


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Keep a uniform random subset of points (new shapefile)."
    )
    p.add_argument("--points", required=True, help="Input point shapefile")
    p.add_argument("--output", required=True, help="Output point shapefile")
    p.add_argument(
        "--keep-fraction",
        type=float,
        default=None,
        help="Fraction of points to keep (e.g. 0.4). Ignored if --keep-count is set.",
    )
    p.add_argument(
        "--keep-count",
        type=int,
        default=None,
        help="Exact number of points to keep (e.g. 1000000)",
    )
    p.add_argument("--seed", type=int, default=42, help="RNG seed (default 42)")
    return p.parse_args(argv)


def _remove_shapefile(path):
    root, ext = os.path.splitext(path)
    if ext.lower() != ".shp":
        if os.path.exists(path):
            os.remove(path)
        return
    for side in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        p = root + side
        if os.path.exists(p):
            os.remove(p)


def main(argv=None):
    from osgeo import ogr, osr  # noqa: WPS433

    ogr.UseExceptions()
    args = parse_args(argv)
    if args.keep_count is None and args.keep_fraction is None:
        args.keep_count = 1_000_000
    if args.keep_fraction is not None and not 0.0 <= args.keep_fraction <= 1.0:
        raise SystemExit("--keep-fraction must be between 0 and 1.")
    if not os.path.exists(args.points):
        raise SystemExit(f"Points not found: {args.points}")
    in_abs = os.path.abspath(args.points)
    out_abs = os.path.abspath(args.output)
    if in_abs.lower() == out_abs.lower():
        raise SystemExit("Output path must differ from input (will not overwrite).")

    t0 = time.time()
    pts_ds = ogr.Open(args.points)
    if pts_ds is None:
        raise SystemExit(f"Could not open points: {args.points}")
    pts_lyr = pts_ds.GetLayer(0)
    n = pts_lyr.GetFeatureCount()
    print(f"Input features: {n}", flush=True)

    kept = sample_uniform_indices(
        n,
        keep_count=args.keep_count,
        keep_fraction=0.4 if args.keep_fraction is None else args.keep_fraction,
        seed=args.seed,
    )
    print(f"Keeping {len(kept)} points (seed={args.seed})", flush=True)

    out_dir = os.path.dirname(out_abs)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    _remove_shapefile(args.output)

    driver = ogr.GetDriverByName("ESRI Shapefile")
    out_ds = driver.CreateDataSource(args.output)
    if out_ds is None:
        raise SystemExit(f"Could not create {args.output}")
    srs = pts_lyr.GetSpatialRef()
    if srs is None:
        srs = osr.SpatialReference()
    out_lyr = out_ds.CreateLayer(
        os.path.splitext(os.path.basename(args.output))[0],
        srs,
        pts_lyr.GetGeomType(),
    )
    in_defn = pts_lyr.GetLayerDefn()
    for fi in range(in_defn.GetFieldCount()):
        out_lyr.CreateField(in_defn.GetFieldDefn(fi))

    print("Writing output…", flush=True)
    written = 0
    pts_lyr.ResetReading()
    feat = pts_lyr.GetNextFeature()
    i = 0
    t_write = time.time()
    while feat is not None:
        if i in kept:
            out_feat = ogr.Feature(out_lyr.GetLayerDefn())
            out_feat.SetFrom(feat)
            out_lyr.CreateFeature(out_feat)
            out_feat = None
            written += 1
            if written % 100000 == 0:
                elapsed = time.time() - t_write
                print(f"  wrote {written}/{len(kept)} ({elapsed:.1f}s)", flush=True)
        i += 1
        feat = pts_lyr.GetNextFeature()

    out_ds = None
    pts_ds = None

    src_cpg = os.path.splitext(in_abs)[0] + ".cpg"
    dst_cpg = os.path.splitext(out_abs)[0] + ".cpg"
    encoding = "UTF-8"
    if os.path.isfile(src_cpg):
        with open(src_cpg, encoding="ascii", errors="ignore") as f:
            encoding = f.read().strip() or encoding
    with open(dst_cpg, "w", encoding="ascii", newline="\n") as f:
        f.write(encoding + "\n")

    print(f"Done: {args.output}", flush=True)
    print(
        f"Features in={n} out={written} "
        f"({100.0 * written / max(n, 1):.1f}%) "
        f"elapsed={time.time() - t0:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
