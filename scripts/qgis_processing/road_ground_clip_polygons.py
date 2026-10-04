# -*- coding: utf-8 -*-
"""
QGIS Processing: polygons where quantized-mesh ground rises through a 3D road.

Does not edit the terrain. The polygons are meant to be used as clip masks in
Unreal so only the poke-through is removed.

Detection builds exact (ground − road) ≥ min_protrusion contours for every
overlapping QM × road triangle pair. Exact output is not clipped to the 2D
mask (that would pull edges onto the curb). The 2D mask only limits which
tiles are searched (clipped to the 3D road footprint).

LOD min/max: process every mesh level in that inclusive range (e.g. 0–13).
Use LOD max = -1 for the highest level present in the folder. Each level
writes its own shapefile (name_lodN.shp when more than one level runs).
"""

from __future__ import annotations

import math
import os
import sys
from pathlib import Path
from typing import Optional

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

from atomic_io import (  # noqa: E402
    begin_atomic_file_output,
    destination_file_path,
    finish_or_abandon,
)
from crs_util import epsg_4326  # noqa: E402
from parallel_util import map_in_processes, resolve_workers  # noqa: E402
from qm_burn_core import fan_triangles_from_ring  # noqa: E402
from quantized_mesh import (  # noqa: E402
    QuantizedMeshSampler,
    discover_terrain_tiles,
    tile_rectangle,
)
from road_bump_core import (  # noqa: E402
    RoadGrid,
    process_tile_task,
    rings_overlapping_rect,
)

# Drop isolated crumbs so numerical specks are not written as polygons.
_PRE_BUFFER_MIN_M2 = 0.01


def _progress(feedback, pct: float, text: str) -> None:
    """Stage label + overall percent (ConsoleFeedback + web UI)."""
    if feedback is None:
        return
    if hasattr(feedback, "setProgressText"):
        feedback.setProgressText(text)
    if hasattr(feedback, "setProgress"):
        feedback.setProgress(int(max(0, min(100, pct))))


def _progress_frac(
    feedback, pct: float, label: str, cur: int, total: int, unit: str
) -> None:
    """
    Linear sub-bar line the web UI parses: ``Label: 12/340 tiles``.

    ``pct`` is the overall job percent for this checkpoint.
    """
    total = max(int(total), 1)
    cur = max(0, min(int(cur), total))
    _progress(feedback, pct, f"{label}: {cur}/{total} {unit}")


def _lod_output_path(base, level: int, multi: bool) -> str:
    """When multiple LODs run, append ``_lod{N}`` before the file suffix."""
    if not base:
        return base
    # Unwrap QgsProcessingOutputLayerDefinition / QgsProperty → plain path.
    text = destination_file_path(base) or str(base).strip()
    if not text or "Qgs" in text:
        return text
    if not multi:
        return text
    p = Path(text)
    return str(p.with_name(f"{p.stem}_lod{int(level)}{p.suffix}"))


def _path_exists(path) -> bool:
    if not path:
        return False
    p = Path(str(path))
    if p.suffix.lower() == ".shp":
        return p.exists() or p.with_suffix(".dbf").exists()
    return p.exists()


def _remove_output_vector(path) -> None:
    """Delete a vector output and common shapefile sidecars."""
    if not path:
        return
    p = Path(str(path))
    if p.suffix.lower() == ".shp":
        stem = p.with_suffix("")
        for ext in (
            ".shp",
            ".shx",
            ".dbf",
            ".prj",
            ".cpg",
            ".qpj",
            ".sbn",
            ".sbx",
            ".qmd",
        ):
            try:
                stem.with_suffix(ext).unlink(missing_ok=True)
            except OSError:
                pass
        return
    try:
        p.unlink(missing_ok=True)
    except OSError:
        pass


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


def _sampler_root(mesh_root: str, level: int) -> Path:
    """Level folder when the tileset is {level}/{x}/{y}.terrain, else the root."""
    root = Path(mesh_root)
    leveled = root / str(int(level))
    if leveled.is_dir():
        return leveled
    return root


