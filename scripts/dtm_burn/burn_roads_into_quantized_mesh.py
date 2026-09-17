# -*- coding: utf-8 -*-
"""
QGIS Processing: push quantized-mesh under a 3D road surface where it pokes through.

Copies the full tileset (all LODs). For each terrain vertex inside the road
2D footprint that is *not* on a mask-boundary QM triangle:

  road_z  = triangle-plane Z of the Unreal road mesh at that lon/lat
  target  = road_z − OFFSET_DOWN
  new_z   = min(old_z, target)   # only lower; never raise

Rim protection (hardcoded): if a QM triangle has any vertex outside the road
mask, none of that triangle's vertices are lowered — even ones inside the
mask. That prevents straddling triangles from pulling the outline down
(upside-down pyramids under the road rim).
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
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsGeometry,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
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
from quantized_mesh import (  # noqa: E402
    discover_terrain_tiles,
    load_tile_from_bytes,
    replace_tile_altitudes,
    tile_rectangle,
    write_terrain_file,
)
from qm_burn_core import fan_triangles_from_ring, plane_z_at  # noqa: E402


def _geometry_2d(geom: QgsGeometry) -> QgsGeometry:
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
    return tris


def _rect_intersects(a: QgsRectangle, west, south, east, north) -> bool:
    return a.intersects(QgsRectangle(west, south, east, north))


def _filter_road_tris(triangles, west, south, east, north, pad=0.0):
    w, s, e, n = west - pad, south - pad, east + pad, north + pad
    out = []
    for tri in triangles:
        xs = (tri[0][0], tri[1][0], tri[2][0])
        ys = (tri[0][1], tri[1][1], tri[2][1])
        if max(xs) < w or min(xs) > e or max(ys) < s or min(ys) > n:
            continue
        out.append(tri)
    return out


def _road_z_at(lon: float, lat: float, triangles):
    for tri in triangles:
        z = plane_z_at(lon, lat, tri)
        if z is not None:
            return float(z)
    return None


def _mask_flags(lons, lats, west, south, east, north, burn_prep):
    """True if vertex lon/lat is inside the road 2D mask (and tile rect)."""
    flags = []
    for lon, lat in zip(lons, lats):
        if lon < west or lon > east or lat < south or lat > north:
            flags.append(False)
            continue
        flags.append(
            burn_prep.contains(QgsGeometry.fromPointXY(QgsPointXY(lon, lat)))
        )
    return flags


def _rim_protected(in_mask, qm_triangles):
    """
    Protect vertices that belong to any QM triangle straddling the mask.

    If a triangle has at least one vertex outside the mask and at least one
    inside, all of its vertices are protected (inside ones won't be lowered).
    """
    n = len(in_mask)
    protected = [False] * n
    for i0, i1, i2 in qm_triangles:
        inside = (1 if in_mask[i0] else 0) + (1 if in_mask[i1] else 0) + (
            1 if in_mask[i2] else 0
        )
        if inside == 0 or inside == 3:
            continue
        if in_mask[i0]:
            protected[i0] = True
        if in_mask[i1]:
            protected[i1] = True
        if in_mask[i2]:
            protected[i2] = True
    return protected


def _load_tile(path: Path, level: int, x: int, y: int):
    raw = path.read_bytes()
    was_gzip = raw[:2] == b"\x1f\x8b"
    data = gzip.decompress(raw) if was_gzip else raw
    tile = load_tile_from_bytes(data, level, x, y)
    return data, was_gzip, tile


def _fast_copy_tileset(src_root: str, dst_root: str, feedback, tr) -> None:
    feedback.setProgressText(tr("Copying quantized-mesh tileset…"))
    if feedback.isCanceled():
        raise QgsProcessingException(tr("Canceled during copy."))
    os.makedirs(dst_root, exist_ok=True)
    if os.name == "nt":
        cmd = [
            "robocopy",
            src_root,
            dst_root,
            "/E",
            "/MT:16",
            "/R:1",
            "/W:1",
            "/NFL",
            "/NDL",
            "/NJH",
            "/NJS",
            "/NC",
            "/NS",
            "/NP",
        ]
        try:
            completed = subprocess.run(
                cmd, capture_output=True, text=True, check=False
            )
            if completed.returncode < 8:
                feedback.pushInfo(
                    tr(f"Tileset copy via robocopy (exit {completed.returncode}).")
                )
                return
            feedback.pushWarning(
                tr(
                    f"robocopy failed (exit {completed.returncode}); "
                    "falling back to Python copytree."
                )
            )
        except OSError as exc:
            feedback.pushWarning(
                tr(f"robocopy unavailable ({exc}); using Python copytree.")
            )
    shutil.copytree(
        src_root,
        dst_root,
        dirs_exist_ok=True,
        copy_function=shutil.copyfile,
    )


class BurnRoadsIntoQuantizedMeshAlgorithm(QgsProcessingAlgorithm):
    INPUT_ROADS = "INPUT_ROADS"
    INPUT_MESH = "INPUT_MESH"
    OFFSET_DOWN = "OFFSET_DOWN"
    OUTPUT_MESH = "OUTPUT_MESH"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return BurnRoadsIntoQuantizedMeshAlgorithm()

    def name(self):
        return "burn_roads_into_quantized_mesh"

    def displayName(self):
        return self.tr("Push QM under 3D roads (poke-through only)")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Lowers quantized-mesh vertices that poke up through a Unreal "
            "3D road mesh (all LODs).\n\n"
            "For each terrain vertex inside the road footprint that is not on "
            "a mask-boundary QM triangle:\n"
            "  road_z = triangle-plane Z of the road at that lon/lat\n"
            "  target = road_z − clearance\n"
            "  new_z  = min(old_z, target)\n\n"
            "Rim protection (hardcoded): if a QM triangle has any vertex "
            "outside the road mask, its in-mask vertices are not lowered. "
            "That stops straddling triangles from creating floating road "
            "outlines / upside-down pyramids at the rim.\n\n"
            "Use a small clearance (centimetres)."
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
            QgsProcessingParameterFile(
                self.INPUT_MESH,
                self.tr("Input quantized-mesh tileset folder"),
                behavior=QgsProcessingParameterFile.Folder,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.OFFSET_DOWN,
                self.tr("Under-road clearance (m)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=0.05,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterFolderDestination(
                self.OUTPUT_MESH,
                self.tr("Output quantized-mesh tileset"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        roads = self.parameterAsVectorLayer(
            parameters, self.INPUT_ROADS, context
        )
        mesh_in = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        mesh_out = self.parameterAsString(
            parameters, self.OUTPUT_MESH, context
        )
        offset = float(
            self.parameterAsDouble(parameters, self.OFFSET_DOWN, context)
        )

        if roads is None:
            raise QgsProcessingException(self.tr("Invalid roads layer."))
        if not mesh_in or not os.path.isdir(mesh_in):
            raise QgsProcessingException(
                self.tr("Invalid input quantized-mesh folder.")
            )
        if not mesh_out:
            raise QgsProcessingException(
                self.tr("Output quantized-mesh folder required.")
            )

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

            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            xform = None
            if roads.sourceCrs().isValid() and roads.sourceCrs() != wgs84:
                xform = QgsCoordinateTransform(
                    roads.sourceCrs(), wgs84, QgsProject.instance()
                )

            feedback.setProgressText(self.tr("Collecting road triangles…"))
            triangles = []
            footprint_parts = []
            total = max(roads.featureCount(), 1)
            for i, feat in enumerate(roads.getFeatures()):
                if feedback.isCanceled():
                    raise QgsProcessingException(self.tr("Canceled."))
                geom = feat.geometry()
                if geom is None or geom.isEmpty():
                    continue
                g = QgsGeometry(geom)
                if xform is not None and g.transform(xform) != 0:
                    continue
                triangles.extend(_triangles_from_geometry(g))
                g2 = _geometry_2d(g)
                if not g2.isEmpty():
                    footprint_parts.append(g2)
                if i % 200 == 0:
                    feedback.setProgress(int(5 + 10 * i / total))

            if not triangles:
                raise QgsProcessingException(
                    self.tr("No road triangles with Z found.")
                )
            if not footprint_parts:
                raise QgsProcessingException(self.tr("Empty road geometries."))

            union = footprint_parts[0]
            for part in footprint_parts[1:]:
                combined = union.combine(part)
                if combined is not None and not combined.isEmpty():
                    union = combined

            burn_prep = QgsGeometry(union)
            burn_bbox = burn_prep.boundingBox()
            feedback.pushInfo(
                self.tr(
                    f"Triangles: {len(triangles)}; clearance: {offset:g} m; "
                    "rim = QM triangles straddling mask (hardcoded)"
                )
            )

            feedback.setProgress(20)
            tiles = discover_terrain_tiles(Path(work_root))
            feedback.pushInfo(self.tr(f"Found {len(tiles)} .terrain tiles."))

            changed_tiles = 0
            changed_verts = 0
            examined = 0
            skipped_bbox = 0
            already_under = 0
            rim_skipped = 0
            n_tiles = max(len(tiles), 1)
            offset_f = float(offset)

            for ti, (tile_path, level, tx, ty) in enumerate(tiles):
                if feedback.isCanceled():
                    raise QgsProcessingException(self.tr("Canceled."))

                west, south, east, north = tile_rectangle(level, tx, ty)
                if not _rect_intersects(burn_bbox, west, south, east, north):
                    skipped_bbox += 1
                    if ti % 50 == 0:
                        feedback.setProgress(
                            int(20 + 75 * (ti + 1) / n_tiles)
                        )
                    continue

                examined += 1
                pad = max((east - west), (north - south)) * 0.05
                local_tris = _filter_road_tris(
                    triangles, west, south, east, north, pad=pad
                )
                if not local_tris:
                    continue

                try:
                    data, was_gzip, tile = _load_tile(
                        Path(tile_path), level, tx, ty
                    )
                except Exception as exc:
                    feedback.pushWarning(
                        self.tr(f"Skip tile {tile_path}: {exc}")
                    )
                    continue

                lons, lats, alts = tile.lons, tile.lats, tile.altitudes
                in_mask = _mask_flags(
                    lons, lats, west, south, east, north, burn_prep
                )
                if not any(in_mask):
                    continue

                protected = _rim_protected(in_mask, tile.triangles)
                new_alts = list(alts)
                n_changed = 0
                for i, ok_mask in enumerate(in_mask):
                    if not ok_mask:
                        continue
                    if protected[i]:
                        rim_skipped += 1
                        continue
                    road_z = _road_z_at(lons[i], lats[i], local_tris)
                    if road_z is None:
                        continue
                    target = road_z - offset_f
                    old = float(new_alts[i])
                    if old <= target:
                        already_under += 1
                        continue
                    new_alts[i] = target
                    n_changed += 1

                if n_changed <= 0:
                    continue

                try:
                    patched = replace_tile_altitudes(
                        data, level, tx, ty, new_alts
                    )
                    write_terrain_file(Path(tile_path), patched, was_gzip)
                    changed_tiles += 1
                    changed_verts += n_changed
                except Exception as exc:
                    feedback.pushWarning(
                        self.tr(f"Failed writing {tile_path}: {exc}")
                    )

                if ti % 25 == 0 or ti + 1 == n_tiles:
                    feedback.setProgress(int(20 + 75 * (ti + 1) / n_tiles))
                    feedback.setProgressText(
                        self.tr(
                            f"LOD tiles {ti + 1}/{n_tiles} examined={examined} "
                            f"patched={changed_tiles} lowered={changed_verts}"
                        )
                    )

            feedback.pushInfo(
                self.tr(
                    f"Done. Examined {examined} tiles, patched {changed_tiles}, "
                    f"lowered {changed_verts} vertices "
                    f"(already under: {already_under}, "
                    f"rim-protected: {rim_skipped}, "
                    f"bbox-skipped {skipped_bbox})."
                )
            )
            if changed_verts == 0:
                feedback.pushWarning(
                    self.tr(
                        "No vertices lowered — either none poke through, "
                        "or CRS/mask overlap is wrong."
                    )
                )

            ok = True
            feedback.setProgress(100)
            return {self.OUTPUT_MESH: out_dest}
        finally:
            finish_or_abandon(atomic, ok=ok)
