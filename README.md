# QGIS Projects

Small QGIS Processing tools for GIS workflows.

```text
scripts/
  quantized_mesh.py      # shared Cesium quantized-mesh reader
  parallel_util.py       # process-pool helpers
  mesh_flatten_workers.py# picklable flatten tile workers
  building_altitude/     # Building altitude + random height
  water_altitude/        # Water median altitude from mesh
  mask_points/           # Polygon mask → outline (+ optional legacy centers)
  mesh_flatten/          # Flatten quantized mesh under roads (Delaunay Z)
  line_of_sight/         # Line-of-Sight checker
  tree_points/           # Tree points: pack, thin, sample RGB
  roof_type/             # Assign roof_type from zone polygons
webapp/                  # Local browser UI over qgis_process
Database/                # Named input/output folders for the UI
```

When adding a script to the QGIS Processing Toolbox, add the **algorithm** `.py` and keep that tool’s other files in the same folder. Also keep [`scripts/quantized_mesh.py`](scripts/quantized_mesh.py) (and for flatten: `parallel_util.py`, `mesh_flatten_workers.py`) available under the same `scripts/` parent. Shared helpers: [`scripts/crs_util.py`](scripts/crs_util.py), [`scripts/atomic_io.py`](scripts/atomic_io.py).

### Output CRS

**All vector outputs are written in EPSG:4326 (WGS84).** Inputs may use any CRS; tools transform geometries on write. Mesh sampling and RGB raster lookups still work in the appropriate source/raster CRS internally.

### Atomic outputs

Vector tools and road-mesh flatten write to a sibling ``*.partial`` path and only rename into the final location after a successful finish. Cancel/error deletes the partial so the named output is not left half-written. A hard process kill can leave an orphan ``*.partial``; the final name stays untouched until replace succeeds.

### Worker processes

Several mesh-heavy tools accept **Worker processes**: `0` = auto (up to 8 cores), `1` = serial. Applies to mesh altitude sampling / tile patching on:

- Flatten road masks in quantized mesh
- Tree mask polygons to spaced points (altitude phase)
- Polygon mask points with altitude
- Building altitude and random height
- Water median quantized-mesh altitude

## Building altitude and random height

Folder: [`scripts/building_altitude/`](scripts/building_altitude/)

| File | Role |
| --- | --- |
| [`building_altitude_and_height.py`](scripts/building_altitude/building_altitude_and_height.py) | QGIS Processing algorithm |
| [`generate_altitude_shapefile.py`](scripts/building_altitude/generate_altitude_shapefile.py) | CLI batch generator (OSGeo4W) |
| Shared: [`quantized_mesh.py`](scripts/quantized_mesh.py) | Mesh reader |

Adds three hardcoded Double attributes:

| Field | Meaning |
| --- | --- |
| `altitude` | Minimum mesh elevation on exterior-ring vertices and edge midpoints |
| `max_altitude` | Maximum mesh elevation on the same sample points |
| `height` | `Uniform(min, max) + (max_altitude - altitude)` |

### Tileset

[`Nablus Data Layers/Quantize Mesh (DTM)/`](Nablus%20Data%20Layers/Quantize%20Mesh%20(DTM)/) — `{x}/{y}.terrain`, **level 14**.

### Install / run in QGIS

1. Processing Toolbox → Scripts → **Add Script to Toolbox…**
2. Select `scripts/building_altitude/building_altitude_and_height.py`
3. Run **QGIS Projects → Building altitude and random height**
4. Remove old scripts from the QGIS scripts folder if present:
   `building_min_dtm_altitude.py`, `random_heights_algorithm.py`, `add_random_attribute_algorithm.py`

### CLI regenerate

```bat
call "C:\Program Files\QGIS 3.44.12\OSGeo4W.bat"
cd /d "C:\Dev\QGIS Projects"
python "scripts\building_altitude\generate_altitude_shapefile.py" --min 0 --max 10 --seed 42
```

Output: `Building Altitude Outputs/B_BUILDINGS_A_with_altitude_precise.gpkg`

## Water median quantized-mesh altitude

Folder: [`scripts/water_altitude/`](scripts/water_altitude/)

| File | Role |
| --- | --- |
| [`water_median_altitude.py`](scripts/water_altitude/water_median_altitude.py) | QGIS Processing algorithm |
| Shared: [`quantized_mesh.py`](scripts/quantized_mesh.py) | Mesh reader |

Adds hardcoded **`altitude`** = **median** mesh elevation for each water polygon.

Samples exterior-ring vertices and edge midpoints, a point-on-surface, and a light interior grid (default step **25 m**; set to **0** for outline-only). Meant for lakes/ponds (one flat elevation per feature). No valid samples → NULL.

### Install / run in QGIS

1. Processing Toolbox → Scripts → **Add Script to Toolbox…**
2. Select `scripts/water_altitude/water_median_altitude.py` (keep `quantized_mesh.py` available)
3. Run **QGIS Projects → Water median quantized-mesh altitude**

## Polygon mask points with altitude

Folder: [`scripts/mask_points/`](scripts/mask_points/)

