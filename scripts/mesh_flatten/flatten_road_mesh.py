# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: flatten quantized-mesh heights under road masks
using the same 2D Delaunay + outline PointZ rule as the Unreal road plugin.

Never overwrites the input tileset; writes a full copy then patches tiles.
"""

import math
import os
import shutil
import subprocess
import sys

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsUnitTypes,
    QgsWkbTypes,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_SCRIPT_DIR)
for _p in (_SCRIPT_DIR, _SCRIPTS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from quantized_mesh import (  # noqa: E402
    discover_terrain_tiles,
    load_tile_altitudes_lonlat,
    replace_tile_altitudes,
    write_terrain_file,
)

ALTITUDE_FIELD_DEFAULT = "altitude"
ROLE_FIELD = "point_role"
ROLE_OUTLINE = "outline"

# Match RoadPlacer (RoadPlacerBPLibrary.cpp).
UNREAL_OUTLINE_SNAP_M = 15.0

# Optional interior carve: keep edge strip at TIN Z, drop deeper interior.
DEFAULT_EDGE_STRIP_M = 0.5
DEFAULT_INTERIOR_DROP_M = 0.5


class _PhaseProgress:
    """Keep the Processing progress bar and status text moving."""

    def __init__(self, feedback, tr):
        self.feedback = feedback
        self.tr = tr
        self.phase = ""
        self.lo = 0
        self.hi = 100
        self._last_pct = -1
        self._last_text = ""

    def begin(self, phase, lo, hi):
        self.phase = phase
        self.lo = lo
        self.hi = hi
        self.tick(0, 1, "starting…")

    def tick(self, done, total, detail=""):
        total = max(float(total), 1e-9)
        frac = max(0.0, min(1.0, float(done) / total))
        pct = int(self.lo + (self.hi - self.lo) * frac)
        pct = max(0, min(99, pct))
        text = self.tr(f"{self.phase}: {detail}" if detail else self.phase)
        # Always refresh text; refresh bar when percent changes.
        if pct != self._last_pct:
            self._last_pct = pct
            self.feedback.setProgress(pct)
        if text != self._last_text:
            self._last_text = text
            self.feedback.setProgressText(text)


class FlattenRoadMeshAlgorithm(QgsProcessingAlgorithm):
    INPUT_MASKS = "INPUT_MASKS"
    INPUT_OUTLINE_POINTS = "INPUT_OUTLINE_POINTS"
    ALTITUDE_FIELD = "ALTITUDE_FIELD"
    INPUT_MESH = "INPUT_MESH"
    OUTPUT_MESH = "OUTPUT_MESH"
    NEAR_DISTANCE = "NEAR_DISTANCE"
    LOWER_INTERIOR = "LOWER_INTERIOR"
    EDGE_STRIP = "EDGE_STRIP"
    INTERIOR_DROP = "INTERIOR_DROP"
    LOWERING_ONLY = "LOWERING_ONLY"
    SMOOTH_BLEND = "SMOOTH_BLEND"
    WORKERS = "WORKERS"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return FlattenRoadMeshAlgorithm()

    def name(self):
        return "flatten_road_mesh"

    def displayName(self):
        return self.tr("Flatten road masks in quantized mesh")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Copies a quantized-mesh tileset and reshapes terrain under road "
            "mask polygons to match the Unreal RoadPlacer surface:\n\n"
            "1. Outline/curb PointZ on/near the mask (default snap 15 m) or "
            "inside the mask. Interior/center points are ignored "
            "(point_role=center skipped when present).\n"
            "2. Inject mask ring vertices (outer + holes) with Z from the "
            "nearest elevation sample.\n"
            "3. 2D Delaunay on XY; keep triangles whose centroid is inside "
            "the mask; set every mesh vertex inside the mask to that "
            "triangle’s linear Z.\n\n"
            "Optional: lower the interior — either a smooth blend (0 at curb "
            "→ full drop across the edge width) or a stair step (0 in the "
            "outer strip, full drop inside).\n\n"
            "Interior-lowering-only mode skips TIN flatten: point your input "
            "at an already-flattened mesh; outline points are not required.\n\n"
            "Off-mask vertices stay unchanged. Input mesh is never "
            "overwritten. Supports {x}/{y}.terrain and "
            "{level}/{x}/{y}.terrain layouts."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_MASKS,
                self.tr("Road mask polygons"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.INPUT_OUTLINE_POINTS,
                self.tr(
                    "Outline points (PointZ; not needed for lowering-only)"
                ),
                [QgsProcessing.TypeVectorPoint],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.ALTITUDE_FIELD,
                self.tr("Altitude field (not needed for lowering-only)"),
                parentLayerParameterName=self.INPUT_OUTLINE_POINTS,
                type=QgsProcessingParameterField.Numeric,
                defaultValue=ALTITUDE_FIELD_DEFAULT,
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_MESH,
                self.tr("Input quantized-mesh tiles folder"),
                behavior=QgsProcessingParameterFile.Folder,
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_MESH,
                self.tr("Output quantized-mesh tiles folder (copy)"),
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.NEAR_DISTANCE,
                self.tr(
                    "Point near-mask snap (meters; Unreal default 15)"
                ),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=UNREAL_OUTLINE_SNAP_M,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.LOWERING_ONLY,
                self.tr(
                    "Interior lowering only (skip TIN flatten; "
                    "use already-flattened mesh as input)"
                ),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.LOWER_INTERIOR,
                self.tr(
                    "Lower interior under mask"
                ),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SMOOTH_BLEND,
                self.tr(
                    "Smooth edge blend (off = stair: outer strip flat, "
                    "full drop inside)"
                ),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.EDGE_STRIP,
                self.tr(
                    "Edge strip / blend width (meters)"
                ),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=DEFAULT_EDGE_STRIP_M,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.INTERIOR_DROP,
                self.tr(
                    "Max interior drop (meters)"
                ),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=DEFAULT_INTERIOR_DROP_M,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.WORKERS,
                self.tr(
                    "Worker processes (0=auto, 1=serial)"
                ),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=0,
                minValue=0,
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        masks_layer = self.parameterAsVectorLayer(
            parameters, self.INPUT_MASKS, context
        )
        points_source = self.parameterAsSource(
            parameters, self.INPUT_OUTLINE_POINTS, context
        )
        alt_field = self.parameterAsString(
            parameters, self.ALTITUDE_FIELD, context
        )
        mesh_in = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        mesh_out = self.parameterAsString(parameters, self.OUTPUT_MESH, context)
        near_m = self.parameterAsDouble(
            parameters, self.NEAR_DISTANCE, context
        )
        lowering_only = self.parameterAsBool(
            parameters, self.LOWERING_ONLY, context
        )
        lower_interior = self.parameterAsBool(
            parameters, self.LOWER_INTERIOR, context
        )
        edge_strip_m = self.parameterAsDouble(
            parameters, self.EDGE_STRIP, context
        )
        interior_drop_m = self.parameterAsDouble(
            parameters, self.INTERIOR_DROP, context
        )
        smooth_blend = self.parameterAsBool(
            parameters, self.SMOOTH_BLEND, context
        )
        workers_req = self.parameterAsInt(
            parameters, self.WORKERS, context
        )

        if masks_layer is None:
            raise QgsProcessingException(self.tr("Invalid mask layer."))
        if not mesh_in or not os.path.isdir(mesh_in):
            raise QgsProcessingException(self.tr("Invalid input mesh folder."))
        if not mesh_out:
            raise QgsProcessingException(self.tr("Output mesh folder required."))
        if near_m < 0:
            raise QgsProcessingException(self.tr("Near-mask snap must be ≥ 0."))
        if edge_strip_m < 0:
            raise QgsProcessingException(self.tr("Edge blend width must be ≥ 0."))
        if interior_drop_m < 0:
            raise QgsProcessingException(self.tr("Interior drop must be ≥ 0."))

        # Lowering-only always applies the smooth interior carve.
        if lowering_only:
            lower_interior = True
            if interior_drop_m <= 0.0:
                raise QgsProcessingException(
                    self.tr(
                        "Interior lowering only requires "
                        "Max interior drop > 0."
                    )
                )
        elif not lower_interior:
            edge_strip_m = 0.0
            interior_drop_m = 0.0
        elif interior_drop_m <= 0.0:
            feedback.pushInfo(
                self.tr(
                    "Lower-interior toggle is on but drop is 0 — "
                    "terrain will match TIN only (no extra carve)."
                )
            )

        if not lowering_only:
            if points_source is None:
                raise QgsProcessingException(
                    self.tr("Invalid outline points.")
                )
            if not alt_field:
                raise QgsProcessingException(
                    self.tr("Altitude field required.")
                )

        in_path = os.path.abspath(mesh_in)
        out_path = os.path.abspath(mesh_out)
        if os.path.normcase(in_path) == os.path.normcase(out_path):
            raise QgsProcessingException(
                self.tr("Output folder must differ from the input mesh.")
            )
        if os.path.exists(out_path) and os.listdir(out_path):
            raise QgsProcessingException(
                self.tr(
                    "Output folder exists and is not empty. "
                    "Choose an empty or new folder."
                )
            )

        progress = _PhaseProgress(feedback, self.tr)

        # --- Phase 1: copy tileset ---
        if lowering_only:
            progress.begin("1/3 Copy tileset", 0, 20)
        else:
            progress.begin("1/5 Copy tileset", 0, 20)
        os.makedirs(out_path, exist_ok=True)
        _fast_copy_tileset(in_path, out_path, feedback, progress, self.tr)

        # --- Build surface (TIN + optional drop, or drop-only) ---
        if lowering_only:
            feedback.pushInfo(
                self.tr(
                    "Interior-lowering-only mode: skipping Delaunay / TIN "
                    f"flatten; drop −{interior_drop_m} m "
                    f"(strip {edge_strip_m} m, "
                    f"{'smooth' if smooth_blend else 'stair'})."
                )
            )
            progress.begin("2/3 Prepare masks", 20, 40)
            surface = self._build_lowering_surface(
                masks_layer,
                feedback,
                progress,
                interior_drop_m=interior_drop_m,
                edge_blend_m=edge_strip_m,
                smooth_blend=smooth_blend,
            )
            progress.begin("3/3 Lower mesh interiors", 40, 100)
        else:
            progress.begin("2/5 Build road TIN", 20, 50)
            surface = self._build_road_surface(
                masks_layer,
                points_source,
                alt_field,
                near_m,
                feedback,
                progress,
                interior_drop_m=interior_drop_m,
                edge_strip_m=edge_strip_m,
                smooth_blend=smooth_blend,
            )
            if not surface.triangles:
                raise QgsProcessingException(
                    self.tr(
                        "No Delaunay triangles kept inside masks. "
                        "Check outline points, altitude field, and near distance."
                    )
                )
            feedback.pushInfo(
                self.tr(
                    f"Kept {len(surface.triangles)} road triangles from "
                    f"{surface.n_outline_used} samples "
                    f"({surface.n_masks} masks; "
                    f"mask-ring verts={surface.n_ring_verts})."
                )
            )
            progress.begin("5/5 Flatten mesh tiles", 50, 100)

        progress.tick(0, 1, "discovering .terrain files…")
        tiles = discover_terrain_tiles(out_path)
        feedback.pushInfo(self.tr(f"Found {len(tiles)} .terrain tiles."))

        from parallel_util import resolve_workers
        from mesh_flatten_workers import (
            init_tile_worker,
            patch_one_tile,
            snapshot_from_road_surface,
        )

        n_workers = resolve_workers(workers_req)
        feedback.pushInfo(
            self.tr(f"Using {n_workers} worker process(es) for tile patch.")
        )

        # Parent: cheap bbox / mask skips, then farm remaining tiles.
        work = []
        skipped_bbox = 0
        skipped_mask = 0
        for tile_path, level, tx, ty in tiles:
            if feedback.isCanceled():
                break
            west, south, east, north = _tile_bounds_deg(level, tx, ty)
            if not surface.bounds_intersect(west, south, east, north):
                skipped_bbox += 1
                continue
            if not surface.tile_hits_mask(west, south, east, north):
                skipped_mask += 1
                continue
            work.append((str(tile_path), int(level), int(tx), int(ty)))

        examined = len(work)
        changed_tiles = 0
        changed_verts = 0
        snap_data = snapshot_from_road_surface(
            surface,
            utm_zone=getattr(surface, "_utm_zone", 14),
            utm_northern=getattr(surface, "_utm_northern", True),
        )

        if n_workers == 1 or len(work) <= 1:
            init_tile_worker(_SCRIPTS_ROOT, snap_data)
            n_work = max(len(work), 1)
            for ti, task in enumerate(work):
                if feedback.isCanceled():
                    break
                if ti % 25 == 0 or ti + 1 == len(work):
                    progress.tick(
                        ti + 1,
                        n_work,
                        (
                            f"tile {ti + 1}/{len(work)} "
                            f"patched={changed_tiles} verts={changed_verts}"
                        ),
                    )
                changed, nverts, err = patch_one_tile(task)
                if err:
                    feedback.pushWarning(self.tr(err))
                if changed:
                    changed_tiles += 1
                    changed_verts += nverts
        else:
            from concurrent.futures import ProcessPoolExecutor, as_completed

            n_work = max(len(work), 1)
            progress.tick(0, n_work, f"0/{len(work)} tiles")
            with ProcessPoolExecutor(
                max_workers=n_workers,
                initializer=init_tile_worker,
                initargs=(_SCRIPTS_ROOT, snap_data),
            ) as pool:
                futures = {
                    pool.submit(patch_one_tile, task): task for task in work
                }
                done = 0
                for fut in as_completed(futures):
                    if feedback.isCanceled():
                        for f in futures:
                            f.cancel()
                        break
                    changed, nverts, err = fut.result()
                    done += 1
                    if err:
                        feedback.pushWarning(self.tr(err))
                    if changed:
                        changed_tiles += 1
                        changed_verts += nverts
                    if done % 10 == 0 or done == len(work):
                        progress.tick(
                            done,
                            n_work,
                            (
                                f"tile {done}/{len(work)} "
                                f"patched={changed_tiles} "
                                f"verts={changed_verts}"
                            ),
                        )

        feedback.setProgress(100)
        feedback.setProgressText(self.tr("Finished"))
        mode = "lowering-only" if lowering_only else "flatten"
        feedback.pushInfo(
            self.tr(
                f"Done ({mode}). Copied mesh to {out_path}. "
                f"Examined {examined} tiles that hit masks, "
                f"patched {changed_tiles} tiles, {changed_verts} vertices "
                f"(bbox-skipped {skipped_bbox}, mask-skipped {skipped_mask})."
            )
        )
        return {self.OUTPUT_MESH: out_path}

    def _build_road_surface(
        self,
        masks_layer,
        points_source,
        alt_field,
        near_m,
        feedback,
        progress,
        interior_drop_m=0.0,
        edge_strip_m=0.0,
        smooth_blend=True,
    ):
        """
        Build the same sample set + Delaunay rules as RoadPlacer:
        snap/inside outline selection, inject mask rings, no interior points.

        If interior_drop_m > 0, subtract a drop based on distance to the mask
        boundary (smooth blend or stair step over edge_strip_m).
        """
        mask_crs = masks_layer.sourceCrs()
        point_crs = points_source.sourceCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        metric_crs = self._metric_crs(masks_layer, feedback)

        to_metric_mask = QgsCoordinateTransform(
            mask_crs, metric_crs, QgsProject.instance()
        )
        to_wgs_mask = QgsCoordinateTransform(
            mask_crs, wgs84, QgsProject.instance()
        )
        to_metric_pts = QgsCoordinateTransform(
            point_crs, metric_crs, QgsProject.instance()
        )
        to_wgs_pts = QgsCoordinateTransform(
            point_crs, wgs84, QgsProject.instance()
        )

        fields = points_source.fields()
        alt_idx = fields.indexOf(alt_field)
        if alt_idx < 0:
            raise QgsProcessingException(
                self.tr(f"Altitude field '{alt_field}' not found.")
            )
        role_idx = fields.indexOf(ROLE_FIELD)

        # Prepare mask geometries (metric + WGS84) once.
        progress.begin("2/5 Prepare road masks", 20, 24)
        to_wgs_from_metric = QgsCoordinateTransform(
            metric_crs, wgs84, QgsProject.instance()
        )
        masks_metric = []
        masks_wgs = []
        mask_boundaries = []
        mask_ring_verts = []  # per-mask list of (mx, my, lon, lat)
        mask_feats = list(masks_layer.getFeatures())
        n_mask_feats = max(len(mask_feats), 1)
        for mi, feat in enumerate(mask_feats):
            if feedback.isCanceled():
                break
            if mi % 20 == 0 or mi + 1 == n_mask_feats:
                progress.tick(
                    mi + 1,
                    n_mask_feats,
                    f"mask geom {mi + 1}/{n_mask_feats}",
                )
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            metric_geom = QgsGeometry(geom)
            if metric_geom.transform(to_metric_mask) != 0:
                continue
            metric_geom = metric_geom.makeValid()
            if metric_geom.isEmpty():
                continue
            wgs_geom = QgsGeometry(geom)
            if wgs_geom.transform(to_wgs_mask) != 0:
                continue
            wgs_geom = wgs_geom.makeValid()
            if wgs_geom.isEmpty():
                continue
            boundary = metric_geom.convertToType(
                QgsWkbTypes.LineGeometry, True
            )
            if boundary is None or boundary.isEmpty():
                boundary = QgsGeometry(metric_geom)
            ring_pts = []
            for ring in _iter_polygon_rings_xy(metric_geom):
                for mx, my in ring:
                    wgs_pt = to_wgs_from_metric.transform(QgsPointXY(mx, my))
                    ring_pts.append((mx, my, wgs_pt.x(), wgs_pt.y()))
            masks_metric.append(metric_geom)
            masks_wgs.append(wgs_geom)
            mask_boundaries.append(boundary)
            mask_ring_verts.append(ring_pts)

        if not masks_metric:
            raise QgsProcessingException(self.tr("No usable mask polygons."))

        # --- Load PointZ candidates ---
        progress.begin("3/5 Load elevation points", 24, 34)
        n_pts_declared = points_source.featureCount()
        n_pts_total = n_pts_declared if n_pts_declared > 0 else None
        raw_pts = []  # (mx, my, lon, lat, z)
        scanned = 0
        for feat in points_source.getFeatures():
            if feedback.isCanceled():
                break
            scanned += 1
            if scanned % 2000 == 0 or (
                n_pts_total and scanned == n_pts_total
            ):
                if n_pts_total:
                    progress.tick(
                        scanned,
                        n_pts_total,
                        f"{scanned}/{n_pts_total} features "
                        f"(raw {len(raw_pts)})",
                    )
                else:
                    progress.tick(
                        scanned,
                        max(scanned, 1),
                        f"scanned {scanned} (raw {len(raw_pts)})",
                    )
            if role_idx >= 0:
                role = feat.attribute(role_idx)
                if role is not None and str(role).lower() == "center":
                    continue
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            pt = geom.asPoint()
            try:
                z = float(feat.attribute(alt_idx))
            except (TypeError, ValueError):
                z = pt.z() if hasattr(pt, "z") else None
            if z is None or not math.isfinite(z):
                continue
            src_xy = QgsPointXY(pt.x(), pt.y())
            mxy = to_metric_pts.transform(src_xy)
            wgs = to_wgs_pts.transform(src_xy)
            raw_pts.append((mxy.x(), mxy.y(), wgs.x(), wgs.y(), z))

        if len(raw_pts) < 3:
            raise QgsProcessingException(
                self.tr("Need at least 3 elevation points with valid altitude.")
            )

        # Spatial index of masks for point-in / near tests.
        mask_index = QgsSpatialIndex()
        for i, g in enumerate(masks_metric):
            f = QgsFeature(i + 1)
            f.setGeometry(g)
            mask_index.addFeature(f)

        # Select like Unreal: inside mask OR within near_m of outline.
        progress.tick(0, 1, "selecting outline points on/near masks…")
        selected = []
        snap_buf = max(near_m, 0.0)
        for pi, (mx, my, lon, lat, z) in enumerate(raw_pts):
            if feedback.isCanceled():
                break
            if pi % 5000 == 0:
                progress.tick(
                    pi + 1,
                    max(len(raw_pts), 1),
                    f"select {pi + 1}/{len(raw_pts)} kept={len(selected)}",
                )
            pt = QgsGeometry.fromPointXY(QgsPointXY(mx, my))
            keep = False
            for fid in mask_index.intersects(pt.boundingBox()):
                gi = fid - 1
                if gi < 0 or gi >= len(masks_metric):
                    continue
                g = masks_metric[gi]
                if g.intersects(pt):
                    keep = True
                    break
                if snap_buf > 0:
                    d = mask_boundaries[gi].distance(pt)
                    if d <= snap_buf:
                        keep = True
                        break
            if keep:
                selected.append((mx, my, lon, lat, z))

        if len(selected) < 3:
            raise QgsProcessingException(
                self.tr(
                    "Fewer than 3 elevation samples sit on or near the road "
                    "mask (Unreal-style snap)."
                )
            )
        feedback.pushInfo(
            self.tr(
                f"Using {len(selected)} outline elevation sample(s) "
                f"(snap={near_m} m)."
            )
        )

        # Inject mask ring vertices with nearest known Z (CollectMaskSamples +
        # FillMissingHeights).
        progress.begin("4/5 Inject mask rings + Delaunay", 34, 50)
        known_for_fill = list(selected)
        z_grid, z_cell = _build_nearest_z_grid(known_for_fill, cell=max(near_m, 5.0))
        n_ring_verts = 0
        ring_samples_by_mask = []
        n_masks_rings = max(len(mask_ring_verts), 1)
        total_ring_pts = max(sum(len(r) for r in mask_ring_verts), 1)
        done_ring_pts = 0
        for mi, ring_pts in enumerate(mask_ring_verts):
            if feedback.isCanceled():
                break
            filled = []
            for mx, my, lon, lat in ring_pts:
                z = _nearest_z(mx, my, known_for_fill, z_grid, z_cell)
                done_ring_pts += 1
                if z is None:
                    continue
                filled.append((mx, my, lon, lat, z))
                n_ring_verts += 1
            ring_samples_by_mask.append(filled)
            if mi % 10 == 0 or mi + 1 == n_masks_rings:
                progress.tick(
                    done_ring_pts,
                    total_ring_pts,
                    (
                        f"fill ring Z mask {mi + 1}/{n_masks_rings} "
                        f"({done_ring_pts}/{total_ring_pts} verts, "
                        f"kept={n_ring_verts})"
                    ),
                )
        feedback.pushInfo(
            self.tr(
                f"Injected {n_ring_verts} mask-ring vertices with nearest Z."
            )
        )

        # Bucket all elevation samples (pointZ + will merge rings per mask).
        cell = max(near_m, 1.0)
        buckets = {}
        for i, (mx, my, _lo, _la, _z) in enumerate(selected):
            key = (int(math.floor(mx / cell)), int(math.floor(my / cell)))
            buckets.setdefault(key, []).append(i)

        def samples_for_mask(mi, metric_geom):
            """PointZ near/inside this mask + this mask's ring vertices."""
            rect = metric_geom.boundingBox()
            if snap_buf > 0:
                rect.grow(snap_buf)
            x0 = int(math.floor(rect.xMinimum() / cell))
            x1 = int(math.floor(rect.xMaximum() / cell))
            y0 = int(math.floor(rect.yMinimum() / cell))
            y1 = int(math.floor(rect.yMaximum() / cell))
            boundary = mask_boundaries[mi]
            out = []
            seen = set()
            for ix in range(x0, x1 + 1):
                for iy in range(y0, y1 + 1):
                    for pi in buckets.get((ix, iy), []):
                        mx, my, lon, lat, z = selected[pi]
                        pt = QgsGeometry.fromPointXY(QgsPointXY(mx, my))
                        if metric_geom.intersects(pt) or (
                            snap_buf > 0 and boundary.distance(pt) <= snap_buf
                        ):
                            key = (round(mx, 3), round(my, 3))
                            if key in seen:
                                continue
                            seen.add(key)
                            out.append((mx, my, lon, lat, z))
            for mx, my, lon, lat, z in ring_samples_by_mask[mi]:
                key = (round(mx, 3), round(my, 3))
                if key in seen:
                    continue
                seen.add(key)
                out.append((mx, my, lon, lat, z))
            return out

        triangles = []
        n_outline_used = 0
        n_masks = 0
        n_masks_total = max(len(masks_metric), 1)
        for mi, metric_geom in enumerate(masks_metric):
            if feedback.isCanceled():
                break
            samples = samples_for_mask(mi, metric_geom)
            if len(samples) < 3:
                progress.tick(
                    mi + 1,
                    n_masks_total,
                    f"skip mask {mi + 1}/{n_masks_total} (few samples)",
                )
                continue
            if len(samples) > 2500:
                before = len(samples)
                samples = _thin_samples_metric(samples, cell_m=2.0)
                if before != len(samples):
                    feedback.pushInfo(
                        self.tr(
                            f"Mask {mi + 1}: thinned {before} → {len(samples)} "
                            f"samples for Delaunay (2 m grid)."
                        )
                    )
            progress.tick(
                mi + 1,
                n_masks_total,
                (
                    f"Delaunay mask {mi + 1}/{n_masks_total} "
                    f"samples={len(samples)} "
                    f"triangles={len(triangles)}"
                ),
            )
            pts_m = [(s[0], s[1]) for s in samples]
            pts_llz = [(s[2], s[3], s[4]) for s in samples]
            simplices = _delaunay_simplices(pts_m, feedback)
            if not simplices:
                continue
            n_masks += 1
            n_outline_used += len(samples)
            progress.tick(
                mi + 1,
                n_masks_total,
                (
                    f"clip triangles mask {mi + 1}/{n_masks_total} "
                    f"raw_tris={len(simplices)} "
                    f"kept={len(triangles)}"
                ),
            )
            for ia, ib, ic in simplices:
                lon0, lat0, z0 = pts_llz[ia]
                lon1, lat1, z1 = pts_llz[ib]
                lon2, lat2, z2 = pts_llz[ic]
                cx = (pts_m[ia][0] + pts_m[ib][0] + pts_m[ic][0]) / 3.0
                cy = (pts_m[ia][1] + pts_m[ib][1] + pts_m[ic][1]) / 3.0
                cpt = QgsGeometry.fromPointXY(QgsPointXY(cx, cy))
                if not metric_geom.intersects(cpt):
                    continue
                triangles.append(
                    {
                        "lon": (lon0, lon1, lon2),
                        "lat": (lat0, lat1, lat2),
                        "z": (z0, z1, z2),
                        "bbox": (
                            min(lon0, lon1, lon2),
                            min(lat0, lat1, lat2),
                            max(lon0, lon1, lon2),
                            max(lat0, lat1, lat2),
                        ),
                    }
                )

        # Distance-based interior drop on _RoadSurface.
        if interior_drop_m > 0.0:
            mode = "smooth blend" if smooth_blend else "stair step"
            feedback.pushInfo(
                self.tr(
                    f"Interior drop enabled: up to −{interior_drop_m} m "
                    f"below TIN ({mode}, strip {edge_strip_m} m)."
                )
            )

        progress.tick(
            1,
            1,
            f"building spatial index ({len(triangles)} triangles)…",
        )
        return _RoadSurface(
            triangles,
            masks_wgs,
            n_outline_used,
            n_masks,
            n_ring_verts,
            progress,
            interior_drop_m=interior_drop_m,
            edge_blend_m=edge_strip_m,
            masks_metric=masks_metric,
            metric_crs=metric_crs,
            smooth_blend=smooth_blend,
        )

    def _build_lowering_surface(
        self,
        masks_layer,
        feedback,
        progress,
        interior_drop_m=0.0,
        edge_blend_m=0.0,
        smooth_blend=True,
    ):
        """Masks-only surface: subtract an interior drop from existing Z."""
        mask_crs = masks_layer.sourceCrs()
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        metric_crs = self._metric_crs(masks_layer, feedback)
        to_metric_mask = QgsCoordinateTransform(
            mask_crs, metric_crs, QgsProject.instance()
        )
        to_wgs_mask = QgsCoordinateTransform(
            mask_crs, wgs84, QgsProject.instance()
        )

        masks_metric = []
        masks_wgs = []
        mask_feats = list(masks_layer.getFeatures())
        n_mask_feats = max(len(mask_feats), 1)
        for mi, feat in enumerate(mask_feats):
            if feedback.isCanceled():
                break
            if mi % 20 == 0 or mi + 1 == n_mask_feats:
                progress.tick(
                    mi + 1,
                    n_mask_feats,
                    f"mask geom {mi + 1}/{n_mask_feats}",
                )
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            metric_geom = QgsGeometry(geom)
            if metric_geom.transform(to_metric_mask) != 0:
                continue
            metric_geom = metric_geom.makeValid()
            if metric_geom.isEmpty():
                continue
            wgs_geom = QgsGeometry(geom)
            if wgs_geom.transform(to_wgs_mask) != 0:
                continue
            wgs_geom = wgs_geom.makeValid()
            if wgs_geom.isEmpty():
                continue
            masks_metric.append(metric_geom)
            masks_wgs.append(wgs_geom)

        if not masks_metric:
            raise QgsProcessingException(self.tr("No usable mask polygons."))

        return _RoadSurface(
            [],
            masks_wgs,
            0,
            len(masks_wgs),
            0,
            progress,
            interior_drop_m=interior_drop_m,
            edge_blend_m=edge_blend_m,
            masks_metric=masks_metric,
            metric_crs=metric_crs,
            lowering_only=True,
            smooth_blend=smooth_blend,
        )

    @staticmethod
    def _metric_crs(layer, feedback):
        crs = layer.sourceCrs()
        if (
            crs.isValid()
            and not crs.isGeographic()
            and crs.mapUnits() == QgsUnitTypes.DistanceMeters
        ):
            return crs
        extent = layer.extent()
        cx = 0.5 * (extent.xMinimum() + extent.xMaximum())
        cy = 0.5 * (extent.yMinimum() + extent.yMaximum())
        if crs.isGeographic():
            lon, lat = cx, cy
        else:
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            to_wgs = QgsCoordinateTransform(
                crs, wgs84, QgsProject.instance()
            )
            pt = to_wgs.transform(QgsPointXY(cx, cy))
            lon, lat = pt.x(), pt.y()
        zone = int(math.floor((lon + 180.0) / 6.0) + 1)
        epsg = (32600 + zone) if lat >= 0 else (32700 + zone)
        metric = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
        if not metric.isValid():
            metric = QgsCoordinateReferenceSystem("EPSG:3857")
        feedback.pushInfo(
            f"Using {metric.authid()} for Delaunay / near-distance."
        )
        return metric


