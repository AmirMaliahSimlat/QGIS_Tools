# -*- coding: utf-8 -*-
"""
Write a 3D road surface from outline points and the road mask.

Triangulation matches road_tin_core (RoadPlacer): Delaunay in local meters,
no edge-length cap, keep a triangle when its centroid lies inside the mask.
Vertex Z is the outline point's altitude field.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import numpy as np
import shapefile
import shapely
from scipy.spatial import Delaunay
from shapely.geometry import Polygon
from shapely.ops import unary_union
from shapely.prepared import prep

sys.path.insert(0, str(Path(__file__).resolve().parent))
from road_tin_core import (  # noqa: E402
    _METERS_LAT,
    _meters_lon,
    dedupe_points,
    quant_key,
)

ROOT = Path(__file__).resolve().parents[2]
POINTS = ROOT / r"Database/Nablus/roads/outline_points/final/road_mask_outline_points_with_QM.shp"
MASK = ROOT / r"Database/Nablus/roads/masks/source/Nablus_union.shp"
OUT = ROOT / r"Database/Nablus/roads/mesh_3d/tests/road_surface_from_outline_QM.shp"
CHUNK = 150_000


def _rings(shp):
    pts = [(float(x), float(y)) for x, y in shp.points]
    parts = list(shp.parts) + [len(pts)]
    rings = []
    for a, b in zip(parts, parts[1:]):
        ring = pts[a:b]
        if len(ring) >= 2 and ring[0] == ring[-1]:
            ring = ring[:-1]
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _load_mask():
    print("loading mask", flush=True)
    reader = shapefile.Reader(str(MASK), encoding="latin1")
    geoms = []
    for shp in reader.iterShapes():
        rings = _rings(shp)
        if not rings:
            continue
        geoms.append(Polygon(rings[0], rings[1:]))
    print(f"mask polygons {len(geoms)}", flush=True)
    mask = unary_union(geoms)
    if not mask.is_valid:
        mask = shapely.make_valid(mask)
    print(f"mask ready {mask.geom_type}", flush=True)
    return prep(mask)


def _load_points(points_path: Path):
    print("loading points", flush=True)
    reader = shapefile.Reader(str(points_path), encoding="latin1")
    z_by_key = {}
    points = []
    null_z = 0
    for shp in reader.iterShapes():
        x, y = shp.points[0]
        z = float(shp.z[0]) if shp.z else 0.0
        if not np.isfinite(z):
            z = 0.0
            null_z += 1
        key = quant_key(x, y)
        if key in z_by_key:
            continue
        z_by_key[key] = z
        points.append((float(x), float(y)))
    print(f"points {len(points)}  null altitude {null_z}", flush=True)
    return points, z_by_key


def main() -> None:
    points_path = Path(sys.argv[1]) if len(sys.argv) > 1 else POINTS
    out_path = Path(sys.argv[2]) if len(sys.argv) > 2 else OUT
    print(f"points file {points_path}", flush=True)
    print(f"output {out_path}", flush=True)
    mask = _load_mask()
    raw_points, z_by_key = _load_points(points_path)
    pts = dedupe_points(raw_points)
    print(f"deduped {len(pts)}", flush=True)
    if len(pts) < 3:
        raise SystemExit("Not enough points to triangulate.")

    origin_lon = sum(p[0] for p in pts) / len(pts)
    origin_lat = sum(p[1] for p in pts) / len(pts)
    mx = _meters_lon(origin_lat)
    xy = np.array(
        [((lon - origin_lon) * mx, (lat - origin_lat) * _METERS_LAT) for lon, lat in pts],
        dtype=np.float64,
    )
    lonlat = np.array(pts, dtype=np.float64)
    z = np.array([z_by_key[quant_key(lon, lat)] for lon, lat in pts], dtype=np.float64)

    print("delaunay", flush=True)
    try:
        tin = Delaunay(xy)
    except Exception as exc:
        print(f"delaunay retry QJ ({exc})", flush=True)
        tin = Delaunay(xy, qhull_options="QJ")
    simplices = tin.simplices
    n = len(simplices)
    print(f"candidate triangles {n}", flush=True)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    writer = shapefile.Writer(str(out_path), shapeType=shapefile.POLYGONZ)
    writer.field("tri", "N", 10, 0)
    kept = 0
    for start in range(0, n, CHUNK):
        idx = simplices[start : start + CHUNK]
        a = lonlat[idx[:, 0]]
        b = lonlat[idx[:, 1]]
        c = lonlat[idx[:, 2]]
        clon = (a[:, 0] + b[:, 0] + c[:, 0]) / 3.0
        clat = (a[:, 1] + b[:, 1] + c[:, 1]) / 3.0
        inside = mask.covers(shapely.points(clon, clat))
        za = z[idx[:, 0]]
        zb = z[idx[:, 1]]
        zc = z[idx[:, 2]]
        for j in np.flatnonzero(inside):
            ax, ay = float(a[j, 0]), float(a[j, 1])
            bx, by = float(b[j, 0]), float(b[j, 1])
            cx, cy = float(c[j, 0]), float(c[j, 1])
            writer.polyz(
                [[
                    (ax, ay, float(za[j])),
                    (bx, by, float(zb[j])),
                    (cx, cy, float(zc[j])),
                    (ax, ay, float(za[j])),
                ]]
            )
            writer.record(kept)
            kept += 1
        print(f"scanned {min(start + CHUNK, n)}/{n}  kept {kept}", flush=True)
    writer.close()
    shutil.copyfile(points_path.with_suffix(".prj"), out_path.with_suffix(".prj"))
    print(f"wrote {kept} triangles -> {out_path}", flush=True)


if __name__ == "__main__":
    main()
