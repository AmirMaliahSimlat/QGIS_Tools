# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: sample polygon-mask points with altitude.

Always densifies outlines (exterior rings and holes). Optionally adds
dense centerline points (chord midpoints across the mask) with mesh
altitude; Unreal can ignore centers below the local road plane.
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
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsUnitTypes,
    QgsWkbTypes,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_SCRIPT_DIR)
for _p in (_SCRIPT_DIR, _SCRIPTS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from quantized_mesh import QuantizedMeshSampler  # noqa: E402

ALTITUDE_FIELD = "altitude"
ROLE_FIELD = "point_role"
ROLE_OUTLINE = "outline"
ROLE_CENTER = "center"


class PolygonMaskPointsAlgorithm(QgsProcessingAlgorithm):
    INPUT_POLYGONS = "INPUT_POLYGONS"
    INPUT_MESH = "INPUT_MESH"
    SPACING = "SPACING"
    ADD_CENTER_POINTS = "ADD_CENTER_POINTS"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return PolygonMaskPointsAlgorithm()

    def name(self):
        return "polygon_mask_points"

    def displayName(self):
        return self.tr("Polygon mask points with altitude")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Samples points along every polygon outline — exterior rings "
            "and inner holes — at a chosen spacing in meters, and assigns "
            "terrain altitude from a Cesium quantized-mesh tileset.\n\n"
            f"Output is PointZ with '{ALTITUDE_FIELD}' and '{ROLE_FIELD}' "
            f"('{ROLE_OUTLINE}' or '{ROLE_CENTER}').\n\n"
            "Optional toggle: Add centerline points. Densely samples chord "
            "midpoints across each exterior ring (inward perpendiculars) "
            "and writes mesh altitude there. Includes centers even where "
            "terrain is below the road plane — the Unreal road plugin can "
            "ignore those. When the toggle is off, only outline points "
            "are written.\n\n"
            "Expects {x}/{y}.terrain tiles (gzip), EPSG:4326 / TMS; "
            "finest LOD in the folder is used."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_POLYGONS,
                self.tr("Polygons"),
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
                self.SPACING,
                self.tr("Distance between points (meters)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=5.0,
                minValue=0.01,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ADD_CENTER_POINTS,
                self.tr("Add centerline points"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Mask points with altitude"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(
            parameters, self.INPUT_POLYGONS, context
        )
        mesh_folder = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        spacing = self.parameterAsDouble(parameters, self.SPACING, context)
        add_center_points = self.parameterAsBool(
            parameters, self.ADD_CENTER_POINTS, context
        )

        if layer is None:
            raise QgsProcessingException(self.tr("Invalid polygon layer."))
        if not mesh_folder or not os.path.isdir(mesh_folder):
            raise QgsProcessingException(
                self.tr("Invalid quantized-mesh tiles folder.")
            )
        if spacing <= 0:
            raise QgsProcessingException(
                self.tr("Spacing must be > 0.")
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
        if add_center_points:
            feedback.pushInfo(
                self.tr(
                    "Centerline points ON (dense mid-chords; "
                    "Unreal can ignore points below the road plane)."
                )
            )
        else:
            feedback.pushInfo(
                self.tr("Outline points only (centerline points OFF).")
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

        fields = QgsFields()
        fields.append(QgsField(ALTITUDE_FIELD, QVariant.Double))
        fields.append(QgsField(ROLE_FIELD, QVariant.String))

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

        n_poly = max(layer.featureCount(), 1)
        written_outline = 0
        written_center = 0
        null_alt = 0

        feedback.setProgressText(self.tr("Sampling outline points…"))
        for i, feature in enumerate(layer.getFeatures()):
            if feedback.isCanceled():
                break
            if i % 50 == 0:
                feedback.setProgress(int(90.0 * i / n_poly))
                feedback.setProgressText(
                    self.tr(
                        f"Polygons {i + 1}/{layer.featureCount()} "
                        f"(outline={written_outline}, "
                        f"center={written_center})"
                    )
                )

            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue

            for ring in self._all_rings(geom):
                if not ring or len(ring) < 2:
                    continue
                metric_ring = []
                for pt in ring:
                    mpt = to_metric.transform(QgsPointXY(pt[0], pt[1]))
                    metric_ring.append((mpt.x(), mpt.y()))

                for mx, my in self._densify_ring(metric_ring, spacing):
                    alt_f, z, ok = self._sample_metric_xy(
                        mx, my, to_source, to_wgs84, sampler
                    )
                    if not ok:
                        null_alt += 1
                    out = QgsFeature(fields)
                    src_pt = to_source.transform(QgsPointXY(mx, my))
                    out.setGeometry(
                        QgsGeometry(QgsPoint(src_pt.x(), src_pt.y(), z))
                    )
                    out.setAttributes([alt_f, ROLE_OUTLINE])
                    sink.addFeature(out, QgsFeatureSink.FastInsert)
                    written_outline += 1

            if not add_center_points:
                continue

            for metric_poly in self._metric_polygon_parts(geom, to_metric):
                if feedback.isCanceled():
                    break
                centers = self._sample_center_points(
                    metric_poly,
                    spacing,
                    to_source,
                    to_wgs84,
                    sampler,
                )
                for cx, cy, alt_f, z in centers:
                    out = QgsFeature(fields)
                    src_pt = to_source.transform(QgsPointXY(cx, cy))
                    out.setGeometry(
                        QgsGeometry(QgsPoint(src_pt.x(), src_pt.y(), z))
                    )
                    out.setAttributes([alt_f, ROLE_CENTER])
                    sink.addFeature(out, QgsFeatureSink.FastInsert)
                    written_center += 1

        feedback.setProgress(100)
        feedback.pushInfo(
            self.tr(
                f"Wrote {written_outline} outline + {written_center} center "
                f"points (altitude null={null_alt}, spacing={spacing} m)."
            )
        )
        return {self.OUTPUT: dest_id}

    def _sample_metric_xy(self, mx, my, to_source, to_wgs84, sampler):
        """Return (alt_or_None, z_for_geom, ok)."""
        src_pt = to_source.transform(QgsPointXY(mx, my))
        wgs = to_wgs84.transform(src_pt)
        alt = sampler.sample(wgs.x(), wgs.y())
        try:
            alt_f = float(alt) if alt is not None else None
        except (TypeError, ValueError):
            alt_f = None
        if alt_f is None or not math.isfinite(alt_f):
            return None, 0.0, False
        return alt_f, alt_f, True

    def _sample_center_points(
        self,
        metric_poly,
        spacing,
        to_source,
        to_wgs84,
        sampler,
    ):
        """
        Return dense (cx, cy, alt, z) chord midpoints across the polygon.

        No height filtering: emit centers even where mesh is below the
        left–right road plane; Unreal can ignore those.
        """
        exterior = self._exterior_ring_xy(metric_poly)
        if exterior is None or len(exterior) < 3:
            return []

        # Slightly denser outline walk → more cross-sections / centers.
        step = max(spacing * 0.5, 0.25)
        densified = list(self._densify_ring(exterior, step))
        if len(densified) < 3:
            return []

        bbox = metric_poly.boundingBox()
        max_ray = max(
            math.hypot(bbox.width(), bbox.height()) * 1.5,
            spacing * 4.0,
            1.0,
        )
        min_chord = max(spacing * 0.25, 0.5)
        inset = max(min(spacing * 0.05, 0.25), 0.02)

        # One point per cell; keep highest mesh altitude in the cell.
        best = {}
        cell = max(spacing * 0.5, 0.25)
        n = len(densified)
        for i, (mx, my) in enumerate(densified):
            prev_pt = densified[(i - 1) % n]
            next_pt = densified[(i + 1) % n]
            tx = next_pt[0] - prev_pt[0]
            ty = next_pt[1] - prev_pt[1]
            tlen = math.hypot(tx, ty)
            if tlen < 1e-6:
                continue
            tx /= tlen
            ty /= tlen

            inward = None
            for nx, ny in ((-ty, tx), (ty, -tx)):
                test = QgsGeometry.fromPointXY(
                    QgsPointXY(mx + nx * inset * 4.0, my + ny * inset * 4.0)
                )
                if metric_poly.contains(test):
                    inward = (nx, ny)
                    break
                if inward is None and metric_poly.intersects(test):
                    inward = (nx, ny)
            if inward is None:
                continue
            nx, ny = inward

            start = QgsPointXY(mx + nx * inset, my + ny * inset)
            end = QgsPointXY(mx + nx * max_ray, my + ny * max_ray)
            if not metric_poly.contains(QgsGeometry.fromPointXY(start)):
                continue

            ray = QgsGeometry.fromPolylineXY([start, end])
            clipped = ray.intersection(metric_poly)
            if clipped is None or clipped.isEmpty():
                continue

            chord = self._longest_line_component(clipped)
            if chord is None:
                continue
            pts = chord.asPolyline()
            if len(pts) < 2:
                continue

            d0 = math.hypot(pts[0].x() - start.x(), pts[0].y() - start.y())
            d1 = math.hypot(pts[-1].x() - start.x(), pts[-1].y() - start.y())
            if d1 >= d0:
                left = (mx, my)
                right = (pts[-1].x(), pts[-1].y())
            else:
                left = (mx, my)
                right = (pts[0].x(), pts[0].y())

            chord_len = math.hypot(right[0] - left[0], right[1] - left[1])
            if chord_len < min_chord:
                continue

            cx = 0.5 * (left[0] + right[0])
            cy = 0.5 * (left[1] + right[1])
            center_geom = QgsGeometry.fromPointXY(QgsPointXY(cx, cy))
            if not metric_poly.contains(center_geom):
                continue

            z_c, z_geom, ok_c = self._sample_metric_xy(
                cx, cy, to_source, to_wgs84, sampler
            )
            if not ok_c:
                continue

            key = (int(math.floor(cx / cell)), int(math.floor(cy / cell)))
            prev = best.get(key)
            if prev is None or z_c > prev[0]:
                best[key] = (z_c, cx, cy, z_c, z_geom)

        return [
            (cx, cy, alt, z)
            for (_rank, cx, cy, alt, z) in best.values()
        ]

    @staticmethod
    def _polyline_length(pts):
        length = 0.0
        for a, b in zip(pts, pts[1:]):
            length += math.hypot(b.x() - a.x(), b.y() - a.y())
        return length

    @staticmethod
    def _longest_line_component(geom):
        """Return the longest LineString component of an intersection geom."""
        if geom is None or geom.isEmpty():
            return None
        wkb = QgsWkbTypes.flatType(geom.wkbType())
        if wkb == QgsWkbTypes.LineString:
            return geom
        if wkb == QgsWkbTypes.MultiLineString:
            best = None
            best_len = -1.0
            for part in geom.asMultiPolyline():
                if len(part) < 2:
                    continue
                length = PolygonMaskPointsAlgorithm._polyline_length(part)
                if length > best_len:
                    best_len = length
                    best = QgsGeometry.fromPolylineXY(part)
            return best
        # GeometryCollection / mixed: pick longest line among parts.
        best = None
        best_len = -1.0
        parts = geom.asGeometryCollection() or []
        for part in parts:
            if part is None or part.isEmpty():
                continue
            part_g = QgsGeometry(part)
            cand = PolygonMaskPointsAlgorithm._longest_line_component(part_g)
            if cand is not None and cand.length() > best_len:
                best_len = cand.length()
                best = cand
        return best

    @staticmethod
    def _exterior_ring_xy(metric_poly):
        """Return exterior ring as list of (x, y) from a single Polygon geom."""
        polygon = metric_poly.asPolygon()
        if not polygon or not polygon[0]:
            return None
        ring = polygon[0]
        return [(p.x(), p.y()) for p in ring]

    @staticmethod
    def _metric_polygon_parts(geometry, to_metric):
        """Yield valid single-polygon geometries in the metric CRS."""
        geom = QgsGeometry(geometry)
        if geom.transform(to_metric) != 0:
            return
        geom = geom.makeValid()
        if geom is None or geom.isEmpty():
            return

        flat = QgsWkbTypes.flatType(geom.wkbType())
        if flat == QgsWkbTypes.Polygon:
            yield geom
            return
        if flat == QgsWkbTypes.MultiPolygon:
            multi = geom.asMultiPolygon()
            if multi:
                for polygon in multi:
                    if polygon and polygon[0]:
                        yield QgsGeometry.fromPolygonXY(polygon)
            return
        collection = geom.asGeometryCollection() or []
        for part in collection:
            if part is None or part.isEmpty():
                continue
            if QgsWkbTypes.flatType(part.wkbType()) == QgsWkbTypes.Polygon:
                yield QgsGeometry(part)

    def _all_rings(self, geometry):
        """Yield all rings (exterior + holes) for Polygon / MultiPolygon."""
        geom = QgsGeometry(geometry)
        wkb = geom.wkbType()

        if QgsWkbTypes.isMultiType(wkb):
            multi = geom.asMultiPolygon()
            if multi:
                for polygon in multi:
                    for ring in polygon:
                        if ring:
                            yield ring
                return
            for part in geom.asGeometryCollection():
                yield from self._all_rings(part)
            return

        polygon = geom.asPolygon()
        if polygon:
            for ring in polygon:
                if ring:
                    yield ring

    @staticmethod
    def _densify_ring(ring, spacing):
        """
        Yield (x, y) along a closed or open ring in meter coordinates.
        Includes each vertex; inserts intermediate stations every ~spacing.
        Skips duplicating a closing vertex that matches the first.
        """
        if not ring or spacing <= 0:
            return

        pts = list(ring)
        # Drop closing duplicate if present.
        if (
            len(pts) >= 2
            and abs(pts[0][0] - pts[-1][0]) < 1e-9
            and abs(pts[0][1] - pts[-1][1]) < 1e-9
        ):
            pts = pts[:-1]
        if len(pts) < 2:
            if pts:
                yield pts[0]
            return

        # Close for walking edges, then don't emit the final close twice.
        closed = pts + [pts[0]]
        yield closed[0]
        for i in range(len(closed) - 1):
            x1, y1 = closed[i]
            x2, y2 = closed[i + 1]
            dx = x2 - x1
            dy = y2 - y1
            length = math.hypot(dx, dy)
            if length < 1e-9:
                continue
            n_extra = int(math.floor(length / spacing))
            for k in range(1, n_extra + 1):
                t = (k * spacing) / length
                if t >= 1.0 - 1e-12:
                    break
                yield (x1 + t * dx, y1 + t * dy)
            if i < len(closed) - 2:
                yield (x2, y2)

    @staticmethod
    def _metric_crs_for_layer(layer, feedback):
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
            f"Using {metric.authid()} for outline spacing "
            f"(layer CRS was {crs.authid() or 'unknown'})."
        )
        return metric