class _RoadSurface:
    def __init__(
        self,
        triangles,
        mask_geoms_wgs,
        n_outline_used,
        n_masks,
        n_ring_verts=0,
        progress=None,
        interior_drop_m=0.0,
        edge_blend_m=0.0,
        masks_metric=None,
        metric_crs=None,
        lowering_only=False,
        smooth_blend=True,
    ):
        self.triangles = triangles
        self.mask_geoms_wgs = mask_geoms_wgs
        self.n_outline_used = n_outline_used
        self.n_masks = n_masks
        self.n_ring_verts = n_ring_verts
        self._interior_drop_m = max(float(interior_drop_m), 0.0)
        self._edge_blend_m = max(float(edge_blend_m), 0.0)
        self._lowering_only = bool(lowering_only)
        self._smooth_blend = bool(smooth_blend)
        self._utm_zone = 14
        self._utm_northern = True
        if metric_crs is not None and metric_crs.isValid():
            auth = metric_crs.authid() or ""
            if auth.startswith("EPSG:"):
                try:
                    code = int(auth.split(":")[1])
                    if 32601 <= code <= 32660:
                        self._utm_zone = code - 32600
                        self._utm_northern = True
                    elif 32701 <= code <= 32760:
                        self._utm_zone = code - 32700
                        self._utm_northern = False
                except ValueError:
                    pass
        self._to_metric = None
        if metric_crs is not None and metric_crs.isValid():
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            self._to_metric = QgsCoordinateTransform(
                wgs84, metric_crs, QgsProject.instance()
            )

        self._index = QgsSpatialIndex()
        self._by_id = {}
        n_tri = max(len(triangles), 1)
        for i, tri in enumerate(triangles):
            west, south, east, north = tri["bbox"]
            pad = 1e-7
            ring = [
                QgsPointXY(west - pad, south - pad),
                QgsPointXY(east + pad, south - pad),
                QgsPointXY(east + pad, north + pad),
                QgsPointXY(west - pad, north + pad),
                QgsPointXY(west - pad, south - pad),
            ]
            feat = QgsFeature(i + 1)
            feat.setGeometry(QgsGeometry.fromPolygonXY([ring]))
            self._index.addFeature(feat)
            self._by_id[i + 1] = i
            if progress is not None and (i % 500 == 0 or i + 1 == n_tri):
                progress.tick(
                    i + 1,
                    n_tri,
                    f"index triangles {i + 1}/{n_tri}",
                )

        self._mask_index = QgsSpatialIndex()
        self._mask_by_id = {}
        self._mask_rings = {}
        self._mask_metric_rings = {}
        metric_list = masks_metric or []
        n_masks_g = max(len(mask_geoms_wgs), 1)
        for i, g in enumerate(mask_geoms_wgs):
            feat = QgsFeature(i + 1)
            # Rings used for fast per-vertex PIP; GEOS prepare is not available
            # on all QGIS builds (QgsGeometry.prepareGeometry missing).
            gg = QgsGeometry(g)
            feat.setGeometry(gg)
            self._mask_index.addFeature(feat)
            self._mask_by_id[i + 1] = gg
            self._mask_rings[i + 1] = _geom_rings_xy(gg)
            if i < len(metric_list):
                self._mask_metric_rings[i + 1] = _geom_rings_xy(metric_list[i])
            if progress is not None and (i % 100 == 0 or i + 1 == n_masks_g):
                progress.tick(
                    i + 1,
                    n_masks_g,
                    f"index masks {i + 1}/{n_masks_g}",
                )

        if triangles:
            ws = min(t["bbox"][0] for t in triangles)
            ss = min(t["bbox"][1] for t in triangles)
            es = max(t["bbox"][2] for t in triangles)
            ns = max(t["bbox"][3] for t in triangles)
            self._extent = (ws, ss, es, ns)
        elif mask_geoms_wgs:
            ws = min(g.boundingBox().xMinimum() for g in mask_geoms_wgs)
            ss = min(g.boundingBox().yMinimum() for g in mask_geoms_wgs)
            es = max(g.boundingBox().xMaximum() for g in mask_geoms_wgs)
            ns = max(g.boundingBox().yMaximum() for g in mask_geoms_wgs)
            self._extent = (ws, ss, es, ns)
        else:
            self._extent = None

    def bounds_intersect(self, west, south, east, north):
        if self._extent is None:
            return False
        ws, ss, es, ns = self._extent
        return not (east < ws or west > es or north < ss or south > ns)

    def tile_hits_mask(self, west, south, east, north):
        """True if this tile rectangle intersects any road mask."""
        rect = QgsRectangle(west, south, east, north)
        tile_geom = QgsGeometry.fromRect(rect)
        for fid in self._mask_index.intersects(rect):
            g = self._mask_by_id.get(fid)
            if g is not None and g.intersects(tile_geom):
                return True
        return False

    def _containing_mask_fid(self, lon, lat):
        rect = QgsRectangle(lon, lat, lon, lat)
        for fid in self._mask_index.intersects(rect):
            rings = self._mask_rings.get(fid)
            if rings and _point_in_rings(lon, lat, rings):
                return fid
        return None

    def _blended_drop(self, lon, lat, mask_fid):
        """
        Drop amount at lon/lat.

        Smooth (default): 0 at curb → full drop beyond edge strip (smoothstep).
        Stair: 0 inside the outer strip, full drop deeper inside.
        """
        if self._interior_drop_m <= 0.0:
            return 0.0
        if self._edge_blend_m <= 0.0 or self._to_metric is None:
            return self._interior_drop_m
        rings = self._mask_metric_rings.get(mask_fid)
        if not rings:
            return self._interior_drop_m
        mxy = self._to_metric.transform(QgsPointXY(lon, lat))
        dist_m = _min_dist_to_rings(mxy.x(), mxy.y(), rings)
        if self._smooth_blend:
            t = max(0.0, min(1.0, dist_m / self._edge_blend_m))
            t = t * t * (3.0 - 2.0 * t)
            return self._interior_drop_m * t
        # Stair: keep strip at TIN / current Z; full drop past strip.
        if dist_m < self._edge_blend_m:
            return 0.0
        return self._interior_drop_m

    def sample_z(self, lon, lat, current_z=None):
        mask_fid = self._containing_mask_fid(lon, lat)
        if mask_fid is None:
            return None
        drop = self._blended_drop(lon, lat, mask_fid)
        if self._lowering_only or not self.triangles:
            if current_z is None:
                return None
            return float(current_z) - drop
        z = self._tin_z(lon, lat)
        if z is None:
            return None
        return z - drop

    def _tin_z(self, lon, lat):
        candidates = self._index.intersects(QgsRectangle(lon, lat, lon, lat))
        best = None
        best_d = None
        for fid in candidates:
            ti = self._by_id.get(fid)
            if ti is None:
                continue
            tri = self.triangles[ti]
            w = _barycentric(
                lon,
                lat,
                tri["lon"][0],
                tri["lat"][0],
                tri["lon"][1],
                tri["lat"][1],
                tri["lon"][2],
                tri["lat"][2],
            )
            if w is not None:
                w1, w2, w3 = w
                return (
                    w1 * tri["z"][0]
                    + w2 * tri["z"][1]
                    + w3 * tri["z"][2]
                )
            cx = sum(tri["lon"]) / 3.0
            cy = sum(tri["lat"]) / 3.0
            d = (cx - lon) ** 2 + (cy - lat) ** 2
            if best_d is None or d < best_d:
                best_d = d
                best = tri
        if best is None:
            return None
        w = _barycentric_clamped(
            lon,
            lat,
            best["lon"][0],
            best["lat"][0],
            best["lon"][1],
            best["lat"][1],
            best["lon"][2],
            best["lat"][2],
        )
        if w is None:
            return None
        w1, w2, w3 = w
        return w1 * best["z"][0] + w2 * best["z"][1] + w3 * best["z"][2]


