# -*- coding: utf-8 -*-
"""
Export quantized-mesh triangles and vertices to QGIS shapefiles.

Reads a Cesium geographic tileset and writes two EPSG:4326 layers:

  *_triangles.shp   POLYGONZ, one face per record (Z is ellipsoid height, m)
  *_vertices.shp    POINTZ, vertices used by those faces

The area is a lon/lat box, or the bounding box of a shapefile (for example
the 3D road mesh). Tiles that miss the box are skipped. A triangle is written
when it overlaps the box; its three vertices are kept even if one sits just
outside, so the face stays the real mesh triangle.
"""

from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

import shapefile

from quantized_mesh import discover_terrain_tiles, load_tile, tile_rectangle

WGS84_PRJ = (
    'GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",'
    'SPHEROID["WGS_1984",6378137.0,298.257223563]],'
    'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]'
)

Box = Tuple[float, float, float, float]  # west, south, east, north


def shapefile_bbox(path: Path) -> Box:
    """Bounding box from the shapefile header (xmin, ymin, xmax, ymax)."""
    with path.open("rb") as handle:
        handle.seek(36)
        xmin, ymin, xmax, ymax = struct.unpack("<4d", handle.read(32))
    if xmax < xmin or ymax < ymin:
        raise ValueError(f"Empty or invalid shapefile extent: {path}")
    return float(xmin), float(ymin), float(xmax), float(ymax)


def _cross(ax: float, ay: float, bx: float, by: float, cx: float, cy: float) -> float:
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def _point_in_triangle(
    px: float, py: float, ax: float, ay: float, bx: float, by: float, cx: float, cy: float
) -> bool:
    c1 = _cross(ax, ay, bx, by, px, py)
    c2 = _cross(bx, by, cx, cy, px, py)
    c3 = _cross(cx, cy, ax, ay, px, py)
    return (c1 >= -1e-14 and c2 >= -1e-14 and c3 >= -1e-14) or (
        c1 <= 1e-14 and c2 <= 1e-14 and c3 <= 1e-14
    )


def _segments_cross(
    ax: float,
    ay: float,
    bx: float,
    by: float,
    cx: float,
    cy: float,
    dx: float,
    dy: float,
) -> bool:
    d1 = _cross(cx, cy, dx, dy, ax, ay)
    d2 = _cross(cx, cy, dx, dy, bx, by)
    d3 = _cross(ax, ay, bx, by, cx, cy)
    d4 = _cross(ax, ay, bx, by, dx, dy)
    return ((d1 > 0.0 and d2 < 0.0) or (d1 < 0.0 and d2 > 0.0)) and (
        (d3 > 0.0 and d4 < 0.0) or (d3 < 0.0 and d4 > 0.0)
    )


def triangle_overlaps_box(
    xs: Sequence[float], ys: Sequence[float], box: Box
) -> bool:
    """True when the triangle and the lon/lat box share any point."""
    west, south, east, north = box
    if max(xs) < west or min(xs) > east or max(ys) < south or min(ys) > north:
        return False
    ax, bx, cx = xs
    ay, by, cy = ys
    for x, y in ((ax, ay), (bx, by), (cx, cy)):
        if west <= x <= east and south <= y <= north:
            return True
    for px, py in (
        (west, south),
        (east, south),
        (east, north),
        (west, north),
    ):
        if _point_in_triangle(px, py, ax, ay, bx, by, cx, cy):
            return True
    edges = ((ax, ay, bx, by), (bx, by, cx, cy), (cx, cy, ax, ay))
    rect = (
        (west, south, east, south),
        (east, south, east, north),
        (east, north, west, north),
        (west, north, west, south),
    )
    for edge in edges:
        for side in rect:
            if _segments_cross(*edge, *side):
                return True
    return False


def _write_sidecars(shp_path: Path) -> None:
    shp_path.with_suffix(".prj").write_text(WGS84_PRJ, encoding="utf-8")
    shp_path.with_suffix(".cpg").write_text("UTF-8", encoding="ascii")


