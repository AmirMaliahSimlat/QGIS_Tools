# -*- coding: utf-8 -*-
"""
Sample GeoTIFF RGB at tree points into fields R, G, B.

--raster may be a single .tif or a folder. Folders are searched recursively
for .tif/.tiff (subfolders included); other files are ignored.

  call "C:\\Program Files\\QGIS 3.44.12\\OSGeo4W.bat"
  python -u scripts/tree_points/sample_tree_rgb_cli.py ^
    --points trees.shp --raster path\\to\\imagery_folder --output trees_rgb.shp
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import OrderedDict

TOOL_DIR = os.path.dirname(os.path.abspath(__file__))
if TOOL_DIR not in sys.path:
    sys.path.insert(0, TOOL_DIR)

from rgb_core import (  # noqa: E402
    B_FIELD,
    G_FIELD,
    R_FIELD,
    list_tiff_files,
    rgb_from_bands,
)

_CACHE_SIZE = 16


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        description="Add R, G, B from a GeoTIFF or a folder of GeoTIFFs."
    )
    p.add_argument("--points", required=True, help="Input point shapefile")
    p.add_argument(
        "--raster",
        required=True,
        help="RGB GeoTIFF file, or a folder of TIFFs (searched recursively)",
    )
    p.add_argument("--output", required=True, help="Output point shapefile")
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


def _data_kind(gdal_type) -> str:
    from osgeo import gdal

    if gdal_type == gdal.GDT_Byte:
        return "byte"
    if gdal_type in (gdal.GDT_Float32, gdal.GDT_Float64):
        return "float"
    if gdal_type in (gdal.GDT_UInt16, gdal.GDT_Int16):
        return "uint16"
    return "other"


def _invert_gt(gdal, gt):
    inv = gdal.InvGeoTransform(gt)
    if inv is None:
        return None
    if isinstance(inv, tuple) and len(inv) == 2 and isinstance(inv[0], (bool, int)):
        ok, inv_gt = inv
        return inv_gt if ok else None
    return inv


def _extent_from_gt(gt, width, height):
    xs, ys = [], []
    for col, row in ((0, 0), (width, 0), (0, height), (width, height)):
        x = gt[0] + col * gt[1] + row * gt[2]
        y = gt[3] + col * gt[4] + row * gt[5]
        xs.append(x)
        ys.append(y)
    return min(xs), min(ys), max(xs), max(ys)


def main(argv=None):
    from osgeo import gdal, ogr, osr

    gdal.UseExceptions()
    ogr.UseExceptions()
    args = parse_args(argv)
    if not os.path.exists(args.points):
        raise SystemExit(f"Points not found: {args.points}")
    if not os.path.exists(args.raster):
        raise SystemExit(f"Raster path not found: {args.raster}")
    in_abs = os.path.abspath(args.points)
    out_abs = os.path.abspath(args.output)
    if in_abs.lower() == out_abs.lower():
        raise SystemExit("Output path must differ from input.")

    paths = list_tiff_files(args.raster)
    if not paths:
        raise SystemExit("No .tif/.tiff files found (folder is searched recursively).")
    print(f"Found {len(paths)} TIFF file(s).", flush=True)

    t0 = time.time()
    tiles = []
    skipped = 0
    for path in paths:
        ds = gdal.Open(path, gdal.GA_ReadOnly)
        if ds is None or ds.RasterCount < 3:
            skipped += 1
            continue
        gt = ds.GetGeoTransform()
        inv_gt = _invert_gt(gdal, gt)
        if inv_gt is None:
            skipped += 1
            ds = None
            continue
        wkt = ds.GetProjection() or ""
        xmin, ymin, xmax, ymax = _extent_from_gt(
            gt, ds.RasterXSize, ds.RasterYSize
        )
        tiles.append(
            {
                "path": path,
                "xmin": xmin,
                "ymin": ymin,
                "xmax": xmax,
                "ymax": ymax,
                "width": ds.RasterXSize,
                "height": ds.RasterYSize,
                "inv_gt": inv_gt,
                "wkt": wkt,
                "kind": _data_kind(ds.GetRasterBand(1).DataType),
                "nodata": ds.GetRasterBand(1).GetNoDataValue(),
            }
        )
        ds = None
    if skipped:
        print(f"Skipped {skipped} invalid or non-RGB TIFF(s).", flush=True)
    if not tiles:
        raise SystemExit("No usable RGB GeoTIFFs (need ≥ 3 bands).")
    print(f"Using {len(tiles)} RGB GeoTIFF(s).", flush=True)

    pts_ds = ogr.Open(args.points)
    if pts_ds is None:
        raise SystemExit(f"Could not open points: {args.points}")
    pts_lyr = pts_ds.GetLayer(0)
    n = pts_lyr.GetFeatureCount()
    print(f"Input features: {n}", flush=True)

    for name in (R_FIELD, G_FIELD, B_FIELD):
        if pts_lyr.FindFieldIndex(name, 1) >= 0:
            raise SystemExit(f"Points already have field '{name}'.")

    src_srs = pts_lyr.GetSpatialRef()
    ct_by_wkt = {}

    def transform_xy(x, y, wkt):
        if not wkt or src_srs is None:
            return x, y
        if wkt not in ct_by_wkt:
            rast_srs = osr.SpatialReference()
            rast_srs.ImportFromWkt(wkt)
            src = src_srs.Clone()
            src.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            rast_srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
            if src.IsSame(rast_srs):
                ct_by_wkt[wkt] = None
            else:
                ct_by_wkt[wkt] = osr.CoordinateTransformation(src, rast_srs)
        ct = ct_by_wkt[wkt]
        if ct is None:
            return x, y
        t = ct.TransformPoint(x, y)
        return t[0], t[1]

    cache = OrderedDict()

    def open_ds(path):
        if path in cache:
            cache.move_to_end(path)
            return cache[path]
        ds = gdal.Open(path, gdal.GA_ReadOnly)
        if ds is None:
            return None
        cache[path] = ds
        while len(cache) > _CACHE_SIZE:
            old_path, old_ds = cache.popitem(last=False)
            old_ds = None
        return ds

    out_dir = os.path.dirname(out_abs)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    _remove_shapefile(args.output)

    driver = ogr.GetDriverByName("ESRI Shapefile")
    out_ds = driver.CreateDataSource(args.output)
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
    for name in (R_FIELD, G_FIELD, B_FIELD):
        out_lyr.CreateField(ogr.FieldDefn(name, ogr.OFTInteger))
    r_idx = out_lyr.GetLayerDefn().GetFieldIndex(R_FIELD)
    g_idx = out_lyr.GetLayerDefn().GetFieldIndex(G_FIELD)
    b_idx = out_lyr.GetLayerDefn().GetFieldIndex(B_FIELD)

    print("Sampling RGB…", flush=True)
    filled = 0
    nulls = 0
    written = 0
    pts_lyr.ResetReading()
    feat = pts_lyr.GetNextFeature()
    t_write = time.time()
    while feat is not None:
        out_feat = ogr.Feature(out_lyr.GetLayerDefn())
        out_feat.SetFrom(feat)
        r = g = b = None
        geom = feat.GetGeometryRef()
        if geom is not None and not geom.IsEmpty():
            x0, y0, *_ = geom.GetPoint()
            for tile in tiles:
                x, y = transform_xy(x0, y0, tile["wkt"])
                if not (
                    tile["xmin"] <= x <= tile["xmax"]
                    and tile["ymin"] <= y <= tile["ymax"]
                ):
                    continue
                px, py = gdal.ApplyGeoTransform(tile["inv_gt"], x, y)
                col, row = int(px), int(py)
                if not (0 <= col < tile["width"] and 0 <= row < tile["height"]):
                    continue
                ds = open_ds(tile["path"])
                if ds is None:
                    continue
                try:
                    rv = ds.GetRasterBand(1).ReadAsArray(col, row, 1, 1)
                    gv = ds.GetRasterBand(2).ReadAsArray(col, row, 1, 1)
                    bv = ds.GetRasterBand(3).ReadAsArray(col, row, 1, 1)
                    r, g, b = rgb_from_bands(
                        None if rv is None else float(rv[0][0]),
                        None if gv is None else float(gv[0][0]),
                        None if bv is None else float(bv[0][0]),
                        nodata=tile["nodata"],
                        data_kind=tile["kind"],
                    )
                except Exception:
                    r = g = b = None
                if r is not None:
                    break
        if r is None:
            out_feat.SetFieldNull(r_idx)
            out_feat.SetFieldNull(g_idx)
            out_feat.SetFieldNull(b_idx)
            nulls += 1
        else:
            out_feat.SetField(r_idx, int(r))
            out_feat.SetField(g_idx, int(g))
            out_feat.SetField(b_idx, int(b))
            filled += 1
        out_lyr.CreateFeature(out_feat)
        out_feat = None
        written += 1
        if written % 50000 == 0 or written == n:
            elapsed = time.time() - t_write
            print(
                f"  {written}/{n} filled={filled} null={nulls} "
                f"({elapsed:.1f}s)",
                flush=True,
            )
        feat = pts_lyr.GetNextFeature()

    src_cpg = os.path.splitext(in_abs)[0] + ".cpg"
    dst_cpg = os.path.splitext(out_abs)[0] + ".cpg"
    encoding = "UTF-8"
    if os.path.isfile(src_cpg):
        with open(src_cpg, encoding="ascii", errors="ignore") as f:
            encoding = f.read().strip() or encoding
    with open(dst_cpg, "w", encoding="ascii", newline="\n") as f:
        f.write(encoding + "\n")

    cache.clear()
    out_ds = None
    pts_ds = None
    print(f"Done: {args.output}", flush=True)
    print(
        f"Features={written} filled={filled} null={nulls} "
        f"elapsed={time.time() - t0:.1f}s",
        flush=True,
    )


if __name__ == "__main__":
    main()
