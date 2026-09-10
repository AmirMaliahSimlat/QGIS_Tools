# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: assign each water polygon a median terrain
altitude from a Cesium quantized-mesh tileset.

Hardcoded field:
  altitude = median mesh elevation on exterior-ring vertices, edge midpoints,
             and a light interior sample set (point-on-surface + bbox grid).
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

from quantized_mesh import (  # noqa: E402
    QuantizedMeshSampler,
    sample_lonlats_parallel,
)
from crs_util import epsg_4326, to_wgs84_geometry  # noqa: E402
from atomic_io import begin_atomic_file_output, finish_or_abandon  # noqa: E402

ALTITUDE_FIELD = "altitude"


class WaterMedianAltitudeAlgorithm(QgsProcessingAlgorithm):
    INPUT_WATER = "INPUT_WATER"
    INPUT_MESH = "INPUT_MESH"
    INTERIOR_STEP = "INTERIOR_STEP"
    WORKERS = "WORKERS"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return WaterMedianAltitudeAlgorithm()

    def name(self):
        return "water_median_altitude"

    def displayName(self):
        return self.tr("Water median quantized-mesh altitude")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Copies a water-polygon layer and adds a Double attribute "
            f"'{ALTITUDE_FIELD}' = median terrain altitude from a Cesium "
            "quantized-mesh tileset.\n\n"
            "Samples each feature's exterior-ring vertices and edge midpoints, "
            "plus a light interior grid (and a point-on-surface). "
            "Holes are ignored. Intended for lakes/ponds (one flat elevation "
            "per polygon). Features with no valid samples get NULL.\n\n"
            "Expects a folder of {x}/{y}.terrain tiles (gzip), geographic "
            "EPSG:4326 / TMS layout — finest LOD in the folder is used."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_WATER,
                self.tr("Water polygons"),
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
                self.INTERIOR_STEP,
                self.tr("Interior sample step (meters, 0 = outline only)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=25.0,
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
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Water with altitude"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        water = self.parameterAsVectorLayer(
            parameters, self.INPUT_WATER, context
        )
        mesh_folder = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        interior_step = self.parameterAsDouble(
            parameters, self.INTERIOR_STEP, context
        )
        workers_req = self.parameterAsInt(parameters, self.WORKERS, context)

        if water is None:
            raise QgsProcessingException(self.tr("Invalid water layer."))
        if not mesh_folder or not os.path.isdir(mesh_folder):
            raise QgsProcessingException(
                self.tr("Invalid quantized-mesh tiles folder.")
            )
        if water.fields().indexOf(ALTITUDE_FIELD) >= 0:
            raise QgsProcessingException(
                self.tr(
                    f"Field '{ALTITUDE_FIELD}' already exists on the water layer."
                )
            )

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

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = None
        if water.crs() != wgs84:
            transform = QgsCoordinateTransform(
                water.crs(),
                wgs84,
                QgsProject.instance(),
            )

        # Metric CRS for interior grid step in meters.
        metric_crs = self._metric_crs_for_layer(water, feedback)
        to_metric = None
        to_source = None
        if water.crs() != metric_crs:
            to_metric = QgsCoordinateTransform(
                water.crs(), metric_crs, QgsProject.instance()
            )
            to_source = QgsCoordinateTransform(
                metric_crs, water.crs(), QgsProject.instance()
            )

        fields = QgsFields(water.fields())
        fields.append(QgsField(ALTITUDE_FIELD, QVariant.Double))

        out_crs = epsg_4326()
        sink_params, atomic = begin_atomic_file_output(parameters, self.OUTPUT)
        (sink, dest_id) = self.parameterAsSink(
            sink_params,
            self.OUTPUT,
            context,
            fields,
            water.wkbType(),
            out_crs,
        )
        if sink is None:
            if atomic:
                atomic.abandon()
            raise QgsProcessingException(
                self.tr("Could not create output sink.")
            )

        total = max(water.featureCount(), 1)
        filled = 0
        nulls = 0
        skipped_crs = 0
        canceled = False

        feats = list(water.getFeatures())
        feat_counts = []
        lonlats = []
        for feature in feats:
            if feedback.isCanceled():
                canceled = True
                break
            pts = self._collect_sample_lonlats(
                feature.geometry(),
                transform,
                to_metric,
                to_source,
                interior_step,
            )
            feat_counts.append(len(pts))
            lonlats.extend(pts)

        alts = []
        if not canceled:
            alts = sample_lonlats_parallel(
                mesh_folder,
                lonlats,
                workers=workers_req,
                level=sampler.level,
                feedback=feedback,
                scripts_root=_SCRIPTS_ROOT,
            )

        offset = 0
        for current, feature in enumerate(feats):
            if feedback.isCanceled():
                canceled = True
                break
            n = feat_counts[current] if current < len(feat_counts) else 0
            chunk = alts[offset : offset + n]
            offset += n

            out_geom = to_wgs84_geometry(feature.geometry(), water.sourceCrs())
            if out_geom is None:
                skipped_crs += 1
                continue

            out_feature = QgsFeature(fields)
            out_feature.setGeometry(out_geom)
            attrs = list(feature.attributes())

            samples = []
            for alt in chunk:
                if alt is None:
                    continue
                try:
                    z = float(alt)
                except (TypeError, ValueError):
                    continue
                if math.isfinite(z):
                    samples.append(z)
            median_z = self._median(samples)
            attrs.append(median_z)
            if median_z is None:
                nulls += 1
            else:
                filled += 1

            out_feature.setAttributes(attrs)
            sink.addFeature(out_feature, QgsFeatureSink.FastInsert)
            feedback.setProgress(int(100.0 * current / total))

        ok = not canceled
        published = finish_or_abandon(atomic, ok=ok, sink=sink)
        sink = None
        if canceled:
            raise QgsProcessingException(self.tr("Canceled."))
        feedback.pushInfo(
            self.tr(
                f"Wrote '{ALTITUDE_FIELD}' (median) in EPSG:4326; "
                f"filled={filled}, null={nulls}"
                f"{f', skipped CRS={skipped_crs}' if skipped_crs else ''}."
            )
        )
        return {self.OUTPUT: published or dest_id}

    def _collect_sample_lonlats(
        self, geometry, transform, to_metric, to_source, interior_step
    ):
        """WGS84 sample coordinates (no mesh I/O)."""
        pts = []
        if geometry is None or geometry.isEmpty():
            return pts
        for exterior in self._exterior_rings(geometry):
            self._ring_lonlats(exterior, transform, pts)
        try:
            pos = geometry.pointOnSurface().asPoint()
            self._append_lonlat(pos.x(), pos.y(), transform, pts)
        except Exception:
            pass
        if interior_step > 0:
            self._interior_grid_lonlats(
                geometry, transform, to_metric, to_source, interior_step, pts
            )
        return pts

    def _ring_lonlats(self, ring, transform, pts):
        if not ring:
            return
        n = len(ring)
        for i in range(n):
            self._append_lonlat(ring[i][0], ring[i][1], transform, pts)
            if i + 1 >= n:
                continue
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[i + 1][0], ring[i + 1][1]
            self._append_lonlat(
                0.5 * (x1 + x2), 0.5 * (y1 + y2), transform, pts
            )

    @staticmethod
    def _append_lonlat(x, y, transform, pts):
        if transform is not None:
            p = transform.transform(x, y)
            x, y = p.x(), p.y()
        pts.append((x, y))

    def _interior_grid_lonlats(
        self, geometry, transform, to_metric, to_source, step, pts
    ):
        geom = QgsGeometry(geometry)
        if to_metric is not None:
            if geom.transform(to_metric) != 0:
                return
        bbox = geom.boundingBox()
        if bbox.isEmpty():
            return
        max_cells = 40
        width = max(bbox.width(), 1e-6)
        height = max(bbox.height(), 1e-6)
        nx = min(max(1, int(math.floor(width / step))), max_cells)
        ny = min(max(1, int(math.floor(height / step))), max_cells)
        dx = width / (nx + 1)
        dy = height / (ny + 1)
        xmin = bbox.xMinimum()
        ymin = bbox.yMinimum()
        for ix in range(1, nx + 1):
            for iy in range(1, ny + 1):
                mx = xmin + ix * dx
                my = ymin + iy * dy
                probe = QgsGeometry.fromPointXY(QgsPointXY(mx, my))
                if not geom.intersects(probe):
                    continue
                if to_source is not None:
                    sx = to_source.transform(QgsPointXY(mx, my))
                    x, y = sx.x(), sx.y()
                else:
                    x, y = mx, my
                self._append_lonlat(x, y, transform, pts)

    def _collect_samples(
        self, geometry, sampler, transform, to_metric, to_source, interior_step
    ):
        if geometry is None or geometry.isEmpty():
            return []

        values = []
        for exterior in self._exterior_rings(geometry):
            self._sample_ring(exterior, sampler, transform, values)

        # Point on surface (stable interior point).
        try:
            pos = geometry.pointOnSurface().asPoint()
            self._append_sample(pos.x(), pos.y(), sampler, transform, values)
        except Exception:
            pass

        if interior_step > 0:
            self._sample_interior_grid(
                geometry,
                sampler,
                transform,
                to_metric,
                to_source,
                interior_step,
                values,
            )
        return values

    def _sample_interior_grid(
        self, geometry, sampler, transform, to_metric, to_source, step, values
    ):
        """Light axis-aligned grid in a meter CRS, kept inside the polygon."""
        geom = QgsGeometry(geometry)
        if to_metric is not None:
            if geom.transform(to_metric) != 0:
                return

        bbox = geom.boundingBox()
        if bbox.isEmpty():
            return

        # Cap grid size so huge lakes stay fast.
        max_cells = 40
        width = max(bbox.width(), 1e-6)
        height = max(bbox.height(), 1e-6)
        nx = max(1, int(math.floor(width / step)))
        ny = max(1, int(math.floor(height / step)))
        nx = min(nx, max_cells)
        ny = min(ny, max_cells)
        if nx == 0 or ny == 0:
            return

        dx = width / (nx + 1)
        dy = height / (ny + 1)
        xmin = bbox.xMinimum()
        ymin = bbox.yMinimum()

        for ix in range(1, nx + 1):
            for iy in range(1, ny + 1):
                mx = xmin + ix * dx
                my = ymin + iy * dy
                probe = QgsGeometry.fromPointXY(QgsPointXY(mx, my))
                if not geom.intersects(probe):
                    continue
                if to_source is not None:
                    sx = to_source.transform(QgsPointXY(mx, my))
                    x, y = sx.x(), sx.y()
                else:
                    x, y = mx, my
                self._append_sample(x, y, sampler, transform, values)

    def _exterior_rings(self, geometry):
        geom = QgsGeometry(geometry)
        wkb = geom.wkbType()

        if QgsWkbTypes.isMultiType(wkb):
            multi = geom.asMultiPolygon()
            if multi:
                for polygon in multi:
                    if polygon:
                        yield polygon[0]
                return
            for part in geom.asGeometryCollection():
                yield from self._exterior_rings(part)
            return

        polygon = geom.asPolygon()
        if polygon:
            yield polygon[0]

    @classmethod
    def _sample_ring(cls, ring, sampler, transform, values):
        if not ring:
            return
        n = len(ring)
        for i in range(n):
            cls._append_sample(
                ring[i][0], ring[i][1], sampler, transform, values
            )
            if i + 1 >= n:
                continue
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[i + 1][0], ring[i + 1][1]
            cls._append_sample(
                0.5 * (x1 + x2),
                0.5 * (y1 + y2),
                sampler,
                transform,
                values,
            )

    @staticmethod
    def _append_sample(x, y, sampler, transform, values):
        if transform is not None:
            pt = transform.transform(x, y)
            x, y = pt.x(), pt.y()
        value = sampler.sample(x, y)
        if value is None:
            return
        try:
            z = float(value)
        except (TypeError, ValueError):
            return
        if not math.isfinite(z):
            return
        values.append(z)

    @staticmethod
    def _median(values):
        if not values:
            return None
        vals = sorted(values)
        n = len(vals)
        mid = n // 2
        if n % 2 == 1:
            return vals[mid]
        return 0.5 * (vals[mid - 1] + vals[mid])

    @staticmethod
    def _metric_crs_for_layer(layer, feedback):
        from qgis.core import QgsUnitTypes

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
            feedback.pushWarning(
                "Could not build UTM CRS; using EPSG:3857 for interior grid."
            )
            metric = QgsCoordinateReferenceSystem("EPSG:3857")
        return metric
