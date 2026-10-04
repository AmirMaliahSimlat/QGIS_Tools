# -*- coding: utf-8 -*-
"""
QGIS Processing: simplify exact bump polygons separately from detection.

Each part becomes its convex hull when that hull lies inside the 2D road
mask. The original part is kept when the hull leaves the road. Optional
edge extensions drop more hull corners when the added patch stays inside
the mask and still covers the hull. Optional enlargement grows each result by sliding its edges outward, keeping the same
corners, and stops when any point would meet the road-outline margin.
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
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsUnitTypes,
    QgsWkbTypes,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_SCRIPT_DIR)
for _p in (_SCRIPT_DIR, _SCRIPTS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from atomic_io import (  # noqa: E402
    begin_atomic_file_output,
    finish_or_abandon,
)
from bump_simplify_core import simplify_bump_set  # noqa: E402


def _progress(feedback, pct: float, text: str) -> None:
    if feedback is None:
        return
    if hasattr(feedback, "setProgressText"):
        feedback.setProgressText(text)
    if hasattr(feedback, "setProgress"):
        feedback.setProgress(int(max(0, min(100, pct))))


def _progress_frac(feedback, label: str, cur: int, total: int, unit: str) -> None:
    total = max(int(total), 1)
    cur = max(0, min(int(cur), total))
    _progress(feedback, 100.0 * cur / total, f"{label}: {cur}/{total} {unit}")


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


def _open_ring(ring) -> list:
    pts = [(float(p.x()), float(p.y())) for p in ring]
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    return pts


def _as_2d(geom: QgsGeometry) -> QgsGeometry:
    """Drop Z and M. asMultiPolygon() raises on a single PolygonZ."""
    if geom is None or geom.isEmpty():
        return QgsGeometry()
    wkb = geom.wkbType()
    if not QgsWkbTypes.hasZ(wkb) and not QgsWkbTypes.hasM(wkb):
        return geom
    clone = geom.constGet().clone()
    clone.dropZValue()
    clone.dropMValue()
    flat = QgsGeometry(clone)
    return flat if flat is not None else QgsGeometry()


def _parts(geom: QgsGeometry):
    if geom is None or geom.isEmpty():
        return []
    geom = _as_2d(geom)
    flat = QgsWkbTypes.flatType(geom.wkbType())
    if flat == QgsWkbTypes.MultiPolygon:
        return geom.asMultiPolygon() or []
    if flat == QgsWkbTypes.Polygon:
        poly = geom.asPolygon()
        return [poly] if poly else []
    if flat != QgsWkbTypes.GeometryCollection:
        return []
    out = []
    for sub in geom.asGeometryCollection():
        out.extend(_parts(sub))
    return out


def _polygons_only(geom) -> QgsGeometry:
    """Drop lines and points left by buffer or intersection."""
    if geom is None or geom.isEmpty():
        return QgsGeometry()
    geom = _as_2d(geom)
    valid = geom.makeValid()
    if valid is not None and not valid.isEmpty():
        geom = valid
    flat = QgsWkbTypes.flatType(geom.wkbType())
    if flat in (QgsWkbTypes.Polygon, QgsWkbTypes.MultiPolygon):
        return geom
    if flat != QgsWkbTypes.GeometryCollection:
        return QgsGeometry()
    kept = []
    try:
        coll = geom.asGeometryCollection()
    except Exception:
        return QgsGeometry()
    for sub in coll:
        piece = _polygons_only(sub)
        if piece is not None and not piece.isEmpty():
            kept.append(piece)
    if not kept:
        return QgsGeometry()
    if len(kept) == 1:
        return kept[0]
    merged = QgsGeometry.unaryUnion(kept)
    return merged if merged is not None else QgsGeometry()


def _signed_area(ring) -> float:
    total = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        total += x1 * y2 - x2 * y1
    return total * 0.5


def _force_ccw(ring):
    pts = [(float(x), float(y)) for x, y in ring]
    if _signed_area(pts) < 0.0:
        pts.reverse()
    return pts


def _offset_ring(ring, dist: float):
    """
    Slide a ring outward by ``dist`` metres.

    Corners stay corners: each new vertex is where the two offset edges
    meet, so the count matches the input. ``dist`` below zero shrinks a
    counter-clockwise ring. None when an edge vanishes or a corner spikes.
    """
    src = _force_ccw(ring)
    n = len(src)
    if n < 3 or abs(dist) < 1e-9:
        return src if n >= 3 else None
    edges = []
    for i in range(n):
        ax, ay = src[i]
        bx, by = src[(i + 1) % n]
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        if length < 1e-8:
            continue
        # Clockwise normal of a CCW edge points out of the polygon.
        nx, ny = dy / length, -dx / length
        edges.append((ax + nx * dist, ay + ny * dist, dx, dy, ax, ay))
    m = len(edges)
    if m < 3:
        return None
    out = []
    # Mitre length is dist / sin(half the interior angle), so this ratio
    # does not improve when the distance shrinks. 80 keeps corners down
    # to about 1.5 degrees and rejects only a numerical spike.
    limit = max(abs(dist) * 80.0, 0.05)
    for i in range(m):
        ox, oy, dx, dy, _, _ = edges[i]
        px, py, ex, ey, cx, cy = edges[(i + 1) % m]
        den = dx * ey - dy * ex
        if abs(den) < 1e-10:
            # Collinear edges share one offset line. Keep that corner.
            hit = (px, py)
        else:
            t = ((px - ox) * ey - (py - oy) * ex) / den
            hit = (ox + t * dx, oy + t * dy)
        if math.hypot(hit[0] - cx, hit[1] - cy) > limit:
            return None
        if out and math.hypot(hit[0] - out[-1][0], hit[1] - out[-1][1]) < 1e-6:
            continue
        out.append(hit)
    if len(out) >= 2 and math.hypot(out[0][0] - out[-1][0], out[0][1] - out[-1][1]) < 1e-6:
        out.pop()
    if len(out) < 3 or _signed_area(out) <= 0.0:
        return None
    return out


def _ring_self_crosses(ring) -> bool:
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            cx, cy = ring[j]
            dx, dy = ring[(j + 1) % n]
            o1 = (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
            o2 = (bx - ax) * (dy - ay) - (by - ay) * (dx - ax)
            o3 = (dx - cx) * (ay - cy) - (dy - cy) * (ax - cx)
            o4 = (dx - cx) * (by - cy) - (dy - cy) * (bx - cx)
            if o1 * o2 < 0.0 and o3 * o4 < 0.0:
                return True
    return False


def _polygon_parts(geom):
    """Open exteriors and holes for each polygon part."""
    out = []
    for part in _parts(geom):
        if not part:
            continue
        exterior = _open_ring(part[0])
        holes = [_open_ring(hole) for hole in part[1:] if len(hole) >= 4]
        if len(exterior) >= 3:
            out.append((exterior, holes))
    return out


class SimplifyBumpPolygonsAlgorithm(QgsProcessingAlgorithm):
    INPUT_BUMPS = "INPUT_BUMPS"
    INPUT_MASK = "INPUT_MASK"
    EXTEND_EDGES = "EXTEND_EDGES"
    ENLARGE = "ENLARGE"
    ENLARGE_M = "ENLARGE_M"
    MARGIN_M = "MARGIN_M"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return SimplifyBumpPolygonsAlgorithm()

    def name(self):
        return "simplify_bump_polygons"

    def displayName(self):
        return self.tr("Simplify bump polygons")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Dissolves exact bump polygons that touch or overlap, then "
            "replaces each part with its convex hull when that hull lies "
            "inside the 2D road mask. Nearby separate bumps share one hull "
            "when the hull of both still lies inside the road, so two bumps "
            "side by side on a straight stretch become one polygon. If a "
            "hull leaves the road, corners are still dropped when the "
            "shortcut across them stays inside the road and still covers "
            "the bump.\n\n"
            "With edge extension on, corners of those hulls are dropped by "
            "extending non-adjacent edges, only while the added patch stays "
            "inside the road and still covers the bumps.\n\n"
            "Bumps more than 200 m apart are not paired directly. A chain of "
            "closer bumps can still become one hull. Holes are filled when "
            "a hull is used.\n\n"
            "After that, simplified polygons that overlap or touch are "
            "unioned into one polygon. That union is not hulled again, so "
            "it can follow a bend and still cover every bump inside it.\n\n"
            "With enlarge on, each polygon then grows by sliding its "
            "edges outward, so the corners stay sharp and the vertex count "
            "stays the same. The whole polygon uses one distance, up to the "
            "enlarge value. It stops sooner when any point would come within "
            "the margin of the road outline. A polygon already closer than "
            "that margin is left unchanged."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_BUMPS,
                self.tr("Exact bump polygons"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_MASK,
                self.tr("Road mask (result stays inside)"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.EXTEND_EDGES,
                self.tr("Extend edges to drop more vertices"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.ENLARGE,
                self.tr("Enlarge polygons inside the road"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.ENLARGE_M,
                self.tr("Enlarge by (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.MARGIN_M,
                self.tr("Margin from the road outline (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.5,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Simplified bump polygons"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        bumps = self.parameterAsVectorLayer(
            parameters, self.INPUT_BUMPS, context
        )
        mask_layer = self.parameterAsVectorLayer(
            parameters, self.INPUT_MASK, context
        )
        extend = self.parameterAsBool(parameters, self.EXTEND_EDGES, context)
        enlarge = self.parameterAsBool(parameters, self.ENLARGE, context)
        enlarge_m = float(
            self.parameterAsDouble(parameters, self.ENLARGE_M, context)
        )
        margin_m = float(
            self.parameterAsDouble(parameters, self.MARGIN_M, context)
        )
        if bumps is None:
            raise QgsProcessingException(self.tr("Invalid bump polygon layer."))
        if mask_layer is None:
            raise QgsProcessingException(self.tr("Invalid road mask layer."))

        metric = self._metric_crs_for_layer(bumps, feedback)
        to_metric_bumps = QgsCoordinateTransform(
            bumps.sourceCrs(), metric, QgsProject.instance()
        )
        to_source = QgsCoordinateTransform(
            metric, bumps.sourceCrs(), QgsProject.instance()
        )
        to_metric_mask = QgsCoordinateTransform(
            mask_layer.sourceCrs(), metric, QgsProject.instance()
        )

        feedback.pushInfo(self.tr("Collecting road mask…"))
        mask_geom = self._load_mask(mask_layer, to_metric_mask, feedback)
        if mask_geom is None or mask_geom.isEmpty():
            raise QgsProcessingException(self.tr("Road mask is empty."))
        valid = mask_geom.makeValid()
        if valid is not None and not valid.isEmpty():
            mask_geom = valid
        try:
            engine = QgsGeometry.createGeometryEngine(mask_geom.constGet())
            engine.prepareGeometry()
        except Exception as exc:
            raise QgsProcessingException(
                self.tr(f"Could not prepare the road mask: {exc}")
            ) from exc

        def covers(ring) -> bool:
            geom = _ring_geom(ring)
            if geom.isEmpty():
                return False
            raw = geom.constGet()
            try:
                if hasattr(engine, "covers"):
                    return bool(engine.covers(raw))
            except Exception:
                pass
            try:
                return bool(engine.contains(raw))
            except Exception:
                return False

        fields = QgsFields(bumps.fields())
        names = {fields.at(i).name() for i in range(fields.count())}
        if "verts_in" not in names:
            fields.append(QgsField("verts_in", QVariant.Int))
        if "verts_out" not in names:
            fields.append(QgsField("verts_out", QVariant.Int))
        in_idx = fields.indexFromName("verts_in")
        out_idx = fields.indexFromName("verts_out")
        area_idx = fields.indexFromName("area_m2")

        try:
            sink_params, atomic = begin_atomic_file_output(
                parameters, self.OUTPUT
            )
        except FileExistsError as exc:
            raise QgsProcessingException(str(exc)) from exc
        sink, dest_id = self.parameterAsSink(
            sink_params,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.Polygon,
            bumps.sourceCrs(),
        )
        if sink is None:
            raise QgsProcessingException(self.tr("Could not create output sink."))

        written = 0
        verts_before = 0
        verts_after = 0
        counts = {"hull": 0, "merged": 0, "extended": 0, "original": 0}
        canceled = False
        ok = False
        try:
            feedback.pushInfo(self.tr("Collecting exact bump polygons…"))
            prepared = []
            for feat in bumps.getFeatures():
                if feedback.isCanceled():
                    canceled = True
                    break
                geom = QgsGeometry(feat.geometry())
                if geom.isEmpty():
                    continue
                if geom.transform(to_metric_bumps) != 0:
                    continue
                geom = _as_2d(geom)
                if geom.isEmpty():
                    continue
                attrs = list(feat.attributes())
                for part in _parts(geom):
                    if not part:
                        continue
                    exterior = _open_ring(part[0])
                    holes = [
                        _open_ring(hole) for hole in part[1:] if len(hole) >= 4
                    ]
                    if len(exterior) < 3:
                        continue
                    part_geom = self._polygon(exterior, holes)
                    prepared.append(
                        {
                            "ring": exterior,
                            "holes": holes,
                            "geom": part_geom,
                            "attrs": attrs,
                            "src_verts": len(exterior),
                        }
                    )
            if not canceled:
                feedback.pushInfo(
                    self.tr(
                        f"Merging {len(prepared)} exact bump parts that touch…"
                    )
                )
                dissolved = self._dissolve_touching(prepared, feedback)
                feedback.pushInfo(
                    self.tr(
                        f"Exact parts {len(prepared)} → {len(dissolved)} after dissolve."
                    )
                )

                def _on_pairs(cur, total):
                    if feedback.isCanceled():
                        return
                    _progress_frac(
                        feedback,
                        "Joining bump hulls",
                        cur,
                        total,
                        "pairs",
                    )

                def _on_extend(cur, total):
                    if feedback.isCanceled():
                        return
                    _progress_frac(
                        feedback,
                        "Extending edges",
                        cur,
                        total,
                        "polygons",
                    )

                results = simplify_bump_set(
                    [item["ring"] for item in dissolved],
                    covers,
                    extend=extend,
                    on_pairs=_on_pairs,
                    on_extend=_on_extend if extend else None,
                )
                simplified_parts = []
                for simplified, status, members in results:
                    if feedback.isCanceled():
                        canceled = True
                        break
                    donors = [dissolved[k] for k in members if k < len(dissolved)]
                    if not donors:
                        continue
                    donor = max(donors, key=lambda item: item["src_verts"])
                    src_verts = sum(item["src_verts"] for item in donors)
                    use_holes = status == "original" and len(donors) == 1
                    holes = donor["holes"] if use_holes else []
                    out_geom = self._polygon(simplified, holes)
                    if out_geom.isEmpty():
                        continue
                    counts[status] = counts.get(status, 0) + 1
                    simplified_parts.append(
                        {
                            "ring": simplified,
                            "holes": holes,
                            "geom": out_geom,
                            "attrs": list(donor["attrs"]),
                            "src_verts": src_verts,
                        }
                    )
                if not canceled:
                    before_overlap = len(simplified_parts)
                    feedback.pushInfo(
                        self.tr(
                            f"Merging {before_overlap} simplified polygons that overlap…"
                        )
                    )
                    overlapped = self._dissolve_touching(simplified_parts, feedback)
                    feedback.pushInfo(
                        self.tr(
                            f"Simplified polygons {before_overlap} → {len(overlapped)} "
                            f"after overlap merge."
                        )
                    )
                    if enlarge and enlarge_m > 0.0 and not canceled:
                        overlapped = self._enlarge_parts(
                            overlapped,
                            mask_geom,
                            enlarge_m,
                            margin_m,
                            feedback,
                        )
                    for item in overlapped:
                        if feedback.isCanceled():
                            canceled = True
                            break
                        out_geom = QgsGeometry(item["geom"])
                        if out_geom.isEmpty():
                            continue
                        area = float(out_geom.area())
                        out_verts = len(item["ring"])
                        verts_before += item["src_verts"]
                        verts_after += out_verts
                        if out_geom.transform(to_source) != 0:
                            continue
                        attrs = list(item["attrs"])
                        while len(attrs) < fields.count():
                            attrs.append(None)
                        attrs[in_idx] = item["src_verts"]
                        attrs[out_idx] = out_verts
                        if area_idx >= 0:
                            attrs[area_idx] = area
                        out = QgsFeature(fields)
                        out.setGeometry(out_geom)
                        out.setAttributes(attrs)
                        sink.addFeature(out, QgsFeatureSink.FastInsert)
                        written += 1
            ok = not canceled
        finally:
            published = finish_or_abandon(
                atomic,
                ok=ok,
                sink=sink,
                context=context,
                dest_id=dest_id,
                flush_shapefile=True,
            )
            sink = None
        if canceled:
            raise QgsProcessingException(self.tr("Canceled."))
        feedback.pushInfo(
            self.tr(
                f"Wrote {written} polygons. Vertices {verts_before} → {verts_after}. "
                f"Hull {counts['hull']}, merged {counts['merged']}, "
                f"extended {counts['extended']}, kept exact {counts['original']}."
            )
        )
        return {self.OUTPUT: published or dest_id}

    @staticmethod
    def _polygon(exterior, holes) -> QgsGeometry:
        rings = []
        for ring in [exterior, *holes]:
            if len(ring) < 3:
                continue
            pts = [QgsPointXY(float(x), float(y)) for x, y in ring]
            if pts[0].x() != pts[-1].x() or pts[0].y() != pts[-1].y():
                pts.append(QgsPointXY(pts[0]))
            rings.append(pts)
        if not rings:
            return QgsGeometry()
        geom = QgsGeometry.fromPolygonXY(rings)
        return geom if geom is not None else QgsGeometry()

    def _dissolve_touching(self, prepared, feedback):
        """Union exact parts that touch or overlap. Disjoint parts stay separate."""
        if len(prepared) <= 1:
            return prepared
        geoms = [
            item["geom"]
            for item in prepared
            if item["geom"] is not None and not item["geom"].isEmpty()
        ]
        if len(geoms) <= 1:
            return prepared
        merged = QgsGeometry.unaryUnion(geoms)
        if merged is None or merged.isEmpty():
            feedback.pushWarning(
                self.tr("Could not dissolve touching bumps; keeping them separate.")
            )
            return prepared
        valid = merged.makeValid()
        if valid is not None and not valid.isEmpty():
            merged = valid
        dissolved = []
        for part in _parts(merged):
            if feedback.isCanceled():
                break
            if not part:
                continue
            exterior = _open_ring(part[0])
            holes = [_open_ring(hole) for hole in part[1:] if len(hole) >= 4]
            if len(exterior) < 3:
                continue
            part_geom = self._polygon(exterior, holes)
            box = part_geom.boundingBox()
            donors = []
            for item in prepared:
                geom = item["geom"]
                if geom is None or geom.isEmpty():
                    continue
                other = geom.boundingBox()
                if not box.intersects(other):
                    continue
                if part_geom.intersects(geom):
                    donors.append(item)
            if not donors:
                donors = prepared[:1]
            donor = max(donors, key=lambda item: item["src_verts"])
            dissolved.append(
                {
                    "ring": exterior,
                    "holes": holes,
                    "geom": part_geom,
                    "attrs": list(donor["attrs"]),
                    "src_verts": sum(item["src_verts"] for item in donors),
                }
            )
        return dissolved or prepared

    def _enlarge_parts(self, parts, mask_geom, enlarge_m, margin_m, feedback):
        """
        Grow each polygon by ``enlarge_m``, stopping at ``margin_m`` inside
        the road outline. The original polygon is kept where it is already
        closer than that margin, so enlargement never shrinks a bump.
        """
        feedback.pushInfo(
            self.tr(
                f"Offsetting polygons by up to {enlarge_m:g} m, "
                f"keeping corners, staying {margin_m:g} m inside the road outline…"
            )
        )
        boundary = self._road_boundary(mask_geom)
        grown_n = 0
        out = []
        total_parts = max(len(parts), 1)
        for pi, item in enumerate(parts):
            if feedback.isCanceled():
                break
            _progress_frac(
                feedback,
                "Offsetting polygons",
                pi + 1,
                total_parts,
                "polygons",
            )
            geom = QgsGeometry(item["geom"])
            if geom.isEmpty():
                continue
            enlarged = self._enlarge_one(
                geom, mask_geom, boundary, enlarge_m, margin_m
            )
            if enlarged is None or enlarged.isEmpty():
                enlarged = geom
            pieces = _polygon_parts(enlarged)
            if not pieces:
                pieces = _polygon_parts(geom)
            if not pieces:
                continue
            if enlarged.area() > geom.area() + 1e-4:
                grown_n += 1
            # Vertex stats stay on the largest piece so a split does not
            # count the source bump twice.
            pieces.sort(
                key=lambda part: self._polygon(part[0], part[1]).area(),
                reverse=True,
            )
            for i, (exterior, holes) in enumerate(pieces):
                part_geom = self._polygon(exterior, holes)
                if part_geom.isEmpty():
                    continue
                out.append(
                    {
                        "ring": exterior,
                        "holes": holes,
                        "geom": part_geom,
                        "attrs": list(item["attrs"]),
                        "src_verts": item["src_verts"] if i == 0 else 0,
                    }
                )
        feedback.pushInfo(
            self.tr(
                f"Enlarged {grown_n} of {len(parts)} polygons "
                f"({len(out)} written)."
            )
        )
        return out or parts

    def _road_boundary(self, mask_geom) -> QgsGeometry:
        raw = mask_geom.constGet() if mask_geom is not None else None
        if raw is None:
            return QgsGeometry()
        curve = raw.boundary()
        if curve is None:
            return QgsGeometry()
        return _as_2d(QgsGeometry(curve))

    @staticmethod
    def _offset_geometry(geom, dist: float) -> QgsGeometry:
        """Outward edge slide. Same corners; holes shrink by the same distance."""
        if geom is None or geom.isEmpty() or abs(dist) < 1e-9:
            return QgsGeometry(geom) if geom is not None else QgsGeometry()
        pieces = _polygon_parts(geom)
        if not pieces:
            return QgsGeometry()
        built = []
        for exterior, holes in pieces:
            outer = _offset_ring(exterior, dist)
            if outer is None or _ring_self_crosses(outer):
                return QgsGeometry()
            hole_rings = []
            for hole in holes:
                inner = _offset_ring(hole, -dist)
                if inner is None or _ring_self_crosses(inner):
                    # A hole already narrower than the offset has collapsed.
                    # A wider hole that cannot slide means this distance fails.
                    xs = [p[0] for p in hole]
                    ys = [p[1] for p in hole]
                    narrow = min(max(xs) - min(xs), max(ys) - min(ys))
                    if narrow <= 2.0 * abs(dist) + 1e-3:
                        continue
                    return QgsGeometry()
                hole_rings.append(inner)
            part = SimplifyBumpPolygonsAlgorithm._polygon(outer, hole_rings)
            if part.isEmpty():
                return QgsGeometry()
            built.append(part)
        if not built:
            return QgsGeometry()
        if len(built) == 1:
            return built[0]
        merged = QgsGeometry.unaryUnion(built)
        return merged if merged is not None else QgsGeometry()

    @staticmethod
    def _offset_fits(grown, local_mask, local_boundary, margin_m: float) -> bool:
        if grown is None or grown.isEmpty():
            return False
        if local_mask is not None and not local_mask.isEmpty():
            outside = grown.difference(local_mask)
            if outside is not None and not outside.isEmpty() and outside.area() > 1e-3:
                return False
        if margin_m <= 0.0 or local_boundary is None or local_boundary.isEmpty():
            return True
        return grown.distance(local_boundary) + 1e-4 >= margin_m

    def _local_road(self, geom, mask_geom, boundary, pad: float):
        """Mask and road outline clipped to a box around ``geom``."""
        rect = geom.boundingBox()
        rect.grow(float(pad))
        rect_geom = QgsGeometry.fromRect(rect)
        local_mask = _polygons_only(mask_geom.intersection(rect_geom))
        local_boundary = QgsGeometry()
        if boundary is not None and not boundary.isEmpty():
            local_boundary = _as_2d(boundary.intersection(rect_geom))
        return local_mask, local_boundary

    def _enlarge_one(self, geom, mask_geom, boundary, enlarge_m, margin_m):
        """
        One offset for the whole polygon, with the same corners.

        The distance is the enlarge value, or less when any point of the
        offset would come within ``margin_m`` of the road outline. A polygon
        that is already that close is returned unchanged.
        """
        near_mask, near_boundary = self._local_road(
            geom, mask_geom, boundary, float(margin_m) + 5.0
        )
        if near_mask.isEmpty():
            return QgsGeometry(geom)
        if (
            margin_m > 0.0
            and near_boundary is not None
            and not near_boundary.isEmpty()
            and geom.distance(near_boundary) + 1e-4 < margin_m
        ):
            return QgsGeometry(geom)

        def fits(dist: float) -> QgsGeometry:
            grown = self._offset_geometry(geom, dist)
            if grown is None or grown.isEmpty():
                return QgsGeometry()
            # Clip to the grown shape so a long sharp corner is checked
            # against the road outline it actually approaches.
            local_mask, local_boundary = self._local_road(
                grown, mask_geom, boundary, max(float(margin_m), 1.0) + 2.0
            )
            if self._offset_fits(grown, local_mask, local_boundary, margin_m):
                return grown
            return QgsGeometry()

        full = fits(float(enlarge_m))
        if not full.isEmpty():
            return full
        lo = 0.0
        hi = float(enlarge_m)
        best = QgsGeometry()
        for _ in range(10):
            mid = (lo + hi) * 0.5
            if hi - lo < 0.01:
                break
            grown = fits(mid)
            if not grown.isEmpty():
                best = grown
                lo = mid
            else:
                hi = mid
        return best if not best.isEmpty() else QgsGeometry(geom)

    def _load_mask(self, layer, to_metric, feedback) -> QgsGeometry:
        parts = []
        for feat in layer.getFeatures():
            if feedback.isCanceled():
                break
            geom = QgsGeometry(feat.geometry())
            if geom.isEmpty():
                continue
            if geom.transform(to_metric) != 0:
                continue
            geom = _as_2d(geom)
            if geom.isEmpty():
                continue
            parts.append(geom)
        if not parts:
            return QgsGeometry()
        if len(parts) == 1:
            return parts[0]
        merged = QgsGeometry.unaryUnion(parts)
        if merged is None or merged.isEmpty():
            return QgsGeometry()
        return merged

    @staticmethod
    def _metric_crs_for_layer(layer, feedback):
        crs = layer.sourceCrs()
        if (
            crs.isValid()
            and not crs.isGeographic()
            and crs.mapUnits() == QgsUnitTypes.DistanceMeters
        ):
            feedback.pushInfo(
                f"Using layer CRS {crs.authid()} for the simplification."
            )
            return crs
        extent = layer.extent()
        lon = 0.5 * (extent.xMinimum() + extent.xMaximum())
        lat = 0.5 * (extent.yMinimum() + extent.yMaximum())
        if not crs.isGeographic():
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            to_wgs = QgsCoordinateTransform(crs, wgs84, QgsProject.instance())
            pt = to_wgs.transform(QgsPointXY(lon, lat))
            lon, lat = pt.x(), pt.y()
        zone = int(math.floor((lon + 180.0) / 6.0) + 1)
        zone = min(60, max(1, zone))
        epsg = (32600 + zone) if lat >= 0 else (32700 + zone)
        metric = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
        if not metric.isValid():
            feedback.pushWarning(
                "Could not build a UTM CRS; using EPSG:3857."
            )
            metric = QgsCoordinateReferenceSystem("EPSG:3857")
        feedback.pushInfo(
            f"Simplifying in {metric.authid()} so extensions are in meters."
        )
        return metric