| File | Role |
| --- | --- |
| [`polygon_mask_points.py`](scripts/mask_points/polygon_mask_points.py) | QGIS Processing algorithm |
| Shared: [`quantized_mesh.py`](scripts/quantized_mesh.py) | Mesh reader |

Samples **PointZ** features along polygon **outlines** (exterior rings **and holes**), at a chosen **outline spacing** in meters. Vertices are always kept; intermediate stations are added along edges. Each point gets hardcoded `altitude` from the quantized mesh plus `point_role` (`outline` or `center`).

Optional toggle **Add center points (legacy)** (off by default): older sparse-grid / chord-midpoint center samples. Prefer **Flatten road masks in quantized mesh** so Unreal and the DTM share the same outline-Delaunay surface.

### Install / run in QGIS

1. Processing Toolbox → Scripts → **Add Script to Toolbox…**
2. Select `scripts/mask_points/polygon_mask_points.py` (keep `quantized_mesh.py` available)
3. Run **QGIS Projects → Polygon mask points with altitude**
4. If you still have the old script loaded, remove `polygon_outline_points.py` from the QGIS scripts folder

## Flatten road masks in quantized mesh

Folder: [`scripts/mesh_flatten/`](scripts/mesh_flatten/)

| File | Role |
| --- | --- |
| [`flatten_road_mesh.py`](scripts/mesh_flatten/flatten_road_mesh.py) | QGIS Processing algorithm |
| Shared: [`quantized_mesh.py`](scripts/quantized_mesh.py) | Mesh read / height rewrite |

**Run after** generating outline PointZ (mask-points tool, centers off). Copies the input tileset to a new folder (never overwrites). Sample selection and TIN rules match **Unreal RoadPlacer**:

- Outline/curb PointZ on/near mask (**15 m** snap default) or inside mask (`point_role=center` skipped)
- Inject mask ring vertices with nearest PointZ height
- 2D Delaunay; keep triangles whose centroid is inside the mask; set mesh vertices inside the mask to that linear Z
- Optional **Lower interior**: drop terrain under masks; **Smooth edge blend** on (default) or off for a stair step; strip/blend width + max drop
- Optional **Interior lowering only**: skip TIN flatten; input an already-flattened mesh and only apply the drop (outline points not required)

Processes **all** `.terrain` tiles under the folder (`{x}/{y}` and `{level}/{x}/{y}`).

### Install / run in QGIS

1. Add `scripts/mesh_flatten/flatten_road_mesh.py` to the toolbox (keep `quantized_mesh.py`, `parallel_util.py`, and `mesh_flatten_workers.py` available)
2. Run **QGIS Projects → Flatten road masks in quantized mesh**
3. Full flatten: road masks, outline points + altitude field, input mesh, empty output folder
4. Lowering only: enable **Interior lowering only**, set input to your previous flatten output, masks + blend/drop meters (no outline points)
5. Optional **Worker processes** (`0`=auto) speeds tile patching on multi-core PCs

## Line-of-Sight checker

Folder: [`scripts/line_of_sight/`](scripts/line_of_sight/)

| File | Role |
| --- | --- |
| [`line_of_sight_checker.py`](scripts/line_of_sight/line_of_sight_checker.py) | QGIS Processing algorithm |
| [`los_core.py`](scripts/line_of_sight/los_core.py) | ECEF LOS + building prism tests |
| Shared: [`quantized_mesh.py`](scripts/quantized_mesh.py) | Mesh reader |

**Input:** two PointZ features.  
**Output:** `true` if no hit, `false` if any hit.

Optional **Consider buildings** uses the altitude layer (`altitude` + `RELATIVE_F` extrusions). **Distance between sample points on the line** is an input (default **1 m**).

## Tree mask polygons to spaced points

Folder: [`scripts/tree_points/`](scripts/tree_points/)

| File | Role |
| --- | --- |
| [`tree_mask_to_points.py`](scripts/tree_points/tree_mask_to_points.py) | QGIS: polygons → spaced points |
| [`filter_trees_on_buildings.py`](scripts/tree_points/filter_trees_on_buildings.py) | QGIS: drop existing points on/near buildings |
| [`sample_tree_rgb.py`](scripts/tree_points/sample_tree_rgb.py) | QGIS: sample GeoTIFF RGB → R/G/B |
| [`sample_tree_rgb_cli.py`](scripts/tree_points/sample_tree_rgb_cli.py) | CLI for RGB sampling |
| [`rgb_core.py`](scripts/tree_points/rgb_core.py) | 0–255 conversion |
| Shared: [`quantized_mesh.py`](scripts/quantized_mesh.py) | Mesh reader |

Converts tree-mask polygons into **PointZ** features with hardcoded `altitude` from the quantized mesh.

Points are placed on a **hexagonal lattice** so nearest neighbors are exactly the chosen spacing; no two points are closer than **Minimum distance between points (meters)** (default **1.5**). Small mask polygons that miss the lattice get a centroid (or point-on-surface) if spacing still allows.

