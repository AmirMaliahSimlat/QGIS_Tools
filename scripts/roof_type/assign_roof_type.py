# -*- coding: utf-8 -*-
"""
QGIS Processing: copy buildings and add integer roof_type from zone polygons.

Hardcoded field: roof_type (on both the zones layer and the output).
"""

import os
import random
import sys

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
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
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsSpatialIndex,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (_SCRIPT_DIR,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from roof_type_core import choose_roof_type  # noqa: E402

ROOF_TYPE_FIELD = "roof_type"
_COMPLETE_AREA_RATIO = 0.999


class AssignRoofTypeAlgorithm(QgsProcessingAlgorithm):
    INPUT_BUILDINGS = "INPUT_BUILDINGS"
    INPUT_ZONES = "INPUT_ZONES"
    SEED = "SEED"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return AssignRoofTypeAlgorithm()

    def name(self):
        return "assign_roof_type"

    def displayName(self):
        return self.tr("Assign roof type from zones")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Copies a buildings polygon layer and adds integer field "
            f"'{ROOF_TYPE_FIELD}' from overlapping zone polygons.\n\n"
            "Zones must already have an integer field named "
            f"'{ROOF_TYPE_FIELD}'.\n\n"
            "Rules:\n"
            "- No overlap → NULL\n"
            "- Partial overlap counts as inside\n"
            "- Completely inside a zone beats only-partial overlap with "
            "another type\n"
            "- Completely inside two different types, or only partially "
            "inside two different types → random among those types\n"
            "Optional seed makes random choices reproducible."
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
            QgsProcessingParameterVectorLayer(
                self.INPUT_ZONES,
                self.tr("Roof type zones (field roof_type)"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SEED,
                self.tr("Random seed (optional, -1 = none)"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=42,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Buildings with roof_type"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        buildings = self.parameterAsVectorLayer(
            parameters, self.INPUT_BUILDINGS, context
        )
        zones = self.parameterAsVectorLayer(
            parameters, self.INPUT_ZONES, context
        )
        seed = self.parameterAsInt(parameters, self.SEED, context)

        if buildings is None:
            raise QgsProcessingException(self.tr("Invalid buildings layer."))
        if zones is None:
            raise QgsProcessingException(self.tr("Invalid zones layer."))
        if buildings.fields().indexOf(ROOF_TYPE_FIELD) >= 0:
            raise QgsProcessingException(
                self.tr(
                    f"Buildings layer already has '{ROOF_TYPE_FIELD}'."
                )
            )
        zone_field = zones.fields().indexOf(ROOF_TYPE_FIELD)
        if zone_field < 0:
            raise QgsProcessingException(
                self.tr(
                    f"Zones layer is missing integer field '{ROOF_TYPE_FIELD}'."
                )
            )

        rng = random.Random(None if seed < 0 else seed)

        transform = None
        if buildings.sourceCrs() != zones.sourceCrs():
            transform = QgsCoordinateTransform(
                buildings.sourceCrs(),
                zones.sourceCrs(),
                QgsProject.instance(),
            )

        zone_geoms = {}
        zone_types = {}
        index = QgsSpatialIndex()
        for feat in zones.getFeatures():
            if feedback.isCanceled():
                break
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            raw = feat.attribute(zone_field)
            if raw is None:
                continue
            try:
                if isinstance(raw, QVariant) and raw.isNull():
                    continue
            except Exception:
                pass
            try:
                rtype = int(raw)
            except (TypeError, ValueError):
                continue
            fid = feat.id()
            stored = QgsFeature(fid)
            stored.setGeometry(QgsGeometry(geom))
            zone_geoms[fid] = stored.geometry()
            zone_types[fid] = rtype
            index.addFeature(stored)

        if not zone_geoms:
            raise QgsProcessingException(
                self.tr("No usable zones with a valid roof_type.")
            )

        fields = QgsFields(buildings.fields())
        fields.append(QgsField(ROOF_TYPE_FIELD, QVariant.Int))

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
        assigned = 0
        nulls = 0
        for i, feature in enumerate(buildings.getFeatures()):
            if feedback.isCanceled():
                break
            if i % 200 == 0:
                feedback.setProgress(int(100.0 * i / total))

            geom = feature.geometry()
            complete = []
            partial = []
            if geom is not None and not geom.isEmpty():
                bgeom = QgsGeometry(geom)
                if transform is not None:
                    if bgeom.transform(transform) != 0:
                        bgeom = None
                if bgeom is not None:
                    complete, partial = self._classify(
                        bgeom, index, zone_geoms, zone_types
                    )

            value = choose_roof_type(complete, partial, rng)
            attrs = list(feature.attributes())
            attrs.append(value)
            out = QgsFeature(fields)
            out.setGeometry(feature.geometry())
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.FastInsert)
            if value is None:
                nulls += 1
            else:
                assigned += 1

        feedback.setProgress(100)
        feedback.pushInfo(
            self.tr(
                f"Wrote '{ROOF_TYPE_FIELD}': assigned={assigned}, null={nulls}."
            )
        )
        return {self.OUTPUT: dest_id}

    @staticmethod
    def _classify(bgeom, index, zone_geoms, zone_types):
        complete = []
        partial = []
        area = bgeom.area()
        for fid in index.intersects(bgeom.boundingBox()):
            zgeom = zone_geoms.get(fid)
            if zgeom is None:
                continue
            if not zgeom.intersects(bgeom):
                continue
            rtype = zone_types[fid]
            if AssignRoofTypeAlgorithm._is_complete(bgeom, zgeom, area):
                complete.append(rtype)
            else:
                partial.append(rtype)
        return complete, partial

    @staticmethod
    def _is_complete(bgeom, zgeom, building_area):
        if zgeom.contains(bgeom) or bgeom.within(zgeom):
            return True
        if building_area <= 0:
            return False
        inter = zgeom.intersection(bgeom)
        if inter is None or inter.isEmpty():
            return False
        return (inter.area() / building_area) >= _COMPLETE_AREA_RATIO
