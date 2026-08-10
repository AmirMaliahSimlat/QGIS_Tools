# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: add a Double attribute with uniform random values.
"""

import random

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsProcessingParameterVectorLayer,
)


class AddRandomAttributeAlgorithm(QgsProcessingAlgorithm):
    INPUT = "INPUT"
    FIELD = "FIELD"
    MIN_VALUE = "MIN_VALUE"
    MAX_VALUE = "MAX_VALUE"
    SEED = "SEED"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return AddRandomAttributeAlgorithm()

    def name(self):
        return "add_random_attribute"

    def displayName(self):
        return self.tr("Add random attribute")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Copies a vector layer and adds a Double attribute filled with "
            "independent uniform random values between the given minimum and "
            "maximum (inclusive of the continuous range). "
            "Optional seed makes results reproducible."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT,
                self.tr("Input layer"),
                [QgsProcessing.TypeVector],
            )
        )
        self.addParameter(
            QgsProcessingParameterString(
                self.FIELD,
                self.tr("Attribute name"),
                defaultValue="rand_val",
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN_VALUE,
                self.tr("Minimum value"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MAX_VALUE,
                self.tr("Maximum value"),
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
                self.tr("Output layer"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(parameters, self.INPUT, context)
        field_name = self.parameterAsString(parameters, self.FIELD, context).strip()
        min_v = self.parameterAsDouble(parameters, self.MIN_VALUE, context)
        max_v = self.parameterAsDouble(parameters, self.MAX_VALUE, context)
        seed = self.parameterAsInt(parameters, self.SEED, context)

        if layer is None:
            raise QgsProcessingException(self.tr("Invalid input layer."))
        if not field_name:
            raise QgsProcessingException(self.tr("Attribute name must not be empty."))
        if min_v > max_v:
            raise QgsProcessingException(self.tr("Minimum must be <= maximum."))
        if layer.fields().indexOf(field_name) >= 0:
            raise QgsProcessingException(
                self.tr(f"Field '{field_name}' already exists.")
            )

        if seed >= 0:
            random.seed(seed)

        fields = QgsFields(layer.fields())
        fields.append(QgsField(field_name, QVariant.Double))

        (sink, dest_id) = self.parameterAsSink(
            parameters,
            self.OUTPUT,
            context,
            fields,
            layer.wkbType(),
            layer.sourceCrs(),
        )
        if sink is None:
            raise QgsProcessingException(self.tr("Could not create output sink."))

        total = max(layer.featureCount(), 1)
        for current, feature in enumerate(layer.getFeatures()):
            if feedback.isCanceled():
                break
            out = QgsFeature(fields)
            out.setGeometry(feature.geometry())
            attrs = list(feature.attributes())
            attrs.append(random.uniform(min_v, max_v))
            out.setAttributes(attrs)
            sink.addFeature(out, QgsFeatureSink.FastInsert)
            feedback.setProgress(int(100.0 * current / total))

        feedback.pushInfo(
            self.tr(
                f"Added '{field_name}' ~ Uniform({min_v}, {max_v}) "
                f"to {layer.featureCount()} features."
            )
        )
        return {self.OUTPUT: dest_id}
