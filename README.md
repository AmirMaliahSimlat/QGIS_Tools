# QGIS Projects

Small QGIS Processing tools for GIS workflows.

```text
scripts/
  quantized_mesh.py      # shared Cesium quantized-mesh reader
  building_altitude/     # Building altitude + random height
  line_of_sight/         # Line-of-Sight checker
  tree_points/           # Tree-mask polygons → spaced points
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
| [`tree_mask_to_points.py`](scripts/tree_points/tree_mask_to_points.py) | QGIS Processing algorithm |
| Shared: [`quantized_mesh.py`](scripts/quantized_mesh.py) | Mesh reader |

Converts tree-mask polygons into **PointZ** features with hardcoded `altitude` from the quantized mesh.

Points are placed on a **hexagonal lattice** so nearest neighbors are exactly the chosen spacing; no two points are closer than **Minimum distance between points (meters)** (default **1.5**). Small mask polygons that miss the lattice get a centroid (or point-on-surface) if spacing still allows.

Distances are computed in an auto-selected UTM zone from the layer extent.

### Install / run in QGIS

1. Processing Toolbox → Scripts → **Add Script to Toolbox…**
2. Select `scripts/tree_points/tree_mask_to_points.py` (keep `quantized_mesh.py` available)
3. Run **QGIS Projects → Tree mask polygons to spaced points**