def _triangles_with_qm_offset(triangles, sampler, offset_m: float, on_unique=None):
    """
    Copy road triangles, replacing each vertex Z with QM height + offset.

    A vertex the mesh cannot sample keeps its shapefile Z.
    ``on_unique(done, total)`` is called while sampling so the bar can move
    across the unique vertices. Returns (triangles, unique_vertices, missed).
    """
    pending = []
    seen = set()
    for tri in triangles:
        for x, y, z in tri:
            key = (x, y)
            if key in seen:
                continue
            seen.add(key)
            pending.append((key, float(z)))
    total = len(pending)
    cache = {}
    missed = 0
    for i, (key, z) in enumerate(pending):
        x, y = key
        qz = sampler.sample(float(x), float(y))
        if qz is None:
            cache[key] = z
            missed += 1
        else:
            cache[key] = float(qz) + float(offset_m)
        done = i + 1
        if on_unique is not None and (done % 4000 == 0 or done == total):
            on_unique(done, total)
    out = []
    for tri in triangles:
        out.append(tuple((x, y, cache[(x, y)]) for x, y, _z in tri))
    return out, total, missed


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


def _union_all(
    geoms,
    feedback=None,
    pct_lo: Optional[float] = None,
    pct_hi: Optional[float] = None,
    label: str = "Merging geometries",
):
    clean = [g for g in geoms if g is not None and not g.isEmpty()]
    if not clean:
        return None
    if len(clean) == 1:
        return QgsGeometry(clean[0])
    batch = 800
    acc = list(clean)
    pass_i = 0
    while len(acc) > 1:
        pass_i += 1
        nxt = []
        n_chunks = max(1, (len(acc) + batch - 1) // batch)
        for ci, start in enumerate(range(0, len(acc), batch)):
            chunk = acc[start : start + batch]
            if len(chunk) == 1:
                nxt.append(chunk[0])
            else:
                merged = QgsGeometry.unaryUnion(chunk)
                if merged is not None and not merged.isEmpty():
                    nxt.append(merged)
                else:
                    nxt.extend(chunk)
            if (
                feedback is not None
                and pct_lo is not None
                and pct_hi is not None
            ):
                # Weight later passes less; first pass is most of the work.
                pass_w = 0.75 if pass_i == 1 else 0.25
                base = float(pct_lo) if pass_i == 1 else (
                    float(pct_lo) + 0.75 * (float(pct_hi) - float(pct_lo))
                )
                span = pass_w * (float(pct_hi) - float(pct_lo))
                _progress_frac(
                    feedback,
                    base + span * ((ci + 1) / n_chunks),
                    f"{label} (pass {pass_i})",
                    ci + 1,
                    n_chunks,
                    "chunks",
                )
        if not nxt or len(nxt) >= len(acc):
            break
        acc = nxt
    if len(acc) == 1:
        return acc[0]
    if feedback is not None and pct_lo is not None and pct_hi is not None:
        _progress(feedback, float(pct_hi), f"{label}: final merge…")
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


def _rings_from_geom(geom: QgsGeometry) -> list:
    """Exterior rings as open (lon, lat) lists."""
    rings = []
    for part in _iter_polygons(geom):
        ring = _open_exterior(part)
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _mask_rings_wgs84(layer, feedback, clip_geom: Optional[QgsGeometry] = None) -> list:
    """
    2D road-mask exterior rings in EPSG:4326 for vertex PIP tests.

    When ``clip_geom`` is set (typically the 3D road footprint), each mask
    feature is intersected with it first so city-wide masks do not flood
    detection outside the 3D roads AOI.
    """
    if layer is None:
        return []
    wgs84 = epsg_4326()
    xform = None
    if layer.sourceCrs().isValid() and layer.sourceCrs() != wgs84:
        xform = QgsCoordinateTransform(
            layer.sourceCrs(), wgs84, QgsProject.instance()
        )
    clip = None
    clip_box = None
    if clip_geom is not None and not clip_geom.isEmpty():
        clip = QgsGeometry(clip_geom)
        clip_box = clip.boundingBox()
    rings = []
    for feat in layer.getFeatures():
        if feedback.isCanceled():
            break
        geom = feat.geometry()
        if geom is None or geom.isEmpty():
            continue
        g = QgsGeometry(geom)
        if xform is not None and g.transform(xform) != 0:
            continue
        g2 = _force_2d(g)
        if g2.isEmpty():
            continue
        if clip is not None:
            if clip_box is not None and not g2.boundingBox().intersects(clip_box):
                continue
            inter = g2.intersection(clip)
            if inter is None or inter.isEmpty():
                continue
            g2 = _force_2d(inter)
            if g2.isEmpty():
                continue
        rings.extend(_rings_from_geom(g2))
    return rings


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
    MIN_AREA = "MIN_AREA"
    SAMPLE_EDGES = "SAMPLE_EDGES"
    USE_QM_ROAD_Z = "USE_QM_ROAD_Z"
    ROAD_Z_OFFSET_CM = "ROAD_Z_OFFSET_CM"
    LOD_MIN = "LOD_MIN"
    LOD_MAX = "LOD_MAX"
    WORKERS = "WORKERS"
    OUTPUT = "OUTPUT"

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
            "Detection builds exact contours where (ground − road) ≥ "
            "minimum protrusion for every overlapping QM × road triangle. "
            "Inside those polygons ground is above the road (for those "
            "planes); isoline edges sit on the protrusion threshold. The "
            "output is those exact contours. It is not clipped to the 2D "
            "mask; the mask only limits which tiles are searched, after "
            "clipping the mask to the 3D road footprint. Shape simplification "
            "is a separate tool.\n\n"
            "LOD min/max select an inclusive range of mesh levels to process "
            "(e.g. min=0 max=13). Set max to -1 to include the highest level "
            "in the folder. Each level writes its own shapefile "
            "(``name_lodN.shp`` when more than one level runs).\n\n"
            "Optional: ignore the 3D road vertex heights and set each vertex "
            "to the quantized-mesh height at that lon/lat for the LOD being "
            "processed, plus an offset in centimetres.\n\n"
            "Output is EPSG:4326. Pieces under 0.01 m² are dropped so "
            "numerical specks are not written."
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
                self.tr("Road mask (limits which tiles are searched)"),
                [QgsProcessing.TypeVectorPolygon],
                optional=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_MESH,
                self.tr("Quantized-mesh folder"),
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
                self.MIN_AREA,
                self.tr("Minimum polygon area (m²)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.25,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.SAMPLE_EDGES,
                self.tr("Sample mesh edges & centroids (slower)"),
                defaultValue=True,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.USE_QM_ROAD_Z,
                self.tr("Use QM altitude for the 3D road"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.ROAD_Z_OFFSET_CM,
                self.tr("Road offset above QM (cm)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=10.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.LOD_MIN,
                self.tr("LOD min (inclusive)"),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=0,
                minValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.LOD_MAX,
                self.tr("LOD max (inclusive; -1 = highest in folder)"),
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

    def _write_polygons(
        self, sink, fields, geom, to_wgs, min_area, feedback, lod=None
    ):
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
            if lod is None:
                feat.setAttributes([area])
            else:
                feat.setAttributes([area, int(lod)])
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
        min_area = float(self.parameterAsDouble(parameters, self.MIN_AREA, context))
        sample_edges = bool(self.parameterAsBool(parameters, self.SAMPLE_EDGES, context))
        use_qm_road_z = bool(
            self.parameterAsBool(parameters, self.USE_QM_ROAD_Z, context)
        )
        road_z_offset_m = (
            float(self.parameterAsDouble(parameters, self.ROAD_Z_OFFSET_CM, context))
            / 100.0
        )
        lod_min = int(self.parameterAsInt(parameters, self.LOD_MIN, context))
        lod_max = int(self.parameterAsInt(parameters, self.LOD_MAX, context))
        workers_req = int(self.parameterAsInt(parameters, self.WORKERS, context))

        if roads is None:
            raise QgsProcessingException(self.tr("Invalid roads layer."))
        if not mesh_in or not os.path.isdir(mesh_in):
            raise QgsProcessingException(self.tr("Invalid quantized-mesh folder."))

        ok = False
        # Per-LOD sinks must be closed immediately after each level so OGR
        # flushes SHP/DBF/SHX headers. Deferring close until the end left
        # every LOD looking empty (record count 0) after a kill / long run.
        open_writes = []  # only used if an exception escapes mid-write
        result_id = None
        published = None
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
                if i % 200 == 0 or i + 1 == total:
                    _progress_frac(
                        feedback,
                        1.0 + 7.0 * ((i + 1) / total),
                        "Collecting road triangles",
                        i + 1,
                        total,
                        "roads",
                    )

            if canceled:
                raise QgsProcessingException(self.tr("Canceled."))
            if not triangles:
                raise QgsProcessingException(self.tr("No road triangles with Z found."))
            if not footprint_parts:
                raise QgsProcessingException(self.tr("Empty road geometries."))

            _progress(feedback, 9, self.tr("Building road footprint…"))
            footprint = _safe(
                _union_all(
                    footprint_parts,
                    feedback=feedback,
                    pct_lo=9.0,
                    pct_hi=10.4,
                    label="Building road footprint",
                )
            )
            if footprint is None:
                raise QgsProcessingException(self.tr("Road footprint is empty."))
            footprint_parts = None

            _progress(feedback, 10.5, self.tr("Collecting mask rings…"))
            mask_rings = _mask_rings_wgs84(mask, feedback, clip_geom=footprint)
            if feedback.isCanceled():
                raise QgsProcessingException(self.tr("Canceled."))
            if mask_rings:
                feedback.pushInfo(
                    self.tr(
                        f"Mask rings (clipped to 3D roads): {len(mask_rings)}."
                    )
                )
                _progress_frac(
                    feedback,
                    10.7,
                    "Collecting mask rings",
                    len(mask_rings),
                    max(len(mask_rings), 1),
                    "chunks",
                )
            else:
                mask_rings = _rings_from_geom(footprint)
                feedback.pushInfo(
                    self.tr(
                        "No road mask overlap with 3D roads; using 3D road "
                        f"footprint rings ({len(mask_rings)})."
                    )
                )
            if not mask_rings:
                raise QgsProcessingException(
                    self.tr("No mask or footprint rings for detection.")
                )

            lon_ref = (minx + maxx) * 0.5
            lat_ref = (miny + maxy) * 0.5

            _progress(feedback, 11, self.tr("Discovering quantized-mesh tiles…"))
            try:
                all_tiles = discover_terrain_tiles(os.path.abspath(mesh_in))
            except ValueError as exc:
                raise QgsProcessingException(str(exc)) from exc

            _progress_frac(
                feedback,
                11.5,
                "Discovering quantized-mesh tiles",
                len(all_tiles),
                max(len(all_tiles), 1),
                "tiles",
            )
            levels = sorted({level for _p, level, _x, _y in all_tiles})
            if not levels:
                raise QgsProcessingException(self.tr("No quantized-mesh tiles found."))
            hi = levels[-1] if lod_max < 0 else int(lod_max)
            lo = max(0, int(lod_min))
            if lo > hi:
                raise QgsProcessingException(
                    self.tr(f"LOD min ({lo}) is greater than LOD max ({hi}).")
                )
            chosen_levels = [lv for lv in levels if lo <= lv <= hi]
            if not chosen_levels:
                raise QgsProcessingException(
                    self.tr(
                        f"No mesh levels in [{lo}, {hi}]. "
                        f"Folder has: {', '.join(str(v) for v in levels)}."
                    )
                )
            # Highest first (matches prior “from highest” order).
            chosen_levels = list(reversed(chosen_levels))
            multi = len(chosen_levels) > 1
            feedback.pushInfo(
                self.tr(
                    f"Road triangles: {len(triangles)}. "
                    f"Contour mode: exact (ground−road)≥ε per QM×road pair. "
                    f"LOD range [{lo}, {hi}] → "
                    f"{', '.join(str(v) for v in chosen_levels)} "
                    f"(folder has {', '.join(str(v) for v in levels)})."
                )
            )

            out_base = destination_file_path(parameters.get(self.OUTPUT)) or parameters.get(
                self.OUTPUT
            )
            if multi and not destination_file_path(out_base) and not (
                isinstance(out_base, str) and out_base.lower().endswith((".shp", ".gpkg"))
            ):
                raise QgsProcessingException(
                    self.tr(
                        "Multi-LOD output needs a filesystem path "
                        "(e.g. …/road_ground_clips.shp), not a temporary layer."
                    )
                )
            planned = []
            for level in chosen_levels:
                out_path = _lod_output_path(out_base, level, multi)
                feedback.pushInfo(
                    self.tr(f"Will write LOD {level} → {out_path}")
                )
                if _path_exists(out_path):
                    raise QgsProcessingException(
                        self.tr(
                            f"Output already exists (choose a different name): "
                            f"{out_path}"
                        )
                    )
                planned.append((level, out_path))

            road_box = QgsRectangle(minx, miny, maxx, maxy)
            pad_lat = 5.0 / 111_320.0
            pad_lon = 5.0 / (
                111_320.0 * max(math.cos(math.radians(lat_ref)), 1e-3)
            )
            pad_deg = max(pad_lon, pad_lat)
            grid = None if use_qm_road_z else RoadGrid(triangles)
            n_workers = resolve_workers(workers_req)
            feedback.pushInfo(self.tr(f"Using {n_workers} worker process(es)."))

            metric = _metric_crs(lon_ref, lat_ref)
            to_metric = QgsCoordinateTransform(
                wgs84, metric, QgsProject.instance()
            )
            to_wgs = QgsCoordinateTransform(
                metric, wgs84, QgsProject.instance()
            )
            feedback.pushInfo(
                self.tr(f"Measuring areas in {metric.authid()}.")
            )

            fields = QgsFields()
            fields.append(QgsField("area_m2", QVariant.Double))
            fields.append(QgsField("lod", QVariant.Int))

            total_written = 0
            n_lod = len(planned)
            # Overall: prep 0–14, LODs 14–98, finish 100.
            # Per LOD: tiles 70%, dissolve 27%, write 3%.
            for li, (level, out_path) in enumerate(planned):
                if feedback.isCanceled():
                    canceled = True
                    break
                p0 = 14.0 + 84.0 * li / n_lod
                p1 = 14.0 + 84.0 * (li + 1) / n_lod
                span = p1 - p0
                tile_end = p0 + 0.70 * span
                dissolve_end = p0 + 0.97 * span

                if use_qm_road_z:
                    _progress(
                        feedback,
                        p0,
                        self.tr(
                            f"Sampling road vertices, LOD {level} "
                            f"({li + 1}/{n_lod})…"
                        ),
                    )
                    feedback.pushInfo(
                        self.tr(
                            f"LOD {level}: setting road vertex Z to QM "
                            f"+ {road_z_offset_m * 100.0:g} cm…"
                        )
                    )
                    try:
                        sampler = QuantizedMeshSampler(
                            _sampler_root(mesh_in, level), level=level
                        )
                    except ValueError as exc:
                        raise QgsProcessingException(str(exc)) from exc

                    def _sampled(done, total, level=level, p0=p0, span=span):
                        _progress_frac(
                            feedback,
                            p0 + 0.28 * span * (done / max(total, 1)),
                            (
                                f"Sampling road vertices, LOD {level} "
                                f"({li + 1}/{n_lod})"
                            ),
                            done,
                            total,
                            "points",
                        )

                    level_tris, n_verts, n_miss = _triangles_with_qm_offset(
                        triangles,
                        sampler,
                        road_z_offset_m,
                        on_unique=_sampled,
                    )
                    grid = RoadGrid(level_tris)
                    feedback.pushInfo(
                        self.tr(
                            f"LOD {level}: QM road Z on {n_verts} vertices"
                            + (
                                f" ({n_miss} kept the shapefile Z, no mesh sample)."
                                if n_miss
                                else "."
                            )
                        )
                    )

                _progress(
                    feedback,
                    p0 + (0.28 * span if use_qm_road_z else 0.0),
                    self.tr(
                        f"LOD {level} ({li + 1}/{n_lod}): selecting tiles…"
                    ),
                )

                level_tiles = [t for t in all_tiles if t[1] == level]
                n_scan = max(len(level_tiles), 1)
                tasks = []
                for si, (tile_path, tile_level, tx, ty) in enumerate(level_tiles):
                    if feedback.isCanceled():
                        canceled = True
                        break
                    west, south, east, north = tile_rectangle(
                        tile_level, tx, ty
                    )
                    if not road_box.intersects(
                        QgsRectangle(west, south, east, north)
                    ):
                        continue
                    local = grid.triangles_in_rect(
                        west - pad_lon,
                        south - pad_lat,
                        east + pad_lon,
                        north + pad_lat,
                    )
                    if not local:
                        continue
                    local_mask = rings_overlapping_rect(
                        mask_rings,
                        west,
                        south,
                        east,
                        north,
                        pad=pad_deg,
                    )
                    if not local_mask:
                        continue
                    tasks.append(
                        (
                            str(tile_path),
                            int(tile_level),
                            int(tx),
                            int(ty),
                            local,
                            local_mask,
                            min_protrusion,
                            _PRE_BUFFER_MIN_M2,
                            sample_edges,
                            True,  # contour_exact
                        )
                    )
                    if si % 50 == 0 or si + 1 == n_scan:
                        _progress_frac(
                            feedback,
                            p0 + 0.02 * span * ((si + 1) / n_scan),
                            f"LOD {level} ({li + 1}/{n_lod}): selecting tiles",
                            si + 1,
                            n_scan,
                            "tasks",
                        )
                if canceled:
                    break

                feedback.pushInfo(
                    self.tr(
                        f"LOD {level} ({li + 1}/{n_lod}): {len(tasks)} tile(s) "
                        f"overlapping roads. Protrusion ≥ {min_protrusion:g} m."
                    )
                )
                if not tasks:
                    _progress(
                        feedback,
                        p1,
                        self.tr(
                            f"LOD {level} ({li + 1}/{n_lod}): no overlapping tiles."
                        ),
                    )
                    continue

                results = map_in_processes(
                    process_tile_task,
                    tasks,
                    workers=n_workers,
                    scripts_root=os.pathsep.join((_SCRIPT_DIR, _SCRIPTS_ROOT)),
                    feedback=feedback,
                    progress_label=(
                        f"LOD {level} ({li + 1}/{n_lod}): processing tiles"
                    ),
                    progress_unit="tiles",
                    progress_start=p0 + 0.02 * span,
                    progress_end=tile_end,
                )
                if feedback.isCanceled():
                    canceled = True
                    break

                tile_geoms = []
                raw_rings = 0
                tile_errors = 0
                n_res = max(len(results), 1)
                for ti, part in enumerate(results):
                    if part is None:
                        canceled = True
                        break
                    rings, err = part
                    if err:
                        tile_errors += 1
                        feedback.pushWarning(self.tr(str(err)))
                    if not rings:
                        if ti % 25 == 0 or ti + 1 == n_res:
                            _progress_frac(
                                feedback,
                                tile_end
                                + (dissolve_end - tile_end) * ((ti + 1) / n_res),
                                f"LOD {level} ({li + 1}/{n_lod}): dissolving tile results",
                                ti + 1,
                                n_res,
                                "parts",
                            )
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
                    if ti % 10 == 0 or ti + 1 == n_res:
                        _progress_frac(
                            feedback,
                            tile_end
                            + (dissolve_end - tile_end) * ((ti + 1) / n_res),
                            f"LOD {level} ({li + 1}/{n_lod}): dissolving tile results",
                            ti + 1,
                            n_res,
                            "parts",
                        )
                results = None
                if canceled:
                    break

                _progress(
                    feedback,
                    dissolve_end,
                    self.tr(f"LOD {level} ({li + 1}/{n_lod}): merging bumps + road footprint…"),
                )
                dissolved = _safe(_union_all(tile_geoms))
                tile_geoms = None
                if dissolved is not None:
                    dissolved = _safe(dissolved.intersection(footprint))

                kept = []
                if dissolved is not None:
                    metric_geom = _transform(dissolved, to_metric)
                    if metric_geom is not None:
                        for part in _iter_polygons(metric_geom):
                            if part.area() >= _PRE_BUFFER_MIN_M2:
                                kept.append(part)
                dissolved = _safe(_union_all(kept)) if kept else None

                # Contour-true bumps: keep (ground-road)>=eps regions.
                # Do not pull to the 2D mask — that puts edges on the curb
                # where height is not the threshold.
                if dissolved is not None:
                    foot_m = _transform(footprint, to_metric)
                    if foot_m is not None:
                        dissolved = _safe(dissolved.intersection(foot_m))
                if dissolved is not None and not dissolved.isEmpty():
                    cleaned = dissolved.buffer(0.0, 1)
                    cleaned = _safe(cleaned)
                    if cleaned is not None:
                        dissolved = cleaned

                _progress(
                    feedback,
                    dissolve_end,
                    self.tr(f"LOD {level} ({li + 1}/{n_lod}): writing shapefile…"),
                )

                lod_params = dict(parameters)
                lod_params[self.OUTPUT] = out_path
                try:
                    sink_params, atomic = begin_atomic_file_output(
                        lod_params, self.OUTPUT
                    )
                except FileExistsError as exc:
                    raise QgsProcessingException(str(exc)) from exc

                sink, dest_id = self.parameterAsSink(
                    sink_params,
                    self.OUTPUT,
                    context,
                    fields,
                    QgsWkbTypes.Polygon,
                    wgs84,
                )
                if sink is None:
                    raise QgsProcessingException(
                        self.tr("Could not create output sink.")
                    )
                written, canceled = self._write_polygons(
                    sink,
                    fields,
                    dissolved,
                    to_wgs,
                    min_area,
                    feedback,
                    lod=level,
                )

                # Close this LOD now so shapefile headers flush before the
                # next level (and before any cancel/kill).
                lod_ok = not canceled
                pub = finish_or_abandon(
                    atomic,
                    ok=lod_ok,
                    sink=sink,
                    context=context,
                    dest_id=dest_id,
                    flush_shapefile=True,
                )
                if li == 0:
                    result_id = dest_id
                    published = pub or out_path

                if canceled:
                    # Direct shapefile writes stay on disk until closed —
                    # remove a half-written level so the next run can recreate it.
                    _remove_output_vector(out_path)
                    break

                total_written += written
                feedback.pushInfo(
                    self.tr(
                        f"LOD {level}: {raw_rings} raw pieces → "
                        f"{written} clip polygon(s)"
                        + (
                            f" ({tile_errors} tile warnings)"
                            if tile_errors
                            else ""
                        )
                        + f". Wrote {out_path}."
                    )
                )
                _progress(
                    feedback,
                    p1,
                    self.tr(
                        f"LOD {level} done ({li + 1}/{n_lod}): "
                        f"{written} clip polygon(s)."
                    ),
                )

            if canceled:
                raise QgsProcessingException(self.tr("Canceled."))
            if total_written == 0:
                feedback.pushWarning(
                    self.tr(
                        "No clip polygons. Ground may already sit below the road, "
                        "or the road CRS / mesh overlap is wrong."
                    )
                )
            ok = True
            feedback.setProgress(100)
        finally:
            # Only unfinished writes (exception mid-LOD) land here.
            for atomic, sink, dest_id in open_writes:
                finish_or_abandon(
                    atomic,
                    ok=False,
                    sink=sink,
                    context=context,
                    dest_id=dest_id,
                    flush_shapefile=True,
                )
        outputs = {self.OUTPUT: published or result_id}
        return outputs