def _iter_polygon_rings_xy(metric_poly):
    """Yield rings as lists of (x, y) for Polygon / MultiPolygon."""
    flat = QgsWkbTypes.flatType(metric_poly.wkbType())
    if flat == QgsWkbTypes.Polygon:
        polygon = metric_poly.asPolygon()
        if not polygon:
            return
        for ring in polygon:
            if ring:
                yield [(p.x(), p.y()) for p in ring]
        return
    if flat == QgsWkbTypes.MultiPolygon:
        multi = metric_poly.asMultiPolygon()
        if not multi:
            return
        for polygon in multi:
            for ring in polygon:
                if ring:
                    yield [(p.x(), p.y()) for p in ring]


def _geom_rings_xy(geom):
    """All rings (exterior + holes) as (x, y) lists for even-odd PIP."""
    return list(_iter_polygon_rings_xy(geom))


def _point_in_rings(x, y, rings):
    """Even-odd point-in-polygon over one or more rings (holes flip)."""
    inside = False
    for ring in rings:
        n = len(ring)
        if n < 3:
            continue
        j = n - 1
        for i in range(n):
            xi, yi = ring[i]
            xj, yj = ring[j]
            if ((yi > y) != (yj > y)) and (
                x < (xj - xi) * (y - yi) / ((yj - yi) or 1e-30) + xi
            ):
                inside = not inside
            j = i
    return inside


