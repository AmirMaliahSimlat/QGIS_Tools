# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: flatten quantized-mesh heights under road masks
using the same 2D Delaunay + outline PointZ rule as the Unreal road plugin.

Never overwrites the input tileset; writes a full copy then patches tiles.
"""

import math
import os
import shutil
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
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterField,
    QgsProcessingParameterFile,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
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
UNREAL_OUTLINE_BAND_M = 1.5
UNREAL_INTERIOR_PROUD_M = 0.0


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
    INTERIOR_PROUD = "INTERIOR_PROUD"

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
            "1. Elevation PointZ on/near the mask (default snap 15 m, same as "
            "RoadPlacer) or inside the mask.\n"
            "2. Drop sagging interior samples below the curb plane "
            "(1.5 m curb band; optional proud threshold).\n"
            "3. Inject mask ring vertices (outer + holes) with Z from the "
            "nearest elevation sample.\n"
            "4. 2D Delaunay on XY; keep triangles whose centroid is inside "
            "the mask; set every mesh vertex inside the mask to that "
            "triangle’s linear Z.\n\n"
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
                self.tr("Outline points (PointZ from mask-points tool)"),
                [QgsProcessing.TypeVectorPoint],
            )
        )
        self.addParameter(
            QgsProcessingParameterField(
                self.ALTITUDE_FIELD,
                self.tr("Altitude field"),
                parentLayerParameterName=self.INPUT_OUTLINE_POINTS,
                type=QgsProcessingParameterField.Numeric,
                defaultValue=ALTITUDE_FIELD_DEFAULT,
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
            QgsProcessingParameterNumber(
                self.INTERIOR_PROUD,
                self.tr(
                    "Interior proud meters (Unreal; 0 = drop any sag)"
                ),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=UNREAL_INTERIOR_PROUD_M,
                minValue=0.0,
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
        proud_m = self.parameterAsDouble(
            parameters, self.INTERIOR_PROUD, context
        )

        if masks_layer is None:
            raise QgsProcessingException(self.tr("Invalid mask layer."))
        if points_source is None:
            raise QgsProcessingException(self.tr("Invalid outline points."))
        if not alt_field:
            raise QgsProcessingException(self.tr("Altitude field required."))
        if not mesh_in or not os.path.isdir(mesh_in):
            raise QgsProcessingException(self.tr("Invalid input mesh folder."))
        if not mesh_out:
            raise QgsProcessingException(self.tr("Output mesh folder required."))
        if near_m < 0:
            raise QgsProcessingException(self.tr("Near-mask snap must be ≥ 0."))
        if proud_m < 0:
            raise QgsProcessingException(self.tr("Interior proud must be ≥ 0."))

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

        # --- Phase 1: copy tileset (0–20%) ---
        progress.begin("1/5 Copy tileset", 0, 20)
        os.makedirs(out_path, exist_ok=True)
        all_files = []
        progress.tick(0, 1, "scanning input folder…")
        for root, _dirs, files in os.walk(in_path):
            if feedback.isCanceled():
                raise QgsProcessingException(self.tr("Canceled during scan."))
            for name in files:
                all_files.append(os.path.join(root, name))
        n_files = max(len(all_files), 1)
        progress.tick(0, n_files, f"0/{n_files} files")
        for fi, src in enumerate(all_files):
            if feedback.isCanceled():
                raise QgsProcessingException(self.tr("Canceled during copy."))
            rel = os.path.relpath(src, in_path)
            dst = os.path.join(out_path, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            if fi % 50 == 0 or fi + 1 == n_files:
                progress.tick(
                    fi + 1,
                    n_files,
                    f"{fi + 1}/{n_files} files ({os.path.basename(src)})",
                )

        # --- Phase 2–3: Delaunay surface (20–50%) ---
        surface = self._build_road_surface(
            masks_layer,
            points_source,
            alt_field,
            near_m,
            proud_m,
            feedback,
            progress,
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
                f"interior kept={surface.interior_kept}, "
                f"skipped={surface.interior_skipped}, "
                f"mask-ring verts={surface.n_ring_verts})."
            )
        )

        # --- Phase 5: flatten tiles (50–100%) ---
        progress.begin("5/5 Flatten mesh tiles", 50, 100)
        progress.tick(0, 1, "discovering .terrain files…")
        tiles = discover_terrain_tiles(out_path)
        feedback.pushInfo(self.tr(f"Found {len(tiles)} .terrain tiles."))

        changed_tiles = 0
        changed_verts = 0
        examined = 0
        skipped_bbox = 0
        n_tiles = max(len(tiles), 1)
        for ti, (tile_path, level, tx, ty) in enumerate(tiles):
            if feedback.isCanceled():
                break

            # Always advance the bar, including fast bbox skips.
            if ti % 5 == 0 or ti + 1 == n_tiles:
                rel = os.path.relpath(str(tile_path), out_path)
                progress.tick(
                    ti + 1,
                    n_tiles,
                    (
                        f"tile {ti + 1}/{n_tiles} LOD {level} "
                        f"examined={examined} patched={changed_tiles} "
                        f"verts={changed_verts} skip_bbox={skipped_bbox} "
                        f"| {rel}"
                    ),
                )

            west, south, east, north = _tile_bounds_deg(level, tx, ty)
            if not surface.bounds_intersect(west, south, east, north):
                skipped_bbox += 1
                continue

            examined += 1
            try:
                data, was_gzip, lons, lats, alts = load_tile_altitudes_lonlat(
                    tile_path, level, tx, ty
                )
            except Exception as exc:
                feedback.pushWarning(
                    self.tr(f"Skip unreadable tile {tile_path}: {exc}")
                )
                continue

            modified = False
            new_alts = list(alts)
            n_verts = len(alts)
            for vi, (lon, lat, old_z) in enumerate(zip(lons, lats, alts)):
                if feedback.isCanceled():
                    break
                # Extra detail on heavy tiles so long vertex loops don't look stuck.
                if n_verts >= 2000 and (
                    vi % 500 == 0 or vi + 1 == n_verts
                ):
                    progress.tick(
                        ti + (vi + 1) / max(n_verts, 1),
                        n_tiles,
                        (
                            f"tile {ti + 1}/{n_tiles} LOD {level} "
                            f"vertices {vi + 1}/{n_verts} "
                            f"patched={changed_tiles} verts={changed_verts}"
                        ),
                    )
                new_z = surface.sample_z(lon, lat)
                if new_z is None:
                    continue
                if abs(new_z - old_z) > 1e-6:
                    new_alts[vi] = new_z
                    modified = True
                    changed_verts += 1

            if not modified:
                continue

            try:
                patched = replace_tile_altitudes(
                    data, level, tx, ty, new_alts
                )
                write_terrain_file(tile_path, patched, was_gzip)
                changed_tiles += 1
            except Exception as exc:
                feedback.pushWarning(
                    self.tr(f"Failed writing {tile_path}: {exc}")
                )

        feedback.setProgress(100)
        feedback.setProgressText(self.tr("Finished"))
        feedback.pushInfo(
            self.tr(
                f"Done. Copied mesh to {out_path}. "
                f"Examined {examined} tiles in road extent, "
                f"patched {changed_tiles} tiles, {changed_verts} vertices "
                f"(bbox-skipped {skipped_bbox})."
            )
        )
        return {self.OUTPUT_MESH: out_path}

    def _build_road_surface(
        self,
        masks_layer,
        points_source,
        alt_field,
        near_m,
        proud_m,
        feedback,
        progress,
    ):
        """
        Build the same sample set + Delaunay rules as RoadPlacer:
        snap/inside selection, drop sagging interiors, inject mask rings.
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
        progress.tick(0, 1, "selecting points on/near masks…")
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
                    # Outside but within snap of curb (boundary distance).
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

        # Drop sagging interiors (RoadPlacer DropSaggingInteriorSamples).
        progress.tick(0, 1, "filtering sagging interior samples…")
        selected, interior_kept, interior_skipped = _drop_sagging_interiors(
            selected,
            masks_metric,
            mask_boundaries,
            mask_index,
            UNREAL_OUTLINE_BAND_M,
            proud_m,
            feedback,
        )
        feedback.pushInfo(
            self.tr(
                f"After Unreal sample filter: {len(selected)} points "
                f"(interior kept={interior_kept}, skipped={interior_skipped})."
            )
        )

        # Inject mask ring vertices with nearest known Z (CollectMaskSamples +
        # FillMissingHeights).
        progress.begin("4/5 Inject mask rings + Delaunay", 34, 50)
        known_for_fill = list(selected)
        n_ring_verts = 0
        ring_samples_by_mask = []
        for mi, ring_pts in enumerate(mask_ring_verts):
            filled = []
            for mx, my, lon, lat in ring_pts:
                z = _nearest_z(mx, my, known_for_fill)
                if z is None:
                    continue
                filled.append((mx, my, lon, lat, z))
                n_ring_verts += 1
            ring_samples_by_mask.append(filled)
        feedback.pushInfo(
            self.tr(
                f"Injected {n_ring_verts} mask-ring vertices with nearest Z."
            )
        )

        # Bucket all elevation samples (pointZ + will merge rings per mask).
        cell = max(near_m, UNREAL_OUTLINE_BAND_M, 1.0)
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
            if mi % 5 == 0 or mi + 1 == n_masks_total:
                progress.tick(
                    mi + 1,
                    n_masks_total,
                    (
                        f"Delaunay mask {mi + 1}/{n_masks_total} "
                        f"triangles={len(triangles)}"
                    ),
                )
            samples = samples_for_mask(mi, metric_geom)
            if len(samples) < 3:
                continue
            pts_m = [(s[0], s[1]) for s in samples]
            pts_llz = [(s[2], s[3], s[4]) for s in samples]
            simplices = _delaunay_simplices(pts_m)
            if not simplices:
                continue
            n_masks += 1
            n_outline_used += len(samples)
            for ia, ib, ic in simplices:
                lon0, lat0, z0 = pts_llz[ia]
                lon1, lat1, z1 = pts_llz[ib]
                lon2, lat2, z2 = pts_llz[ic]
                cx = (pts_m[ia][0] + pts_m[ib][0] + pts_m[ic][0]) / 3.0
                cy = (pts_m[ia][1] + pts_m[ib][1] + pts_m[ic][1]) / 3.0
                # Match Unreal PointInMask(centroid): contains/intersects.
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
            interior_kept,
            interior_skipped,
            n_ring_verts,
            progress,
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
        interior_kept=0,
        interior_skipped=0,
        n_ring_verts=0,
        progress=None,
    ):
        self.triangles = triangles
        self.mask_geoms_wgs = mask_geoms_wgs
        self.n_outline_used = n_outline_used
        self.n_masks = n_masks
        self.interior_kept = interior_kept
        self.interior_skipped = interior_skipped
        self.n_ring_verts = n_ring_verts
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
        n_masks_g = max(len(mask_geoms_wgs), 1)
        for i, g in enumerate(mask_geoms_wgs):
            feat = QgsFeature(i + 1)
            feat.setGeometry(QgsGeometry(g))
            self._mask_index.addFeature(feat)
            self._mask_by_id[i + 1] = g
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
        else:
            self._extent = None

    def bounds_intersect(self, west, south, east, north):
        if self._extent is None:
            return False
        ws, ss, es, ns = self._extent
        return not (east < ws or west > es or north < ss or south > ns)

    def _inside_mask(self, lon, lat):
        pt = QgsGeometry.fromPointXY(QgsPointXY(lon, lat))
        for fid in self._mask_index.intersects(pt.boundingBox()):
            g = self._mask_by_id.get(fid)
            if g is not None and g.intersects(pt):
                return True
        return False

    def sample_z(self, lon, lat):
        if not self._inside_mask(lon, lat):
            return None
        pt = QgsPointXY(lon, lat)
        candidates = self._index.intersects(
            QgsGeometry.fromPointXY(pt).boundingBox()
        )
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


