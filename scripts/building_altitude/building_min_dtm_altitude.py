# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: assign each building the minimum terrain altitude
sampled from a Cesium quantized-mesh tileset at its exterior-ring vertices.
"""

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
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsProcessingParameterString,
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


class BuildingMinDtmAltitudeAlgorithm(QgsProcessingAlgorithm):
    INPUT_BUILDINGS = "INPUT_BUILDINGS"
    INPUT_MESH = "INPUT_MESH"
    ALTITUDE_FIELD = "ALTITUDE_FIELD"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return BuildingMinDtmAltitudeAlgorithm()

    def name(self):
        return "building_min_dtm_altitude"

    def displayName(self):
        return self.tr("Building min quantized-mesh altitude")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Copies a buildings polygon layer and adds a Double attribute "
            "holding the minimum terrain altitude sampled from a Cesium "
            "quantized-mesh tileset at each feature's exterior-ring vertices "
            "(holes are ignored). "
            "Expects a folder of {x}/{y}.terrain tiles (gzip), geographic "
            "EPSG:4326 / TMS layout — the highest (finest) LOD present in that "
            "folder is used. "
            "This is ground altitude from the mesh, not architectural building "
            "size. Features with no valid samples get NULL."
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
            QgsProcessingParameterString(
                self.ALTITUDE_FIELD,
                self.tr("Output attribute name"),
                defaultValue="altitude",
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Buildings with altitude"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        buildings = self.parameterAsVectorLayer(
            parameters, self.INPUT_BUILDINGS, context
        )
        mesh_folder = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        altitude_field = self.parameterAsString(
            parameters, self.ALTITUDE_FIELD, context
        ).strip()

        if buildings is None:
            raise QgsProcessingException(self.tr("Invalid buildings layer."))
        if not mesh_folder or not os.path.isdir(mesh_folder):
            raise QgsProcessingException(
                self.tr("Invalid quantized-mesh tiles folder.")
            )
        if not altitude_field:
            raise QgsProcessingException(
                self.tr("Output attribute name must not be empty.")
            )
        if buildings.fields().indexOf(altitude_field) >= 0:
            raise QgsProcessingException(
                self.tr(
                    f"Field '{altitude_field}' already exists on the buildings layer."
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
        if buildings.crs() != wgs84:
            transform = QgsCoordinateTransform(
                buildings.crs(),
                wgs84,
                QgsProject.instance(),
            )

        fields = QgsFields(buildings.fields())
        fields.append(QgsField(altitude_field, QVariant.Double))

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

        for current, feature in enumerate(buildings.getFeatures()):
            if feedback.isCanceled():
                break

            out_feature = QgsFeature(fields)
            out_feature.setGeometry(feature.geometry())
            attrs = list(feature.attributes())
            attrs.append(
                self._min_exterior_altitude(
                    feature.geometry(), sampler, transform
                )
            )
            out_feature.setAttributes(attrs)
            sink.addFeature(out_feature, QgsFeatureSink.FastInsert)
            feedback.setProgress(int(100.0 * current / total))

        return {self.OUTPUT: dest_id}

    def _min_exterior_altitude(self, geometry, sampler, transform):
        """Minimum valid mesh sample on exterior rings, or None."""
        if geometry is None or geometry.isEmpty():
            return None

        min_z = None
        for exterior in self._exterior_rings(geometry):
            min_z = self._update_min_from_ring(
                exterior, sampler, transform, min_z
            )
        return min_z

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

    @staticmethod
    def _update_min_from_ring(ring, sampler, transform, min_z):
        if not ring:
            return min_z

        for point in ring:
            x, y = point[0], point[1]
            if transform is not None:
                pt = transform.transform(x, y)
                x, y = pt.x(), pt.y()

            value = sampler.sample(x, y)
            if value is None:
                continue
            try:
                z = float(value)
            except (TypeError, ValueError):
                continue
            if min_z is None or z < min_z:
                min_z = z

        return min_z