def _point_segment_dist2(px, py, ax, ay, bx, by):
    abx = bx - ax
    aby = by - ay
    apx = px - ax
    apy = py - ay
    ab2 = abx * abx + aby * aby
    if ab2 <= 1e-18:
        dx = px - ax
        dy = py - ay
        return dx * dx + dy * dy
    t = max(0.0, min(1.0, (apx * abx + apy * aby) / ab2))
    dx = px - (ax + t * abx)
    dy = py - (ay + t * aby)
    return dx * dx + dy * dy


def _min_dist_to_rings(x, y, rings):
    """Minimum Euclidean distance from point to any ring edge."""
    best = None
    for ring in rings:
        n = len(ring)
        if n < 2:
            continue
        # Rings from QGIS usually repeat the first vertex at the end.
        limit = n - 1 if ring[0] == ring[-1] else n
        for i in range(limit):
            ax, ay = ring[i]
            bx, by = ring[(i + 1) % n]
            d2 = _point_segment_dist2(x, y, ax, ay, bx, by)
            if best is None or d2 < best:
                best = d2
    if best is None:
        return 0.0
    return math.sqrt(best)


def _fast_copy_tileset(src_root, dst_root, feedback, progress, tr):
    """
    Copy a quantized-mesh tree as fast as practical.

    Windows: multithreaded robocopy (many small .terrain files).
    Fallback: shutil.copytree with copyfile (no metadata).
    """
    progress.tick(0, 1, "copying tileset…")
    if feedback.isCanceled():
        raise QgsProcessingException(tr("Canceled during copy."))

    if os.name == "nt":
        # /MT: parallel copy; quiet flags; /E = subdirs including empty.
        # Robocopy exit codes 0–7 are success (bit flags); >= 8 is failure.
        cmd = [
            "robocopy",
            src_root,
            dst_root,
            "/E",
            "/MT:16",
            "/R:1",
            "/W:1",
            "/NFL",
            "/NDL",
            "/NJH",
            "/NJS",
            "/NC",
            "/NS",
            "/NP",
        ]
        progress.tick(0, 1, "robocopy /MT:16…")
        try:
            completed = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=False,
            )
            if completed.returncode < 8:
                feedback.pushInfo(
                    tr(
                        f"Tileset copy via robocopy "
                        f"(exit {completed.returncode})."
                    )
                )
                progress.tick(1, 1, "copy done")
                return
            feedback.pushWarning(
                tr(
                    f"robocopy failed (exit {completed.returncode}); "
                    "falling back to Python copytree."
                )
            )
        except OSError as exc:
            feedback.pushWarning(
                tr(f"robocopy unavailable ({exc}); using Python copytree.")
            )

    progress.tick(0, 1, "copytree…")
    # dirs_exist_ok: destination may already be an empty folder we created.
    shutil.copytree(
        src_root,
        dst_root,
        dirs_exist_ok=True,
        copy_function=shutil.copyfile,
    )
    if feedback.isCanceled():
        raise QgsProcessingException(tr("Canceled during copy."))
    progress.tick(1, 1, "copy done")