def export_qm_shapefiles(
    mesh_root: Path,
    level: int,
    box: Box,
    triangles_path: Path,
    vertices_path: Path,
) -> Tuple[int, int, int]:
    """
    Write triangle and vertex shapefiles for one LOD inside ``box``.

    Returns (tiles_read, triangle_count, vertex_count).
    """
    tiles = [
        item
        for item in discover_terrain_tiles(mesh_root)
        if item[1] == int(level)
    ]
    if not tiles:
        raise ValueError(f"No LOD {level} tiles under {mesh_root}")

    west, south, east, north = box
    selected = []
    for path, tile_level, tx, ty in tiles:
        tw, ts, te, tn = tile_rectangle(tile_level, tx, ty)
        if te < west or tw > east or tn < south or ts > north:
            continue
        selected.append((path, tile_level, tx, ty))
    if not selected:
        raise ValueError(
            f"No LOD {level} tiles intersect "
            f"({west:.6f}, {south:.6f})–({east:.6f}, {north:.6f})"
        )

    triangles_path.parent.mkdir(parents=True, exist_ok=True)
    n_tri = 0
    n_vert = 0
    with shapefile.Writer(
        str(triangles_path), shapeType=shapefile.POLYGONZ, encoding="utf-8"
    ) as tri_w, shapefile.Writer(
        str(vertices_path), shapeType=shapefile.POINTZ, encoding="utf-8"
    ) as vert_w:
        tri_w.field("lod", "N", 4, 0)
        tri_w.field("tile_x", "N", 8, 0)
        tri_w.field("tile_y", "N", 8, 0)
        tri_w.field("tri", "N", 8, 0)
        tri_w.field("z0", "F", 12, 3)
        tri_w.field("z1", "F", 12, 3)
        tri_w.field("z2", "F", 12, 3)

        vert_w.field("lod", "N", 4, 0)
        vert_w.field("tile_x", "N", 8, 0)
        vert_w.field("tile_y", "N", 8, 0)
        vert_w.field("vert", "N", 8, 0)
        vert_w.field("altitude", "F", 12, 3)

        for ti, (path, tile_level, tx, ty) in enumerate(selected, start=1):
            tile = load_tile(path, tile_level, tx, ty)
            used: List[int] = []
            seen = set()
            for tri_i, (i0, i1, i2) in enumerate(tile.triangles):
                xs = (tile.lons[i0], tile.lons[i1], tile.lons[i2])
                ys = (tile.lats[i0], tile.lats[i1], tile.lats[i2])
                if not triangle_overlaps_box(xs, ys, box):
                    continue
                z0 = float(tile.altitudes[i0])
                z1 = float(tile.altitudes[i1])
                z2 = float(tile.altitudes[i2])
                ring = [
                    [xs[0], ys[0], z0],
                    [xs[1], ys[1], z1],
                    [xs[2], ys[2], z2],
                    [xs[0], ys[0], z0],
                ]
                tri_w.polyz([ring])
                tri_w.record(int(level), int(tx), int(ty), int(tri_i), z0, z1, z2)
                n_tri += 1
                for vertex in (i0, i1, i2):
                    if vertex not in seen:
                        seen.add(vertex)
                        used.append(vertex)
            for vertex in used:
                vert_w.pointz(
                    float(tile.lons[vertex]),
                    float(tile.lats[vertex]),
                    float(tile.altitudes[vertex]),
                )
                vert_w.record(
                    int(level),
                    int(tx),
                    int(ty),
                    int(vertex),
                    float(tile.altitudes[vertex]),
                )
                n_vert += 1
            if ti == 1 or ti == len(selected) or ti % 25 == 0:
                print(
                    f"  tiles {ti}/{len(selected)}  triangles {n_tri}  vertices {n_vert}",
                    flush=True,
                )

    _write_sidecars(triangles_path)
    _write_sidecars(vertices_path)
    return len(selected), n_tri, n_vert


def _parse_box(args: argparse.Namespace) -> Box:
    has_box = None not in (args.west, args.south, args.east, args.north)
    if args.roads and has_box:
        raise SystemExit("Pass either --roads or --west/--south/--east/--north, not both.")
    if args.roads:
        return shapefile_bbox(Path(args.roads))
    if not has_box:
        raise SystemExit("Pass --roads or all of --west --south --east --north.")
    box = (float(args.west), float(args.south), float(args.east), float(args.north))
    if box[2] < box[0] or box[3] < box[1]:
        raise SystemExit("Bounding box is empty (east < west or north < south).")
    return box


def main(argv: Optional[Iterable[str]] = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--mesh",
        required=True,
        help="Quantized-mesh root ({level}/{x}/{y}.terrain) or one level folder",
    )
    parser.add_argument("--level", type=int, required=True, help="LOD to export")
    parser.add_argument(
        "--roads",
        help="Shapefile whose header bounding box is the export area",
    )
    parser.add_argument("--west", type=float)
    parser.add_argument("--south", type=float)
    parser.add_argument("--east", type=float)
    parser.add_argument("--north", type=float)
    parser.add_argument(
        "--out-dir",
        required=True,
        help="Folder for qm_lod{N}_triangles.shp and qm_lod{N}_vertices.shp",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    box = _parse_box(args)
    out_dir = Path(args.out_dir)
    stem = f"qm_lod{int(args.level)}"
    triangles_path = out_dir / f"{stem}_triangles.shp"
    vertices_path = out_dir / f"{stem}_vertices.shp"
    west, south, east, north = box
    print(
        f"LOD {args.level} box ({west:.6f}, {south:.6f})–({east:.6f}, {north:.6f})",
        flush=True,
    )
    n_tiles, n_tri, n_vert = export_qm_shapefiles(
        Path(args.mesh),
        int(args.level),
        box,
        triangles_path,
        vertices_path,
    )
    print(
        f"Wrote {n_tri} triangles and {n_vert} vertices "
        f"from {n_tiles} tiles",
        flush=True,
    )
    print(triangles_path, flush=True)
    print(vertices_path, flush=True)


if __name__ == "__main__":
    main(sys.argv[1:])
