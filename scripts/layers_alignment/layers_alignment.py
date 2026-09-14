# -*- coding: utf-8 -*-
"""
QGIS Processing: align overlapping layers by priority.

Priority (highest first): roads → water → buildings → trees.

- Water: drop / cut where it overlaps roads
- Buildings: drop / cut where they overlap roads or water
- Trees (polygons or points): drop / cut where they overlap roads,
  water, or buildings

Roads are never modified. Optional clearance buffers higher-priority
geometries before cutting.
"""

from __future__ import annotations

import os
import sys

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsFields,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
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

from crs_util import epsg_4326, to_wgs84_geometry  # noqa: E402
from atomic_io import begin_atomic_file_output, finish_or_abandon  # noqa: E402


class _GeomIndex:
    """Metric geometries + spatial index for clip queries."""

    def __init__(self):
        self.index = QgsSpatialIndex()
        self.geoms = {}  # fid -> QgsGeometry

    def add(self, fid: int, geom: QgsGeometry) -> None:
        feat = QgsFeature(fid)
        feat.setGeometry(geom)
        self.index.addFeature(feat)
        self.geoms[fid] = geom

    def __len__(self) -> int:
        return len(self.geoms)


class LayersAlignmentAlgorithm(QgsProcessingAlgorithm):
    INPUT_ROADS = "INPUT_ROADS"
    INPUT_WATER = "INPUT_WATER"
    INPUT_BUILDINGS = "INPUT_BUILDINGS"
    INPUT_TREES = "INPUT_TREES"
    CLEARANCE = "CLEARANCE"
    OUTPUT_WATER = "OUTPUT_WATER"
    OUTPUT_BUILDINGS = "OUTPUT_BUILDINGS"
    OUTPUT_TREES = "OUTPUT_TREES"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return LayersAlignmentAlgorithm()

    def name(self):
        return "layers_alignment"

    def displayName(self):
        return self.tr("Layers alignment")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Removes overlaps by priority: roads → water → buildings → trees.\n\n"
            "• Water is cut where it overlaps roads\n"
            "• Buildings are cut where they overlap roads or water\n"
            "• Trees (polygon footprints or points) are removed / cut where "
            "they overlap roads, water, or buildings\n\n"
            "Roads are not modified. Clearance (meters) buffers higher-priority "
            "geometries before cutting. Outputs are EPSG:4326."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_ROADS,
                self.tr("Roads (polygons)"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_WATER,
                self.tr("Water polygons"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_BUILDINGS,
                self.tr("Buildings polygons"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_TREES,
                self.tr("Trees (polygons or points)"),
                [
                    QgsProcessing.TypeVectorPolygon,
                    QgsProcessing.TypeVectorPoint,
                ],
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CLEARANCE,
                self.tr("Clearance buffer (meters)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_WATER,
                self.tr("Aligned water"),
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_BUILDINGS,
                self.tr("Aligned buildings"),
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_TREES,
                self.tr("Aligned trees"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        roads = self.parameterAsVectorLayer(
            parameters, self.INPUT_ROADS, context
        )
        water = self.parameterAsVectorLayer(
            parameters, self.INPUT_WATER, context
        )
        buildings = self.parameterAsVectorLayer(
            parameters, self.INPUT_BUILDINGS, context
        )
        trees = self.parameterAsVectorLayer(
            parameters, self.INPUT_TREES, context
        )
        clearance = self.parameterAsDouble(
            parameters, self.CLEARANCE, context
        )

        if roads is None:
            raise QgsProcessingException(self.tr("Invalid roads layer."))
        if water is None:
            raise QgsProcessingException(self.tr("Invalid water layer."))
        if buildings is None:
            raise QgsProcessingException(self.tr("Invalid buildings layer."))
        if trees is None:
            raise QgsProcessingException(self.tr("Invalid trees layer."))
        if clearance < 0:
            raise QgsProcessingException(
                self.tr("Clearance must be ≥ 0.")
            )

        metric_crs = self._metric_crs_for_layer(roads, feedback)
        feedback.pushInfo(
            self.tr(
                f"Aligning in {metric_crs.authid()} "
                f"(clearance={clearance} m)."
            )
        )

        feedback.setProgressText(self.tr("Indexing roads…"))
        roads_idx = self._load_index(
            roads, metric_crs, clearance, feedback
        )
        if feedback.isCanceled():
            raise QgsProcessingException(self.tr("Canceled."))
        feedback.pushInfo(self.tr(f"Roads clip features: {len(roads_idx)}"))

        feedback.setProgressText(self.tr("Aligning water…"))
        water_kept, water_dropped = self._write_aligned(
            parameters,
            context,
            feedback,
            layer=water,
            metric_crs=metric_crs,
            clip_indexes=[roads_idx],
            output_key=self.OUTPUT_WATER,
            progress_base=0,
            progress_span=30,
        )
        if feedback.isCanceled():
            raise QgsProcessingException(self.tr("Canceled."))
        feedback.pushInfo(
            self.tr(
                f"Water: kept {water_kept}, removed/emptied {water_dropped}."
            )
        )

        feedback.setProgressText(self.tr("Indexing water for buildings/trees…"))
        # Clip buildings/trees with original water (buffered), not the cut result.
        water_idx = self._load_index(
            water, metric_crs, clearance, feedback
        )
        if feedback.isCanceled():
            raise QgsProcessingException(self.tr("Canceled."))

        feedback.setProgressText(self.tr("Aligning buildings…"))
        bldg_kept, bldg_dropped = self._write_aligned(
            parameters,
            context,
            feedback,
            layer=buildings,
            metric_crs=metric_crs,
            clip_indexes=[roads_idx, water_idx],
            output_key=self.OUTPUT_BUILDINGS,
            progress_base=30,
            progress_span=35,
        )
        if feedback.isCanceled():
            raise QgsProcessingException(self.tr("Canceled."))
        feedback.pushInfo(
            self.tr(
                f"Buildings: kept {bldg_kept}, "
                f"removed/emptied {bldg_dropped}."
            )
        )

        feedback.setProgressText(self.tr("Indexing buildings for trees…"))
        buildings_idx = self._load_index(
            buildings, metric_crs, clearance, feedback
        )
        if feedback.isCanceled():
            raise QgsProcessingException(self.tr("Canceled."))

        feedback.setProgressText(self.tr("Aligning trees…"))
        tree_kept, tree_dropped = self._write_aligned(
            parameters,
            context,
            feedback,
            layer=trees,
            metric_crs=metric_crs,
            clip_indexes=[roads_idx, water_idx, buildings_idx],
            output_key=self.OUTPUT_TREES,
            progress_base=65,
            progress_span=35,
        )
        if feedback.isCanceled():
            raise QgsProcessingException(self.tr("Canceled."))
        feedback.pushInfo(
            self.tr(
                f"Trees: kept {tree_kept}, removed/emptied {tree_dropped}."
            )
        )

        feedback.setProgress(100)
        feedback.setProgressText(self.tr("Done."))
        return {
            self.OUTPUT_WATER: parameters[self.OUTPUT_WATER],
            self.OUTPUT_BUILDINGS: parameters[self.OUTPUT_BUILDINGS],
            self.OUTPUT_TREES: parameters[self.OUTPUT_TREES],
        }

    def _write_aligned(
        self,
        parameters,
        context,
        feedback,
        *,
        layer,
        metric_crs,
        clip_indexes,
        output_key,
        progress_base,
        progress_span,
    ):
        wgs84 = epsg_4326()
        to_metric = QgsCoordinateTransform(
            layer.sourceCrs(), metric_crs, QgsProject.instance()
        )
        fields = QgsFields(layer.fields())
        wkb = layer.wkbType()
        # Output always WGS84; preserve point/polygon dimensionality roughly.
        if QgsWkbTypes.geometryType(wkb) == QgsWkbTypes.PointGeometry:
            out_wkb = (
                QgsWkbTypes.PointZ
                if QgsWkbTypes.hasZ(wkb)
                else QgsWkbTypes.Point
            )
        else:
            out_wkb = (
                QgsWkbTypes.MultiPolygon
                if QgsWkbTypes.isMultiType(wkb)
                else QgsWkbTypes.Polygon
            )
            if QgsWkbTypes.hasZ(wkb):
                out_wkb = QgsWkbTypes.addZ(out_wkb)

        sink_params, atomic = begin_atomic_file_output(parameters, output_key)
        sink, dest_id = self.parameterAsSink(
            sink_params,
            output_key,
            context,
            fields,
            out_wkb,
            wgs84,
        )
        if sink is None:
            if atomic:
                atomic.abandon()
            raise QgsProcessingException(
                self.tr(f"Could not create output sink ({output_key}).")
            )

        total = max(layer.featureCount(), 1)
        kept = 0
        dropped = 0
        canceled = False
        is_point = (
            QgsWkbTypes.geometryType(wkb) == QgsWkbTypes.PointGeometry
        )

        for i, feat in enumerate(layer.getFeatures()):
            if feedback.isCanceled():
                canceled = True
                break
            if i % 200 == 0 or i + 1 == total:
                feedback.setProgress(
                    progress_base
                    + int(progress_span * (i + 1) / total)
                )

            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                dropped += 1
                continue

            metric = QgsGeometry(geom)
            if metric.transform(to_metric) != 0:
                dropped += 1
                continue
            metric = metric.makeValid()
            if metric.isEmpty():
                dropped += 1
                continue

            if is_point:
                if self._point_blocked(metric, clip_indexes):
                    dropped += 1
                    continue
                out_geom = metric
            else:
                out_geom = self._polygon_difference(metric, clip_indexes)
                if out_geom is None or out_geom.isEmpty():
                    dropped += 1
                    continue

            out_geom = to_wgs84_geometry(out_geom, metric_crs)
            if out_geom is None or out_geom.isEmpty():
                dropped += 1
                continue

            out = QgsFeature(fields)
            out.setAttributes(feat.attributes())
            out.setGeometry(out_geom)
            sink.addFeature(out, QgsFeatureSink.FastInsert)
            kept += 1

        published = finish_or_abandon(
            atomic,
            ok=not canceled,
            sink=sink,
            context=context,
            dest_id=dest_id,
        )
        if canceled:
            raise QgsProcessingException(self.tr("Canceled."))
        if published:
            parameters[output_key] = published
        return kept, dropped

    @staticmethod
    def _point_blocked(metric_geom, clip_indexes) -> bool:
        # Use bbox of the point (or multipoint).
        rect = metric_geom.boundingBox()
        for idx in clip_indexes:
            if not idx.geoms:
                continue
            for fid in idx.index.intersects(rect):
                clip = idx.geoms.get(fid)
                if clip is not None and metric_geom.intersects(clip):
                    return True
        return False

    @staticmethod
    def _polygon_difference(metric_geom, clip_indexes):
        clip_union = None
        rect = metric_geom.boundingBox()
        for idx in clip_indexes:
            if not idx.geoms:
                continue
            for fid in idx.index.intersects(rect):
                clip = idx.geoms.get(fid)
                if clip is None or not metric_geom.intersects(clip):
                    continue
                if clip_union is None:
                    clip_union = QgsGeometry(clip)
                else:
                    clip_union = clip_union.combine(clip)
        if clip_union is None or clip_union.isEmpty():
            return metric_geom
        try:
            result = metric_geom.difference(clip_union)
        except Exception:
            return None
        if result is None:
            return None
        result = result.makeValid()
        return result

    @staticmethod
    def _load_index(layer, metric_crs, clearance, feedback) -> _GeomIndex:
        to_metric = QgsCoordinateTransform(
            layer.sourceCrs(), metric_crs, QgsProject.instance()
        )
        idx = _GeomIndex()
        n = 0
        for feat in layer.getFeatures():
            if feedback.isCanceled():
                break
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            metric = QgsGeometry(geom)
            if metric.transform(to_metric) != 0:
                continue
            metric = metric.makeValid()
            if metric.isEmpty():
                continue
            if clearance > 0:
                buffered = metric.buffer(clearance, 5)
                if buffered is not None and not buffered.isEmpty():
                    metric = buffered
            idx.add(n, metric)
            n += 1
        return idx

    @staticmethod
    def _metric_crs_for_layer(layer, feedback):
        crs = layer.sourceCrs()
        if (
            crs.isValid()
            and not crs.isGeographic()
            and crs.mapUnits() == QgsUnitTypes.DistanceMeters
        ):
            return crs
        extent = layer.extent()
        if extent.isEmpty():
            # Fallback: WGS84 UTM from a default lon
            from qgis.core import QgsCoordinateReferenceSystem

            return QgsCoordinateReferenceSystem("EPSG:32614")
        # Transform extent center to WGS84 for UTM zone.
        wgs84 = epsg_4326()
        center = QgsPointXY(extent.center())
        if crs.isValid() and crs != wgs84:
            xform = QgsCoordinateTransform(
                crs, wgs84, QgsProject.instance()
            )
            try:
                center = xform.transform(center)
            except Exception:
                pass
        lon = center.x()
        lat = center.y()
        zone = int((lon + 180.0) / 6.0) + 1
        epsg = (32600 if lat >= 0 else 32700) + zone
        from qgis.core import QgsCoordinateReferenceSystem

        metric = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
        feedback.pushInfo(
            f"Using {metric.authid()} for meter distances "
            f"(layer CRS was {crs.authid() or 'unknown'})."
        )
        return metric