def _build_nearest_z_grid(samples, cell):
    """Spatial hash for fast nearest-Z lookups (metric XY)."""
    cell = max(float(cell), 1.0)
    grid = {}
    for i, (mx, my, _lon, _lat, _z) in enumerate(samples):
        key = (int(math.floor(mx / cell)), int(math.floor(my / cell)))
        grid.setdefault(key, []).append(i)
    return grid, cell


def _nearest_z(mx, my, samples, grid=None, cell=None):
    if not samples:
        return None
    if grid is None or cell is None:
        best_d = None
        best_z = None
        for sx, sy, _lon, _lat, z in samples:
            d = (sx - mx) ** 2 + (sy - my) ** 2
            if best_d is None or d < best_d:
                best_d = d
                best_z = z
        return best_z

    cx = int(math.floor(mx / cell))
    cy = int(math.floor(my / cell))
    best_d = None
    best_z = None
    for r in range(0, 64):
        found = False
        for dy in range(-r, r + 1):
            for dx in range(-r, r + 1):
                if r > 0 and abs(dx) != r and abs(dy) != r:
                    continue
                for i in grid.get((cx + dx, cy + dy), []):
                    sx, sy, _lon, _lat, z = samples[i]
                    d = (sx - mx) ** 2 + (sy - my) ** 2
                    if best_d is None or d < best_d:
                        best_d = d
                        best_z = z
                        found = True
        if found:
            break
    if best_z is not None:
        return best_z
    # Fallback: rare empty-grid case.
    return _nearest_z(mx, my, samples)


