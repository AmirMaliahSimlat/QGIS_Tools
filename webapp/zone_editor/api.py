# -*- coding: utf-8 -*-
"""HTTP API for zone editor: vectors, imagery tiles, save zones."""

from __future__ import annotations

from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from fastapi import HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response

from catalog_loader import (
    CATALOG_PATH,
    load_catalog,
    map_root,
    library_choices,
    domain_tiers,
    ensure_database_layout,
    tier_path,
)
from zone_editor.tile_server import (
    empty_png,
    list_rasters,
    mosaic_bounds_wgs84,
    rasterio_available,
    render_tile_png,
)
from zone_editor.vector_io import (
    ROOF_TYPE_FIELD,
    geojson_bbox,
    vector_to_geojson,
    write_zones_shapefile,
)


def _safe_under(root: Path, path: Path) -> Path:
    root = root.resolve()
    path = path.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise HTTPException(403, "Path outside Database") from exc
    return path


def register_zone_api(app, *, get_state) -> None:
    """Attach /api/zones/* routes to the NiceGUI FastAPI app."""

    def _db_map():
        state = get_state()
        db = Path(state.get("database") or ".")
        mid = state.get("map")
        if not mid:
            raise HTTPException(400, "Choose a map first")
        catalog = load_catalog(CATALOG_PATH)
        ensure_database_layout(db, catalog, map_id=str(mid))
        return db, str(mid), map_root(db, str(mid)), catalog

    @app.get("/api/zones/layers")
    def layers() -> JSONResponse:
        db, mid, mroot, catalog = _db_map()
        buildings = library_choices(
            mroot, catalog, "buildings_footprints", "vector_file"
        )
        zones = library_choices(mroot, catalog, "buildings_zones", "vector_file")
        imagery_files = library_choices(mroot, catalog, "imagery", "raster_file")
        # Also offer imagery folders (source may hold a tile directory).
        imagery_folders = library_choices(mroot, catalog, "imagery", "folder")
        vals = (get_state().get("values") or {}).get("assign_roof_type") or {}
        return JSONResponse(
            {
                "map": mid,
                "database": str(db),
                "buildings": buildings,
                "zones": zones,
                "imagery_files": imagery_files,
                "imagery_folders": imagery_folders,
                "defaults": {
                    "buildings": vals.get("INPUT_BUILDINGS"),
                    "zones": vals.get("INPUT_ZONES"),
                },
                "rasterio": rasterio_available(),
                "roof_type_field": ROOF_TYPE_FIELD,
            }
        )

    @app.get("/api/zones/geojson")
    def geojson(
        path: str = Query(...),
        max_features: Optional[int] = Query(None),
        properties: bool = Query(False),
        simplify: float = Query(0.0),
    ) -> JSONResponse:
        db, _mid, mroot, _catalog = _db_map()
        # Allow Database root broadly (map folder).
        src = _safe_under(db, Path(unquote(path)))
        if not src.is_file():
            raise HTTPException(404, f"Not found: {src}")
        try:
            # Buildings: light simplify + no props for speed
            fc = vector_to_geojson(
                src,
                max_features=max_features,
                properties=properties,
                simplify_deg=simplify,
            )
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc
        bbox = geojson_bbox(fc)
        return JSONResponse(
            {
                "geojson": fc,
                "bbox": bbox,
                "count": len(fc.get("features") or []),
                "path": str(src),
            }
        )

    @app.get("/api/zones/imagery/meta")
    def imagery_meta(path: str = Query(...)) -> JSONResponse:
        db, _mid, _mroot, _catalog = _db_map()
        src = _safe_under(db, Path(unquote(path)))
        rasters = list_rasters(src)
        if not rasters:
            raise HTTPException(404, "No GeoTIFF found at path")
        if not rasterio_available():
            raise HTTPException(500, "rasterio not installed — pip install rasterio")
        bounds = mosaic_bounds_wgs84(rasters)
        return JSONResponse(
            {
                "path": str(src),
                "raster_count": len(rasters),
                "bounds": list(bounds) if bounds else None,
            }
        )

    @app.get("/api/zones/tiles/{z}/{x}/{y}.png")
    def tiles(
        z: int,
        x: int,
        y: int,
        path: str = Query(...),
    ) -> Response:
        db, _mid, _mroot, _catalog = _db_map()
        src = _safe_under(db, Path(unquote(path)))
        rasters = list_rasters(src)
        if not rasters or not rasterio_available():
            return Response(content=empty_png(), media_type="image/png")
        png = render_tile_png(rasters, z, x, y)
        if png is None:
            return Response(content=empty_png(), media_type="image/png")
        return Response(content=png, media_type="image/png")

    @app.post("/api/zones/save")
    async def save(request: Request) -> JSONResponse:
        db, mid, mroot, catalog = _db_map()
        body = await request.json()
        fc = body.get("geojson")
        if not isinstance(fc, dict):
            raise HTTPException(400, "geojson FeatureCollection required")
        name = str(body.get("name") or "roof_zones").strip()
        name = "".join(c if c.isalnum() or c in "-_" else "_" for c in name).strip("_")
        if not name:
            name = "roof_zones"
        tier = str(body.get("tier") or "final")
        if tier not in domain_tiers(catalog, "buildings_zones"):
            tier = "final"
        out_dir = tier_path(mroot, catalog, "buildings_zones", tier)
        if out_dir is None:
            raise HTTPException(500, "buildings_zones domain missing")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"{name}.shp"
        try:
            written = write_zones_shapefile(fc, out_path)
        except Exception as exc:
            raise HTTPException(400, str(exc)) from exc

        # Wire into assign_roof_type config for this session.
        state = get_state()
        state.setdefault("values", {}).setdefault("assign_roof_type", {})[
            "INPUT_ZONES"
        ] = str(written)

        return JSONResponse(
            {
                "ok": True,
                "path": str(written),
                "map": mid,
                "tier": tier,
                "count": len(fc.get("features") or []),
            }
        )
