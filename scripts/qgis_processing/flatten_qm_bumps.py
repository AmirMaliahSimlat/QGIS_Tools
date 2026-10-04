# -*- coding: utf-8 -*-
"""
QGIS Processing: insert bump polygons into the highest QM LOD and lower them.

Copies the tileset, then for each highest-LOD tile that intersects a bump:
  - cuts the bump outline into the mesh, stopping original edges on that outline
  - removes the mesh inside the outline and fills it with fans
  - one new vertex per convex piece, at the lowest outline height
  - leaves the mesh outside the outline at its original height
  - rewrites the .terrain tile
"""

from __future__ import annotations

import gzip
import os
import shutil
import subprocess
import sys
from pathlib import Path

from qgis.PyQt.QtCore import QCoreApplication
from qgis.core import (
    QgsCoordinateTransform,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFolderDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterFile,
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

from atomic_io import begin_atomic_dir_output, finish_or_abandon  # noqa: E402
from crs_util import epsg_4326  # noqa: E402
from quantized_mesh import (  # noqa: E402
    discover_terrain_tiles,
    encode_quantized_mesh_tile,
    load_tile_from_bytes,
    tile_rectangle,
    write_terrain_file,
)
from qm_bump_flatten_core import flatten_tile_bumps  # noqa: E402


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


def _rings_from_geom(geom: QgsGeometry) -> list:
    rings = []
    for part in _iter_polygons(geom):
        ring = _open_exterior(part)
        if len(ring) >= 3:
            rings.append(ring)
    return rings


def _fast_copy_tileset(src_root: str, dst_root: str, feedback, tr) -> None:
    if feedback.isCanceled():
        raise QgsProcessingException(tr("Canceled during copy."))
    try:
        completed = subprocess.run(
            [
                "robocopy",
                src_root,
                dst_root,
                "/E",
                "/NFL",
                "/NDL",
                "/NJH",
                "/NJS",
                "/nc",
                "/ns",
                "/np",
            ],
            check=False,
            capture_output=True,
            text=True,
        )
        # robocopy 0–7 are success-ish
        if completed.returncode < 8:
            feedback.pushInfo(tr(f"Tileset copy via robocopy (exit {completed.returncode})."))
            return
        feedback.pushWarning(
            tr(
                f"robocopy failed (exit {completed.returncode}); "
                "falling back to Python copytree."
            )
        )
    except FileNotFoundError as exc:
        feedback.pushWarning(tr(f"robocopy unavailable ({exc}); using Python copytree."))
    shutil.copytree(
        src_root,
        dst_root,
        dirs_exist_ok=True,
        copy_function=shutil.copyfile,
    )


class FlattenQmBumpsAlgorithm(QgsProcessingAlgorithm):
    INPUT_BUMPS = "INPUT_BUMPS"
    INPUT_MASK = "INPUT_MASK"
    INPUT_MESH = "INPUT_MESH"
    BURY_DEPTH = "BURY_DEPTH"
    STRAIGHT_FAN = "STRAIGHT_FAN"
    OUTPUT_MESH = "OUTPUT_MESH"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return FlattenQmBumpsAlgorithm()

    def name(self):
        return "flatten_qm_bumps"

    def displayName(self):
        return self.tr("Lower QM ground under bumps")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Inserts bump polygons into the highest quantized-mesh LOD and "
            "lowers the ground under them.\n\n"
            "Each bump outline is cut into the mesh. An original edge that "
            "crosses the outline stops there. The mesh outside the outline "
            "keeps its height. Inside, each convex piece gets one new vertex "
            "at the lowest outline height, and the triangles fan out to the "
            "outline. A concave bump gets one vertex per piece. Straight fan "
            "height instead places each inner vertex at the lowest straight "
            "profile of its fan-edge pairs, so every pair stays a valley. "
            "Depth below boundary drops those vertices further below whichever "
            "height was chosen. Lower LODs are copied through unchanged."
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
                self.tr("Road mask"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterFile(
                self.INPUT_MESH,
                self.tr("Input quantized-mesh tileset folder"),
                behavior=QgsProcessingParameterFile.Folder,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.BURY_DEPTH,
                self.tr("Depth below boundary (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterBoolean(
                self.STRAIGHT_FAN,
                self.tr("Straight fan height"),
                defaultValue=False,
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_MESH,
                self.tr("Output quantized-mesh tileset"),
            )
        )

    def _collect_rings(self, layer, feedback):
        wgs84 = epsg_4326()
        xform = None
        if layer.sourceCrs().isValid() and layer.sourceCrs() != wgs84:
            xform = QgsCoordinateTransform(
                layer.sourceCrs(), wgs84, QgsProject.instance()
            )
        rings = []
        minx = miny = float("inf")
        maxx = maxy = float("-inf")
        for feat in layer.getFeatures():
            if feedback.isCanceled():
                raise QgsProcessingException(self.tr("Canceled."))
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            g = QgsGeometry(geom)
            if xform is not None and g.transform(xform) != 0:
                continue
            g2 = _force_2d(g)
            if g2.isEmpty():
                continue
            rings.extend(_rings_from_geom(g2))
            box = g2.boundingBox()
            minx = min(minx, box.xMinimum())
            miny = min(miny, box.yMinimum())
            maxx = max(maxx, box.xMaximum())
            maxy = max(maxy, box.yMaximum())
        return rings, minx, miny, maxx, maxy

    def processAlgorithm(self, parameters, context, feedback):
        bumps = self.parameterAsVectorLayer(parameters, self.INPUT_BUMPS, context)
        mask = self.parameterAsVectorLayer(parameters, self.INPUT_MASK, context)
        mesh_in = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        mesh_out = self.parameterAsString(parameters, self.OUTPUT_MESH, context)
        bury = float(self.parameterAsDouble(parameters, self.BURY_DEPTH, context))
        straight_fan = bool(
            self.parameterAsBool(parameters, self.STRAIGHT_FAN, context)
        )

        if bumps is None:
            raise QgsProcessingException(self.tr("Invalid bump polygons layer."))
        if mask is None:
            raise QgsProcessingException(self.tr("Invalid road mask layer."))
        if not mesh_in or not os.path.isdir(mesh_in):
            raise QgsProcessingException(self.tr("Invalid input quantized-mesh folder."))
        if not mesh_out:
            raise QgsProcessingException(self.tr("Output quantized-mesh folder required."))

        in_path = os.path.abspath(mesh_in)
        out_dest = os.path.abspath(mesh_out)
        if os.path.normcase(in_path) == os.path.normcase(out_dest):
            raise QgsProcessingException(
                self.tr("Output folder must differ from the input mesh.")
            )

        atomic = begin_atomic_dir_output(out_dest)
        work_root = str(atomic.temp)
        ok = False
        try:
            _fast_copy_tileset(in_path, work_root, feedback, self.tr)

            feedback.setProgressText(self.tr("Collecting bump rings…"))
            rings, minx, miny, maxx, maxy = self._collect_rings(bumps, feedback)
            if not rings:
                raise QgsProcessingException(self.tr("No bump polygons found."))

            bump_box = QgsRectangle(minx, miny, maxx, maxy)
            tiles = discover_terrain_tiles(Path(work_root))
            if not tiles:
                raise QgsProcessingException(self.tr("No .terrain tiles found."))
            max_level = max(int(level) for _path, level, _tx, _ty in tiles)
            tiles = [t for t in tiles if int(t[1]) == max_level]
            feedback.pushInfo(
                self.tr(
                    f"Bump rings: {len(rings)}. Highest LOD {max_level}: "
                    f"{len(tiles)} tile(s)."
                )
            )

            touched = 0
            splits = 0
            inner = 0
            reused = 0
            buried = 0
            n_tiles = max(len(tiles), 1)

            def _report_tile(done: int) -> None:
                # qgis_process prints pushInfo. setProgressText never reaches
                # the web bar, so the tile count has to be a normal info line.
                done = max(0, min(int(done), n_tiles))
                feedback.setProgress(5 + int(90 * done / n_tiles))
                feedback.pushInfo(
                    self.tr(f"Lowering bump ground: {done}/{n_tiles} tiles")
                )
                sys.stdout.flush()

            for ti, (tile_path, level, tx, ty) in enumerate(tiles):
                if feedback.isCanceled():
                    raise QgsProcessingException(self.tr("Canceled."))
                _report_tile(ti + 1)
                west, south, east, north = tile_rectangle(level, tx, ty)
                if not bump_box.intersects(QgsRectangle(west, south, east, north)):
                    continue

                raw = Path(tile_path).read_bytes()
                was_gzip = raw[:2] == b"\x1f\x8b"
                data = gzip.decompress(raw) if was_gzip else raw
                tile = load_tile_from_bytes(data, int(level), int(tx), int(ty))

                # Local rings only (bbox filter).
                local = []
                for ring in rings:
                    xs = [p[0] for p in ring]
                    ys = [p[1] for p in ring]
                    if (
                        max(xs) < west
                        or min(xs) > east
                        or max(ys) < south
                        or min(ys) > north
                    ):
                        continue
                    local.append(ring)
                if not local:
                    continue

                ol, oa, oz, ot, st = flatten_tile_bumps(
                    tile.lons,
                    tile.lats,
                    tile.altitudes,
                    tile.triangles,
                    local,
                    bounds=(west, south, east, north),
                    bury_depth=bury,
                    straight_fan=straight_fan,
                )
                if st.get("failed"):
                    reasons = list(st.get("fail_reasons") or [])
                    counts = {}
                    order = []
                    for reason in reasons:
                        if reason not in counts:
                            order.append(reason)
                            counts[reason] = 0
                        counts[reason] += 1
                    if len(reasons) == 1:
                        detail = reasons[0]
                    elif order:
                        detail = "; ".join(
                            f"{counts[reason]} {reason}" for reason in order
                        )
                    else:
                        detail = ""
                    message = (
                        f"Tile {level}/{tx}/{ty}: {st['failed']} bump polygon(s) "
                        "could not be inserted"
                    )
                    if detail:
                        message = f"{message}: {detail}"
                    feedback.pushWarning(self.tr(message + "."))
                if st["splits"] == 0 and st["inner"] == 0 and st["reused"] == 0:
                    continue
                if not ot:
                    feedback.pushWarning(
                        self.tr(f"Tile {tile_path}: flatten produced no triangles; skipped.")
                    )
                    continue

                encoded = encode_quantized_mesh_tile(
                    ol, oa, oz, ot, int(level), int(tx), int(ty)
                )
                write_terrain_file(Path(tile_path), encoded, use_gzip=was_gzip)
                touched += 1
                splits += st["splits"]
                inner += st["inner"]
                reused += st["reused"]
                buried += st["buried"]

            if straight_fan:
                height_note = (
                    f"set {buried} inner vert(s) to {bury:g} m below the straight fan profile"
                )
            else:
                height_note = (
                    f"set {buried} inner vert(s) to {bury:g} m below the lowest boundary vertex"
                )
            feedback.pushInfo(
                self.tr(
                    f"Rewrote {touched} tile(s) at LOD {max_level}; "
                    f"inserted {splits} boundary vert(s) and {inner} inner vert(s); "
                    f"reused {reused} existing interior vert(s); "
                    f"{height_note}."
                )
            )
            if touched == 0:
                feedback.pushWarning(
                    self.tr(
                        "No tiles were rewritten. Bumps may not overlap the mesh, "
                        "or CRS / LOD mismatch."
                    )
                )
            ok = True
            feedback.setProgress(100)
        finally:
            published = finish_or_abandon(atomic, ok=ok)
        return {self.OUTPUT_MESH: published or out_dest}


def classFactory(iface=None):  # noqa: N802 — QGIS scripts entry (unused)
    return FlattenQmBumpsAlgorithm()
