# -*- coding: utf-8 -*-
"""
QGIS Processing algorithm: convert tree-mask polygons to spaced points.

Places points on a hexagonal lattice so every pair is at least
``min_distance`` meters apart, keeps only points that fall inside the
input polygons, and samples terrain altitude from a Cesium quantized-mesh
tileset into a hardcoded ``altitude`` attribute (and PointZ Z).

Packing notes:
- Multipart features are packed per-part (each part's own bbox) so empty
  space between parts is not scanned.
- Uses prepared GEOS engines for fast point-in-polygon / building tests.
- Primary hex phase only; half-offset fill phases run only when a feature
  still has zero points (centroid fallback remains as last resort).
"""

import math
import os
import sys
import time

from qgis.PyQt.QtCore import QCoreApplication, QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsFeature,
    QgsFeatureSink,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsPoint,
    QgsPointXY,
    QgsProcessing,
    QgsProcessingAlgorithm,
    QgsProcessingException,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterFile,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsProject,
    QgsRectangle,
    QgsSpatialIndex,
    QgsWkbTypes,
)

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_SCRIPTS_ROOT = os.path.dirname(_SCRIPT_DIR)
for _p in (_SCRIPT_DIR, _SCRIPTS_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from quantized_mesh import (  # noqa: E402
    QuantizedMeshSampler,
    sample_lonlats_parallel,
)
from atomic_io import begin_atomic_file_output, finish_or_abandon  # noqa: E402

ALTITUDE_FIELD = "altitude"


class TreeMaskToPointsAlgorithm(QgsProcessingAlgorithm):
    INPUT_POLYGONS = "INPUT_POLYGONS"
    INPUT_BUILDINGS = "INPUT_BUILDINGS"
    INPUT_MESH = "INPUT_MESH"
    MIN_DISTANCE = "MIN_DISTANCE"
    BUILDING_CLEARANCE = "BUILDING_CLEARANCE"
    WORKERS = "WORKERS"
    OUTPUT = "OUTPUT"

    def tr(self, string):
        return QCoreApplication.translate("Processing", string)

    def createInstance(self):
        return TreeMaskToPointsAlgorithm()

    def name(self):
        return "tree_mask_to_points"

    def displayName(self):
        return self.tr("Tree mask polygons to spaced points")

    def group(self):
        return self.tr("QGIS Projects")

    def groupId(self):
        return "qgis_projects"

    def shortHelpString(self):
        return self.tr(
            "Converts tree-mask polygons (e.g. binary CV footprints) into "
            "points with a guaranteed minimum spacing in meters.\n\n"
            "Points are generated on a hexagonal lattice covering the masks, "
            "so no two accepted points are closer than the chosen distance. "
            "Polygons that receive no lattice point get their centroid if it "
            "still respects the spacing.\n\n"
            "A buildings polygon layer is required: no tree is placed inside "
            "a footprint or within the building-clearance distance (default "
            "1 m).\n\n"
            f"Each output point is PointZ with attribute '{ALTITUDE_FIELD}' "
            "sampled from the quantized-mesh tileset at that location."
        )

    def initAlgorithm(self, config=None):
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_POLYGONS,
                self.tr("Tree mask polygons"),
                [QgsProcessing.TypeVectorPolygon],
            )
        )
        self.addParameter(
            QgsProcessingParameterVectorLayer(
                self.INPUT_BUILDINGS,
                self.tr("Buildings footprints"),
                [QgsProcessing.TypeVectorPolygon],
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
            QgsProcessingParameterNumber(
                self.MIN_DISTANCE,
                self.tr("Minimum distance between points (meters)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.5,
                minValue=0.01,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.BUILDING_CLEARANCE,
                self.tr("Clearance from buildings (meters)"),
                type=QgsProcessingParameterNumber.Double,
                defaultValue=1.0,
                minValue=0.0,
            )
        )
        self.addParameter(
            QgsProcessingParameterNumber(
                self.WORKERS,
                self.tr(
                    "Worker processes (0=auto, 1=serial)"
                ),
                type=QgsProcessingParameterNumber.Integer,
                defaultValue=0,
                minValue=0,
            )
        )
        self.addParameter(
            QgsProcessingParameterFeatureSink(
                self.OUTPUT,
                self.tr("Spaced tree points"),
            )
        )

    def processAlgorithm(self, parameters, context, feedback):
        layer = self.parameterAsVectorLayer(
            parameters, self.INPUT_POLYGONS, context
        )
        buildings = self.parameterAsVectorLayer(
            parameters, self.INPUT_BUILDINGS, context
        )
        mesh_folder = self.parameterAsFile(parameters, self.INPUT_MESH, context)
        min_dist = self.parameterAsDouble(
            parameters, self.MIN_DISTANCE, context
        )
        clearance = self.parameterAsDouble(
            parameters, self.BUILDING_CLEARANCE, context
        )
        workers_req = self.parameterAsInt(parameters, self.WORKERS, context)

        if layer is None:
            raise QgsProcessingException(self.tr("Invalid polygon layer."))
        if buildings is None:
            raise QgsProcessingException(self.tr("Invalid buildings layer."))
        if not mesh_folder or not os.path.isdir(mesh_folder):
            raise QgsProcessingException(
                self.tr("Invalid quantized-mesh tiles folder.")
            )
        if min_dist <= 0:
            raise QgsProcessingException(
                self.tr("Minimum distance must be > 0.")
            )
        if clearance < 0:
            raise QgsProcessingException(
                self.tr("Building clearance must be ≥ 0.")
            )

        source_crs = layer.sourceCrs()
        metric_crs = self._metric_crs_for_layer(layer, feedback)
        to_metric = QgsCoordinateTransform(
            source_crs, metric_crs, QgsProject.instance()
        )
        to_source = QgsCoordinateTransform(
            metric_crs, source_crs, QgsProject.instance()
        )
        wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
        to_wgs84 = QgsCoordinateTransform(
            source_crs, wgs84, QgsProject.instance()
        )

        feedback.setProgressText(self.tr("Preparing polygons…"))
        metric_features = []
        canceled = False
        for idx, feature in enumerate(layer.getFeatures()):
            if feedback.isCanceled():
                canceled = True
                break
            geom = feature.geometry()
            if geom is None or geom.isEmpty():
                continue
            metric_geom = QgsGeometry(geom)
            if metric_geom.transform(to_metric) != 0:
                continue
            metric_geom = metric_geom.makeValid()
            if metric_geom.isEmpty():
                continue
            # Stable unique id (memory layers often use -1 for every feature).
            out_feat = QgsFeature(idx)
            out_feat.setGeometry(metric_geom)
            metric_features.append(out_feat)

        if not metric_features:
            raise QgsProcessingException(
                self.tr("No usable polygon geometries found.")
            )

        n_poly = len(metric_features)
        feedback.pushInfo(
            self.tr(
                f"Packing {n_poly} polygons at ≥ {min_dist} m "
                f"(CRS {metric_crs.authid()})."
            )
        )

        feedback.setProgressText(self.tr("Indexing buildings…"))
        bldg_index, bldg_engines, n_bldg = self._index_buildings(
            buildings, metric_crs, clearance, feedback
        )
        if feedback.isCanceled():
            canceled = True
        feedback.pushInfo(
            self.tr(
                f"Excluding {n_bldg} buildings with {clearance} m clearance."
            )
        )

        # Pack in a true meter CRS. Prefer the layer CRS when it is already
        # projected metres; otherwise use the local UTM zone.
        dx = min_dist
        dy = min_dist * math.sqrt(3.0) / 2.0
        # Primary hex lattice already enforces ≥ min_dist between neighbors.
        # Extra half-offset phases only fill holes left by PIP/building rejects;
        # run them only when a feature still has zero points.
        primary_phases = ((0.0, 0.0),)
        fill_phases = (
            (0.5 * dx, 0.0),
            (0.0, 0.5 * dy),
            (0.5 * dx, 0.5 * dy),
        )
        accepted_metric = []  # list of (x, y) in metric CRS
        min_dist_sq = min_dist * min_dist
        # Cell smaller than min_dist so a 3x3 neighborhood always covers
        # any point within min_dist (safe against float edge cases).
        cell = min_dist / math.sqrt(2.0)
        grid = {}
        covered_ids = set()

        feedback.setProgressText(self.tr("Placing points in masks..."))
        last_report = 0.0
        last_pct = -1
        for i, mf in enumerate(metric_features):
            if feedback.isCanceled():
                canceled = True
                break
            done = i + 1
            pct = int(90.0 * done / max(n_poly, 1))
            if pct != last_pct:
                last_pct = pct
                feedback.setProgress(pct)

            geom = mf.geometry()
            parts = self._geometry_parts(geom)
            placed = 0
            probes = 0
            poly_t0 = time.monotonic()

            def _report(now: float, *, force: bool = False) -> None:
                nonlocal last_report
                if not force and (now - last_report) < 0.35:
                    return
                last_report = now
                if probes > 0 or (now - poly_t0) >= 0.35:
                    feedback.setProgressText(
                        self.tr(
                            f"Placing points... {done}/{n_poly} polygons "
                            f"(#{done}: {probes} probes, {placed} hits, "
                            f"{now - poly_t0:.0f}s) — "
                            f"{len(accepted_metric)} points so far"
                        )
                    )
                else:
                    feedback.setProgressText(
                        self.tr(
                            f"Placing points... {done}/{n_poly} polygons "
                            f"({len(accepted_metric)} points so far)"
                        )
                    )

            _report(poly_t0, force=False)

            def _pack_phases(phase_list) -> None:
                nonlocal placed, probes, canceled
                for part in parts:
                    if canceled:
                        return
                    engine = self._prepare_engine(part)
                    if engine is None:
                        continue
                    bbox = part.boundingBox()
                    for ox, oy in phase_list:
                        if canceled:
                            return
                        for x, y in self._hex_points_xy(bbox, dx, dy, ox, oy):
                            probes += 1
                            now = time.monotonic()
                            if (now - last_report) >= 1.0:
                                _report(now, force=True)
                                if feedback.isCanceled():
                                    canceled = True
                                    return
                            # Prepared GEOS point test — no per-probe QgsGeometry.
                            if not engine.intersects(QgsPoint(x, y)):
                                continue
                            xy = (x, y)
                            if self._blocked_by_building(
                                xy, bldg_index, bldg_engines
                            ):
                                continue
                            if not self._far_enough_grid(
                                xy, grid, cell, min_dist_sq
                            ):
                                continue
                            accepted_metric.append(xy)
                            self._grid_insert(grid, cell, xy)
                            placed += 1

            _pack_phases(primary_phases)
            if placed == 0 and not canceled:
                _pack_phases(fill_phases)
            if canceled:
                break
            if placed:
                covered_ids.add(mf.id())

        # Representative point for polygons that still have nothing.
        leftovers = [mf for mf in metric_features if mf.id() not in covered_ids]
        feedback.pushInfo(
            self.tr(
                f"Packed {len(accepted_metric)} points; "
                f"{len(leftovers)} polygons need centroid fallback."
            )
        )
        feedback.setProgressText(self.tr("Placing leftover centroids..."))

        for j, mf in enumerate(leftovers):
            if feedback.isCanceled():
                canceled = True
                break
            if leftovers:
                feedback.setProgress(
                    90 + int(5.0 * (j + 1) / max(len(leftovers), 1))
                )
                if j % 25 == 0 or j + 1 == len(leftovers):
                    feedback.setProgressText(
                        self.tr(
                            f"Centroid fallback... {j + 1}/{len(leftovers)} polygons"
                        )
                    )

            geom = mf.geometry()
            engine = self._prepare_engine(geom)
            centroid = geom.centroid().asPoint()
            cxy = (centroid.x(), centroid.y())
            if engine is None or not engine.intersects(
                QgsPoint(cxy[0], cxy[1])
            ):
                try:
                    p = geom.pointOnSurface().asPoint()
                    cxy = (p.x(), p.y())
                except Exception:
                    continue
            if not self._far_enough_grid(cxy, grid, cell, min_dist_sq):
                continue
            if self._blocked_by_building(cxy, bldg_index, bldg_engines):
                continue
            accepted_metric.append(cxy)
            self._grid_insert(grid, cell, cxy)

        # Final enforcement pass (drop any pair that still violates min_dist).
        accepted_metric, dropped = self._enforce_min_distance(
            accepted_metric, min_dist
        )
        if dropped:
            feedback.pushWarning(
                self.tr(
                    f"Removed {dropped} points that violated "
                    f"{min_dist} m spacing after packing."
                )
            )

        nn = self._min_nearest_neighbor(accepted_metric)
        if nn is not None:
            feedback.pushInfo(
                self.tr(
                    f"Measured min nearest-neighbor distance: {nn:.3f} m "
                    f"(requested ≥ {min_dist} m) in {metric_crs.authid()}."
                )
            )
        if nn is not None and nn < min_dist - 1e-3:
            raise QgsProcessingException(
                self.tr(
                    f"Internal spacing error: nearest points are {nn:.3f} m "
                    f"apart but minimum was {min_dist} m."
                )
            )

        fields = QgsFields()
        fields.append(QgsField(ALTITUDE_FIELD, QVariant.Double))

        sink_params, atomic = begin_atomic_file_output(parameters, self.OUTPUT)
        (sink, dest_id) = self.parameterAsSink(
            sink_params,
            self.OUTPUT,
            context,
            fields,
            QgsWkbTypes.PointZ,
            wgs84,
        )
        if sink is None:
            if atomic:
                atomic.abandon()
            raise QgsProcessingException(
                self.tr("Could not create output sink.")
            )

        total_out = len(accepted_metric)
        feedback.pushInfo(
            self.tr(f"Sampling altitudes for {total_out} points…")
        )
        feedback.setProgressText(self.tr("Opening quantized-mesh…"))
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
        feedback.setProgressText(
            self.tr(f"Sampling altitudes (0/{total_out})…")
        )

        # Transform once in parent; mesh samples in worker processes.
        # Output geometries are always EPSG:4326 (lon, lat, Z).
        records = []  # (lon, lat)
        lonlats = []
        for mpt in accepted_metric:
            if feedback.isCanceled():
                canceled = True
                break
            src_pt = to_source.transform(QgsPointXY(mpt[0], mpt[1]))
            wgs = to_wgs84.transform(src_pt)
            records.append((wgs.x(), wgs.y()))
            lonlats.append((wgs.x(), wgs.y()))

        alts = []
        if not canceled:
            alts = sample_lonlats_parallel(
                mesh_folder,
                lonlats,
                workers=workers_req,
                level=sampler.level,
                feedback=feedback,
                scripts_root=_SCRIPTS_ROOT,
            )

        written = 0
        null_alt = 0
        for (lon, lat), alt in zip(records, alts):
            if feedback.isCanceled():
                canceled = True
                break
            try:
                alt_f = float(alt) if alt is not None else None
            except (TypeError, ValueError):
                alt_f = None
            if alt_f is None or not math.isfinite(alt_f):
                null_alt += 1
                z = 0.0
                alt_f = None
            else:
                z = alt_f

            out = QgsFeature(fields)
            out.setGeometry(QgsGeometry(QgsPoint(lon, lat, z)))
            out.setAttributes([alt_f])
            sink.addFeature(out, QgsFeatureSink.FastInsert)
            written += 1

        ok = not canceled
        published = finish_or_abandon(atomic, ok=ok, sink=sink)
        sink = None
        if canceled:
            raise QgsProcessingException(self.tr("Canceled."))
        feedback.setProgress(100)
        feedback.setProgressText(self.tr("Done."))
        feedback.pushInfo(
            self.tr(
                f"Wrote {written} points in EPSG:4326 "
                f"(altitude null={null_alt}, min spacing={min_dist} m)."
            )
        )
        return {self.OUTPUT: published or dest_id}

    @staticmethod
    def _metric_crs_for_layer(layer, feedback):
        """Meter CRS for planar distance: layer CRS if metric, else local UTM."""
        from qgis.core import QgsUnitTypes

        crs = layer.sourceCrs()
        if (
            crs.isValid()
            and not crs.isGeographic()
            and crs.mapUnits() == QgsUnitTypes.DistanceMeters
        ):
            feedback.pushInfo(
                f"Using layer CRS {crs.authid()} for meter distances."
            )
            return crs

        extent = layer.extent()
        cx = 0.5 * (extent.xMinimum() + extent.xMaximum())
        cy = 0.5 * (extent.yMinimum() + extent.yMaximum())

        if crs.isGeographic():
            lon, lat = cx, cy
        else:
            wgs84 = QgsCoordinateReferenceSystem("EPSG:4326")
            to_wgs = QgsCoordinateTransform(
                crs, wgs84, QgsProject.instance()
            )
            pt = to_wgs.transform(QgsPointXY(cx, cy))
            lon, lat = pt.x(), pt.y()

        zone = int(math.floor((lon + 180.0) / 6.0) + 1)
        epsg = (32600 + zone) if lat >= 0 else (32700 + zone)
        metric = QgsCoordinateReferenceSystem(f"EPSG:{epsg}")
        if not metric.isValid():
            feedback.pushWarning(
                "Could not build UTM CRS; using EPSG:3857 for distances."
            )
            metric = QgsCoordinateReferenceSystem("EPSG:3857")
        feedback.pushInfo(
            f"Using {metric.authid()} for meter distances "
            f"(layer CRS was {crs.authid() or 'unknown'})."
        )
        return metric

    @staticmethod
    def _geometry_parts(geom):
        """Yield ring-parts so hex packing uses each part's bbox (not the multipoly hull)."""
        if geom is None or geom.isEmpty():
            return []
        if geom.isMultipart():
            parts = []
            for part in geom.asGeometryCollection():
                if part is None or part.isEmpty():
                    continue
                parts.append(QgsGeometry(part))
            return parts
        return [geom]

    @staticmethod
    def _prepare_engine(geom):
        if geom is None or geom.isEmpty():
            return None
        try:
            engine = QgsGeometry.createGeometryEngine(geom.constGet())
            engine.prepareGeometry()
            return engine
        except Exception:
            return None

    @staticmethod
    def _index_buildings(buildings, metric_crs, clearance, feedback):
        """Buffered buildings in metric CRS + spatial index + prepared engines."""
        to_metric = QgsCoordinateTransform(
            buildings.sourceCrs(), metric_crs, QgsProject.instance()
        )
        index = QgsSpatialIndex()
        engines = {}
        n = 0
        for feat in buildings.getFeatures():
            if feedback.isCanceled():
                break
            geom = feat.geometry()
            if geom is None or geom.isEmpty():
                continue
            metric_geom = QgsGeometry(geom)
            if metric_geom.transform(to_metric) != 0:
                continue
            metric_geom = metric_geom.makeValid()
            if metric_geom.isEmpty():
                continue
            if clearance > 0:
                buffered = metric_geom.buffer(clearance, 5)
                if buffered is None or buffered.isEmpty():
                    buffered = metric_geom
                metric_geom = buffered
            stored = QgsFeature(n)
            stored.setGeometry(metric_geom)
            engine = TreeMaskToPointsAlgorithm._prepare_engine(metric_geom)
            if engine is None:
                continue
            engines[n] = engine
            index.addFeature(stored)
            n += 1
        return index, engines, n

    @staticmethod
    def _blocked_by_building(xy, index, engines):
        if not engines:
            return False
        x, y = xy
        # Degenerate point bbox for the spatial index query.
        rect = QgsRectangle(x, y, x, y)
        pt = QgsPoint(x, y)
        for fid in index.intersects(rect):
            eng = engines.get(fid)
            if eng is not None and eng.intersects(pt):
                return True
        return False

    @staticmethod
    def _hex_points_xy(bbox, dx, dy, phase_x=0.0, phase_y=0.0):
        """Yield (x, y) hex-lattice points covering bbox (no QgsPointXY alloc)."""
        origin_x = bbox.xMinimum() + phase_x
        origin_y = bbox.yMinimum() + phase_y
        x_end = bbox.xMaximum() + dx
        y_end = bbox.yMaximum() + dy
        xmin = bbox.xMinimum() - 1e-9
        xmax = bbox.xMaximum() + 1e-9
        ymin = bbox.yMinimum() - 1e-9
        ymax = bbox.yMaximum() + 1e-9

        row = 0
        y = origin_y
        while y <= y_end:
            x_off = 0.0 if (row % 2 == 0) else (0.5 * dx)
            x = origin_x + x_off
            while x <= x_end:
                if xmin <= x <= xmax and ymin <= y <= ymax:
                    yield (x, y)
                x += dx
            y += dy
            row += 1

    @staticmethod
    def _grid_insert(grid, cell, xy):
        key = (
            int(math.floor(xy[0] / cell)),
            int(math.floor(xy[1] / cell)),
        )
        grid.setdefault(key, []).append(xy)

    @staticmethod
    def _far_enough_grid(xy, grid, cell, min_dist_sq):
        """Return False if xy is closer than sqrt(min_dist_sq) to any point."""
        cx = int(math.floor(xy[0] / cell))
        cy = int(math.floor(xy[1] / cell))
        px, py = xy
        # radius 2 with cell = min_dist/sqrt(2) covers all points within min_dist
        for ix in range(cx - 2, cx + 3):
            for iy in range(cy - 2, cy + 3):
                for ox, oy in grid.get((ix, iy), ()):
                    ddx = px - ox
                    ddy = py - oy
                    if ddx * ddx + ddy * ddy < min_dist_sq:
                        return False
        return True

    @staticmethod
    def _enforce_min_distance(points, min_dist):
        """Keep points in order; drop any that fall within min_dist of a keeper."""
        if not points:
            return points, 0
        min_dist_sq = min_dist * min_dist
        cell = min_dist / math.sqrt(2.0)
        kept = []
        grid = {}
        dropped = 0
        for xy in points:
            if TreeMaskToPointsAlgorithm._far_enough_grid(
                xy, grid, cell, min_dist_sq
            ):
                kept.append(xy)
                TreeMaskToPointsAlgorithm._grid_insert(grid, cell, xy)
            else:
                dropped += 1
        return kept, dropped

    @staticmethod
    def _min_nearest_neighbor(points):
        if len(points) < 2:
            return None
        cell = None
        # Use a hash grid and only compare local neighborhoods.
        # Cell size = max spacing we care about reporting; use large enough
        # neighborhood via scanning all for small n, grid for large n.
        if len(points) <= 2000:
            best = float("inf")
            for i, (x1, y1) in enumerate(points):
                for x2, y2 in points[i + 1 :]:
                    d = math.hypot(x1 - x2, y1 - y2)
                    if d < best:
                        best = d
            return best

        # Approximate NN via grid of cell size based on bbox diagonal fraction
        xs = [p[0] for p in points]
        ys = [p[1] for p in points]
        span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        cell = span / max(math.sqrt(len(points)), 1.0)
        grid = {}
        for xy in points:
            TreeMaskToPointsAlgorithm._grid_insert(grid, cell, xy)
        best = float("inf")
        for x1, y1 in points:
            cx = int(math.floor(x1 / cell))
            cy = int(math.floor(y1 / cell))
            for ix in range(cx - 2, cx + 3):
                for iy in range(cy - 2, cy + 3):
                    for x2, y2 in grid.get((ix, iy), ()):
                        if x1 == x2 and y1 == y2:
                            continue
                        d = math.hypot(x1 - x2, y1 - y2)
                        if d < best:
                            best = d
        return best if best < float("inf") else None