def _barycentric(px, py, ax, ay, bx, by, cx, cy):
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if den == 0.0:
        return None
    w1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
    w2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
    w3 = 1.0 - w1 - w2
    eps = -1e-10
    if w1 < eps or w2 < eps or w3 < eps:
        return None
    return w1, w2, w3


def _barycentric_clamped(px, py, ax, ay, bx, by, cx, cy):
    den = (by - cy) * (ax - cx) + (cx - bx) * (ay - cy)
    if den == 0.0:
        return None
    w1 = ((by - cy) * (px - cx) + (cx - bx) * (py - cy)) / den
    w2 = ((cy - ay) * (px - cx) + (ax - cx) * (py - cy)) / den
    w3 = 1.0 - w1 - w2
    w1 = max(0.0, w1)
    w2 = max(0.0, w2)
    w3 = max(0.0, w3)
    s = w1 + w2 + w3
    if s <= 0.0:
        return None
    return w1 / s, w2 / s, w3 / s


def _thin_samples_metric(samples, cell_m=2.0):
    """Keep one sample per metric grid cell (faster Delaunay on dense outlines)."""
    cell_m = max(float(cell_m), 0.1)
    best = {}
    for s in samples:
        key = (int(math.floor(s[0] / cell_m)), int(math.floor(s[1] / cell_m)))
        if key not in best:
            best[key] = s
    return list(best.values())


