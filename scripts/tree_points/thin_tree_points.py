# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: uniformly thin a point layer.

Keeps a random subset of features (default 40%) so the map looks the same
but sparser. Does not use polygons. Does not overwrite the input.
"""

import random

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
)


class ThinTreePointsAlgorithm(QgsProcessingAlgorithm):
    INPUT_POINTS = "INPUT_POINTS"
    KEEP_FRACTION = "KEEP_FRACTION"
    SEED = "SEED"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return ThinTreePointsAlgorithm()

    def name(self):
        return "thin_tree_points"

    def displayName(self):
        return self.tr("Thin tree points")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Copies a random subset of points (default 40%) to a new layer. "
            "Use this to sparsify a dense tree-point layer without polygons. "
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
            QgsProcessingParameterNumber(
                self.KEEP_FRACTION,
                self.tr("Keep fraction (0–1)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.4,
                minValue=0.0,
                maxValue=1.0,
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
                self.tr("Thinned tree points"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        points = self.parameterAsVectorLayer(
            parameters, self.INPUT_POINTS, context
        )
        keep_fraction = self.parameterAsDouble(
            parameters, self.KEEP_FRACTION, context
        )
        seed = self.parameterAsInt(parameters, self.SEED, context)

        if points is None:
            raise QgsProcessingException(self.tr("Invalid points layer."))
        if keep_fraction < 0 or keep_fraction > 1:
            raise QgsProcessingException(
                self.tr("Keep fraction must be between 0 and 1.")
            )

        n = points.featureCount()
        if n < 0:
            n = sum(1 for _ in points.getFeatures())
        k = int(round(n * keep_fraction))
        k = max(0, min(k, n))

        rng = random.Random(None if seed < 0 else seed)
        kept = set(rng.sample(range(n), k)) if k < n else set(range(n))

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

        written = 0
        for i, feat in enumerate(points.getFeatures()):
            if feedback.isCanceled():
                break
            if i not in kept:
                continue
            sink.addFeature(QgsFeature(feat), QgsFeatureSink.FastInsert)
            written += 1
            if written % 5000 == 0:
                feedback.setProgress(int(100.0 * i / max(n, 1)))
                feedback.setProgressText(
                    self.tr(f"Writing {written}/{k} points…")
                )

        feedback.setProgress(100)
        feedback.pushInfo(
            self.tr(
                f"Kept {written} of {n} points "
                f"({100.0 * written / max(n, 1):.1f}%)."
            )
        )
        return {self.OUTPUT: dest_id}
