# Database folder

Layout is **map → domain → tier**:

```text
Database/
  Fort_Riley/
    buildings/
      footprints/{source,staging,tests,final}/   # import + stages + best
      zones/{staging,tests,final}/               # tool output
    trees/
      footprints/{source,staging,tests,final}/   # import
      points/{staging,tests,final}/              # tool output
    roads/
      vectors/{source,staging,tests,final}/      # import
      masks/{staging,tests,final}/               # tool output
      outline_points/{staging,tests,final}/      # tool output
      mesh_3d/{source,staging,tests,final}/      # Unreal 3D road triangles
    water/
      footprints/{source,staging,tests,final}/   # import
      outline_points/{staging,tests,final}/      # tool output
    elevation/
      quantized-mesh/{source,staging,tests,final}/
        <tileset>/                 # e.g. "Quantized Mesh"
          {0..15}/{x}/{y}.terrain
      DTM/{source,staging,tests,final}/          # raster DTM / DEM imports
    imagery/{source,staging,tests,final}/        # import
  Nablus/
    … same skeleton …
  GFK/
    … same skeleton …
```

| Tier | Purpose |
| --- | --- |
| `source/` | Raw imports (often from the internet) — only on domains listed below |
| `staging/` | Intermediate stages between source and final (e.g. buildings with altitude/height but no roof type yet) |
| `tests/` | Experiments / temporary tool runs |
| `final/` | Best finished product for that topic (e.g. final buildings shapefile) |

**Domains with `source/`:** buildings footprints, trees footprints, roads vectors, roads 3D mesh, water footprints, elevation quantized-mesh, elevation DTM, imagery.

### UI
- **MAP** — select on Configure (folder under `Database/`)
- **Inputs** — lists `final` → `staging` → `source` (if any) → `tests` for that map
- **Outputs** — pick Save to `final` | `staging` | `tests` + file name →  
  `Database/<map>/<domain>/<tier>/<name>`

Add a new map by creating `Database/Your_Map/` (or add it under `maps:` in `webapp/catalog/tools.yaml`); the app creates the domain/tier folders on select.