_DELAUNAY_BACKEND_LOGGED = False


def _delaunay_simplices(points_xy, feedback=None):
    """
    Return list of (i,j,k) vertex index triples.

    Prefers SciPy, then QGIS native/qgis Delaunay, then a tiny pure-Python
    fallback only for small point sets.
    """
    global _DELAUNAY_BACKEND_LOGGED
    n = len(points_xy)
    if n < 3:
        return []

    def _log(msg):
        global _DELAUNAY_BACKEND_LOGGED
        if feedback is not None and not _DELAUNAY_BACKEND_LOGGED:
            feedback.pushInfo(msg)
            _DELAUNAY_BACKEND_LOGGED = True

    try:
        import numpy as np
        from scipy.spatial import Delaunay

        arr = np.asarray(points_xy, dtype=float)
        tri = Delaunay(arr)
        _log(f"Delaunay backend: SciPy ({n} points).")
        return [tuple(map(int, s)) for s in tri.simplices]
    except Exception as exc:
        if feedback is not None and not _DELAUNAY_BACKEND_LOGGED:
            feedback.pushInfo(
                f"SciPy Delaunay unavailable ({exc}); trying QGIS Delaunay."
            )

    try:
        simplices = _delaunay_simplices_qgis(points_xy)
        if simplices:
            _log(f"Delaunay backend: QGIS processing ({n} points).")
            return simplices
    except Exception as exc:
        if feedback is not None:
            feedback.pushWarning(f"QGIS Delaunay failed: {exc}")

    if n > 800:
        if feedback is not None:
            feedback.pushWarning(
                f"No fast Delaunay backend for {n} points. "
                "Install SciPy into QGIS Python, or expect a very slow run. "
                "Skipping this mask's triangulation."
            )
        return []

    _log(f"Delaunay backend: pure-Python fallback ({n} points).")
    return _delaunay_simplices_bowyer(points_xy)


