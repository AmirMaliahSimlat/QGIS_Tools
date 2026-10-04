# -*- coding: utf-8 -*-
"""Compare a plugin-style TIN to roads_export.shp (XY only, height ignored)."""

from __future__ import annotations

import sys
from pathlib import Path

import shapefile
from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union
from shapely.prepared import prep

sys.path.insert(0, str(Path(__file__).resolve().parent))
from road_tin_core import _METERS_LAT, _meters_lon, build_tin, tri_key  # noqa: E402

MASK = Path(r"Database/Nablus/roads/masks/source/Nablus_union.shp")
POINTS = Path(r"Database/Nablus/roads/outline_points/source/mask_points_5m_fixed.shp")
EXPORT = Path(r"Database/Nablus/roads/mesh_3d/source/roads_export.shp")


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


def main() -> None:
    root = Path(__file__).resolve().parents[2]
    mr = shapefile.Reader(str(root / MASK), encoding="latin1")
    geoms = []
    outers = []
    holes = []
    for sr in mr.iterShapeRecords():
        rings = _rings(sr.shape)
        if not rings:
            continue
        outers.append(rings[0])
        holes.append(rings[1:])
        geoms.append(Polygon(rings[0], rings[1:]))
    print(f"mask polygons {len(geoms)}", flush=True)
    mask = unary_union(geoms)
    print("mask union built", flush=True)

    er = shapefile.Reader(str(root / EXPORT), encoding="latin1")
    minx, miny, maxx, maxy = er.bbox
    lat = 0.5 * (miny + maxy)
    pad_lon = 80.0 / _meters_lon(lat)
    pad_lat = 80.0 / _METERS_LAT
    window = box(minx - pad_lon, miny - pad_lat, maxx + pad_lon, maxy + pad_lat)
    local = mask.intersection(window)
    prepared_near = prep(local.buffer(15.0 / _METERS_LAT))
    local_outers = []
    local_holes = []
    parts = [local] if local.geom_type == "Polygon" else list(local.geoms)
    for part in parts:
        outer = [(float(x), float(y)) for x, y in part.exterior.coords]
        if outer and outer[0] == outer[-1]:
            outer = outer[:-1]
        hole_rings = []
        for hole in part.interiors:
            ring = [(float(x), float(y)) for x, y in hole.coords]
            if ring and ring[0] == ring[-1]:
                ring = ring[:-1]
            if len(ring) >= 3:
                hole_rings.append(ring)
        if len(outer) >= 3:
            local_outers.append(outer)
            local_holes.append(hole_rings)
    print(f"mask clipped to export pad, parts {len(local_outers)}", flush=True)

    samples = []
    pr = shapefile.Reader(str(root / POINTS), encoding="latin1")
    for sr in pr.iterShapeRecords():
        x, y = sr.shape.points[0]
        if not window.covers(Point(x, y)):
            continue
        if prepared_near.covers(Point(x, y)):
            samples.append((float(x), float(y)))
    print(f"points on or near mask {len(samples)}", flush=True)

    prepared_local = prep(local)

    def centroid_inside(lon: float, lat: float) -> bool:
        return prepared_local.covers(Point(lon, lat))

    tris = build_tin(
        samples,
        local_outers,
        local_holes,
        max_edge_m=0.0,
        centroid_inside=centroid_inside,
    )
    print(f"built triangles {len(tris)}", flush=True)

    export_keys = set()
    for sr in er.iterShapeRecords():
        raw = [(float(x), float(y)) for x, y in sr.shape.points]
        if len(raw) >= 2 and raw[0] == raw[-1]:
            raw = raw[:-1]
        if len(raw) >= 3:
            export_keys.add(tri_key(raw[:3]))
    built_keys = set()
    for a, b, c in tris:
        clon = (a[0] + b[0] + c[0]) / 3.0
        clat = (a[1] + b[1] + c[1]) / 3.0
        if minx <= clon <= maxx and miny <= clat <= maxy:
            built_keys.add(tri_key((a, b, c)))
    both = export_keys & built_keys
    print(f"export unique {len(export_keys)}")
    print(f"built in export bbox {len(built_keys)}")
    print(
        f"match {len(both)}  only_export {len(export_keys - built_keys)}  "
        f"only_built {len(built_keys - export_keys)}"
    )
    print(f"export covered {100.0 * len(both) / max(len(export_keys), 1):.1f}%")


if __name__ == "__main__":
    main()
