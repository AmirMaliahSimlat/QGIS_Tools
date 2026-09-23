# -*- coding: utf-8 -*-
"""
QGIS Processing: polygons where quantized-mesh ground rises through a 3D road.

Does not edit the terrain. The polygons are meant to be used as clip masks in
Unreal so only the poke-through is removed.

Uses the highest LOD in the mesh folder unless LOD is set. Coarser levels are
skipped because their large triangles would mark long stretches of road.
"""

from __future__ import annotations

import math
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
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsRectangle,
    QgsWkbTypes,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_SCRIPT_DIR)
for _p in (_SCRIPT_DIR, _SCRIPTS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atomic_io import begin_atomic_file_output, finish_or_abandon  # noqa: E402
from crs_util import epsg_4326  # noqa: E402
from parallel_util import map_in_processes, resolve_workers  # noqa: E402
from qm_burn_core import fan_triangles_from_ring  # noqa: E402
from quantized_mesh import discover_terrain_tiles, tile_rectangle  # noqa: E402
from road_bump_core import RoadGrid, process_tile_task  # noqa: E402
from road_clip_simplify_core import RoadEdges, simplify_clips  # noqa: E402

# Drop isolated crumbs before the buffer so they do not swell into clip disks.
_PRE_BUFFER_MIN_M2 = 0.01


def _force_2d(geom: QgsGeometry) -> QgsGeometry:
    if geom is None or geom.isEmpty():
        return QgsGeometry()
    g = QgsGeometry(geom)
    try:
        raw = g.get()
        if raw is not None and hasattr(raw, "clone"):
            clone = raw.clone()
            if hasattr(clone, "dropZValue"):
                clone.dropZValue()
            if hasattr(clone, "dropMValue"):
                clone.dropMValue()
            return QgsGeometry(clone)
    except Exception:
        pass
    return QgsGeometry.fromWkt(g.asWkt())


def _ring_xyz(geom_part) -> list:
    if geom_part is None:
        return []
    ring = (
        geom_part.exteriorRing()
        if hasattr(geom_part, "exteriorRing")
        else geom_part
    )
    if ring is None:
        return []
    pts = []
    for i in range(ring.numPoints()):
        p = ring.pointN(i)
        try:
            zf = float(p.z()) if hasattr(p, "z") else 0.0
            if zf != zf:
                zf = 0.0
        except (TypeError, ValueError):
            zf = 0.0
        pts.append((float(p.x()), float(p.y()), zf))
    return pts


def _triangles_from_geometry(geom: QgsGeometry) -> list:
    if geom is None or geom.isEmpty():
        return []
    g = QgsGeometry(geom)
    wkb = g.wkbType()
    if QgsWkbTypes.geometryType(wkb) != QgsWkbTypes.PolygonGeometry:
        return []
    const = g.constGet()
    if const is None:
        return []
    tris = []
    if QgsWkbTypes.isMultiType(wkb):
        for i in range(const.numGeometries()):
            tris.extend(fan_triangles_from_ring(_ring_xyz(const.geometryN(i))))
    else:
        tris.extend(fan_triangles_from_ring(_ring_xyz(const)))
    return [
        tri
        for tri in tris
        if all(math.isfinite(c) for p in tri for c in p)
    ]


def _iter_polygons(geom: QgsGeometry):
    if geom is None or geom.isEmpty():
        return
    if QgsWkbTypes.geometryType(geom.wkbType()) == QgsWkbTypes.PolygonGeometry:
        if geom.isMultipart():
            for part in geom.asGeometryCollection():
                if part is not None and not part.isEmpty():
                    yield part
        else:
            yield geom
        return
    flat = QgsWkbTypes.flatType(geom.wkbType())
    if flat == QgsWkbTypes.GeometryCollection:
        for part in geom.asGeometryCollection():
            yield from _iter_polygons(part)


def _polygons_only(geom: QgsGeometry):
    parts = [p for p in _iter_polygons(geom) if p is not None and not p.isEmpty()]
    if not parts:
        return None
    if len(parts) == 1:
        return parts[0]
    return _union_all(parts)


def _union_all(geoms):
    clean = [g for g in geoms if g is not None and not g.isEmpty()]
    if not clean:
        return None
    if len(clean) == 1:
        return QgsGeometry(clean[0])
    batch = 1500
    acc = list(clean)
    while len(acc) > 1:
        nxt = []
        for i in range(0, len(acc), batch):
            chunk = acc[i : i + batch]
            if len(chunk) == 1:
                nxt.append(chunk[0])
                continue
            merged = QgsGeometry.unaryUnion(chunk)
            if merged is not None and not merged.isEmpty():
                nxt.append(merged)
            else:
                nxt.extend(chunk)
        if not nxt or len(nxt) >= len(acc):
            break
        acc = nxt
    if len(acc) == 1:
        return acc[0]
    merged = QgsGeometry.unaryUnion(acc)
    if merged is not None and not merged.isEmpty():
        return merged
    collected = QgsGeometry.collectGeometry(acc)
    if collected is None or collected.isEmpty():
        return None
    return collected


def _ring_geom(ring) -> QgsGeometry:
    pts = [QgsPointXY(float(x), float(y)) for x, y in ring]
    if len(pts) < 3:
        return QgsGeometry()
    if pts[0].x() != pts[-1].x() or pts[0].y() != pts[-1].y():
        pts.append(QgsPointXY(pts[0]))
    geom = QgsGeometry.fromPolygonXY([pts])
    if geom is None:
        return QgsGeometry()
    return geom


def _safe(geom: QgsGeometry):
    if geom is None or geom.isEmpty():
        return None
    valid = geom.makeValid()
    if valid is None or valid.isEmpty():
        return None
    return _polygons_only(valid)


def _open_exterior(geom: QgsGeometry):
    if geom is None or geom.isEmpty():
        return []
    poly = geom.asPolygon()
    if not poly and geom.isMultipart():
        multi = geom.asMultiPolygon()
        if multi:
            poly = multi[0]
    if not poly:
        return []
    pts = [(float(p.x()), float(p.y())) for p in poly[0]]
    if (
        len(pts) >= 2
        and abs(pts[0][0] - pts[-1][0]) < 1e-8
        and abs(pts[0][1] - pts[-1][1]) < 1e-8
    ):
        pts = pts[:-1]
    return pts


def _exterior_edges(geom: QgsGeometry):
    edges = []
    for part in _iter_polygons(geom):
        ring = _open_exterior(part)
        if len(ring) < 2:
            continue
        for i in range(len(ring)):
            edges.append((ring[i], ring[(i + 1) % len(ring)]))
    return edges


def _collect(geoms):
    clean = [g for g in geoms if g is not None and not g.isEmpty()]
    if not clean:
        return None
    if len(clean) == 1:
        return clean[0]
    return QgsGeometry.collectGeometry(clean)


def _mask_in_metric(layer, metric, feedback, tr):
    """Union the 2D road mask in ``metric``. None when no mask was given."""
    if layer is None:
        return None
    xform = None
    if layer.sourceCrs().isValid() and layer.sourceCrs() != metric:
        xform = QgsCoordinateTransform(
            layer.sourceCrs(), metric, QgsProject.instance()
        )
    parts = []
    for feat in layer.getFeatures():
        if feedback.isCanceled():
            break
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        g = QgsGeometry(geom)
        if xform is not None and g.transform(xform) != 0:
            continue
        g = _force_2d(g)
        if g.isEmpty():
            continue
        valid = g.makeValid()
        if valid is not None and not valid.isEmpty():
            parts.append(valid)
    mask = _safe(_union_all(parts))
    if mask is None:
        feedback.pushWarning(tr("Road mask is empty; skipping low-vertex shapes."))
        return None
    feedback.pushInfo(
        tr("Low-vertex shapes stay inside the 2D road mask.")
    )
    return mask


def _metric_crs(lon: float, lat: float) -> QgsCoordinateReferenceSystem:
    zone = int((lon + 180.0) / 6.0) + 1
    zone = min(60, max(1, zone))
    epsg = (32600 if lat >= 0 else 32700) + zone
    return QgsCoordinateReferenceSystem(f"EPSG:{epsg}")


def _transform(geom: QgsGeometry, xform: QgsCoordinateTransform):
    g = QgsGeometry(geom)
    if g.transform(xform) != 0:
        return None
    return g


class RoadGroundClipPolygonsAlgorithm(QgsProcessingAlgorithm):
    INPUT_ROADS = "INPUT_ROADS"
    INPUT_MASK = "INPUT_MASK"
    INPUT_MESH = "INPUT_MESH"
    MIN_PROTRUSION = "MIN_PROTRUSION"
    BUFFER = "BUFFER"
    SIMPLIFY = "SIMPLIFY"
    MERGE_GAP = "MERGE_GAP"
    LOW_VERTEX = "LOW_VERTEX"
    WRITE_EXACT = "WRITE_EXACT"
    MIN_AREA = "MIN_AREA"
    LOD = "LOD"
    WORKERS = "WORKERS"
    OUTPUT = "OUTPUT"
    OUTPUT_EXACT = "OUTPUT_EXACT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return RoadGroundClipPolygonsAlgorithm()

    def name(self):
        return "road_ground_clip_polygons"

    def displayName(self):
        return self.tr("Road ground clip polygons")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Writes polygons where the quantized-mesh ground surface rises "
            "above a 3D road mesh. Clip the ground and imagery with these "
            "polygons in Unreal. The terrain file is not modified.\n\n"
            "A bump is the part of each ground triangle that is higher than "
            "the road plane by at least the minimum protrusion, cut to the "
            "road footprint. Touching pieces are dissolved. Buffer grows them "
            "slightly, then cuts them back to the road.\n\n"
            "With a 2D road mask, each bump is replaced by a road-aligned "
            "rectangle when that rectangle stays inside the mask. When it "
            "would stick out, the rectangle is cut to the mask and corners "
            "are removed until dropping another one would uncover the bump "
            "or leave the road. Nearby bumps merge when one simpler shape "
            "covers both and has fewer corners. Turn simplification off to "
            "keep those exact bumps. Optionally write them to a second "
            "shapefile while the main output stays simplified. Without a "
            "mask, Douglas-Peucker is used instead.\n\n"
            "LOD -1 uses only the highest level in the mesh folder. Coarser "
            "levels are skipped because their large triangles would mark long "
            "stretches of road.\n\n"
            "Output is EPSG:4326. Pieces under 0.01 m² are dropped before the "
            "buffer so numerical specks do not become clip disks."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_ROADS,
                self.tr("Roads 3D mesh (triangle polygons with Z)"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_MASK,
                self.tr("Road mask (clip must stay inside)"),
                [QgsProcessing.TypeVectorPolygon],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_MESH,
                self.tr("Quantized-mesh folder (highest LOD)"),
                behavior=QgsProcessingParameterFile.Folder,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN_PROTRUSION,
                self.tr("Minimum protrusion (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.02,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUFFER,
                self.tr("Buffer inside the road (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.SIMPLIFY,
                self.tr("Simplify tolerance (m, used only without a road mask)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MERGE_GAP,
                self.tr("Merge bumps up to this far apart (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=100.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.LOW_VERTEX,
                self.tr("Simplify clip polygons"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.WRITE_EXACT,
                self.tr("Also save exact bump polygons"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MIN_AREA,
                self.tr("Minimum polygon area (m²)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.25,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.LOD,
                self.tr("LOD (-1 = highest in the folder)"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=-1,
                minValue=-1,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.WORKERS,
                self.tr("Worker processes (0 = auto, 1 = serial)"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=0,
                minValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Road ground clip polygons"),
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT_EXACT,
                self.tr("Exact bump polygons"),
                optional=True,
            )
        )

    def _low_vertex_clips(self, dissolved, mask_metric, merge_gap, feedback):
        if dissolved is None or dissolved.isEmpty():
            return None
        bump_rings = []
        for part in _iter_polygons(dissolved):
            ring = _open_exterior(part)
            if len(ring) >= 3:
                bump_rings.append(ring)
        if not bump_rings:
            return None
        before = sum(len(ring) for ring in bump_rings)
        edges = _exterior_edges(mask_metric)
        frame = RoadEdges(edges)

        def contains_fn(ring):
            geom = _ring_geom(ring)
            if geom.isEmpty():
                return False
            if not geom.isGeosValid():
                geom = geom.makeValid()
                if geom is None or geom.isEmpty():
                    return False
            diff = geom.difference(mask_metric)
            if diff is None or diff.isEmpty():
                return True
            try:
                return float(diff.area()) <= 0.05
            except Exception:
                return False

        def intersect_fn(ring):
            geom = _ring_geom(ring)
            if geom.isEmpty():
                return []
            inter = geom.intersection(mask_metric)
            if inter is None or inter.isEmpty():
                return []
            valid = inter.makeValid()
            if valid is None or valid.isEmpty():
                return []
            out = []
            for part in _iter_polygons(valid):
                exterior = _open_exterior(part)
                if len(exterior) >= 3:
                    out.append(exterior)
            return out

        feedback.setProgressText(self.tr("Fitting low-vertex clip shapes…"))
        shapes = simplify_clips(
            bump_rings,
            merge_gap,
            contains_fn,
            frame.angle_for,
            intersect_fn,
        )
        geoms = []
        for shape in shapes:
            geom = _ring_geom(shape)
            if not geom.isEmpty():
                geoms.append(geom)
        after = sum(len(shape) for shape in shapes)
        feedback.pushInfo(
            self.tr(
                f"Low-vertex clips: {len(bump_rings)} parts, {before} vertices "
                f"→ {len(shapes)} parts, {after} vertices."
            )
        )
        return _collect(geoms)

    def _write_polygons(self, sink, fields, geom, to_wgs, min_area, feedback):
        written = 0
        if geom is None:
            return written, False
        for part in _iter_polygons(geom):
            if feedback.isCanceled():
                return written, True
            area = float(part.area())
            if area < min_area:
                continue
            out_geom = _force_2d(part)
            out_geom = _transform(out_geom, to_wgs)
            if out_geom is None or out_geom.isEmpty():
                continue
            feat = QgsFeature(fields)
            feat.setGeometry(out_geom)
            feat.setAttributes([area])
            sink.addFeature(feat, QgsFeatureSink.FastInsert)
            written += 1
        return written, False

    def processAlgorithm(self, parameters, context, feedback):
        roads = self.parameterAsVectorLayer(parameters, self.INPUT_ROADS, context)
        mask = self.parameterAsVectorLayer(parameters, self.INPUT_MASK, context)
        mesh_in = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        min_protrusion = float(
            self.parameterAsDouble(parameters, self.MIN_PROTRUSION, context)
        )
        buffer_m = float(self.parameterAsDouble(parameters, self.BUFFER, context))
        simplify_m = float(self.parameterAsDouble(parameters, self.SIMPLIFY, context))
        merge_gap = float(self.parameterAsDouble(parameters, self.MERGE_GAP, context))
        low_vertex = bool(self.parameterAsBool(parameters, self.LOW_VERTEX, context))
        write_exact = bool(self.parameterAsBool(parameters, self.WRITE_EXACT, context))
        min_area = float(self.parameterAsDouble(parameters, self.MIN_AREA, context))
        lod = int(self.parameterAsInt(parameters, self.LOD, context))
        workers_req = int(self.parameterAsInt(parameters, self.WORKERS, context))

        if roads is None:
            raise QgsProcessingException(self.tr("Invalid roads layer."))
        if not mesh_in or not os.path.isdir(mesh_in):
            raise QgsProcessingException(self.tr("Invalid quantized-mesh folder."))

        try:
            sink_params, atomic = begin_atomic_file_output(parameters, self.OUTPUT)
            exact_params, exact_atomic = (None, None)
            if write_exact:
                exact_params, exact_atomic = begin_atomic_file_output(
                    parameters, self.OUTPUT_EXACT
                )
        except FileExistsError as exc:
            raise QgsProcessingException(str(exc)) from exc

        ok = False
        sink = None
        dest_id = None
        exact_sink = None
        exact_dest_id = None
        canceled = False
        try:
            wgs84 = epsg_4326()
            xform = None
            if roads.sourceCrs().isValid() and roads.sourceCrs() != wgs84:
                xform = QgsCoordinateTransform(
                    roads.sourceCrs(), wgs84, QgsProject.instance()
                )

            feedback.setProgressText(self.tr("Collecting road triangles…"))
            triangles = []
            footprint_parts = []
            total = max(roads.featureCount(), 1)
            minx = miny = math.inf
            maxx = maxy = -math.inf
            for i, feat in enumerate(roads.getFeatures()):
                if feedback.isCanceled():
                    canceled = True
                    break
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                g = QgsGeometry(geom)
                if xform is not None and g.transform(xform) != 0:
                    continue
                triangles.extend(_triangles_from_geometry(g))
                g2 = _force_2d(g)
                if not g2.isEmpty():
                    footprint_parts.append(g2)
                    box = g2.boundingBox()
                    minx = min(minx, box.xMinimum())
                    miny = min(miny, box.yMinimum())
                    maxx = max(maxx, box.xMaximum())
                    maxy = max(maxy, box.yMaximum())
                if i % 500 == 0:
                    feedback.setProgress(int(8 * i / total))

            if canceled:
                raise QgsProcessingException(self.tr("Canceled."))
            if not triangles:
                raise QgsProcessingException(self.tr("No road triangles with Z found."))
            if not footprint_parts:
                raise QgsProcessingException(self.tr("Empty road geometries."))

            feedback.setProgress(10)
            feedback.setProgressText(self.tr("Building road footprint…"))
            footprint = _safe(_union_all(footprint_parts))
            if footprint is None:
                raise QgsProcessingException(self.tr("Road footprint is empty."))
            footprint_parts = None

            try:
                tiles = discover_terrain_tiles(os.path.abspath(mesh_in))
            except ValueError as exc:
                raise QgsProcessingException(str(exc)) from exc

            levels = sorted({level for _p, level, _x, _y in tiles})
            if lod >= 0:
                chosen = lod
                tiles = [t for t in tiles if t[1] == chosen]
                if not tiles:
                    raise QgsProcessingException(
                        self.tr(
                            f"No tiles at LOD {chosen}. Folder has levels: "
                            f"{', '.join(str(v) for v in levels)}."
                        )
                    )
            else:
                chosen = levels[-1]
                tiles = [t for t in tiles if t[1] == chosen]
            feedback.pushInfo(
                self.tr(
                    f"Road triangles: {len(triangles)}. "
                    f"Using LOD {chosen}"
                    f"{' (highest)' if lod < 0 and len(levels) > 1 else ''}. "
                    f"Tiles at this LOD: {len(tiles)}."
                )
            )

            road_box = QgsRectangle(minx, miny, maxx, maxy)
            lat_ref = (miny + maxy) * 0.5
            pad_lat = 5.0 / 111_320.0
            pad_lon = 5.0 / (111_320.0 * max(math.cos(math.radians(lat_ref)), 1e-3))
            grid = RoadGrid(triangles)
            tasks = []
            for tile_path, level, tx, ty in tiles:
                if feedback.isCanceled():
                    canceled = True
                    break
                west, south, east, north = tile_rectangle(level, tx, ty)
                if not road_box.intersects(QgsRectangle(west, south, east, north)):
                    continue
                local = grid.triangles_in_rect(
                    west - pad_lon,
                    south - pad_lat,
                    east + pad_lon,
                    north + pad_lat,
                )
                if not local:
                    continue
                tasks.append(
                    (
                        str(tile_path),
                        int(level),
                        int(tx),
                        int(ty),
                        local,
                        min_protrusion,
                        0.0,
                    )
                )
            triangles = None
            grid = None
            if canceled:
                raise QgsProcessingException(self.tr("Canceled."))

            feedback.setProgress(18)
            feedback.pushInfo(
                self.tr(
                    f"Tiles overlapping roads: {len(tasks)}. "
                    f"Protrusion ≥ {min_protrusion:g} m, buffer {buffer_m:g} m, "
                    f"merge gap {merge_gap:g} m, min area {min_area:g} m²."
                )
            )

            n_workers = resolve_workers(workers_req)
            feedback.pushInfo(self.tr(f"Using {n_workers} worker process(es)."))
            results = map_in_processes(
                process_tile_task,
                tasks,
                workers=n_workers,
                scripts_root=os.pathsep.join((_SCRIPT_DIR, _SCRIPTS_ROOT)),
                feedback=feedback,
                progress_label="Road bump tiles",
            )
            if feedback.isCanceled():
                canceled = True
                raise QgsProcessingException(self.tr("Canceled."))

            feedback.setProgress(78)
            feedback.setProgressText(self.tr("Dissolving bump polygons…"))
            tile_geoms = []
            raw_rings = 0
            tile_errors = 0
            for part in results:
                if part is None:
                    canceled = True
                    break
                rings, err = part
                if err:
                    tile_errors += 1
                    feedback.pushWarning(self.tr(str(err)))
                if not rings:
                    continue
                raw_rings += len(rings)
                geoms = []
                for ring in rings:
                    g = _ring_geom(ring)
                    if not g.isEmpty():
                        geoms.append(g)
                merged = _safe(_union_all(geoms))
                if merged is not None:
                    tile_geoms.append(merged)
            results = None
            if canceled:
                raise QgsProcessingException(self.tr("Canceled."))

            dissolved = _safe(_union_all(tile_geoms))
            tile_geoms = None
            if dissolved is not None:
                dissolved = _safe(dissolved.intersection(footprint))

            metric = _metric_crs((minx + maxx) * 0.5, (miny + maxy) * 0.5)
            to_metric = QgsCoordinateTransform(wgs84, metric, QgsProject.instance())
            to_wgs = QgsCoordinateTransform(metric, wgs84, QgsProject.instance())
            feedback.pushInfo(
                self.tr(f"Measuring buffers in {metric.authid()}.")
            )

            pre_floor = _PRE_BUFFER_MIN_M2
            kept = []
            if dissolved is not None:
                metric_geom = _transform(dissolved, to_metric)
                if metric_geom is not None:
                    for part in _iter_polygons(metric_geom):
                        if part.area() >= pre_floor:
                            kept.append(part)
            dissolved = _safe(_union_all(kept)) if kept else None
            kept = None

            if dissolved is not None and buffer_m > 0.0:
                grown = dissolved.buffer(buffer_m, 8)
                grown = _safe(grown)
                if grown is not None:
                    foot_m = _transform(footprint, to_metric)
                    if foot_m is not None:
                        grown = _safe(grown.intersection(foot_m))
                dissolved = grown

            mask_metric = _mask_in_metric(mask, metric, feedback, self.tr)
            if dissolved is not None and mask_metric is not None:
                dissolved = _safe(dissolved.intersection(mask_metric))
            exact_geom = dissolved
            if not low_vertex:
                feedback.pushInfo(
                    self.tr("Simplification is off. Writing the exact bump polygons.")
                )
            elif dissolved is not None and mask_metric is not None:
                dissolved = self._low_vertex_clips(
                    dissolved, mask_metric, merge_gap, feedback
                )
            elif dissolved is not None and simplify_m > 0.0:
                simplified = dissolved.simplify(simplify_m)
                dissolved = _safe(simplified if simplified is not None else dissolved)

            fields = QgsFields()
            fields.append(QgsField("area_m2", QVariant.Double))
            sink, dest_id = self.parameterAsSink(
                sink_params,
                self.OUTPUT,
                context,
                fields,
                QgsWkbTypes.Polygon,
                wgs84,
            )
            if sink is None:
                raise QgsProcessingException(self.tr("Could not create output sink."))

            written, canceled = self._write_polygons(
                sink, fields, dissolved, to_wgs, min_area, feedback
            )
            exact_written = 0
            if write_exact and not canceled:
                exact_sink, exact_dest_id = self.parameterAsSink(
                    exact_params if exact_params is not None else parameters,
                    self.OUTPUT_EXACT,
                    context,
                    fields,
                    QgsWkbTypes.Polygon,
                    wgs84,
                )
                if exact_sink is None:
                    raise QgsProcessingException(
                        self.tr("Could not create the exact-bump output.")
                    )
                exact_written, canceled = self._write_polygons(
                    exact_sink, fields, exact_geom, to_wgs, min_area, feedback
                )

            if canceled:
                raise QgsProcessingException(self.tr("Canceled."))

            feedback.pushInfo(
                self.tr(
                    f"Raw overlap pieces: {raw_rings}. "
                    f"Wrote {written} clip polygon(s)"
                    + (
                        f" and {exact_written} exact bump polygon(s)"
                        if write_exact
                        else ""
                    )
                    + (f" ({tile_errors} tile warnings)" if tile_errors else "")
                    + "."
                )
            )
            if written == 0:
                feedback.pushWarning(
                    self.tr(
                        "No clip polygons. Ground may already sit below the road, "
                        "or the road CRS / mesh overlap is wrong."
                    )
                )
            ok = True
            feedback.setProgress(100)
            result_id = dest_id
            exact_result_id = exact_dest_id
        finally:
            published = finish_or_abandon(
                atomic,
                ok=ok,
                sink=sink,
                context=context,
                dest_id=dest_id,
            )
            exact_published = finish_or_abandon(
                exact_atomic,
                ok=ok,
                sink=exact_sink,
                context=context,
                dest_id=exact_dest_id,
            )
            sink = None
            exact_sink = None
        outputs = {self.OUTPUT: published or result_id}
        if write_exact:
            outputs[self.OUTPUT_EXACT] = exact_published or exact_result_id
        return outputs
