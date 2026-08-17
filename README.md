# QGIS Projects

Small QGIS Processing tools for GIS workflows.

```text
scripts/
  quantized_mesh.py      # shared Cesium quantized-mesh reader
  building_altitude/     # Building altitude + random height
  line_of_sight/         # Line-of-Sight checker
  tree_points/           # Tree points: pack, thin, sample RGB
  roof_type/             # Assign roof_type from zone polygons
```

When adding a script to the QGIS Processing Toolbox, add the **algorithm** `.py` and keep that tool’s other files in the same folder. Also keep [`scripts/quantized_mesh.py`](scripts/quantized_mesh.py) available (same `scripts/` parent, or copy it next to the algorithm if QGIS isolates scripts).

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
| [`sample_tree_rgb.py`](scripts/tree_points/sample_tree_rgb.py) | QGIS: sample GeoTIFF RGB → R/G/B |
| [`sample_tree_rgb_cli.py`](scripts/tree_points/sample_tree_rgb_cli.py) | CLI for RGB sampling |
| [`rgb_core.py`](scripts/tree_points/rgb_core.py) | 0–255 conversion |
| Shared: [`quantized_mesh.py`](scripts/quantized_mesh.py) | Mesh reader |

Converts tree-mask polygons into **PointZ** features with hardcoded `altitude` from the quantized mesh.

Points are placed on a **hexagonal lattice** so nearest neighbors are exactly the chosen spacing; no two points are closer than **Minimum distance between points (meters)** (default **1.5**). Small mask polygons that miss the lattice get a centroid (or point-on-surface) if spacing still allows.

Distances are computed in an auto-selected UTM zone from the layer extent.

### Install / run in QGIS

1. Processing Toolbox → Scripts → **Add Script to Toolbox…**
2. Select `scripts/tree_points/tree_mask_to_points.py` (keep `quantized_mesh.py` available)
3. Run **QGIS Projects → Tree mask polygons to spaced points**

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

