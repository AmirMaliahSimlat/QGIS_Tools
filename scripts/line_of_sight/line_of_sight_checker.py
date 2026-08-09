# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: Line-of-Sight checker against quantized-mesh terrain
and optionally extruded buildings.
"""

import os
import sys

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPoint,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingOutputBoolean,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSource,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProject,
    QgsWkbTypes,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_SCRIPT_DIR)
for _p in (_SCRIPT_DIR, _SCRIPTS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from los_core import BuildingIndex, BuildingPrism, check_line_of_sight  # noqa: E402
from quantized_mesh import QuantizedMeshSampler  # noqa: E402


class LineOfSightCheckerAlgorithm(QgsProcessingAlgorithm):
    START_POINT = "START_POINT"
    END_POINT = "END_POINT"
    INPUT_MESH = "INPUT_MESH"
    CONSIDER_BUILDINGS = "CONSIDER_BUILDINGS"
    BUILDINGS = "BUILDINGS"
    SAMPLE_SPACING = "SAMPLE_SPACING"
    CLEARANCE_TOL = "CLEARANCE_TOL"
    OUTPUT_CLEAR = "OUTPUT_CLEAR"

    ALTITUDE_FIELD = "altitude"
    HEIGHT_FIELD = "RELATIVE_F"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return LineOfSightCheckerAlgorithm()

    def name(self):
        return "line_of_sight_checker"

    def displayName(self):
        return self.tr("Line-of-Sight checker")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Input: two 3D points (PointZ). "
            "Optional flag to also block on buildings from the altitude "
            "shapefile (footprint extruded from 'altitude' up by 'RELATIVE_F'). "
            "When the flag is off, only quantized-mesh terrain is tested. "
            "Output: true if the line hits nothing, false if it hits terrain "
            "or (when enabled) a building."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.START_POINT,
                self.tr("Start 3D point"),
                [QgsProcessing.TypeVectorPoint],
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSource(
                self.END_POINT,
                self.tr("End 3D point"),
                [QgsProcessing.TypeVectorPoint],
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
            QgsProcessingParameterBoolean(
                self.CONSIDER_BUILDINGS,
                self.tr("Consider buildings"),
                defaultValue=False,
            )
        )
        buildings = QgsProcessingParameterFeatureSource(
            self.BUILDINGS,
            self.tr("Buildings with altitude (extruded by RELATIVE_F)"),
            [QgsProcessing.TypeVectorPolygon],
            optional=True,
        )
        self.addParameter(buildings)
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SAMPLE_SPACING,
                self.tr("Distance between sample points on the line (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0,
                minValue=0.1,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.CLEARANCE_TOL,
                self.tr("Terrain clearance tolerance (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.5,
                minValue=0.0,
            )
        )
        self.addOutput(
            QgsProcessingOutputBoolean(
                self.OUTPUT_CLEAR,
                self.tr("Clear (true if no hit)"),
            )
        )

    def _read_3d_point(self, source, label):
        if source is None:
            raise QgsProcessingException(self.tr(f"Invalid {label} layer."))
        features = list(source.getFeatures())
        if not features:
            raise QgsProcessingException(
                self.tr(f"{label}: no features found (select one PointZ feature).")
            )
        geom = features[0].geometry()
        if geom is None or geom.isEmpty():
            raise QgsProcessingException(self.tr(f"{label}: empty geometry."))
        if not QgsWkbTypes.hasZ(geom.wkbType()):
            raise QgsProcessingException(
                self.tr(
                    f"{label}: must be a 3D point (PointZ) with altitude as Z."
                )
            )
        vertex = geom.vertexAt(0)
        return QgsPoint(vertex.x(), vertex.y(), vertex.z()), source.sourceCrs()

    def _load_building_index(self, source, feedback):
        if source is None:
            raise QgsProcessingException(
                self.tr(
                    "Consider buildings is on, but no buildings layer was provided. "
                    "Use the buildings shapefile with an 'altitude' attribute "
                    "(output of Building min quantized-mesh altitude)."
                )
            )
        fields = source.fields()
        alt_idx = fields.indexOf(self.ALTITUDE_FIELD)
        h_idx = fields.indexOf(self.HEIGHT_FIELD)
        if alt_idx < 0:
            raise QgsProcessingException(
                self.tr(
                    f"Buildings layer is missing '{self.ALTITUDE_FIELD}' field."
                )
            )
        if h_idx < 0:
            raise QgsProcessingException(
                self.tr(
                    f"Buildings layer is missing '{self.HEIGHT_FIELD}' field."
                )
            )

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        xform = None
        if source.sourceCrs().isValid() and source.sourceCrs() != wgs84:
            xform = QgsCoordinateTransform(
                source.sourceCrs(), wgs84, QgsProject.instance()
            )

        prisms = []
        total = source.featureCount()
        for i, feat in enumerate(source.getFeatures()):
            if feedback.isCanceled():
                break
            if total > 0 and i % 2000 == 0:
                feedback.setProgress(int(30.0 * i / total))

            alt = feat[alt_idx]
            height = feat[h_idx]
            if alt is None or height is None:
                continue
            try:
                z_min = float(alt)
                rel_h = float(height)
            except (TypeError, ValueError):
                continue
            if rel_h <= 0:
                continue
            z_max = z_min + rel_h

            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            if xform is not None:
                geom = QgsGeometry(geom)
                geom.transform(xform)

            for exterior in self._exterior_rings_lonlat(geom):
                if len(exterior) < 3:
                    continue
                lons = [p[0] for p in exterior]
                lats = [p[1] for p in exterior]
                prisms.append(
                    BuildingPrism(
                        exterior=exterior,
                        z_min=z_min,
                        z_max=z_max,
                        min_lon=min(lons),
                        min_lat=min(lats),
                        max_lon=max(lons),
                        max_lat=max(lats),
                    )
                )

        feedback.pushInfo(
            self.tr(f"Loaded {len(prisms)} building extrusions for LOS.")
        )
        if not prisms:
            raise QgsProcessingException(
                self.tr(
                    "No usable buildings (need altitude + positive RELATIVE_F)."
                )
            )
        return BuildingIndex(prisms)

    def _exterior_rings_lonlat(self, geom: QgsGeometry):
        wkb = geom.wkbType()
        if QgsWkbTypes.isMultiType(wkb):
            multi = geom.asMultiPolygon()
            if multi:
                for poly in multi:
                    if poly:
                        yield [(p[0], p[1]) for p in poly[0]]
                return
            for part in geom.asGeometryCollection():
                yield from self._exterior_rings_lonlat(part)
            return
        poly = geom.asPolygon()
        if poly:
            yield [(p[0], p[1]) for p in poly[0]]

    def processAlgorithm(self, parameters, context, feedback):
        mesh_folder = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        consider_buildings = self.parameterAsBoolean(
            parameters, self.CONSIDER_BUILDINGS, context
        )
        spacing = self.parameterAsDouble(parameters, self.SAMPLE_SPACING, context)
        clearance_tol = self.parameterAsDouble(
            parameters, self.CLEARANCE_TOL, context
        )

        if not mesh_folder or not os.path.isdir(mesh_folder):
            raise QgsProcessingException(
                self.tr("Invalid quantized-mesh tiles folder.")
            )

        start_source = self.parameterAsSource(
            parameters, self.START_POINT, context
        )
        end_source = self.parameterAsSource(parameters, self.END_POINT, context)
        start_pt, start_crs = self._read_3d_point(start_source, "Start point")
        end_pt, end_crs = self._read_3d_point(end_source, "End point")

        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")

        def to_wgs84(pt: QgsPoint, crs):
            if crs.isValid() and crs != wgs84:
                xform = QgsCoordinateTransform(
                    crs, wgs84, QgsProject.instance()
                )
                out = QgsPoint(pt)
                out.transform(xform)
                return out
            return pt

        start_wgs = to_wgs84(start_pt, start_crs)
        end_wgs = to_wgs84(end_pt, end_crs)
        lon1, lat1, alt1 = start_wgs.x(), start_wgs.y(), start_wgs.z()
        lon2, lat2, alt2 = end_wgs.x(), end_wgs.y(), end_wgs.z()

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
        feedback.pushInfo(
            self.tr(
                f"Start ({lon1:.6f}, {lat1:.6f}, {alt1:.3f} m) → "
                f"End ({lon2:.6f}, {lat2:.6f}, {alt2:.3f} m)"
            )
        )

        building_index = None
        if consider_buildings:
            buildings_source = self.parameterAsSource(
                parameters, self.BUILDINGS, context
            )
            feedback.pushInfo(self.tr("Consider buildings: ON"))
            building_index = self._load_building_index(buildings_source, feedback)
        else:
            feedback.pushInfo(self.tr("Consider buildings: OFF (terrain only)"))

        try:
            result = check_line_of_sight(
                sampler,
                lon1,
                lat1,
                lon2,
                lat2,
                sample_spacing_m=spacing,
                clearance_tol_m=clearance_tol,
                start_absolute_altitude_m=alt1,
                end_absolute_altitude_m=alt2,
                buildings=building_index,
            )
        except Exception as exc:
            raise QgsProcessingException(str(exc)) from exc

        verdict = "true" if result.clear else "false"
        feedback.pushInfo(self.tr(f"Line-of-Sight clear: {verdict}"))
        if not result.clear:
            feedback.pushInfo(
                self.tr(
                    f"First hit ({result.hit_type}) at "
                    f"{result.first_hit_distance_m:.2f} m "
                    f"({result.first_hit_lon:.6f}, {result.first_hit_lat:.6f})"
                )
            )

        return {self.OUTPUT_CLEAR: result.clear}
