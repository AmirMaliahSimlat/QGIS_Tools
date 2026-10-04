# -*- coding: utf-8 -*-
"""
Verify bump polygon vertices that lie on the road-mask outline.

For each such vertex, sample quantized-mesh ground Z and 3D-road Z.
If ground <= road, the outline touch is a mask-clip artifact (not a
detected poke-through). If ground > road, the heights really disagree
and either the 3D road or detection logic needs a closer look.
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import shapefile
from shapely.geometry import LineString, MultiPolygon, Point, Polygon, shape
from shapely.ops import unary_union
from shapely.prepared import prep

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "scripts" / "dtm_burn"))

from quantized_mesh import QuantizedMeshSampler  # noqa: E402
from road_bump_core import RoadGrid, _ROAD_Z_SNAP_M  # noqa: E402

BUMPS = ROOT / "Database/Nablus/roads/clip_polygons/tests/road_ground_exact_v5.shp"
MASK = ROOT / "Database/Nablus/roads/masks/source/Nablus_union.shp"
ROADS = ROOT / "Database/Nablus/roads/mesh_3d/source/roads_export.shp"
MESH = ROOT / "Database/Nablus/elevation/quantized-mesh/source/14"

# ~0.3 m in degrees at Nablus latitude
OUTLINE_TOL_DEG = 0.3 / 111_320.0
# Also probe a few centimetres inward along -normal for context
INSET_M = 0.25


def _read_polys(path: Path):
    r = shapefile.Reader(str(path), encoding="latin1")
    geoms = []
    for sr in r.iterShapeRecords():
        g = shape(sr.shape.__geo_interface__)
        if g.is_empty:
            continue
        if g.geom_type == "Polygon":
            geoms.append(g)
        elif g.geom_type == "MultiPolygon":
            geoms.extend(list(g.geoms))
    return geoms


def _tri_from_points(pts):
    """Fan triangles from a ring of (x,y,z) points (exterior only)."""
    if len(pts) < 3:
        return []
    # drop closing duplicate
    if pts[0][:2] == pts[-1][:2]:
        pts = pts[:-1]
    if len(pts) < 3:
        return []
    tris = []
    a = pts[0]
    for i in range(1, len(pts) - 1):
        b, c = pts[i], pts[i + 1]
        if len(a) < 3 or len(b) < 3 or len(c) < 3:
            continue
        if a[2] is None or b[2] is None or c[2] is None:
            continue
        tris.append(
            (
                (float(a[0]), float(a[1]), float(a[2])),
                (float(b[0]), float(b[1]), float(b[2])),
                (float(c[0]), float(c[1]), float(c[2])),
            )
        )
    return tris


def _load_road_grid(path: Path) -> RoadGrid:
    r = shapefile.Reader(str(path), encoding="latin1")
    tris = []
    for s in r.iterShapes():
        if s.shapeType not in (
            shapefile.POLYGONZ,
            shapefile.POLYGONM,
            shapefile.POLYGON,
            15,  # POLYGONZ
        ) and getattr(s, "z", None) is None:
            # try parts anyway
            pass
        pts = list(s.points)
        zs = list(getattr(s, "z", []) or [])
        if not zs:
            continue
        # shapefile stores z per vertex; parts[0] is exterior
        parts = list(s.parts) + [len(pts)]
        for i in range(len(parts) - 1):
            a, b = parts[i], parts[i + 1]
            ring = []
            for j in range(a, b):
                z = zs[j] if j < len(zs) else None
                ring.append((pts[j][0], pts[j][1], z))
            tris.extend(_tri_from_points(ring))
    print(f"Road triangles with Z: {len(tris)}")
    return RoadGrid(tris)


def _outline_verts(bumps, mask_boundary, tol: float):
    """Bump exterior verts within ``tol`` degrees of the mask boundary."""
    hits = []
    for bi, poly in enumerate(bumps):
        coords = list(poly.exterior.coords)
        for ci, (x, y, *rest) in enumerate(coords[:-1]):
            d = Point(x, y).distance(mask_boundary)
            if d <= tol:
                hits.append((bi, ci, x, y, d))
    return hits


def _inset_point(poly: Polygon, x: float, y: float, inset_m: float):
    """Move slightly toward polygon centroid (approx inward)."""
    c = poly.centroid
    dx, dy = c.x - x, c.y - y
    L = math.hypot(dx, dy)
    if L < 1e-15:
        return None
    # metres → degrees
    lat = y
    dlon = (inset_m / (111_320.0 * max(math.cos(math.radians(lat)), 1e-3))) * (dx / L)
    dlat = (inset_m / 111_320.0) * (dy / L)
    return x + dlon, y + dlat


def main() -> int:
    print("Loading bumps…", BUMPS.name)
    bumps = _read_polys(BUMPS)
    print(f"  {len(bumps)} polygons")

    print("Loading mask…", MASK.name)
    mask_polys = _read_polys(MASK)
    mask = unary_union(mask_polys)
    print(f"  {len(mask_polys)} parts -> {mask.geom_type}")
    mask_boundary = mask.boundary
    prepared_mask = prep(mask)

    print(f"Finding verts within ~{OUTLINE_TOL_DEG * 111_320:.2f} m of mask outline…")
    hits = _outline_verts(bumps, mask_boundary, OUTLINE_TOL_DEG)
    print(f"  outline-touching verts: {len(hits)}")
    if not hits:
        print("No outline-touching verts — nothing to verify.")
        return 0

    # Unique by rounded lon/lat
    uniq = {}
    for bi, ci, x, y, d in hits:
        key = (round(x, 7), round(y, 7))
        uniq.setdefault(key, (bi, x, y, d))
    samples = list(uniq.values())
    print(f"  unique XY: {len(samples)}")

    print("Loading 3D roads…")
    roads = _load_road_grid(ROADS)

    print("Opening QM sampler (LOD 14)…")
    qm = QuantizedMeshSampler(str(MESH), level=14)

    above = 0
    below_or_eq = 0
    no_road = 0
    no_qm = 0
    rows = []
    for bi, x, y, d_m_deg in samples:
        gz = qm.sample(x, y)
        rz = roads.road_z_at(x, y)
        if rz is None:
            rz = roads.road_z_near(x, y, max_m=_ROAD_Z_SNAP_M)
        dist_m = d_m_deg * 111_320.0
        if gz is None:
            no_qm += 1
            excess = None
        elif rz is None:
            no_road += 1
            excess = None
        else:
            excess = gz - rz
            if excess > 0.02:
                above += 1
            else:
                below_or_eq += 1
        rows.append((bi, x, y, dist_m, gz, rz, excess))

    # Inset probes for the first N outline hits that have coords
    inset_above = inset_below = inset_none = 0
    inset_n = 0
    for bi, x, y, _d in samples[: min(80, len(samples))]:
        poly = bumps[bi]
        ip = _inset_point(poly, x, y, INSET_M)
        if ip is None:
            continue
        ix, iy = ip
        if not prepared_mask.contains(Point(ix, iy)):
            continue
        inset_n += 1
        gz = qm.sample(ix, iy)
        rz = roads.road_z_at(ix, iy) or roads.road_z_near(ix, iy, max_m=_ROAD_Z_SNAP_M)
        if gz is None or rz is None:
            inset_none += 1
            continue
        if gz - rz > 0.02:
            inset_above += 1
        else:
            inset_below += 1

    print()
    print("=== Outline-touching bump verts (on mask boundary) ===")
    print(f"unique samples:     {len(samples)}")
    print(f"ground > road+2cm:  {above}   ← real poke-through at outline")
    print(f"ground <= road+2cm: {below_or_eq}   ← clip artifact / no bump height")
    print(f"no road Z:          {no_road}")
    print(f"no QM Z:            {no_qm}")
    print()
    print(f"=== {INSET_M*100:.0f} cm inward from outline (same bumps) ===")
    print(f"probes:             {inset_n}")
    print(f"ground > road+2cm:  {inset_above}")
    print(f"ground <= road+2cm: {inset_below}")
    print(f"missing Z:          {inset_none}")
    print()

    # Show worst / best excess examples
    with_ex = [r for r in rows if r[6] is not None]
    with_ex.sort(key=lambda r: r[6], reverse=True)
    print("Top 8 excess (ground - road) on outline:")
    for bi, x, y, dist_m, gz, rz, ex in with_ex[:8]:
        print(
            f"  bump#{bi}  excess={ex:+.3f} m  "
            f"gz={gz:.2f} rz={rz:.2f}  dist_to_mask={dist_m:.3f} m  "
            f"({x:.6f},{y:.6f})"
        )
    print("Bottom 8 excess on outline:")
    for bi, x, y, dist_m, gz, rz, ex in with_ex[-8:]:
        print(
            f"  bump#{bi}  excess={ex:+.3f} m  "
            f"gz={gz:.2f} rz={rz:.2f}  dist_to_mask={dist_m:.3f} m  "
            f"({x:.6f},{y:.6f})"
        )

    if below_or_eq > above and below_or_eq > len(samples) * 0.5:
        print()
        print(
            "CONCLUSION: Most outline-touching verts are NOT protruding ground. "
            "They come from intersecting bump polygons with the 2D mask "
            "(clip boundary), not from detecting QM above the road there."
        )
    elif above > len(samples) * 0.5:
        print()
        print(
            "CONCLUSION: Most outline verts really have QM above the road. "
            "Either the 3D road is short of the mask (vs Unreal), or detection "
            "is correctly flagging rim poke-through that Unreal may handle differently."
        )
    else:
        print()
        print("CONCLUSION: Mixed — see counts above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