def _delaunay_simplices_qgis(points_xy):
    """Fast Delaunay via QGIS Processing (no SciPy required)."""
    import processing
    from qgis.PyQt.QtCore import QVariant
    from qgis.core import QgsField, QgsFields, QgsVectorLayer

    layer = QgsVectorLayer("Point?crs=EPSG:3857", "road_delaunay_in", "memory")
    if not layer.isValid():
        raise RuntimeError("Could not create memory point layer.")
    fields = QgsFields()
    fields.append(QgsField("sid", QVariant.Int))
    prov = layer.dataProvider()
    prov.addAttributes(fields)
    layer.updateFields()

    key_to_i = {}
    feats = []
    for i, (x, y) in enumerate(points_xy):
        f = QgsFeature(layer.fields())
        f.setGeometry(QgsGeometry.fromPointXY(QgsPointXY(float(x), float(y))))
        f.setAttributes([i])
        feats.append(f)
        key_to_i[(round(float(x), 4), round(float(y), 4))] = i
    prov.addFeatures(feats)
    layer.updateExtents()

    result = None
    last_err = None
    for alg_id in (
        "native:delaunaytriangulation",
        "qgis:delaunaytriangulation",
    ):
        try:
            result = processing.run(
                alg_id,
                {"INPUT": layer, "OUTPUT": "memory:"},
            )
            break
        except Exception as exc:
            last_err = exc
            result = None
    if result is None:
        raise RuntimeError(f"Delaunay algorithm failed: {last_err}")

    out = result.get("OUTPUT")
    if out is None:
        raise RuntimeError("Delaunay produced no output layer.")

    def lookup(x, y):
        key = (round(float(x), 4), round(float(y), 4))
        idx = key_to_i.get(key)
        if idx is not None:
            return idx
        # Rare numeric drift: nearest input.
        best_i = 0
        best_d = None
        for i, (px, py) in enumerate(points_xy):
            d = (px - x) ** 2 + (py - y) ** 2
            if best_d is None or d < best_d:
                best_d = d
                best_i = i
        return best_i

    simplices = []
    for feat in out.getFeatures():
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        ring = None
        flat = QgsWkbTypes.flatType(geom.wkbType())
        if flat == QgsWkbTypes.Polygon:
            poly = geom.asPolygon()
            if poly:
                ring = poly[0]
        elif flat == QgsWkbTypes.MultiPolygon:
            multi = geom.asMultiPolygon()
            if multi and multi[0]:
                ring = multi[0][0]
        if not ring or len(ring) < 3:
            continue
        pts = ring[:-1] if (
            abs(ring[0].x() - ring[-1].x()) < 1e-9
            and abs(ring[0].y() - ring[-1].y()) < 1e-9
        ) else ring
        if len(pts) < 3:
            continue
        ia = lookup(pts[0].x(), pts[0].y())
        ib = lookup(pts[1].x(), pts[1].y())
        ic = lookup(pts[2].x(), pts[2].y())
        if len({ia, ib, ic}) == 3:
            simplices.append((ia, ib, ic))
    return simplices


def _delaunay_simplices_bowyer(points_xy):
    """Small pure-Python Bowyer–Watson fallback for tiny point sets only."""
    pts = [(float(x), float(y)) for x, y in points_xy]
    n = len(pts)
    if n < 3:
        return []

    min_x = min(p[0] for p in pts)
    min_y = min(p[1] for p in pts)
    max_x = max(p[0] for p in pts)
    max_y = max(p[1] for p in pts)
    dx = max_x - min_x or 1.0
    dy = max_y - min_y or 1.0
    delta = max(dx, dy) * 10.0
    cx = 0.5 * (min_x + max_x)
    cy = 0.5 * (min_y + max_y)
    p_st = [
        (cx - 2 * delta, cy - delta),
        (cx, cy + 2 * delta),
        (cx + 2 * delta, cy - delta),
    ]
    all_pts = pts + p_st
    st_i = (n, n + 1, n + 2)
    triangles = {st_i}

    def circumcircle_contains(tri, p):
        ax, ay = all_pts[tri[0]]
        bx, by = all_pts[tri[1]]
        cx_, cy_ = all_pts[tri[2]]
        d = 2 * (
            ax * (by - cy_) + bx * (cy_ - ay) + cx_ * (ay - by)
        )
        if abs(d) < 1e-18:
            return False
        ux = (
            (ax * ax + ay * ay) * (by - cy_)
            + (bx * bx + by * by) * (cy_ - ay)
            + (cx_ * cx_ + cy_ * cy_) * (ay - by)
        ) / d
        uy = (
            (ax * ax + ay * ay) * (cx_ - bx)
            + (bx * bx + by * by) * (ax - cx_)
            + (cx_ * cx_ + cy_ * cy_) * (bx - ax)
        ) / d
        px, py = p
        r2 = (ax - ux) ** 2 + (ay - uy) ** 2
        return (px - ux) ** 2 + (py - uy) ** 2 <= r2 + 1e-12

    for i, p in enumerate(pts):
        bad = [t for t in triangles if circumcircle_contains(t, p)]
        edges = []
        for t in bad:
            triangles.remove(t)
            edges.extend(
                [
                    tuple(sorted((t[0], t[1]))),
                    tuple(sorted((t[1], t[2]))),
                    tuple(sorted((t[2], t[0]))),
                ]
            )
        counts = {}
        for e in edges:
            counts[e] = counts.get(e, 0) + 1
        boundary = [e for e, c in counts.items() if c == 1]
        for a, b in boundary:
            triangles.add(tuple(sorted((i, a, b))))

    kept = []
    for t in triangles:
        if t[0] >= n or t[1] >= n or t[2] >= n:
            continue
        kept.append((t[0], t[1], t[2]))
    return kept


def _tile_bounds_deg(level, x, y):
    from quantized_mesh import tile_rectangle

    return tile_rectangle(level, x, y)
