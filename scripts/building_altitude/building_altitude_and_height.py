# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: sample min/max terrain altitude from quantized-mesh
exterior-ring vertices, then add a random height attribute.

Hardcoded fields:
  altitude      = min mesh elevation
  max_altitude  = max mesh elevation
  height        = Uniform(min, max) + (max_altitude - altitude)
"""

import math
import os
import random
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
MAX_ALTITUDE_FIELD = "max_altitude"
HEIGHT_FIELD = "height"


class BuildingAltitudeAndHeightAlgorithm(QgsProcessingAlgorithm):
    INPUT_BUILDINGS = "INPUT_BUILDINGS"
    INPUT_MESH = "INPUT_MESH"
    MIN_VALUE = "MIN_VALUE"
    MAX_VALUE = "MAX_VALUE"
    SEED = "SEED"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return BuildingAltitudeAndHeightAlgorithm()

    def name(self):
        return "building_altitude_and_height"

    def displayName(self):
        return self.tr("Building altitude and random height")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Copies a buildings polygon layer and adds three Double attributes:\n"
            f"  {ALTITUDE_FIELD} — minimum terrain altitude on exterior-ring "
            "vertices and edge midpoints\n"
            f"  {MAX_ALTITUDE_FIELD} — maximum terrain altitude on the same "
            "sample points\n"
            f"  {HEIGHT_FIELD} — Uniform(min, max) + "
            f"({MAX_ALTITUDE_FIELD} - {ALTITUDE_FIELD})\n"
            "Terrain comes from a Cesium quantized-mesh tileset "
            "({x}/{y}.terrain, gzip, EPSG:4326; finest LOD in the folder). "
            "Holes are ignored. Features with no valid mesh samples get NULL "
            "for all three fields. Attribute names are fixed."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_BUILDINGS,
                self.tr("Buildings footprints"),
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
                self.MIN_VALUE,
                self.tr("Minimum random height component"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_VALUE,
                self.tr("Maximum random height component"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SEED,
                self.tr("Random seed (optional, -1 = none)"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=-1,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Buildings with altitude and height"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        buildings = self.parameterAsVectorLayer(
            parameters, self.INPUT_BUILDINGS, context
        )
        mesh_folder = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        min_v = self.parameterAsDouble(parameters, self.MIN_VALUE, context)
        max_v = self.parameterAsDouble(parameters, self.MAX_VALUE, context)
        seed = self.parameterAsInt(parameters, self.SEED, context)

        if buildings is None:
            raise QgsProcessingException(self.tr("Invalid buildings layer."))
        if not mesh_folder or not os.path.isdir(mesh_folder):
            raise QgsProcessingException(
                self.tr("Invalid quantized-mesh tiles folder.")
            )
        if min_v > max_v:
            raise QgsProcessingException(self.tr("Minimum must be <= maximum."))

        for name in (ALTITUDE_FIELD, MAX_ALTITUDE_FIELD, HEIGHT_FIELD):
            if buildings.fields().indexOf(name) >= 0:
                raise QgsProcessingException(
                    self.tr(
                        f"Field '{name}' already exists on the buildings layer."
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

        if seed >= 0:
            random.seed(seed)

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        transform = None
        if buildings.crs() != wgs84:
            transform = QgsCoordinateTransform(
                buildings.crs(),
                wgs84,
                QgsProject.instance(),
            )

        fields = QgsFields(buildings.fields())
        fields.append(QgsField(ALTITUDE_FIELD, QVariant.Double))
        fields.append(QgsField(MAX_ALTITUDE_FIELD, QVariant.Double))
        fields.append(QgsField(HEIGHT_FIELD, QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            buildings.wkbType(),
            buildings.sourceCrs(),
        )
        if sink is None:
            raise QgsProcessingException(
                self.tr("Could not create output sink.")
            )

        total = max(buildings.featureCount(), 1)
        filled = 0
        nulls = 0

        for current, feature in enumerate(buildings.getFeatures()):
            if feedback.isCanceled():
                break

            out_feature = QgsFeature(fields)
            out_feature.setGeometry(feature.geometry())
            attrs = list(feature.attributes())

            min_z, max_z = self._exterior_altitude_range(
                feature.geometry(), sampler, transform
            )
            if (
                min_z is not None
                and max_z is not None
                and math.isfinite(min_z)
                and math.isfinite(max_z)
            ):
                height = random.uniform(min_v, max_v) + (max_z - min_z)
                attrs.extend([min_z, max_z, height])
                filled += 1
            else:
                attrs.extend([None, None, None])
                nulls += 1

            out_feature.setAttributes(attrs)
            sink.addFeature(out_feature, QgsFeatureSink.FastInsert)
            feedback.setProgress(int(100.0 * current / total))

        feedback.pushInfo(
            self.tr(
                f"Wrote {ALTITUDE_FIELD}, {MAX_ALTITUDE_FIELD}, {HEIGHT_FIELD}; "
                f"filled={filled}, null={nulls}."
            )
        )
        return {self.OUTPUT: dest_id}

    def _exterior_altitude_range(self, geometry, sampler, transform):
        """(min, max) valid mesh samples on exterior rings, or (None, None)."""
        if geometry is None or geometry.isEmpty():
            return None, None

        min_z = None
        max_z = None
        for exterior in self._exterior_rings(geometry):
            min_z, max_z = self._update_range_from_ring(
                exterior, sampler, transform, min_z, max_z
            )
        return min_z, max_z

    def _exterior_rings(self, geometry):
        """Yield exterior ring point sequences for Polygon / MultiPolygon."""
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
    def _update_range_from_ring(cls, ring, sampler, transform, min_z, max_z):
        """Sample each vertex and each edge midpoint (between neighbors)."""
        if not ring:
            return min_z, max_z

        n = len(ring)
        for i in range(n):
            min_z, max_z = cls._sample_xy(
                ring[i][0], ring[i][1], sampler, transform, min_z, max_z
            )
            if i + 1 >= n:
                continue
            x1, y1 = ring[i][0], ring[i][1]
            x2, y2 = ring[i + 1][0], ring[i + 1][1]
            min_z, max_z = cls._sample_xy(
                0.5 * (x1 + x2),
                0.5 * (y1 + y2),
                sampler,
                transform,
                min_z,
                max_z,
            )

        return min_z, max_z

    @staticmethod
    def _sample_xy(x, y, sampler, transform, min_z, max_z):
        if transform is not None:
            pt = transform.transform(x, y)
            x, y = pt.x(), pt.y()

        value = sampler.sample(x, y)
        if value is None:
            return min_z, max_z
        try:
            z = float(value)
        except (TypeError, ValueError):
            return min_z, max_z
        if min_z is None or z < min_z:
            min_z = z
        if max_z is None or z > max_z:
            max_z = z
        return min_z, max_z
