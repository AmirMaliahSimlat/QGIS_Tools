# -*- coding: utf-8 -*-
"""
QGIS Processing: sample GeoTIFF RGB at tree points into fields R, G, B.

Imagery input is a folder: all .tif/.tiff files are used, including those
in any subfolders. Non-TIFF files are ignored.
"""

import os
import sys
from collections import OrderedDict

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    Qgis,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsRaster,
    QgsRasterLayer,
    QgsRectangle,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

from rgb_core import (  # noqa: E402
    B_FIELD,
    G_FIELD,
    R_FIELD,
    list_tiff_files,
    rgb_from_bands,
)

_CACHE_SIZE = 16


def _data_kind(qgis_type) -> str:
    byte_types = [Qgis.Byte]
    if hasattr(Qgis, "UInt8"):
        byte_types.append(Qgis.UInt8)
    if qgis_type in byte_types:
        return "byte"
    if qgis_type in (Qgis.Float32, Qgis.Float64):
        return "float"
    if qgis_type in (Qgis.UInt16, Qgis.Int16):
        return "uint16"
    return "other"


class _TileInfo:
    __slots__ = ("path", "extent", "crs")

    def __init__(self, path, extent, crs):
        self.path = path
        self.extent = extent
        self.crs = crs


class SampleTreeRgbAlgorithm(QgsProcessingAlgorithm):
    INPUT_POINTS = "INPUT_POINTS"
    INPUT_FOLDER = "INPUT_FOLDER"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return SampleTreeRgbAlgorithm()

    def name(self):
        return "sample_tree_rgb"

    def displayName(self):
        return self.tr("Sample tree RGB from GeoTIFF")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Copies tree points and adds integer fields R, G, B sampled from "
            "georeferenced TIFF imagery (bands 1/2/3 = red/green/blue).\n\n"
            "Choose a folder of imagery. The tool recursively finds all "
            ".tif/.tiff files in that folder and any subfolders; other files "
            "are ignored. Each point is sampled from the tile whose extent "
            "contains it (first valid tile if tiles overlap).\n\n"
            "Points outside all images, or on NoData, get NULL. Coordinates "
            "are transformed to each raster CRS if needed."
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
            QgsProcessingParameterFile(
                self.INPUT_FOLDER,
                self.tr("Imagery folder (TIFF files, including subfolders)"),
                behavior=QgsProcessingParameterFile.Folder,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Trees with RGB"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        points = self.parameterAsVectorLayer(
            parameters, self.INPUT_POINTS, context
        )
        folder = self.parameterAsFile(parameters, self.INPUT_FOLDER, context)
        if points is None:
            raise QgsProcessingException(self.tr("Invalid points layer."))
        if not folder or not os.path.isdir(folder):
            raise QgsProcessingException(self.tr("Invalid imagery folder."))
        for name in (R_FIELD, G_FIELD, B_FIELD):
            if points.fields().indexOf(name) >= 0:
                raise QgsProcessingException(
                    self.tr(f"Points layer already has '{name}'.")
                )

        paths = list_tiff_files(folder)
        if not paths:
            raise QgsProcessingException(
                self.tr("No .tif/.tiff files found in the folder or subfolders.")
            )
        feedback.pushInfo(self.tr(f"Found {len(paths)} TIFF file(s)."))

        tiles = []
        skipped = 0
        for i, path in enumerate(paths):
            if feedback.isCanceled():
                break
            if i % 25 == 0:
                feedback.setProgressText(
                    self.tr(f"Indexing TIFFs… {i + 1}/{len(paths)}")
                )
            layer = QgsRasterLayer(path, os.path.basename(path))
            if not layer.isValid() or layer.bandCount() < 3:
                skipped += 1
                continue
            tiles.append(_TileInfo(path, QgsRectangle(layer.extent()), layer.crs()))
            del layer
        if skipped:
            feedback.pushWarning(
                self.tr(
                    f"Skipped {skipped} TIFF(s) that were invalid or had "
                    "fewer than 3 bands."
                )
            )
        if not tiles:
            raise QgsProcessingException(
                self.tr("No usable RGB GeoTIFFs (need ≥ 3 bands).")
            )
        feedback.pushInfo(self.tr(f"Using {len(tiles)} RGB GeoTIFF(s)."))

        transforms = {}

        def to_raster_xy(pt_xy, raster_crs):
            key = raster_crs.authid() or raster_crs.toWkt()
            if points.sourceCrs() == raster_crs:
                return pt_xy
            if key not in transforms:
                transforms[key] = QgsCoordinateTransform(
                    points.sourceCrs(), raster_crs, QgsProject.instance()
                )
            return transforms[key].transform(pt_xy)

        cache = OrderedDict()

        def open_tile(path):
            if path in cache:
                cache.move_to_end(path)
                return cache[path]
            layer = QgsRasterLayer(path, os.path.basename(path))
            if not layer.isValid():
                return None
            cache[path] = layer
            while len(cache) > _CACHE_SIZE:
                cache.popitem(last=False)
            return layer

        fields = QgsFields(points.fields())
        fields.append(QgsField(R_FIELD, QVariant.Int))
        fields.append(QgsField(G_FIELD, QVariant.Int))
        fields.append(QgsField(B_FIELD, QVariant.Int))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            points.wkbType(),
            points.sourceCrs(),
        )
        if sink is None:
            raise QgsProcessingException(
                self.tr("Could not create output sink.")
            )

        total = max(points.featureCount(), 1)
        filled = 0
        nulls = 0
        for i, feature in enumerate(points.getFeatures()):
            if feedback.isCanceled():
                break
            if i % 2000 == 0:
                feedback.setProgress(int(100.0 * i / total))
                feedback.setProgressText(
                    self.tr(f"Sampling RGB… {i}/{points.featureCount()}")
                )

            r = g = b = None
            geom = feature.geometry()
            if geom is not None and not geom.isEmpty():
                src_xy = QgsPointXY(geom.asPoint().x(), geom.asPoint().y())
                r, g, b = self._sample_point(
                    src_xy, tiles, to_raster_xy, open_tile
                )

            attrs = list(feature.attributes())
            attrs.extend([r, g, b])
            out = QgsFeature(fields)
            out.setGeometry(feature.geometry())
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.FastInsert)
            if r is None and g is None and b is None:
                nulls += 1
            else:
                filled += 1

        feedback.setProgress(100)
        feedback.pushInfo(
            self.tr(f"RGB sampled: filled={filled}, null={nulls}.")
        )
        return {self.OUTPUT: dest_id}

    @staticmethod
    def _sample_point(src_xy, tiles, to_raster_xy, open_tile):
        for tile in tiles:
            xy = to_raster_xy(src_xy, tile.crs)
            if not tile.extent.contains(xy):
                continue
            layer = open_tile(tile.path)
            if layer is None:
                continue
            provider = layer.dataProvider()
            ident = provider.identify(xy, QgsRaster.IdentifyFormatValue)
            if not ident.isValid():
                continue
            results = ident.results()
            kind = _data_kind(provider.dataType(1))
            nodata = provider.sourceNoDataValue(1)
            if provider.sourceHasNoDataValue(1) is False:
                nodata = None
            r, g, b = rgb_from_bands(
                results.get(1),
                results.get(2),
                results.get(3),
                nodata=nodata,
                data_kind=kind,
            )
            if r is not None:
                return r, g, b
        return None, None, None
