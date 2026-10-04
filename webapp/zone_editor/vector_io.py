# -*- coding: utf-8 -*-
"""Vector read/write helpers for the zone map editor (WGS84 GeoJSON / shapefile)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import shapefile
from pyproj import CRS, Transformer

WGS84 = "EPSG:4326"
ROOF_TYPE_FIELD = "roof_type"

_VECTOR_EXTS = {".shp", ".geojson", ".json"}


def _transformer_to_wgs84(prj_path: Optional[Path]) -> Optional[Transformer]:
    if prj_path is None or not prj_path.is_file():
        return None
    try:
        text = prj_path.read_text(encoding="utf-8", errors="ignore").strip()
        if not text:
            return None
        src = CRS.from_user_input(text)
        if src.to_epsg() == 4326:
            return None
        return Transformer.from_crs(src, WGS84, always_xy=True)
    except Exception:
        return None


def _xy(xform: Optional[Transformer], x: float, y: float) -> Tuple[float, float]:
    if xform is None:
        return float(x), float(y)
    lon, lat = xform.transform(x, y)
    return float(lon), float(lat)


def _ring_to_coords(
    points: Sequence[Sequence[float]],
    xform: Optional[Transformer],
) -> List[List[float]]:
    coords: List[List[float]] = []
    for p in points:
        lon, lat = _xy(xform, float(p[0]), float(p[1]))
        if coords and abs(coords[-1][0] - lon) < 1e-15 and abs(coords[-1][1] - lat) < 1e-15:
            continue
        coords.append([lon, lat])
    if len(coords) >= 3:
        if coords[0] != coords[-1]:
            coords.append(coords[0][:])
    return coords


def shapefile_to_geojson(
    path: Path,
    *,
    max_features: Optional[int] = None,
    properties: bool = False,
    simplify_deg: float = 0.0,
) -> Dict[str, Any]:
    """Load .shp to GeoJSON FeatureCollection in EPSG:4326."""
    path = Path(path)
    reader = shapefile.Reader(str(path))
    xform = _transformer_to_wgs84(path.with_suffix(".prj"))
    field_names = [f[0] for f in reader.fields[1:]]
    features: List[Dict[str, Any]] = []
    for i, shape_rec in enumerate(reader.iterShapeRecords()):
        if max_features is not None and i >= max_features:
            break
        shape = shape_rec.shape
        # pyshp: POLYGON=5, POLYGONZ=15, POLYGONM=25
        if int(shape.shapeType) not in (5, 15, 25):
            continue
        parts = list(shape.parts) + [len(shape.points)]
        rings: List[List[List[float]]] = []
        for a, b in zip(parts[:-1], parts[1:]):
            ring = _ring_to_coords(shape.points[a:b], xform)
            if len(ring) >= 4:
                if simplify_deg > 0:
                    ring = _simplify_ring(ring, simplify_deg)
                if len(ring) >= 4:
                    rings.append(ring)
        if not rings:
            continue
        if len(rings) == 1:
            geom: Dict[str, Any] = {"type": "Polygon", "coordinates": rings}
        else:
            # shapefile parts: first outer, subsequent may be holes or extra outers.
            # Treat as MultiPolygon of single-ring polys for simplicity when multiple parts.
            geom = {
                "type": "MultiPolygon",
                "coordinates": [[r] for r in rings],
            }
        props: Dict[str, Any] = {"id": i}
        if properties:
            for name, val in zip(field_names, shape_rec.record):
                if name.lower() == "deletionflag":
                    continue
                props[name] = val
        features.append({"type": "Feature", "geometry": geom, "properties": props})
    return {"type": "FeatureCollection", "features": features}


def geojson_file_to_fc(path: Path) -> Dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if data.get("type") == "FeatureCollection":
        return data
    if data.get("type") == "Feature":
        return {"type": "FeatureCollection", "features": [data]}
    raise ValueError("Unsupported GeoJSON type")


def vector_to_geojson(
    path: Path,
    *,
    max_features: Optional[int] = None,
    properties: bool = False,
    simplify_deg: float = 0.0,
) -> Dict[str, Any]:
    path = Path(path)
    ext = path.suffix.lower()
    if ext == ".shp":
        return shapefile_to_geojson(
            path,
            max_features=max_features,
            properties=properties,
            simplify_deg=simplify_deg,
        )
    if ext in {".geojson", ".json"}:
        return geojson_file_to_fc(path)
    raise ValueError(f"Unsupported vector format: {ext}")


def geojson_bbox(fc: Dict[str, Any]) -> Optional[List[float]]:
    minx = miny = math.inf
    maxx = maxy = -math.inf

    def walk(coords: Any) -> None:
        nonlocal minx, miny, maxx, maxy
        if not coords:
            return
        if isinstance(coords[0], (int, float)):
            x, y = float(coords[0]), float(coords[1])
            minx, maxx = min(minx, x), max(maxx, x)
            miny, maxy = min(miny, y), max(maxy, y)
            return
        for c in coords:
            walk(c)

    for feat in fc.get("features") or []:
        geom = feat.get("geometry") or {}
        walk(geom.get("coordinates"))
    if minx is math.inf:
        return None
    return [minx, miny, maxx, maxy]


def write_zones_shapefile(fc: Dict[str, Any], out_path: Path) -> Path:
    """Write Polygon shapefile with integer roof_type (EPSG:4326)."""
    out_path = Path(out_path)
    if out_path.suffix.lower() != ".shp":
        out_path = out_path.with_suffix(".shp")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Remove sidecars if reusing name
    for suf in (".shp", ".shx", ".dbf", ".prj", ".cpg"):
        p = out_path.with_suffix(suf)
        if p.exists():
            p.unlink()

    writer = shapefile.Writer(str(out_path), shapeType=shapefile.POLYGON)
    writer.field(ROOF_TYPE_FIELD, "N", 10, 0)
    writer.field("name", "C", 64)
    n = 0
    for feat in fc.get("features") or []:
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        gtype = geom.get("type")
        coords = geom.get("coordinates")
        if not coords:
            continue
        try:
            roof = int(props.get(ROOF_TYPE_FIELD))
        except (TypeError, ValueError):
            continue
        name = str(props.get("name") or "")[:64]
        polys: List[List[List[List[float]]]] = []
        if gtype == "Polygon":
            polys = [coords]
        elif gtype == "MultiPolygon":
            polys = coords
        else:
            continue
        for poly in polys:
            rings_xy: List[List[Tuple[float, float]]] = []
            for ring in poly:
                if len(ring) < 4:
                    continue
                rings_xy.append([(float(p[0]), float(p[1])) for p in ring])
            if not rings_xy:
                continue
            writer.poly(rings_xy)
            writer.record(roof, name)
            n += 1
    if n == 0:
        writer.close()
        raise ValueError("No valid zone polygons with roof_type to write.")
    writer.close()
    out_path.with_suffix(".prj").write_text(
        'GEOGCS["WGS 84",DATUM["WGS_1984",SPHEROID["WGS 84",6378137,298.257223563]],'
        'PRIMEM["Greenwich",0],UNIT["degree",0.0174532925199433]]',
        encoding="utf-8",
    )
    out_path.with_suffix(".cpg").write_text("UTF-8", encoding="utf-8")
    return out_path


def _simplify_ring(ring: List[List[float]], eps: float) -> List[List[float]]:
    """Tiny RDP for closed rings (lon/lat degrees)."""
    if len(ring) < 5 or eps <= 0:
        return ring
    closed = ring[0] == ring[-1]
    pts = ring[:-1] if closed else ring[:]

    def perp(p, a, b):
        ax, ay = a[0], a[1]
        bx, by = b[0], b[1]
        px, py = p[0], p[1]
        dx, dy = bx - ax, by - ay
        if dx == 0 and dy == 0:
            return math.hypot(px - ax, py - ay)
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

    def rdp(seq: List[List[float]]) -> List[List[float]]:
        if len(seq) < 3:
            return seq
        a, b = seq[0], seq[-1]
        idx, dmax = 0, 0.0
        for i in range(1, len(seq) - 1):
            d = perp(seq[i], a, b)
            if d > dmax:
                idx, dmax = i, d
        if dmax > eps:
            left = rdp(seq[: idx + 1])
            right = rdp(seq[idx:])
            return left[:-1] + right
        return [a, b]

    out = rdp(pts)
    if len(out) < 3:
        return ring
    if closed:
        out = out + [out[0][:]]
    return out


def is_allowed_vector(path: Path) -> bool:
    return path.suffix.lower() in _VECTOR_EXTS
