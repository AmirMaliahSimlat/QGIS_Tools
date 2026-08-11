# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: convert tree-mask polygons to spaced points.

Places points on a hexagonal lattice so every pair is at least
``min_distance`` meters apart, keeps only points that fall inside the
input polygons, and samples terrain altitude from a Cesium quantized-mesh
tileset into a hardcoded ``altitude`` attribute (and PointZ Z).
"""

import math
import os
import sys

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPoint,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsWkbTypes,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_SCRIPT_DIR)
for _p in (_SCRIPT_DIR, _SCRIPTS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from quantized_mesh import QuantizedMeshSampler  # noqa: E402

ALTITUDE_FIELD = "altitude"


class TreeMaskToPointsAlgorithm(QgsProcessingAlgorithm):
    INPUT_POLYGONS = "INPUT_POLYGONS"
    INPUT_MESH = "INPUT_MESH"
    MIN_DISTANCE = "MIN_DISTANCE"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return TreeMaskToPointsAlgorithm()

    def name(self):
        return "tree_mask_to_points"

    def displayName(self):
        return self.tr("Tree mask polygons to spaced points")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Converts tree-mask polygons (e.g. binary CV footprints) into "
            "points with a guaranteed minimum spacing in meters.\n\n"
            "Points are generated on a hexagonal lattice covering the masks, "
            "so no two accepted points are closer than the chosen distance. "
            "Polygons that receive no lattice point get their centroid if it "
            "still respects the spacing.\n\n"
            f"Each output point is PointZ with attribute '{ALTITUDE_FIELD}' "
            "sampled from the quantized-mesh tileset at that location."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_POLYGONS,
                self.tr("Tree mask polygons"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_MESH,
                self.tr("Quantized-mesh tiles folder"),
                behavior=QgsProcessingParameterFile.Folder,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN_DISTANCE,
                self.tr("Minimum distance between points (meters)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.5,
                minValue=0.01,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Spaced tree points"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(
            parameters, self.INPUT_POLYGONS, context
        )
        mesh_folder = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        min_dist = self.parameterAsDouble(
            parameters, self.MIN_DISTANCE, context
        )

        if layer is None:
            raise QgsProcessingException(self.tr("Invalid polygon layer."))
        if not mesh_folder or not os.path.isdir(mesh_folder):
            raise QgsProcessingException(
                self.tr("Invalid quantized-mesh tiles folder.")
            )
        if min_dist <= 0:
            raise QgsProcessingException(
                self.tr("Minimum distance must be > 0.")
            )

        feedback.setProgressText(self.tr("Opening quantized-mesh…"))
        try:
            sampler = QuantizedMeshSampler(mesh_folder)
        except Exception as exc:
            raise QgsProcessingException(
                self.tr(f"Failed to open quantized-mesh tileset: {exc}")
            ) from exc

        feedback.pushInfo(
            self.tr(
                f"Using geographic quantized-mesh level {sampler.level} "
                f"({len(sampler.tiles_index)} tiles)."
            )
        )

        source_crs = layer.sourceCrs()
        metric_crs = self._metric_crs_for_layer(layer, feedback)
        to_metric = QgsCoordinateTransform(
            source_crs, metric_crs, QgsProject.instance()
        )
        to_source = QgsCoordinateTransform(
            metric_crs, source_crs, QgsProject.instance()
        )
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        to_wgs84 = QgsCoordinateTransform(
            source_crs, wgs84, QgsProject.instance()
        )

        feedback.setProgressText(self.tr("Preparing polygons…"))
        metric_features = []
        for idx, feature in enumerate(layer.getFeatures()):
            if feedback.isCanceled():
                break
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue
            metric_geom = QgsGeometry(geom)
            if metric_geom.transform(to_metric) != 0:
                continue
            metric_geom = metric_geom.makeValid()
            if metric_geom.isEmpty():
                continue
            # Stable unique id (memory layers often use -1 for every feature).
            out_feat = QgsFeature(idx)
            out_feat.setGeometry(metric_geom)
            metric_features.append(out_feat)

        if not metric_features:
            raise QgsProcessingException(
                self.tr("No usable polygon geometries found.")
            )

        n_poly = len(metric_features)
        feedback.pushInfo(
            self.tr(
                f"Packing {n_poly} polygons at ≥ {min_dist} m "
                f"(CRS {metric_crs.authid()})."
            )
        )

        # Pack in a true meter CRS. Prefer the layer CRS when it is already
        # projected metres; otherwise use the local UTM zone.
        dx = min_dist
        dy = min_dist * math.sqrt(3.0) / 2.0
        # Extra phases fill gaps, but every candidate must pass a meter check.
        phases = (
            (0.0, 0.0),
            (0.5 * dx, 0.0),
            (0.0, 0.5 * dy),
            (0.5 * dx, 0.5 * dy),
        )
        accepted_metric = []  # list of (x, y) in metric CRS
        min_dist_sq = min_dist * min_dist
        # Cell smaller than min_dist so a 3x3 neighborhood always covers
        # any point within min_dist (safe against float edge cases).
        cell = min_dist / math.sqrt(2.0)
        grid = {}
        covered_ids = set()

        feedback.setProgressText(self.tr("Placing points in masks…"))
        for i, mf in enumerate(metric_features):
            if feedback.isCanceled():
                break
            if i % 25 == 0 or i + 1 == n_poly:
                feedback.setProgress(int(55.0 * i / max(n_poly, 1)))
                feedback.setProgressText(
                    self.tr(
                        f"Placing points… {i + 1}/{n_poly} polygons "
                        f"({len(accepted_metric)} points so far)"
                    )
                )

            geom = mf.geometry()
            bbox = geom.boundingBox()
            placed = 0
            for ox, oy in phases:
                for pt in self._hex_points_local(bbox, dx, dy, ox, oy):
                    probe = QgsGeometry.fromPointXY(pt)
                    # intersects: include boundary (contains often excludes it)
                    if not geom.intersects(probe):
                        continue
                    xy = (pt.x(), pt.y())
                    if not self._far_enough_grid(xy, grid, cell, min_dist_sq):
                        continue
                    accepted_metric.append(xy)
                    self._grid_insert(grid, cell, xy)
                    placed += 1
            if placed:
                covered_ids.add(mf.id())

        # Representative point for polygons that still have nothing.
        leftovers = [mf for mf in metric_features if mf.id() not in covered_ids]
        feedback.pushInfo(
            self.tr(
                f"Packed {len(accepted_metric)} points; "
                f"{len(leftovers)} polygons need centroid fallback."
            )
        )
        feedback.setProgressText(self.tr("Placing leftover centroids…"))

        for j, mf in enumerate(leftovers):
            if feedback.isCanceled():
                break
            if j % 50 == 0 or j + 1 == len(leftovers):
                feedback.setProgress(
                    55 + int(10.0 * j / max(len(leftovers), 1))
                )

            geom = mf.geometry()
            centroid = geom.centroid().asPoint()
            cxy = (centroid.x(), centroid.y())
            if not geom.intersects(
                QgsGeometry.fromPointXY(QgsPointXY(cxy[0], cxy[1]))
            ):
                try:
                    p = geom.pointOnSurface().asPoint()
                    cxy = (p.x(), p.y())
                except Exception:
                    continue
            if not self._far_enough_grid(cxy, grid, cell, min_dist_sq):
                continue
            accepted_metric.append(cxy)
            self._grid_insert(grid, cell, cxy)

        # Final enforcement pass (drop any pair that still violates min_dist).
        accepted_metric, dropped = self._enforce_min_distance(
            accepted_metric, min_dist
        )
        if dropped:
            feedback.pushWarning(
                self.tr(
                    f"Removed {dropped} points that violated "
                    f"{min_dist} m spacing after packing."
                )
            )

        nn = self._min_nearest_neighbor(accepted_metric)
        if nn is not None:
            feedback.pushInfo(
                self.tr(
                    f"Measured min nearest-neighbor distance: {nn:.3f} m "
                    f"(requested ≥ {min_dist} m) in {metric_crs.authid()}."
                )
            )
        if nn is not None and nn < min_dist - 1e-3:
            raise QgsProcessingException(
                self.tr(
                    f"Internal spacing error: nearest points are {nn:.3f} m "
                    f"apart but minimum was {min_dist} m."
                )
            )

        fields = QgsFields()
        fields.append(QgsField(ALTITUDE_FIELD, QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.PointZ,
            source_crs,
        )
        if sink is None:
            raise QgsProcessingException(
                self.tr("Could not create output sink.")
            )

        total_out = len(accepted_metric)
        feedback.pushInfo(
            self.tr(f"Sampling altitudes for {total_out} points…")
        )
        feedback.setProgressText(
            self.tr(f"Sampling altitudes (0/{total_out})…")
        )

        written = 0
        null_alt = 0
        denom = max(total_out, 1)
        for k, mpt in enumerate(accepted_metric):
            if feedback.isCanceled():
                break
            if k % 100 == 0 or k + 1 == total_out:
                feedback.setProgress(65 + int(35.0 * k / denom))
                feedback.setProgressText(
                    self.tr(
                        f"Sampling altitudes… {k + 1}/{total_out}"
                    )
                )

            src_pt = to_source.transform(QgsPointXY(mpt[0], mpt[1]))
            wgs = to_wgs84.transform(src_pt)
            alt = sampler.sample(wgs.x(), wgs.y())
            try:
                alt_f = float(alt) if alt is not None else None
            except (TypeError, ValueError):
                alt_f = None
            if alt_f is None:
                null_alt += 1
                z = 0.0
            else:
                z = alt_f

            out = QgsFeature(fields)
            out.setGeometry(QgsGeometry(QgsPoint(src_pt.x(), src_pt.y(), z)))
            out.setAttributes([alt_f])
            sink.addFeature(out, QgsFeatureSink.FastInsert)
            written += 1

        feedback.setProgress(100)
        feedback.setProgressText(self.tr("Done."))
        feedback.pushInfo(
            self.tr(
                f"Wrote {written} points "
                f"(altitude null={null_alt}, min spacing={min_dist} m)."
            )
        )
        return {self.OUTPUT: dest_id}

    @staticmethod
    def _metric_crs_for_layer(layer, feedback):
        """Meter CRS for planar distance: layer CRS if metric, else local UTM."""
        from qgis.core import QgsUnitTypes

        crs = layer.sourceCrs()
        if (
            crs.isValid()
            and not crs.isGeographic()
            and crs.mapUnits() == QgsUnitTypes.DistanceMeters
        ):
            feedback.pushInfo(
                f"Using layer CRS {crs.authid()} for meter distances."
            )
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
            feedback.pushWarning(
                "Could not build UTM CRS; using EPSG:3857 for distances."
            )
            metric = QgsCoordinateReferenceSystem("EPSG:3857")
        feedback.pushInfo(
            f"Using {metric.authid()} for meter distances "
            f"(layer CRS was {crs.authid() or 'unknown'})."
        )
        return metric

    @staticmethod
    def _hex_points_local(bbox, dx, dy, phase_x=0.0, phase_y=0.0):
        """Yield hex-lattice points covering bbox, origin at bbox min + phase."""
        origin_x = bbox.xMinimum() + phase_x
        origin_y = bbox.yMinimum() + phase_y
        x_end = bbox.xMaximum() + dx
        y_end = bbox.yMaximum() + dy

        row = 0
        y = origin_y
        while y <= y_end:
            x_off = 0.0 if (row % 2 == 0) else (0.5 * dx)
            x = origin_x + x_off
            while x <= x_end:
                if (
                    bbox.xMinimum() - 1e-9 <= x <= bbox.xMaximum() + 1e-9
                    and bbox.yMinimum() - 1e-9 <= y <= bbox.yMaximum() + 1e-9
                ):
                    yield QgsPointXY(x, y)
                x += dx
            y += dy
            row += 1

    @staticmethod
    def _grid_insert(grid, cell, xy):
        key = (
            int(math.floor(xy[0] / cell)),
            int(math.floor(xy[1] / cell)),
        )
        grid.setdefault(key, []).append(xy)

    @staticmethod
    def _far_enough_grid(xy, grid, cell, min_dist_sq):
        """Return False if xy is closer than sqrt(min_dist_sq) to any point."""
        cx = int(math.floor(xy[0] / cell))
        cy = int(math.floor(xy[1] / cell))
        px, py = xy
        # radius 2 with cell = min_dist/sqrt(2) covers all points within min_dist
        for ix in range(cx - 2, cx + 3):
            for iy in range(cy - 2, cy + 3):
                for ox, oy in grid.get((ix, iy), ()):
                    ddx = px - ox
                    ddy = py - oy
                    if ddx * ddx + ddy * ddy < min_dist_sq:
                        return False
        return True

    @staticmethod
    def _enforce_min_distance(points, min_dist):
        """Keep points in order; drop any that fall within min_dist of a keeper."""
        if not points:
            return points, 0
        min_dist_sq = min_dist * min_dist
        cell = min_dist / math.sqrt(2.0)
        kept = []
        grid = {}
        dropped = 0
        for xy in points:
            if TreeMaskToPointsAlgorithm._far_enough_grid(
                xy, grid, cell, min_dist_sq
            ):
                kept.append(xy)
                TreeMaskToPointsAlgorithm._grid_insert(grid, cell, xy)
            else:
                dropped += 1
        return kept, dropped

    @staticmethod
    def _min_nearest_neighbor(points):
        if len(points) < 2:
            return None
        cell = None
        # Use a hash grid and only compare local neighborhoods.
        # Cell size = max spacing we care about reporting; use large enough
        # neighborhood via scanning all for small n, grid for large n.
        if len(points) <= 2000:
            best = float("inf")
            for i, (x1, y1) in enumerate(points):
                for x2, y2 in points[i + 1 :]:
                    d = math.hypot(x1 - x2, y1 - y2)
                    if d < best:
                        best = d
            return best

        # Approximate NN via grid of cell size based on bbox diagonal fraction
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        cell = span / max(math.sqrt(len(points)), 1.0)
        grid = {}
        for xy in points:
            TreeMaskToPointsAlgorithm._grid_insert(grid, cell, xy)
        best = float("inf")
        for x1, y1 in points:
            cx = int(math.floor(x1 / cell))
            cy = int(math.floor(y1 / cell))
            for ix in range(cx - 2, cx + 3):
                for iy in range(cy - 2, cy + 3):
                    for x2, y2 in grid.get((ix, iy), ()):
                        if x1 == x2 and y1 == y2:
                            continue
                        d = math.hypot(x1 - x2, y1 - y2)
                        if d < best:
                            best = d
        return best if best < float("inf") else None
