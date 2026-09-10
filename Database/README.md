# Database folder

Layout is **map → domain → tier**:

```text
Database/
  Fort_Riley/
    buildings/
      footprints/{source,working,tests}/   # import
      zones/{working,tests}/               # tool output
    trees/
      footprints/{source,working,tests}/   # import
      points/{working,tests}/              # tool output
    roads/
      vectors/{source,working,tests}/      # import
      masks/{working,tests}/               # tool output
      outline_points/{working,tests}/      # tool output
    water/
      footprints/{source,working,tests}/   # import
    mesh/{source,working,tests}/
      <tileset>/                 # e.g. "Quantized Mesh", "mesh_flattened"
        {0..15}/{x}/{y}.terrain  # all LODs in one tileset folder
    imagery/{source,working,tests}/        # import
  Nablus/
    … same skeleton …
  GFK/
    … same skeleton …
```

| Tier | Purpose |
| --- | --- |
| `source/` | Immutable imports — only on domains listed below |
| `working/` | Current best / production version |
| `tests/` | Temporary runs while trying tools |

**Domains with `source/`:** buildings footprints, trees footprints, roads vectors, water footprints, quantized mesh, imagery.

### UI
- **MAP** — select on Configure (folder under `Database/`)
- **Inputs** — lists `working` → `source` (if any) → `tests` for that map
- **Outputs** — pick Save to `working` | `tests` + file name →  
  `Database/<map>/<domain>/<tier>/<name>`

Add a new map by creating `Database/Your_Map/` (or add it under `maps:` in `webapp/catalog/tools.yaml`); the app creates the domain/tier folders on select.