def _nearest_z(mx, my, samples):
    if not samples:
        return None
    best_d = None
    best_z = None
    for sx, sy, _lon, _lat, z in samples:
        d = (sx - mx) ** 2 + (sy - my) ** 2
        if best_d is None or d < best_d:
            best_d = d
            best_z = z
    return best_z


def _drop_sagging_interiors(
    samples,
    masks_metric,
    mask_boundaries,
    mask_index,
    outline_band_m,
    proud_m,
    feedback,
):
    """
    Mirror RoadPlacer DropSaggingInteriorSamples:
    curb = on/near outline band OR outside mask; keep interiors only if
    height >= interpolated curb Z + proud_m.
    """
    curb = []
    interior = []
    for s in samples:
        if feedback.isCanceled():
            break
        mx, my, lon, lat, z = s
        pt = QgsGeometry.fromPointXY(QgsPointXY(mx, my))
        inside = False
        on_curb = False
        for fid in mask_index.intersects(pt.boundingBox()):
            gi = fid - 1
            if gi < 0 or gi >= len(masks_metric):
                continue
            if masks_metric[gi].intersects(pt):
                inside = True
                if mask_boundaries[gi].distance(pt) <= outline_band_m:
                    on_curb = True
                break
            if mask_boundaries[gi].distance(pt) <= outline_band_m:
                on_curb = True
        if on_curb or not inside:
            curb.append(s)
        else:
            interior.append(s)

    if not interior:
        return curb, 0, 0
    if not curb:
        return samples, len(interior), 0

    # Grid of curb samples for nearest search (metric meters).
    cell = max(outline_band_m, 2.0)
    grid = {}
    for i, (mx, my, _lo, _la, _z) in enumerate(curb):
        key = (int(math.floor(mx / cell)), int(math.floor(my / cell)))
        grid.setdefault(key, []).append(i)

    def curb_z_at(mx, my):
        cx = int(math.floor(mx / cell))
        cy = int(math.floor(my / cell))
        hits = []
        for r in range(0, 21):
            if len(hits) >= 8:
                break
            for dy in range(-r, r + 1):
                for dx in range(-r, r + 1):
                    if r > 0 and abs(dx) != r and abs(dy) != r:
                        continue
                    for ki in grid.get((cx + dx, cy + dy), []):
                        kx, ky, _lon, _lat, kz = curb[ki]
                        d2 = (mx - kx) ** 2 + (my - ky) ** 2
                        hits.append((d2, kz))
            if hits:
                break
        if not hits:
            return curb[0][4]
        hits.sort(key=lambda t: t[0])
        use = hits[:6]
        wsum = 0.0
        zsum = 0.0
        for d2, kz in use:
            w = 1.0 / max(d2, 1.0e-6)
            wsum += w
            zsum += w * kz
        return zsum / wsum if wsum > 0 else use[0][1]

    kept_interior = 0
    skipped = 0
    out = list(curb)
    for s in interior:
        mx, my, lon, lat, z = s
        cz = curb_z_at(mx, my)
        if z + 1.0e-6 >= cz + proud_m:
            out.append(s)
            kept_interior += 1
        else:
            skipped += 1
    return out, kept_interior, skipped


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


def _delaunay_simplices(points_xy):
    """Return list of (i,j,k) vertex index triples."""
    try:
        import numpy as np
        from scipy.spatial import Delaunay

        arr = np.asarray(points_xy, dtype=float)
        if arr.shape[0] < 3:
            return []
        tri = Delaunay(arr)
        return [tuple(map(int, s)) for s in tri.simplices]
    except Exception:
        pass
    return _delaunay_simplices_bowyer(points_xy)


def _delaunay_simplices_bowyer(points_xy):
    """Small pure-Python Bowyer–Watson fallback when SciPy is unavailable."""
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
