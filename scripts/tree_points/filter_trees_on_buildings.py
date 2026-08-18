# -*- coding: utf-8 -*-
"""
QGIS Processing: drop tree points that fall on (or within clearance of)
building footprints. Writes a new layer; does not overwrite the input.
"""

import math

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
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
)


class FilterTreesOnBuildingsAlgorithm(QgsProcessingAlgorithm):
    INPUT_POINTS = "INPUT_POINTS"
    INPUT_BUILDINGS = "INPUT_BUILDINGS"
    BUILDING_CLEARANCE = "BUILDING_CLEARANCE"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return FilterTreesOnBuildingsAlgorithm()

    def name(self):
        return "filter_trees_on_buildings"

    def displayName(self):
        return self.tr("Remove tree points on buildings")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Copies a tree-point layer, dropping any point that falls inside "
            "a building footprint or within the clearance distance (default "
            "1 m). Use this on points you already generated, without "
            "re-running polygon-to-points.\n\n"
            "The input is not overwritten."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_POINTS,
                self.tr("Tree points"),
                [QgsProcessing.TypeVectorPoint],
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_BUILDINGS,
                self.tr("Buildings footprints"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUILDING_CLEARANCE,
                self.tr("Clearance from buildings (meters)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Trees away from buildings"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        points = self.parameterAsVectorLayer(
            parameters, self.INPUT_POINTS, context
        )
        buildings = self.parameterAsVectorLayer(
            parameters, self.INPUT_BUILDINGS, context
        )
        clearance = self.parameterAsDouble(
            parameters, self.BUILDING_CLEARANCE, context
        )

        if points is None:
            raise QgsProcessingException(self.tr("Invalid points layer."))
        if buildings is None:
            raise QgsProcessingException(self.tr("Invalid buildings layer."))
        if clearance < 0:
            raise QgsProcessingException(
                self.tr("Building clearance must be ≥ 0.")
            )

        metric_crs = self._metric_crs_for_layer(points, feedback)
        to_metric = QgsCoordinateTransform(
            points.sourceCrs(), metric_crs, QgsProject.instance()
        )

        feedback.setProgressText(self.tr("Indexing buildings…"))
        index, geoms, n_bldg = self._index_buildings(
            buildings, metric_crs, clearance, feedback
        )
        feedback.pushInfo(
            self.tr(
                f"Testing against {n_bldg} buildings "
                f"({clearance} m clearance, {metric_crs.authid()})."
            )
        )

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            points.fields(),
            points.wkbType(),
            points.sourceCrs(),
        )
        if sink is None:
            raise QgsProcessingException(
                self.tr("Could not create output sink.")
            )

        n = max(points.featureCount(), 1)
        kept = 0
        dropped = 0
        for i, feat in enumerate(points.getFeatures()):
            if feedback.isCanceled():
                break
            if i % 5000 == 0:
                feedback.setProgress(int(100.0 * i / n))
                feedback.setProgressText(
                    self.tr(
                        f"Filtering… {i}/{points.featureCount()} "
                        f"(kept={kept}, dropped={dropped})"
                    )
                )

            geom = feat.geometry()
            blocked = False
            if geom is not None and not geom.isEmpty() and geoms:
                pt = geom.asPoint()
                mpt = to_metric.transform(QgsPointXY(pt.x(), pt.y()))
                xy = (mpt.x(), mpt.y())
                blocked = self._blocked_by_building(xy, index, geoms)

            if blocked:
                dropped += 1
                continue
            sink.addFeature(QgsFeature(feat), QgsFeatureSink.FastInsert)
            kept += 1

        feedback.setProgress(100)
        feedback.pushInfo(
            self.tr(
                f"Kept {kept}, removed {dropped} "
                f"({100.0 * dropped / max(kept + dropped, 1):.1f}% on/near buildings)."
            )
        )
        return {self.OUTPUT: dest_id}

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
            f"Using {metric.authid()} for meter distances "
            f"(layer CRS was {crs.authid() or 'unknown'})."
        )
        return metric

    @staticmethod
    def _index_buildings(buildings, metric_crs, clearance, feedback):
        to_metric = QgsCoordinateTransform(
            buildings.sourceCrs(), metric_crs, QgsProject.instance()
        )
        index = QgsSpatialIndex()
        geoms = {}
        n = 0
        for feat in buildings.getFeatures():
            if feedback.isCanceled():
                break
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            metric_geom = QgsGeometry(geom)
            if metric_geom.transform(to_metric) != 0:
                continue
            metric_geom = metric_geom.makeValid()
            if metric_geom.isEmpty():
                continue
            if clearance > 0:
                buffered = metric_geom.buffer(clearance, 8)
                if buffered is None or buffered.isEmpty():
                    buffered = metric_geom
                metric_geom = buffered
            stored = QgsFeature(n)
            stored.setGeometry(metric_geom)
            geoms[n] = stored.geometry()
            index.addFeature(stored)
            n += 1
        return index, geoms, n

    @staticmethod
    def _blocked_by_building(xy, index, geoms):
        if not geoms:
            return False
        probe = QgsGeometry.fromPointXY(QgsPointXY(xy[0], xy[1]))
        for fid in index.intersects(probe.boundingBox()):
            g = geoms.get(fid)
            if g is not None and g.intersects(probe):
                return True
        return False
