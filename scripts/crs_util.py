# -*- coding: utf-8 -*-
"""CRS helpers: vector outputs are always EPSG:4326."""

from __future__ import annotations

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProject,
)

WGS84 = "EPSG:4326"


def epsg_4326() -> QgsCoordinateReferenceSystem:
    return QgsCoordinateReferenceSystem(WGS84)


def to_wgs84_geometry(geom, source_crs):
    """
    Return a copy of ``geom`` in EPSG:4326.

    If ``source_crs`` is missing/invalid or already 4326, returns a copy.
    Returns None if the transform fails.
    """
    if geom is None:
        return None
    g = QgsGeometry(geom)
    if g.isEmpty():
        return g
    wgs84 = epsg_4326()
    if source_crs is None or not source_crs.isValid() or source_crs == wgs84:
        return g
    xform = QgsCoordinateTransform(source_crs, wgs84, QgsProject.instance())
    if g.transform(xform) != 0:
        return None
    return g