A **buildings** polygon layer is required. Trees are not placed inside footprints, and a **Clearance from buildings** (default **1 m**) keeps them off the walls.

Distances are computed in an auto-selected UTM zone from the layer extent.

### Install / run in QGIS

1. Processing Toolbox → Scripts → **Add Script to Toolbox…**
2. Select `scripts/tree_points/tree_mask_to_points.py` (keep `quantized_mesh.py` available)
3. Run **QGIS Projects → Tree mask polygons to spaced points**

## Remove tree points on buildings

Post-process an **existing** tree-point layer: drop points inside building footprints or within **Clearance from buildings** (default **1 m**). Does not re-pack from polygons. Copy `filter_trees_on_buildings.py` into the QGIS scripts folder.

### Install / run in QGIS

1. Processing Toolbox → Scripts → **Add Script to Toolbox…**
2. Select `scripts/tree_points/filter_trees_on_buildings.py`
3. Run **QGIS Projects → Remove tree points on buildings**

## Sample tree RGB from GeoTIFF

Adds integer fields **`R`**, **`G`**, **`B`** (0–255) from bands 1/2/3 of georeferenced TIFFs at each tree point. **Input is an imagery folder**: the tool recursively finds every `.tif`/`.tiff` in that folder and all subfolders (other files are ignored). Each point is sampled from the tile that covers it. Points off all images get NULL.

Copy `sample_tree_rgb.py` **and** `rgb_core.py` into the QGIS scripts folder.

### Install / run in QGIS

1. Processing Toolbox → Scripts → **Add Script to Toolbox…**
2. Select `scripts/tree_points/sample_tree_rgb.py`
3. Run **QGIS Projects → Sample tree RGB from GeoTIFF**

### CLI

```bat
call "C:\Program Files\QGIS 3.44.12\OSGeo4W.bat"
cd /d "C:\Dev\QGIS Projects"
python -u "scripts\tree_points\sample_tree_rgb_cli.py" ^
  --points "Fort Riley Data Layers\Trees\tree_points_1M.shp" ^
  --raster "path\to\imagery_folder" ^
  --output "Fort Riley Data Layers\Trees\tree_points_1M_rgb.shp"
```

## Thin tree points

Folder: [`scripts/tree_points/`](scripts/tree_points/)

| File | Role |
| --- | --- |
| [`thin_tree_points.py`](scripts/tree_points/thin_tree_points.py) | QGIS Processing algorithm |
| [`thin_tree_points_cli.py`](scripts/tree_points/thin_tree_points_cli.py) | CLI (OSGeo4W / GDAL) |
| [`thin_core.py`](scripts/tree_points/thin_core.py) | Uniform random sample |

Keeps a **uniform random subset** of points (no polygons). Default target for Fort Riley is **1,000,000** points from ~2.5M. Writes a **new** shapefile; the input is not overwritten.

Copy `thin_tree_points.py` into the QGIS scripts folder (CLI also needs `thin_core.py` next to it).

### CLI (Fort Riley ~2.5M → 1M)

```bat
call "C:\Program Files\QGIS 3.44.12\OSGeo4W.bat"
cd /d "C:\Dev\QGIS Projects"
python -u "scripts\tree_points\thin_tree_points_cli.py" ^
  --points "Fort Riley Data Layers\Trees\tree_points.shp" ^
  --output "Fort Riley Data Layers\Trees\tree_points_1M.shp" ^
  --keep-count 1000000 ^
  --seed 42
```

## Assign roof type from zones

Folder: [`scripts/roof_type/`](scripts/roof_type/)

| File | Role |
| --- | --- |
| [`assign_roof_type.py`](scripts/roof_type/assign_roof_type.py) | QGIS Processing algorithm |
| [`roof_type_core.py`](scripts/roof_type/roof_type_core.py) | Overlap → type rules |

Copies buildings and adds integer **`roof_type`** from a zones polygon layer (same field name).

| Building vs zones | Result |
| --- | --- |
| No overlap | NULL |
| Partial overlap | counts as inside |
| Completely inside one type, only partial in another | the complete type |
| Completely inside two different types, or only partial in two different types | random among those types |

Copy **both** `assign_roof_type.py` and `roof_type_core.py` into the QGIS scripts folder.

### Install / run in QGIS

1. Processing Toolbox → Scripts → **Add Script to Toolbox…**
2. Select `scripts/roof_type/assign_roof_type.py`
3. Run **QGIS Projects → Assign roof type from zones**

## Browser UI (local)

Folder: [`webapp/`](webapp/) — tabbed localhost UI over `qgis_process` (QGIS must be installed; Desktop app not required).

1. Put inputs under [`Database/`](Database/) (see that folder’s README for layout).
2. Ensure Processing scripts are in the QGIS scripts folder (same as Toolbox install).
3. Run:

```bat
webapp\run.bat
```

Or: `python webapp\app.py` after `pip install -r webapp\requirements.txt`.

Opens **http://127.0.0.1:8080** — pick tools by tab → Configure → Run queue.
Optional: set `QGIS_PROCESS_BAT` if auto-detect misses your install.

