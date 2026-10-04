# -*- coding: utf-8 -*-
"""
Insert simplified bump polygons into one quantized-mesh tile.

Each bump outline is cut into the existing mesh. An original edge that
crosses the outline stops on that outline, and the part inside the bump is
removed. Outside, the leftover pieces stay on the original surface. Inside,
each convex piece of the outline is filled by a fan from one new vertex, set
to the lowest outline height, out to the outline vertices themselves. A
concave bump gets one such vertex per piece. An optional straight-fan
height instead places that vertex at the lowest straight profile of its
fan-edge pairs, so every pair stays a valley.

On the outline, the only vertices are the bump polygon's own corners and the
points where an original mesh edge crosses that outline. Splitting that edge
also ties the new point to the opposite corner of the triangle. No other
point is added along the outline. After the cut, each new outline vertex
moves to the nearest grid point that stays outside the bump. A corner has to
be outside both sides that meet there, and a crossing has to be outside the
side it lies on, so the new outline wraps the bump. The fan center stays
inside and keeps the nearest grid point.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence, Tuple

XY = Tuple[float, float]
Tri = Tuple[int, int, int]

_BAR_EPS = 1e-9
_ORIENT_EPS = 1e-14
# A skirt vertex sits near 0 m while the same triangle still has real ground.
# The gap keeps a low but genuine height (a few tens of metres) from being
# treated as a skirt.
_SKIRT_Z = 5.0
_SKIRT_GAP = 20.0
# Same integer grid encode_quantized_mesh_tile writes into a .terrain file.
_QM_Q = 32767.0


def _is_skirt_height(z: float, top: float) -> bool:
    return z < _SKIRT_Z and top - z > _SKIRT_GAP


def _blend_above_skirt(zs: Sequence[float], weights: Sequence[float]) -> float:
    """Interpolate ``zs``, leaving out skirt heights when the face also has ground."""
    top = max(zs)
    pairs = [
        (w, z)
        for w, z in zip(weights, zs)
        if not _is_skirt_height(z, top)
    ]
    if len(pairs) == len(zs) or not pairs:
        return sum(w * z for w, z in zip(weights, zs))
    total = sum(w for w, _z in pairs)
    if total <= 1e-15:
        return max(z for _w, z in pairs)
    return sum(w * z for w, z in pairs) / total


def _orient(a: XY, b: XY, c: XY) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _hypot(a: XY, b: XY) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _on_seg(p: XY, a: XY, b: XY, eps: float = 1e-12) -> bool:
    abx, aby = b[0] - a[0], b[1] - a[1]
    span = abx * abx + aby * aby
    if span <= 1e-28:
        return _hypot(p, a) <= eps
    t = ((p[0] - a[0]) * abx + (p[1] - a[1]) * aby) / span
    if t < -1e-8 or t > 1.0 + 1e-8:
        return False
    t = max(0.0, min(1.0, t))
    qx = a[0] + t * abx
    qy = a[1] + t * aby
    return math.hypot(p[0] - qx, p[1] - qy) <= eps


def _proper_cross(a: XY, b: XY, c: XY, d: XY) -> bool:
    o1 = _orient(a, b, c)
    o2 = _orient(a, b, d)
    o3 = _orient(c, d, a)
    o4 = _orient(c, d, b)
    return o1 * o2 < 0.0 and o3 * o4 < 0.0


def _pip(x: float, y: float, ring: Sequence[XY]) -> bool:
    inside = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) == (y2 > y):
            continue
        xint = x1 + (y - y1) / (y2 - y1) * (x2 - x1)
        if x < xint:
            inside = not inside
    return inside


def _strict_inside(p: XY, ring: Sequence[XY]) -> bool:
    n = len(ring)
    for i in range(n):
        if _on_seg(p, ring[i], ring[(i + 1) % n]):
            return False
    return _pip(p[0], p[1], ring)


def _ccw_ring(ring: Sequence[XY]) -> List[XY]:
    if len(ring) < 3:
        return list(ring)
    # Shift to the first vertex. Absolute lon/lat products cancel and can
    # flip the sign of a polygon that is only a few metres across.
    ox, oy = ring[0]
    area = 0.0
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i][0] - ox, ring[i][1] - oy
        x2, y2 = ring[(i + 1) % n][0] - ox, ring[(i + 1) % n][1] - oy
        area += x1 * y2 - x2 * y1
    if area < 0.0:
        return list(reversed(ring))
    return list(ring)


def _dedupe_ring(ring: Sequence[XY]) -> List[XY]:
    out: List[XY] = []
    for p in ring:
        if out and _hypot(out[-1], p) <= 1e-12:
            continue
        out.append((float(p[0]), float(p[1])))
    if len(out) >= 2 and _hypot(out[0], out[-1]) <= 1e-12:
        out.pop()
    return out


def _centroid(ring: Sequence[XY]) -> XY:
    n = len(ring)
    ox = sum(p[0] for p in ring) / n
    oy = sum(p[1] for p in ring) / n
    area = 0.0
    cx = 0.0
    cy = 0.0
    for i in range(n):
        x1, y1 = ring[i][0] - ox, ring[i][1] - oy
        x2, y2 = ring[(i + 1) % n][0] - ox, ring[(i + 1) % n][1] - oy
        cross = x1 * y2 - x2 * y1
        area += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area) <= 1e-24:
        return (ox, oy)
    cx /= 3.0 * area
    cy /= 3.0 * area
    return (ox + cx, oy + cy)


def _in_kernel(p: XY, ring: Sequence[XY]) -> bool:
    """True when ``p`` is inside every edge. Uses the sign only.

    Polygon edges in degrees are a few metres long, so the cross product of
    an interior point can be far below 1e-14 and still be strictly inside.
    """
    n = len(ring)
    for i in range(n):
        if _orient(ring[i], ring[(i + 1) % n], p) <= 0.0:
            return False
    return True


def _visible(ring: Sequence[XY], i: int, j: int) -> bool:
    n = len(ring)
    if j % n in ((i - 1) % n, i, (i + 1) % n):
        return False
    a = ring[i]
    b = ring[j]
    mid = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
    if not _strict_inside(mid, ring) and not _pip(mid[0], mid[1], ring):
        return False
    for k in range(n):
        c = ring[k]
        d = ring[(k + 1) % n]
        if k in (i, j) or (k + 1) % n in (i, j):
            continue
        if _proper_cross(a, b, c, d):
            return False
    return _pip(mid[0], mid[1], ring)


def _split_ring(ring: Sequence[XY], i: int, j: int) -> Tuple[List[XY], List[XY]]:
    n = len(ring)
    if i > j:
        i, j = j, i
    part_a = list(ring[i : j + 1])
    part_b = list(ring[j:]) + list(ring[: i + 1])
    if len(part_b) < 3:
        part_b = []
    return part_a, part_b


def _line_intersect(a: XY, b: XY, c: XY, d: XY) -> Optional[XY]:
    den = (b[0] - a[0]) * (d[1] - c[1]) - (b[1] - a[1]) * (d[0] - c[0])
    if abs(den) <= 1e-18:
        return None
    t = ((c[0] - a[0]) * (d[1] - c[1]) - (c[1] - a[1]) * (d[0] - c[0])) / den
    return (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))


def _kernel_point(ring: Sequence[XY]) -> Optional[XY]:
    """A point that can see the whole polygon, or None when the kernel is empty."""
    ring = _ccw_ring(ring)
    if len(ring) < 3:
        return None
    cand = _centroid(ring)
    if _in_kernel(cand, ring):
        return cand
    n = len(ring)
    for i in range(n):
        a = ring[i]
        b = ring[(i + 1) % n]
        for j in range(i + 1, n):
            hit = _line_intersect(a, b, ring[j], ring[(j + 1) % n])
            if hit is not None and _in_kernel(hit, ring):
                return hit
    return None


def _ear_pieces(ring: Sequence[XY]) -> List[List[XY]]:
    """Split a simple polygon into triangles. Each triangle can take one inner vertex."""
    ring = _dedupe_ring(_ccw_ring(list(ring)))
    idx = list(range(len(ring)))
    pieces: List[List[XY]] = []
    guard = 0
    while len(idx) > 3 and guard < len(idx) * len(idx) + 8:
        guard += 1
        n = len(idx)
        clipped = False
        for i in range(n):
            ip = idx[(i - 1) % n]
            ic = idx[i]
            inn = idx[(i + 1) % n]
            prev, cur, nxt = ring[ip], ring[ic], ring[inn]
            if _orient(prev, cur, nxt) <= 0.0:
                continue
            area = _orient(prev, cur, nxt)
            blocked = False
            for k in idx:
                if k in (ip, ic, inn):
                    continue
                px, py = ring[k]
                w0 = _orient(cur, nxt, (px, py)) / area
                w1 = _orient(nxt, prev, (px, py)) / area
                w2 = _orient(prev, cur, (px, py)) / area
                if w0 > 1e-8 and w1 > 1e-8 and w2 > 1e-8:
                    blocked = True
                    break
            if blocked:
                continue
            pieces.append([prev, cur, nxt])
            del idx[i]
            clipped = True
            break
        if not clipped:
            return []
    if len(idx) == 3:
        pieces.append([ring[i] for i in idx])
    return pieces


def _convex_pieces(ring: Sequence[XY], depth: int = 0) -> List[List[XY]]:
    """Split until each piece can be fanned from one interior vertex."""
    ring = _dedupe_ring(_ccw_ring(ring))
    if len(ring) < 3:
        return []
    if _kernel_point(ring) is not None or depth > 24:
        return [ring]
    n = len(ring)
    reflex = None
    for i in range(n):
        if _orient(ring[i - 1], ring[i], ring[(i + 1) % n]) < -_ORIENT_EPS:
            reflex = i
            break
    if reflex is None:
        return _ear_pieces(ring) or [ring]
    for step in range(2, n - 1):
        j = (reflex + step) % n
        if not _visible(ring, reflex, j):
            continue
        part_a, part_b = _split_ring(ring, reflex, j)
        if len(part_a) < 3 or len(part_b) < 3:
            continue
        return _convex_pieces(part_a, depth + 1) + _convex_pieces(part_b, depth + 1)
    return _ear_pieces(ring) or [ring]


def _drop_vertices_on_edges(ring: Sequence[XY]) -> List[XY]:
    """Drop a vertex that lies on an edge it does not belong to.

    A clip against a tile edge can leave several vertices on that same
    straight line, with the ring jumping along it. Those extra vertices make
    the ring cross itself. Removing them does not change the area.
    """
    ring = _dedupe_ring(ring)
    changed = True
    while changed and len(ring) >= 4:
        changed = False
        n = len(ring)
        for i in range(n):
            p = ring[i]
            for k in range(n):
                if i in (k, (k + 1) % n):
                    continue
                a = ring[k]
                b = ring[(k + 1) % n]
                if _hypot(p, a) <= 1e-12 or _hypot(p, b) <= 1e-12:
                    continue
                if _on_seg(p, a, b, eps=1e-12):
                    del ring[i]
                    changed = True
                    break
            if changed:
                break
    return ring


def _clip_rect(
    ring: Sequence[XY], west: float, south: float, east: float, north: float
) -> List[XY]:
    def clip(poly: List[XY], inside, cross) -> List[XY]:
        if not poly:
            return []
        out: List[XY] = []
        for i, cur in enumerate(poly):
            prev = poly[i - 1]
            pin = inside(prev)
            cin = inside(cur)
            if cin:
                if not pin:
                    out.append(cross(prev, cur))
                out.append(cur)
            elif pin:
                out.append(cross(prev, cur))
        return out

    poly = [(float(x), float(y)) for x, y in ring]

    def x_cross(p, q, x):
        if abs(q[0] - p[0]) < 1e-18:
            return (x, p[1])
        t = (x - p[0]) / (q[0] - p[0])
        return (x, p[1] + t * (q[1] - p[1]))

    def y_cross(p, q, y):
        if abs(q[1] - p[1]) < 1e-18:
            return (p[0], y)
        t = (y - p[1]) / (q[1] - p[1])
        return (p[0] + t * (q[0] - p[0]), y)

    poly = clip(poly, lambda p: p[0] >= west - 1e-15, lambda p, q: x_cross(p, q, west))
    poly = clip(poly, lambda p: p[0] <= east + 1e-15, lambda p, q: x_cross(p, q, east))
    poly = clip(poly, lambda p: p[1] >= south - 1e-15, lambda p, q: y_cross(p, q, south))
    poly = clip(poly, lambda p: p[1] <= north + 1e-15, lambda p, q: y_cross(p, q, north))
    return _dedupe_ring(poly)


def _tri_edges(tri: Tri) -> Tuple[Tuple[int, int], Tuple[int, int], Tuple[int, int]]:
    a, b, c = tri
    return (a, b), (b, c), (c, a)


def _edge_key(a: int, b: int) -> Tuple[int, int]:
    return (a, b) if a < b else (b, a)


class _Tin:
    def __init__(
        self,
        lons: Sequence[float],
        lats: Sequence[float],
        alts: Sequence[float],
        triangles: Sequence[Tri],
    ) -> None:
        self.lon = [float(v) for v in lons]
        self.lat = [float(v) for v in lats]
        self.alt = [float(v) for v in alts]
        self.tris: List[Tri] = [tuple(t) for t in triangles]
        self.edges: Dict[Tuple[int, int], List[int]] = {}
        self._grid: Optional[Tuple[float, float, float, float]] = None
        self._cells: Dict[Tuple[int, int], int] = {}
        # Outline cuts must not invent a midpoint. That point is not a crossing
        # of an original edge, and snapping it leaves a gap or an overlap.
        self._allow_steiner = True
        # Edges from the input tile. Pieces of those edges stay in the set
        # when a crossing splits them. Nothing else is an original edge.
        self._original_edges: set = set()
        # First reason the current bump could not be cut in. Empty when it could.
        self._fail_reason = ""
        self._rebuild()

    def _uv(self, p: XY) -> Tuple[int, int]:
        west, south, east, north = self._grid or (0.0, 0.0, 1.0, 1.0)
        du = max(east - west, 1e-15)
        dv = max(north - south, 1e-15)
        u = int(round((p[0] - west) / du * _QM_Q))
        v = int(round((p[1] - south) / dv * _QM_Q))
        q = int(_QM_Q)
        return max(0, min(q, u)), max(0, min(q, v))

    def _grid_step(self) -> float:
        if self._grid is None:
            return 0.0
        west, _south, east, north = self._grid
        return min((east - west) / _QM_Q, (north - self._grid[1]) / _QM_Q)

    def _from_uv(self, uv: Tuple[int, int]) -> XY:
        west, south, east, north = self._grid or (0.0, 0.0, 1.0, 1.0)
        u, v = uv
        return (
            west + (east - west) * (u / _QM_Q),
            south + (north - south) * (v / _QM_Q),
        )

    def _snap(self, p: XY) -> XY:
        if self._grid is None:
            return (float(p[0]), float(p[1]))
        return self._from_uv(self._uv(p))

    def enable_grid(self, west: float, south: float, east: float, north: float) -> None:
        """Snap every vertex onto the quantized-mesh grid and merge shared cells.

        Later inserts land on that same grid, so writing the .terrain file
        does not move them again.
        """
        self._grid = (float(west), float(south), float(east), float(north))
        self._cells = {}
        remap = list(range(len(self.lon)))
        for i in range(len(self.lon)):
            key = self._uv((self.lon[i], self.lat[i]))
            if key in self._cells:
                remap[i] = self._cells[key]
                continue
            sp = self._from_uv(key)
            self.lon[i] = sp[0]
            self.lat[i] = sp[1]
            self._cells[key] = i
        if any(remap[i] != i for i in range(len(remap))):
            welded: List[Tri] = []
            for tri in self.tris:
                a, b, c = remap[tri[0]], remap[tri[1]], remap[tri[2]]
                if len({a, b, c}) < 3:
                    continue
                welded.append(self._orient_tri((a, b, c)))
            self.tris = welded
        self._rebuild()

    def _reindex_cells(self) -> None:
        if self._grid is None:
            return
        self._cells = {}
        for i, (x, y) in enumerate(zip(self.lon, self.lat)):
            self._cells[self._uv((x, y))] = i

    def xy(self, i: int) -> XY:
        return (self.lon[i], self.lat[i])

    def _rebuild(self) -> None:
        edges: Dict[Tuple[int, int], List[int]] = {}
        for ti, tri in enumerate(self.tris):
            if tri[0] == tri[1] or tri[1] == tri[2] or tri[2] == tri[0]:
                continue
            for a, b in _tri_edges(tri):
                edges.setdefault(_edge_key(a, b), []).append(ti)
        self.edges = edges

    def _orient_tri(self, tri: Tri) -> Tri:
        a, b, c = tri
        if _orient(self.xy(a), self.xy(b), self.xy(c)) < 0.0:
            return (a, c, b)
        return (a, b, c)

    def _bary(self, p: XY, tri: Tri) -> Optional[Tuple[float, float, float]]:
        a = self.xy(tri[0])
        b = self.xy(tri[1])
        c = self.xy(tri[2])
        area = _orient(a, b, c)
        if abs(area) <= 1e-24:
            return None
        w0 = _orient(b, c, p) / area
        w1 = _orient(c, a, p) / area
        w2 = _orient(a, b, p) / area
        if w0 < -_BAR_EPS or w1 < -_BAR_EPS or w2 < -_BAR_EPS:
            return None
        return w0, w1, w2

    def find_tri(self, p: XY) -> Optional[Tuple[int, Tuple[float, float, float]]]:
        for ti, tri in enumerate(self.tris):
            weights = self._bary(p, tri)
            if weights is not None:
                return ti, weights
        return None

    def _add(self, p: XY, z: float, snap: bool = True) -> int:
        """Append ``p``, or return the vertex already in that grid cell.

        A reused vertex keeps the height it already has. ``snap`` is false for
        a point that must stay on an existing edge; moving it to the grid
        cell would leave the edge and the outline cut would stop.
        """
        on_cell = False
        if self._grid is not None and snap:
            p = self._snap(p)
        if self._grid is not None:
            key = self._uv(p)
            center = self._from_uv(key)
            on_cell = (
                abs(center[0] - p[0]) <= 1e-9 and abs(center[1] - p[1]) <= 1e-9
            )
            if on_cell and key in self._cells:
                return self._cells[key]
            if not on_cell:
                for i, (x, y) in enumerate(zip(self.lon, self.lat)):
                    if abs(x - p[0]) <= 1e-12 and abs(y - p[1]) <= 1e-12:
                        return i
        vid = len(self.lon)
        self.lon.append(float(p[0]))
        self.lat.append(float(p[1]))
        self.alt.append(float(z))
        if on_cell:
            self._cells[key] = vid
        return vid

    def _occupy_new(self, p: XY, z: float) -> Optional[int]:
        """Add ``p`` at ``z`` only when its grid cell is empty."""
        if self._grid is None:
            return self._add(p, z)
        key = self._uv(p)
        if key in self._cells:
            return None
        return self._add(p, z)

    def _occupy_inside(self, target: XY, z: float, ring: Sequence[XY]) -> Optional[int]:
        """Place ``z`` at ``target`` or a nearby free cell inside ``ring``."""
        hit = self._occupy_new(target, z)
        if hit is not None or self._grid is None:
            return hit
        west, south, east, north = self._grid
        du = (east - west) / _QM_Q
        dv = (north - south) / _QM_Q
        for radius in range(1, 6):
            for iu in range(-radius, radius + 1):
                for iv in range(-radius, radius + 1):
                    if max(abs(iu), abs(iv)) != radius:
                        continue
                    p = (target[0] + iu * du, target[1] + iv * dv)
                    if not (_pip(p[0], p[1], ring) or _strict_inside(p, ring)):
                        continue
                    hit = self._occupy_new(p, z)
                    if hit is not None:
                        return hit
        return None

    def _interp(self, tri: Tri, weights: Tuple[float, float, float]) -> float:
        """Height inside ``tri``. Skirt vertices are left out of the blend.

        A skirt triangle is (0, 0, terrain). A point that falls in it would
        otherwise be stored near 0 m, and the fan would dig the whole bump
        down to that height.
        """
        zs = [self.alt[v] for v in tri]
        return _blend_above_skirt(zs, weights)

    def _edge_height(self, e0: int, e1: int, u: float) -> float:
        """Height at fraction ``u`` along an edge, ignoring a skirt end."""
        z0, z1 = self.alt[e0], self.alt[e1]
        top = max(z0, z1)
        low0 = _is_skirt_height(z0, top)
        low1 = _is_skirt_height(z1, top)
        if low0 and not low1:
            return z1
        if low1 and not low0:
            return z0
        return z0 + u * (z1 - z0)

    def insert(self, p: XY, exact: bool = False) -> Optional[int]:
        """Insert ``p`` into the triangulation. Z is interpolated from the mesh.

        ``exact`` keeps the point where it was given. Outline corners and
        crossings use that so a shared grid cell cannot pull them off the
        outline. Other inserts still land on the quantized-mesh grid.
        """
        if self._grid is not None and not exact:
            snapped = self._snap(p)
            hit = self._cells.get(self._uv(snapped))
            if hit is not None:
                return hit
            # Keep the outline point when the grid cell beside it has no
            # triangle. Snapping it there would drop the whole bump.
            if self.find_tri(snapped) is not None:
                p = snapped
        else:
            for i in range(len(self.lon)):
                if _hypot(self.xy(i), p) <= 1e-12:
                    return i
        found = self.find_tri(p)
        if found is None:
            return None
        ti, weights = found
        tri = self.tris[ti]
        w0, w1, w2 = weights
        for w, vid in zip(weights, tri):
            if w >= 1.0 - 1e-8:
                return vid
        z = self._interp(tri, weights)
        if w0 <= _BAR_EPS:
            return self._split_edge(tri[1], tri[2], p, z)
        if w1 <= _BAR_EPS:
            return self._split_edge(tri[2], tri[0], p, z)
        if w2 <= _BAR_EPS:
            return self._split_edge(tri[0], tri[1], p, z)
        return self._split_face(ti, p, z)

    def _split_face(self, ti: int, p: XY, z: float) -> int:
        i0, i1, i2 = self.tris[ti]
        vid = self._add(p, z, snap=False)
        if vid in (i0, i1, i2):
            return vid
        self.tris[ti] = self._orient_tri((i0, i1, vid))
        self.tris.append(self._orient_tri((i1, i2, vid)))
        self.tris.append(self._orient_tri((i2, i0, vid)))
        self._rebuild()
        return vid

    def _split_edge(self, a: int, b: int, p: XY, z: float) -> int:
        key = _edge_key(a, b)
        owners = list(self.edges.get(key, []))
        vid = self._add(p, z, snap=False)
        if vid in (a, b) or not owners:
            return vid
        for ti in sorted(owners, reverse=True):
            tri = self.tris[ti]
            other = [v for v in tri if v != a and v != b]
            if len(other) != 1:
                continue
            c = other[0]
            if c == vid:
                continue
            ia = tri.index(a)
            forward = tri[(ia + 1) % 3] == b
            if forward:
                first = (a, vid, c)
                second = (vid, b, c)
            else:
                first = (b, vid, c)
                second = (vid, a, c)
            self.tris[ti] = self._orient_tri(first)
            self.tris.append(self._orient_tri(second))
        self._rebuild()
        if key in self._original_edges and vid not in (a, b):
            if self.has_edge(a, vid):
                self._original_edges.add(_edge_key(a, vid))
            if self.has_edge(vid, b):
                self._original_edges.add(_edge_key(vid, b))
        return vid

    def has_edge(self, a: int, b: int) -> bool:
        return _edge_key(a, b) in self.edges

    def _incident(self, vid: int) -> List[int]:
        found = []
        seen = set()
        for ti, tri in enumerate(self.tris):
            if vid in tri and ti not in seen:
                seen.add(ti)
                found.append(ti)
        return found

    def _side(self, a: int, b: int, vid: int) -> float:
        return _orient(self.xy(a), self.xy(b), self.xy(vid))

    def _enters(self, ti: int, a: int, b: int) -> bool:
        tri = self.tris[ti]
        if a not in tri:
            return False
        others = [v for v in tri if v != a]
        if len(others) != 2:
            return False
        u, v = others
        spread = _orient(self.xy(a), self.xy(u), self.xy(v))
        toward_u = _orient(self.xy(a), self.xy(u), self.xy(b))
        toward_v = _orient(self.xy(a), self.xy(b), self.xy(v))
        # A few metres of polygon in degrees makes a 1e-14 test miss a
        # direction that runs along an existing edge.
        tol = 1e-12
        if spread >= 0.0:
            return toward_u >= -tol and toward_v >= -tol
        return toward_u <= tol and toward_v <= tol

    def _exit_edge(
        self, ti: int, a: int, b: int, entry: Optional[Tuple[int, int]]
    ) -> Optional[Tuple[int, int]]:
        for e0, e1 in _tri_edges(self.tris[ti]):
            if entry is not None and _edge_key(e0, e1) == _edge_key(*entry):
                continue
            if entry is None and a in (e0, e1):
                continue
            if b in (e0, e1):
                return (e0, e1)
            if _proper_cross(self.xy(a), self.xy(b), self.xy(e0), self.xy(e1)):
                return (e0, e1)
        return None

    def _verts_on_open_segment(self, a: int, b: int) -> List[int]:
        ax, ay = self.xy(a)
        bx, by = self.xy(b)
        dx, dy = bx - ax, by - ay
        span = dx * dx + dy * dy
        if span <= 1e-28:
            return []
        found: List[Tuple[float, int]] = []
        for i in range(len(self.lon)):
            if i == a or i == b:
                continue
            t = ((self.lon[i] - ax) * dx + (self.lat[i] - ay) * dy) / span
            if t <= 1e-8 or t >= 1.0 - 1e-8:
                continue
            if _on_seg(self.xy(i), self.xy(a), self.xy(b), eps=1e-9):
                found.append((t, i))
        found.sort()
        return [i for _t, i in found]

    def _push(self, chain: List[int], vid: int) -> None:
        if not chain or chain[-1] != vid:
            chain.append(vid)

    def _link_vertex_in_tri(self, ti: int, vid: int) -> None:
        """Connect an existing vertex that lies inside ``tri`` to that face."""
        i0, i1, i2 = self.tris[ti]
        if vid in (i0, i1, i2):
            return
        self.tris[ti] = self._orient_tri((i0, i1, vid))
        self.tris.append(self._orient_tri((i1, i2, vid)))
        self.tris.append(self._orient_tri((i2, i0, vid)))
        self._rebuild()

    def _split_edge_at_vertex(self, a: int, end: int, mid: int) -> bool:
        """Replace edge ``a-end`` with ``a-mid`` and ``mid-end``. ``mid`` already exists."""
        owners = list(self.edges.get(_edge_key(a, end), []))
        if not owners or mid in (a, end):
            return False
        for ti in sorted(owners, reverse=True):
            tri = self.tris[ti]
            other = [v for v in tri if v != a and v != end]
            if len(other) != 1:
                continue
            c = other[0]
            self.tris[ti] = self._orient_tri((a, mid, c))
            self.tris.append(self._orient_tri((mid, end, c)))
        self._rebuild()
        return self.has_edge(a, mid)

    def _vertex_on_opposite(self, vid: int) -> Optional[Tuple[int, int]]:
        """Edge of an incident triangle that ``vid`` already lies on.

        A flat triangle has its third vertex on the opposite edge. The walk
        then treats that edge as the way forward and the step lands on the
        vertex it just reached.
        """
        for ti in self._incident(vid):
            tri = self.tris[ti]
            others = [v for v in tri if v != vid]
            if len(others) != 2 or others[0] == others[1]:
                continue
            e0, e1 = others
            if _on_seg(self.xy(vid), self.xy(e0), self.xy(e1), eps=1e-9):
                return e0, e1
        return None

    def _stitch_on_edge(self, vid: int, e0: int, e1: int) -> bool:
        """Split ``e0-e1`` at ``vid`` and drop the flat triangle that caused it."""
        if vid in (e0, e1):
            return False
        owners = list(self.edges.get(_edge_key(e0, e1), []))
        if not owners:
            return False
        for ti in sorted(owners, reverse=True):
            tri = self.tris[ti]
            other = [v for v in tri if v != e0 and v != e1]
            if len(other) != 1:
                continue
            c = other[0]
            if c == vid:
                del self.tris[ti]
                continue
            self.tris[ti] = self._orient_tri((e0, vid, c))
            self.tris.append(self._orient_tri((vid, e1, c)))
        self._rebuild()
        return True

    def _force_along_edge(self, a: int, b: int) -> bool:
        """If ``b`` lies on an edge that leaves ``a``, split that edge at ``b``."""
        for ti in self._incident(a):
            for end in self.tris[ti]:
                if end == a:
                    continue
                if not _on_seg(self.xy(b), self.xy(a), self.xy(end), eps=1e-9):
                    continue
                # b is on a-end, or end is on a-b.
                ax, ay = self.xy(a)
                bx, by = self.xy(b)
                ex, ey = self.xy(end)
                ab = (bx - ax) * (bx - ax) + (by - ay) * (by - ay)
                if ab <= 1e-28:
                    return False
                t = ((ex - ax) * (bx - ax) + (ey - ay) * (by - ay)) / ab
                if t < 1.0 - 1e-6:
                    continue
                return self._split_edge_at_vertex(a, end, b)
        return False

    def _edge_hit(
        self, a: int, b: int, e0: int, e1: int
    ) -> Optional[Tuple[XY, float]]:
        """Where segment ``a-b`` crosses edge ``e0-e1``. ``u`` runs along ``e0`` to ``e1``."""
        px, py = self.xy(a)
        qx, qy = self.xy(b)
        cx, cy = self.xy(e0)
        dx, dy = self.xy(e1)
        rx, ry = qx - px, qy - py
        sx, sy = dx - cx, dy - cy
        den = rx * sy - ry * sx
        if abs(den) < 1e-30:
            return None
        u = ((cx - px) * ry - (cy - py) * rx) / den
        pt = (cx + u * sx, cy + u * sy)
        return pt, u

    def _vertex_near(self, p: XY, eps: float = 1e-9) -> Optional[int]:
        for i in range(len(self.lon)):
            if _hypot(self.xy(i), p) <= eps:
                return i
        return None

    def _force_simple(self, a: int, b: int) -> bool:
        """Make ``a-b`` an edge by splitting each crossed triangle in its own plane.

        New points lie on existing edges and keep that edge's height, so the
        surface on both sides of the cut stays the original surface.
        """
        if a == b or self.has_edge(a, b):
            return True
        mids = self._verts_on_open_segment(a, b)
        if mids:
            return self._force_simple(a, mids[0]) and self._force_simple(mids[0], b)
        guard = 0
        limit = max(8, len(self.lon) + len(self.tris) + 4)
        cur = a
        while cur != b and not self.has_edge(cur, b) and guard < limit:
            guard += 1
            if not self._advance_toward(cur, b):
                return False
            nxt = self._frontier
            if nxt == cur:
                self.fail_reason = "stuck"
                return False
            cur = nxt
        if cur != b and not self.has_edge(cur, b):
            self.fail_reason = "too_long"
            return False
        return True

    def _advance_toward(self, a: int, b: int) -> bool:
        """Split the next edge between ``a`` and ``b``. Sets ``self._frontier``."""
        self._frontier = a
        if a == b or self.has_edge(a, b):
            self._frontier = b
            return True
        for _ in range(4):
            flat = self._vertex_on_opposite(a)
            if flat is None:
                break
            if not self._stitch_on_edge(a, flat[0], flat[1]):
                break
        candidates = []
        for ti in self._incident(a):
            if b in self.tris[ti]:
                self._frontier = b
                return True
            if not self._enters(ti, a, b):
                continue
            if self._bary(self.xy(b), self.tris[ti]) is not None:
                self._link_vertex_in_tri(ti, b)
                self._frontier = b
                return self.has_edge(a, b)
            if self._exit_edge(ti, a, b, None) is None:
                continue
            candidates.append(ti)
        for start in candidates:
            nxt = self._step_across(a, b, start)
            if nxt is None or nxt == a:
                continue
            if _hypot(self.xy(nxt), self.xy(a)) <= 1e-12:
                continue
            if not self.has_edge(a, nxt) and nxt != b:
                continue
            self._frontier = nxt
            return True
        if self._force_along_edge(a, b):
            self._frontier = b
            return self.has_edge(a, b)
        if not self._allow_steiner:
            self.fail_reason = "no_steiner"
            return False
        if _hypot(self.xy(a), self.xy(b)) > 1e-7:
            mid = (
                (self.lon[a] + self.lon[b]) * 0.5,
                (self.lat[a] + self.lat[b]) * 0.5,
            )
            if self.find_tri(mid) is None:
                self.fail_reason = "no_start_outside"
                return False
            vid = self.insert(mid)
            if vid is None or vid in (a, b):
                step = self._grid_step()
                if step > 0.0 and _hypot(self.xy(a), self.xy(b)) <= step * 2.0:
                    self._frontier = b
                    return True
                self.fail_reason = "no_start"
                return False
            ok = self._force_simple(a, vid) and self._force_simple(vid, b)
            self._frontier = b
            if not ok:
                self.fail_reason = "no_start_split:" + str(getattr(self, "fail_reason", ""))
            return ok
        step = self._grid_step()
        if step > 0.0 and _hypot(self.xy(a), self.xy(b)) <= step:
            # Closer than one quantized-mesh cell. The saved tile cannot
            # tell these vertices apart, so the outline continues.
            self._frontier = b
            return True
        self.fail_reason = "no_start"
        return False

    def _step_across(self, a: int, b: int, start: int) -> Optional[int]:
        """Vertex where ``a-b`` leaves triangle ``start``, splitting that edge in plane."""
        exit_e = self._exit_edge(start, a, b, None)
        if exit_e is None:
            return None
        e0, e1 = exit_e
        if b in (e0, e1):
            return b
        hit = self._edge_hit(a, b, e0, e1)
        if hit is None:
            return None
        pt, u = hit
        if _hypot(pt, self.xy(a)) <= 1e-12:
            return None
        if u <= 1e-7:
            nxt = e0
        elif u >= 1.0 - 1e-7:
            nxt = e1
        else:
            nxt = self._vertex_near(pt)
            if nxt == a:
                return None
            if nxt is None:
                z = self._edge_height(e0, e1, u)
                nxt = self._split_edge(e0, e1, pt, z)
            elif nxt not in (e0, e1) and not self.has_edge(e0, nxt):
                self._split_edge_at_vertex(e0, e1, nxt)
        return nxt

    def _polygon_tris(self, poly: List[int]) -> List[Tri]:
        idx = [v for i, v in enumerate(poly) if i == 0 or v != poly[i - 1]]
        if len(idx) >= 2 and idx[0] == idx[-1]:
            idx.pop()
        if len(idx) < 3:
            return []
        area = 0.0
        ox, oy = self.xy(idx[0])
        for i, vid in enumerate(idx):
            x1, y1 = self.xy(vid)
            x2, y2 = self.xy(idx[(i + 1) % len(idx)])
            area += (x1 - ox) * (y2 - oy) - (x2 - ox) * (y1 - oy)
        if area < 0.0:
            idx.reverse()
        # Ear clipping can drop a vertex that lies on the straight edge
        # between its neighbours. That vertex is still part of the boundary,
        # so it has to be put back onto the edge it belongs to.
        chain = list(idx)
        tris: List[Tri] = []
        guard = 0
        while len(idx) > 3 and guard < len(idx) * len(idx) + 4:
            guard += 1
            n = len(idx)
            clipped = False
            for i in range(n):
                prev = idx[(i - 1) % n]
                cur = idx[i]
                nxt = idx[(i + 1) % n]
                if _orient(self.xy(prev), self.xy(cur), self.xy(nxt)) <= 1e-14:
                    continue
                if self._ear_blocked(idx, prev, cur, nxt):
                    continue
                tris.append(self._orient_tri((prev, cur, nxt)))
                del idx[i]
                clipped = True
                break
            if not clipped:
                break
        if len(idx) == 3:
            tris.append(self._orient_tri((idx[0], idx[1], idx[2])))
        elif len(idx) > 3:
            origin = idx[0]
            for i in range(1, len(idx) - 1):
                tris.append(self._orient_tri((origin, idx[i], idx[i + 1])))
        tris = [t for t in tris if abs(_orient(self.xy(t[0]), self.xy(t[1]), self.xy(t[2]))) > 1e-22]
        return self._stitch_collinear(chain, tris)

    def _stitch_collinear(self, verts: Sequence[int], tris: List[Tri]) -> List[Tri]:
        """Split any new edge that still has a chain vertex lying on it."""
        for _ in range(len(verts) + 1):
            used = {v for tri in tris for v in tri}
            pending = [v for v in verts if v not in used]
            if not pending:
                break
            placed = False
            for vid in pending:
                owners = []
                for ti, tri in enumerate(tris):
                    a, b, c = tri
                    for e0, e1, opp in ((a, b, c), (b, c, a), (c, a, b)):
                        if vid in (e0, e1, opp):
                            continue
                        if _hypot(self.xy(vid), self.xy(e0)) <= 1e-12:
                            continue
                        if _hypot(self.xy(vid), self.xy(e1)) <= 1e-12:
                            continue
                        if _on_seg(self.xy(vid), self.xy(e0), self.xy(e1), eps=1e-9):
                            owners.append((ti, e0, e1, opp))
                            break
                if not owners:
                    continue
                for ti, e0, e1, opp in sorted(owners, key=lambda item: item[0], reverse=True):
                    tris[ti] = self._orient_tri((e0, vid, opp))
                    tris.append(self._orient_tri((vid, e1, opp)))
                placed = True
                break
            if not placed:
                break
        return [
            t
            for t in tris
            if abs(_orient(self.xy(t[0]), self.xy(t[1]), self.xy(t[2]))) > 1e-22
        ]

    def _ear_blocked(self, idx: Sequence[int], prev: int, cur: int, nxt: int) -> bool:
        pa, pb, pc = self.xy(prev), self.xy(cur), self.xy(nxt)
        for vid in idx:
            if vid in (prev, cur, nxt):
                continue
            # Clipping this ear would replace prev-nxt and leave a vertex
            # that sits on that boundary out of the mesh.
            if _on_seg(self.xy(vid), pa, pc, eps=1e-12):
                return True
            weights = self._bary(self.xy(vid), (prev, cur, nxt))
            if weights is None:
                continue
            if all(w > 1e-10 for w in weights):
                return True
            if _pip(self.lon[vid], self.lat[vid], (pa, pb, pc)):
                return True
        return False

    def force_edge(self, a: int, b: int) -> List[int]:
        """Make ``a-b`` a mesh edge. Return the vertex chain from ``a`` to ``b``."""
        if a == b:
            return [a]
        mids = self._verts_on_open_segment(a, b)
        nodes = [a, *mids, b]
        chain = [a]
        for u, v in zip(nodes, nodes[1:]):
            if not self._force_simple(u, v):
                return []
            chain.append(v)
        return chain

    def interior_ids(self, ring: Sequence[XY]) -> List[int]:
        found = []
        for i in range(len(self.lon)):
            if _strict_inside(self.xy(i), ring):
                found.append(i)
        return found

    def remove_inside(self, ring: Sequence[XY]) -> None:
        kept: List[Tri] = []
        for tri in self.tris:
            cx = (self.lon[tri[0]] + self.lon[tri[1]] + self.lon[tri[2]]) / 3.0
            cy = (self.lat[tri[0]] + self.lat[tri[1]] + self.lat[tri[2]]) / 3.0
            if _strict_inside((cx, cy), ring):
                continue
            kept.append(tri)
        self.tris = kept
        self._rebuild()

    def snapshot(self):
        return (self.lon[:], self.lat[:], self.alt[:], list(self.tris))

    def restore(self, snap) -> None:
        lon, lat, alt, tris = snap
        self.lon = list(lon)
        self.lat = list(lat)
        self.alt = list(alt)
        self.tris = list(tris)
        self._rebuild()
        self._reindex_cells()

    def add_fan(
        self,
        center: int,
        loop: Sequence[int],
        ring: Optional[Sequence[XY]] = None,
    ) -> None:
        seq = [v for i, v in enumerate(loop) if i == 0 or v != loop[i - 1]]
        if len(seq) >= 2 and seq[0] == seq[-1]:
            seq.pop()
        if len(seq) < 3:
            return
        for i, vid in enumerate(seq):
            nxt = seq[(i + 1) % len(seq)]
            if vid == nxt or vid == center or nxt == center:
                continue
            tri = self._orient_tri((center, vid, nxt))
            if abs(_orient(self.xy(tri[0]), self.xy(tri[1]), self.xy(tri[2]))) <= 1e-22:
                continue
            if ring is not None and _face_escapes(self, tri, ring):
                self._fill_clipped(tri, ring)
                continue
            self.tris.append(tri)
        self._rebuild()

    def _fill_clipped(self, tri: Tri, ring: Sequence[XY]) -> None:
        """Fill the part of ``tri`` that stays inside ``ring`` and is still open."""
        poly = _clip_to_tri(ring, self.xy(tri[0]), self.xy(tri[1]), self.xy(tri[2]))
        if len(poly) < 3:
            return
        ids: List[int] = []
        for p in poly:
            area = _orient(self.xy(tri[0]), self.xy(tri[1]), self.xy(tri[2]))
            if abs(area) <= 1e-24:
                return
            w0 = _orient(self.xy(tri[1]), self.xy(tri[2]), p) / area
            w1 = _orient(self.xy(tri[2]), self.xy(tri[0]), p) / area
            w2 = _orient(self.xy(tri[0]), self.xy(tri[1]), p) / area
            z = (
                w0 * self.alt[tri[0]]
                + w1 * self.alt[tri[1]]
                + w2 * self.alt[tri[2]]
            )
            ids.append(self._add(p, z))
        if len(ids) < 3:
            return
        anchor = ids[0]
        for i in range(1, len(ids) - 1):
            piece = self._orient_tri((anchor, ids[i], ids[i + 1]))
            if len(set(piece)) < 3:
                continue
            if abs(_orient(self.xy(piece[0]), self.xy(piece[1]), self.xy(piece[2]))) <= 1e-22:
                continue
            if _face_escapes(self, piece, ring) and not _graze_only(self, piece, ring):
                continue
            cx = sum(self.lon[v] for v in piece) / 3.0
            cy = sum(self.lat[v] for v in piece) / 3.0
            if self.find_tri((cx, cy)) is not None:
                continue
            self.tris.append(piece)


def _xy_key(p: XY) -> Tuple[float, float]:
    return (round(p[0], 10), round(p[1], 10))


def _on_ring(p: XY, ring: Sequence[XY]) -> bool:
    n = len(ring)
    for i in range(n):
        if _on_seg(p, ring[i], ring[(i + 1) % n]):
            return True
    return False


def _outside_ring(p: XY, ring: Sequence[XY]) -> bool:
    return not _strict_inside(p, ring) and not _on_ring(p, ring)


def _right_of(p: XY, a: XY, b: XY, min_m: float = 1e-4) -> bool:
    """True when ``p`` is strictly to the right of directed edge ``a`` -> ``b``.

    Outline rings are counter-clockwise, so the bump interior is on the left
    and the outside of a side is on the right. ``min_m`` ignores a point that
    only clears the line by float dust.
    """
    dx, dy = b[0] - a[0], b[1] - a[1]
    span = math.hypot(dx, dy)
    if span <= 1e-15:
        return False
    left = _orient(a, b, p) / span
    mx, my = _metres_scale((a[1] + b[1]) * 0.5)
    scale = math.hypot((-dy / span) * mx, (dx / span) * my)
    return left * scale < -min_m


def _outline_snap_edges(p: XY, ring: Sequence[XY]) -> List[Tuple[XY, XY]]:
    """Sides that ``p`` has to stay outside.

    A corner returns the two sides that meet there. A crossing returns the
    one side it lies on. The ring is made counter-clockwise, so the outside
    of each side is to the right.
    """
    ring = _dedupe_ring(_ccw_ring(list(ring)))
    n = len(ring)
    if n < 3:
        return []
    mx, my = _metres_scale(p[1])
    best_i = 0
    best_d: Optional[float] = None
    for i, v in enumerate(ring):
        dist = math.hypot((p[0] - v[0]) * mx, (p[1] - v[1]) * my)
        if best_d is None or dist < best_d:
            best_i, best_d = i, dist
    # A new corner still sits on the ring vertex. A few millimetres of
    # slack covers the intersection math without swallowing a real crossing.
    if best_d is not None and best_d <= 0.005:
        prev = ring[(best_i - 1) % n]
        here = ring[best_i]
        nxt = ring[(best_i + 1) % n]
        return [(prev, here), (here, nxt)]
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        if _on_seg(p, a, b, eps=1e-8):
            return [(a, b)]
    best_e: Optional[Tuple[XY, XY]] = None
    best_ed: Optional[float] = None
    for i in range(n):
        a, b = ring[i], ring[(i + 1) % n]
        dist = _dist_seg_m(p, a, b, mx, my)
        if best_ed is None or dist < best_ed:
            best_e, best_ed = (a, b), dist
    if best_e is not None and best_ed is not None and best_ed <= 0.05:
        return [best_e]
    return []


def _outward_bisector(
    edges: Sequence[Tuple[XY, XY]], lat: float
) -> Optional[Tuple[float, float]]:
    """Direction, in degrees per metre, through the outside of ``edges``."""
    mx, my = _metres_scale(lat)
    normals: List[XY] = []
    for a, b in edges:
        dx = (b[0] - a[0]) * mx
        dy = (b[1] - a[1]) * my
        span = math.hypot(dx, dy)
        if span <= 1e-9:
            continue
        normals.append((dy / span, -dx / span))
    if not normals:
        return None
    sx = sum(n[0] for n in normals)
    sy = sum(n[1] for n in normals)
    span = math.hypot(sx, sy)
    if span <= 1e-9:
        rx, ry = normals[0]
    else:
        rx, ry = sx / span, sy / span
    return (rx / mx, ry / my)


def _closest_outside_grid(
    p: XY,
    ring: Sequence[XY],
    west: float,
    south: float,
    east: float,
    north: float,
    taken: set,
    edges: Optional[Sequence[Tuple[XY, XY]]] = None,
) -> Optional[XY]:
    """Nearest quantized-mesh grid point that lies outside ``ring``.

    The terrain file can only store these grid points. The nearest one may
    fall inside the bump, or just outside it but on the inner side of a
    side, so the edge to the next vertex cuts the bump. When ``edges`` is
    set, the point also has to lie outside each of those lines. A corner
    passes both sides that meet there. A crossing passes the one side it
    lies on.
    """
    du = max(east - west, 1e-15)
    dv = max(north - south, 1e-15)
    q = int(_QM_Q)
    u0 = int(round((p[0] - west) / du * _QM_Q))
    v0 = int(round((p[1] - south) / dv * _QM_Q))
    u0 = max(0, min(q, u0))
    v0 = max(0, min(q, v0))
    mx, my = _metres_scale(p[1])
    cell_m = min(du / _QM_Q * mx, dv / _QM_Q * my)
    best: Optional[XY] = None
    best_uv: Optional[Tuple[int, int]] = None
    best_d: Optional[float] = None

    def consider(u: int, v: int) -> None:
        nonlocal best, best_uv, best_d
        if u < 0 or v < 0 or u > q or v > q:
            return
        if (u, v) in taken:
            return
        g = (west + du * (u / _QM_Q), south + dv * (v / _QM_Q))
        if edges:
            for a, b in edges:
                if not _right_of(g, a, b):
                    return
        if not _outside_ring(g, ring):
            return
        dist = math.hypot((g[0] - p[0]) * mx, (g[1] - p[1]) * my)
        if best_d is None or dist < best_d:
            best, best_uv, best_d = g, (u, v), dist

    # A wide corner finds a grid point within a few cells. A thin wedge can
    # miss those cells; the bisector walk below picks that up.
    hard_cap = 32 if edges else 6
    for radius in range(0, hard_cap + 1):
        for iu in range(-radius, radius + 1):
            for iv in range(-radius, radius + 1):
                if max(abs(iu), abs(iv)) != radius:
                    continue
                consider(u0 + iu, v0 + iv)
        if best_d is not None and best_d <= (radius + 1) * cell_m:
            break
    if best is None and edges and cell_m > 0.0:
        direction = _outward_bisector(edges, p[1])
        if direction is not None:
            for k in range(1, 2001):
                dist_m = k * 0.5 * cell_m
                lon = p[0] + direction[0] * dist_m
                lat = p[1] + direction[1] * dist_m
                uu = int(round((lon - west) / du * _QM_Q))
                vv = int(round((lat - south) / dv * _QM_Q))
                for iu in range(-1, 2):
                    for iv in range(-1, 2):
                        consider(uu + iu, vv + iv)
                if best_d is not None and dist_m > best_d + 2.0 * cell_m:
                    break
    if best is None or best_uv is None:
        if edges:
            return _closest_outside_grid(p, ring, west, south, east, north, taken)
        return None
    taken.add(best_uv)
    return best


# Metres. The road outline lies on the polygon edge. Interior ground is lowered
# to the lowest boundary vertex, which can be far below that edge. Quantized
# mesh rounds vertex positions by a few centimetres when the tile is saved, and
# a point on the edge can land just inside. This band stays at the original
# height so that rounding cannot pick up the lowered interior.
_SHELF_M = 0.35


def _metres_scale(lat: float) -> Tuple[float, float]:
    rad = math.radians(lat)
    return (111320.0 * math.cos(rad), 111320.0)


def _dist_seg_m(p: XY, a: XY, b: XY, mx: float, my: float) -> float:
    px, py = (p[0] - a[0]) * mx, (p[1] - a[1]) * my
    bx, by = (b[0] - a[0]) * mx, (b[1] - a[1]) * my
    span = bx * bx + by * by
    if span <= 1e-12:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px * bx + py * by) / span))
    return math.hypot(px - t * bx, py - t * by)


def _dist_line_m(p: XY, a: XY, b: XY, mx: float, my: float) -> float:
    px, py = (p[0] - a[0]) * mx, (p[1] - a[1]) * my
    bx, by = (b[0] - a[0]) * mx, (b[1] - a[1]) * my
    span = math.hypot(bx, by)
    if span <= 1e-9:
        return math.hypot(px, py)
    return abs(px * by - py * bx) / span


def _dist_ring_m(p: XY, ring: Sequence[XY], mx: float, my: float) -> float:
    best = None
    n = len(ring)
    for i in range(n):
        d = _dist_seg_m(p, ring[i], ring[(i + 1) % n], mx, my)
        if best is None or d < best:
            best = d
    return 0.0 if best is None else best


def _edge_on_ring(a: XY, b: XY, ring: Sequence[XY]) -> bool:
    n = len(ring)
    for i in range(n):
        r0 = ring[i]
        r1 = ring[(i + 1) % n]
        if _on_seg(a, r0, r1) and _on_seg(b, r0, r1):
            return True
    return False


def _insert_shelf(mesh: _Tin, ring: Sequence[XY], mx: float, my: float) -> None:
    """Split triangles off the polygon edge so the edge is not tied to a deep vertex.

    The new vertices sit on the original surface. Lowering then stays on the
    inner side of this band.
    """
    jobs: List[Tuple[XY, XY, float]] = []
    for tri in list(mesh.tris):
        pts = [mesh.xy(v) for v in tri]
        for j in range(3):
            a = pts[j]
            b = pts[(j + 1) % 3]
            c = pts[(j + 2) % 3]
            if not _edge_on_ring(a, b, ring):
                continue
            line_d = _dist_line_m(c, a, b, mx, my)
            seg_d = _dist_seg_m(c, a, b, mx, my)
            if line_d <= _SHELF_M + 0.02 or seg_d <= _SHELF_M + 0.02:
                continue
            t = _SHELF_M / line_d
            if t <= 0.0 or t >= 1.0:
                continue
            jobs.append((a, c, t))
            jobs.append((b, c, t))
    for a, c, t in jobs:
        mesh.insert((a[0] + t * (c[0] - a[0]), a[1] + t * (c[1] - a[1])))


def _embed_polygon(mesh: _Tin, ring: Sequence[XY]) -> Optional[List[int]]:
    """Insert ``ring`` as constrained edges.

    An edge that leaves the triangulated surface is skipped. Crossing edges
    are split in the plane of each triangle they meet, so heights outside the
    polygon do not move. Returns the mesh vertices of the ring.
    """
    ring = _dedupe_ring(_ccw_ring(ring))
    if len(ring) < 3:
        return None
    ids: List[Optional[int]] = []
    for p in ring:
        ids.append(mesh.insert(p))
    if not any(vid is not None for vid in ids):
        return None
    n = len(ids)
    for i in range(n):
        ia = ids[i]
        ib = ids[(i + 1) % n]
        if ia is None or ib is None:
            continue
        chain = mesh.force_edge(ia, ib)
        if len(chain) < 2:
            # The edge misses the triangulation, or it is a sliver the walk
            # cannot cross. Leave it unenforced. Splits already made stay on
            # the original planes, and interior lowering will not move a
            # vertex that still touches the outside.
            continue
    present = [vid for vid in ids if vid is not None]
    return present or None


def _face_escapes(mesh: _Tin, tri: Tri, ring: Sequence[XY]) -> bool:
    """True when ``tri`` covers any ground outside ``ring``."""
    pts = [mesh.xy(v) for v in tri]
    if any(_outside_ring(p, ring) for p in pts):
        return True
    for i in range(3):
        mid = (
            (pts[i][0] + pts[(i + 1) % 3][0]) * 0.5,
            (pts[i][1] + pts[(i + 1) % 3][1]) * 0.5,
        )
        if _outside_ring(mid, ring):
            return True
    n = len(ring)
    # Ignore orientations smaller than this. A shared boundary edge is
    # collinear and its raw sign flips around 1e-19, which is not a crossing.
    tol = 1e-16
    for i in range(n):
        a = ring[i]
        b = ring[(i + 1) % n]
        for j in range(3):
            c = pts[j]
            d = pts[(j + 1) % 3]
            o1 = _orient(a, b, c)
            o2 = _orient(a, b, d)
            o3 = _orient(c, d, a)
            o4 = _orient(c, d, b)
            if abs(o1) <= tol:
                o1 = 0.0
            if abs(o2) <= tol:
                o2 = 0.0
            if abs(o3) <= tol:
                o3 = 0.0
            if abs(o4) <= tol:
                o4 = 0.0
            if o1 * o2 < 0.0 and o3 * o4 < 0.0:
                return True
    # A polygon vertex sitting inside the triangle means the boundary cuts
    # through it, so the face covers ground outside the polygon as well.
    a, b, c = pts
    area = _orient(a, b, c)
    if abs(area) > 1e-24:
        for q in ring:
            o1 = _orient(a, b, q)
            o2 = _orient(b, c, q)
            o3 = _orient(c, a, q)
            if area > 0.0:
                inside = o1 > 1e-18 and o2 > 1e-18 and o3 > 1e-18
            else:
                inside = o1 < -1e-18 and o2 < -1e-18 and o3 < -1e-18
            if inside:
                return True
    return False


def _cut_at(p: XY, q: XY, a: XY, b: XY) -> XY:
    """Where segment ``p``–``q`` meets the line through ``a``–``b``."""
    op = _orient(a, b, p)
    oq = _orient(a, b, q)
    den = op - oq
    if abs(den) < 1e-24:
        return q
    t = max(0.0, min(1.0, op / den))
    return (p[0] + t * (q[0] - p[0]), p[1] + t * (q[1] - p[1]))


def _clip_to_tri(poly: Sequence[XY], a: XY, b: XY, c: XY) -> List[XY]:
    """The part of ``poly`` that lies inside triangle ``a b c``."""
    if _orient(a, b, c) < 0.0:
        b, c = c, b
    out: List[XY] = list(poly)
    for s, e in ((a, b), (b, c), (c, a)):
        if len(out) < 3:
            return []
        nxt: List[XY] = []
        m = len(out)
        for i in range(m):
            p = out[i]
            q = out[(i + 1) % m]
            pin = _orient(s, e, p) >= -1e-18
            qin = _orient(s, e, q) >= -1e-18
            if qin:
                if not pin:
                    nxt.append(_cut_at(p, q, s, e))
                nxt.append(q)
            elif pin:
                nxt.append(_cut_at(p, q, s, e))
        out = nxt
    return _dedupe_ring(out) if len(out) >= 3 else []


def _boundary_cuts(pts: Sequence[XY], ring: Sequence[XY]) -> bool:
    """True when ``ring`` cuts through the triangle or a ring vertex sits inside it."""
    n = len(ring)
    tol = 1e-16
    for i in range(n):
        a = ring[i]
        b = ring[(i + 1) % n]
        for j in range(3):
            c = pts[j]
            d = pts[(j + 1) % 3]
            o1 = _orient(a, b, c)
            o2 = _orient(a, b, d)
            o3 = _orient(c, d, a)
            o4 = _orient(c, d, b)
            if abs(o1) <= tol:
                o1 = 0.0
            if abs(o2) <= tol:
                o2 = 0.0
            if abs(o3) <= tol:
                o3 = 0.0
            if abs(o4) <= tol:
                o4 = 0.0
            if o1 * o2 < 0.0 and o3 * o4 < 0.0:
                return True
    a, b, c = pts[0], pts[1], pts[2]
    area = _orient(a, b, c)
    if abs(area) > 1e-24:
        for q in ring:
            o1 = _orient(a, b, q)
            o2 = _orient(b, c, q)
            o3 = _orient(c, a, q)
            if area > 0.0:
                inside = o1 > 1e-18 and o2 > 1e-18 and o3 > 1e-18
            else:
                inside = o1 < -1e-18 and o2 < -1e-18 and o3 < -1e-18
            if inside:
                return True
    return False


def _face_meets(mesh: _Tin, tri: Tri, ring: Sequence[XY]) -> bool:
    """True when ``tri`` covers any ground of ``ring``, including its boundary."""
    pts = [mesh.xy(v) for v in tri]
    if any(not _outside_ring(p, ring) for p in pts):
        return True
    return _boundary_cuts(pts, ring)


def _mask_rings_near_tile(
    rings: Sequence[Sequence[XY]],
    west: float,
    south: float,
    east: float,
    north: float,
    lat: float,
) -> List[List[XY]]:
    """Mask rings clipped a couple of metres outside the tile.

    The pad keeps the tile border from looking like the road edge, so a road
    that continues into the next tile can still be lowered up to this tile's
    edge.
    """
    mx, my = _metres_scale(lat)
    pad_x = 2.0 / max(mx, 1.0)
    pad_y = 2.0 / max(my, 1.0)
    out: List[List[XY]] = []
    for ring in rings:
        if len(ring) < 3:
            continue
        xs = [p[0] for p in ring]
        ys = [p[1] for p in ring]
        if (
            max(xs) < west
            or min(xs) > east
            or max(ys) < south
            or min(ys) > north
        ):
            continue
        clipped = _dedupe_ring(
            _clip_rect(
                ring,
                west - pad_x,
                south - pad_y,
                east + pad_x,
                north + pad_y,
            )
        )
        if len(clipped) >= 3:
            out.append(clipped)
    return out


def _bary_pts(
    p: XY, a: XY, b: XY, c: XY
) -> Optional[Tuple[float, float, float]]:
    area = _orient(a, b, c)
    if abs(area) <= 1e-24:
        return None
    w0 = _orient(b, c, p) / area
    w1 = _orient(c, a, p) / area
    w2 = _orient(a, b, p) / area
    if w0 < -1e-8 or w1 < -1e-8 or w2 < -1e-8:
        return None
    return w0, w1, w2


def _seg_hit(a: XY, b: XY, c: XY, d: XY) -> Optional[XY]:
    """Intersection of segments ``a-b`` and ``c-d``, including endpoints."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    if abs(den) < 1e-30:
        return None
    t = ((c[0] - a[0]) * sy - (c[1] - a[1]) * sx) / den
    u = ((c[0] - a[0]) * ry - (c[1] - a[1]) * rx) / den
    if t < -1e-8 or t > 1.0 + 1e-8 or u < -1e-8 or u > 1.0 + 1e-8:
        return None
    t = min(1.0, max(0.0, t))
    return (a[0] + t * rx, a[1] + t * ry)


def _segment_across_triangle(
    a: XY, b: XY, pts: Sequence[XY]
) -> Optional[Tuple[XY, XY]]:
    """The part of segment ``a-b`` that lies inside the triangle, if it cuts it."""
    hits: List[XY] = []

    def _add(p: XY) -> None:
        for q in hits:
            if _hypot(p, q) <= 1e-9:
                return
        hits.append(p)

    if _bary_pts(a, pts[0], pts[1], pts[2]) is not None:
        _add(a)
    if _bary_pts(b, pts[0], pts[1], pts[2]) is not None:
        _add(b)
    for i in range(3):
        hit = _seg_hit(a, b, pts[i], pts[(i + 1) % 3])
        if hit is not None:
            _add(hit)
    if len(hits) < 2:
        return None
    # A mask edge that only runs along a triangle edge is already in the mesh.
    for i in range(3):
        e0, e1 = pts[i], pts[(i + 1) % 3]
        if all(_on_seg(p, e0, e1, eps=1e-9) for p in hits):
            return None
    best = None
    best_d = -1.0
    for i in range(len(hits)):
        for j in range(i + 1, len(hits)):
            dist = _hypot(hits[i], hits[j])
            if dist > best_d:
                best = (hits[i], hits[j])
                best_d = dist
    if best is None or best_d <= 1e-9:
        return None
    return best


def _cut_bumps_at_mask(
    mesh: _Tin,
    bumps: Sequence[Sequence[XY]],
    mask_rings: Sequence[Sequence[XY]],
) -> None:
    """Cut the road edge through any triangle that covers a bump and leaves the road.

    The piece under the bump can then drop without moving the piece outside
    the road. Mask edges that do not cross a bump are left alone.
    """
    if not bumps or not mask_rings:
        return
    jobs: List[Tuple[XY, XY]] = []
    seen = set()
    for tri in list(mesh.tris):
        if not any(_face_meets(mesh, tri, bump) for bump in bumps):
            continue
        if any(not _face_escapes(mesh, tri, ring) for ring in mask_rings):
            continue
        pts = [mesh.xy(v) for v in tri]
        for ring in mask_rings:
            n = len(ring)
            for i in range(n):
                piece = _segment_across_triangle(ring[i], ring[(i + 1) % n], pts)
                if piece is None:
                    continue
                key = tuple(sorted((
                    (round(piece[0][0], 8), round(piece[0][1], 8)),
                    (round(piece[1][0], 8), round(piece[1][1], 8)),
                )))
                if key in seen:
                    continue
                seen.add(key)
                jobs.append(piece)
    for p, q in jobs:
        ia = mesh.insert(p)
        ib = mesh.insert(q)
        if ia is None or ib is None or ia == ib:
            continue
        mesh.force_edge(ia, ib)


def _lower_on_road(
    mesh: _Tin,
    bump: Sequence[XY],
    mask_rings: Sequence[Sequence[XY]],
    floor: float,
) -> Tuple[int, int]:
    """Lower every vertex of ``bump`` that stays on the road.

    Boundary vertices are included, so the whole polygon drops, not only a
    vertex that happened to sit in the middle. A vertex shared with a
    triangle outside the road is left alone. Returns ``(buried, new_vertices)``.
    """
    if not mask_rings:
        return 0, 0

    def _on_road(p: XY) -> bool:
        return any(not _outside_ring(p, ring) for ring in mask_rings)

    def _leaves_road(tri: Tri) -> bool:
        return all(_face_escapes(mesh, tri, ring) for ring in mask_rings)

    buried = 0
    for vid in range(len(mesh.lon)):
        p = mesh.xy(vid)
        if _outside_ring(p, bump) or not _on_road(p):
            continue
        if mesh.alt[vid] <= floor:
            continue
        incident = mesh._incident(vid)
        if not incident or any(_leaves_road(mesh.tris[ti]) for ti in incident):
            continue
        mesh.alt[vid] = floor
        buried += 1
    return buried, 0


def _lower_inside(mesh: _Tin, ring: Sequence[XY], floor: float) -> Tuple[int, int]:
    """Drop ground inside ``ring`` down to ``floor``. Never raise it.

    Returns ``(buried, new_vertices)``. Only vertices strictly inside the
    polygon change height, and only when they were above ``floor`` and more
    than ``_SHELF_M`` metres from the boundary. A triangle entirely inside
    that inner area, with no vertex below ``floor`` and with its center still
    above ``floor``, gets one new vertex at ``floor``. A vertex already below
    ``floor`` keeps its triangle, so a pit is not lifted.
    """
    mx, my = _metres_scale(sum(p[1] for p in ring) / len(ring))
    _insert_shelf(mesh, ring, mx, my)

    def _on_shelf(p: XY) -> bool:
        return _dist_ring_m(p, ring, mx, my) <= _SHELF_M + 0.02

    def _in_band(p: XY) -> bool:
        # Closer than the shelf line: the boundary band that must stay put.
        return _dist_ring_m(p, ring, mx, my) < _SHELF_M - 0.05

    def _touches_band(tri: Tri) -> bool:
        return any(_in_band(mesh.xy(v)) for v in tri)

    buried = 0
    for vid in range(len(mesh.lon)):
        p = mesh.xy(vid)
        if not _strict_inside(p, ring) or _on_shelf(p):
            continue
        if mesh.alt[vid] <= floor:
            continue
        safe = True
        for ti in mesh._incident(vid):
            tri = mesh.tris[ti]
            if _face_escapes(mesh, tri, ring) or _touches_band(tri):
                safe = False
                break
        if not safe:
            continue
        mesh.alt[vid] = floor
        buried += 1
    dents: List[XY] = []
    for tri in mesh.tris:
        if any(mesh.alt[v] < floor - 1e-4 for v in tri):
            continue
        if _touches_band(tri) or _face_escapes(mesh, tri, ring):
            continue
        cx = (mesh.lon[tri[0]] + mesh.lon[tri[1]] + mesh.lon[tri[2]]) / 3.0
        cy = (mesh.lat[tri[0]] + mesh.lat[tri[1]] + mesh.lat[tri[2]]) / 3.0
        if _strict_inside((cx, cy), ring) and not _on_shelf((cx, cy)):
            dents.append((cx, cy))
    inner = 0
    for cx, cy in dents:
        found = mesh.find_tri((cx, cy))
        if found is None:
            continue
        ti, weights = found
        tri = mesh.tris[ti]
        if any(mesh.alt[v] < floor - 1e-4 for v in tri):
            continue
        if _touches_band(tri) or _face_escapes(mesh, tri, ring):
            continue
        if mesh._interp(tri, weights) <= floor + 1e-6:
            continue
        cxy = (
            (mesh.lon[tri[0]] + mesh.lon[tri[1]] + mesh.lon[tri[2]]) / 3.0,
            (mesh.lat[tri[0]] + mesh.lat[tri[1]] + mesh.lat[tri[2]]) / 3.0,
        )
        if not _strict_inside(cxy, ring) or _on_shelf(cxy):
            continue
        mesh._split_face(ti, cxy, floor)
        buried += 1
        inner += 1
    return buried, inner


def _piece_loop(
    piece: Sequence[XY],
    ids_of: Dict[Tuple[float, float], int],
    mesh: _Tin,
    outer: Sequence[XY],
) -> List[int]:
    """Boundary chain of one convex piece.

    Vertices strictly inside the original polygon are left out. They are not
    support points: a later lower of the inner vertices has to move the whole
    interior, including any old mesh vertex the split happened to cross.
    """
    loop: List[int] = []
    n = len(piece)
    for i in range(n):
        a = piece[i]
        b = piece[(i + 1) % n]
        ia = ids_of.get(_xy_key(a))
        ib = ids_of.get(_xy_key(b))
        if ia is None or ib is None:
            return []
        chain = mesh.force_edge(ia, ib)
        if len(chain) < 2:
            return []
        for vid in chain[:-1]:
            if _strict_inside(mesh.xy(vid), outer):
                continue
            if loop and loop[-1] == vid:
                continue
            loop.append(vid)
    return loop


def _choose_inner(mesh: _Tin, piece: Sequence[XY]) -> Optional[int]:
    target = _kernel_point(piece)
    if target is None:
        return None
    best = None
    best_d = None
    for vid in mesh.interior_ids(piece):
        if not _in_kernel(mesh.xy(vid), piece):
            continue
        dist = _hypot(mesh.xy(vid), target)
        if best_d is None or dist < best_d:
            best = vid
            best_d = dist
    if best is not None:
        return best
    return mesh.insert(target)


def _split_outline_crossings(mesh: _Tin, ring: Sequence[XY]) -> None:
    """Split each original mesh edge where the true outline crosses it.

    The new vertex is the intersection, so it stays on the outline and on
    that edge. Edges added later to retie a triangle are left alone.
    """
    if len(ring) < 2 or not mesh._original_edges:
        return
    n = len(ring)
    hits: Dict[Tuple[int, int], List[Tuple[float, XY]]] = {}
    for key, owners in list(mesh.edges.items()):
        if not owners:
            continue
        e0, e1 = key
        if key not in mesh._original_edges:
            continue
        a, b = mesh.xy(e0), mesh.xy(e1)
        dx, dy = b[0] - a[0], b[1] - a[1]
        span = dx * dx + dy * dy
        if span <= 1e-28:
            continue
        for i in range(n):
            c, d = ring[i], ring[(i + 1) % n]
            pt = _seg_hit(a, b, c, d)
            if pt is None:
                continue
            if (
                _hypot(pt, a) <= 1e-12
                or _hypot(pt, b) <= 1e-12
                or _hypot(pt, c) <= 1e-12
                or _hypot(pt, d) <= 1e-12
            ):
                continue
            u = ((pt[0] - a[0]) * dx + (pt[1] - a[1]) * dy) / span
            hits.setdefault(key, []).append((min(1.0, max(0.0, u)), pt))
    for (e0, e1), pts in hits.items():
        pts.sort(key=lambda item: item[0])
        # Split from the far end so the piece that still starts at e0
        # keeps the crossings that have not been cut yet.
        cur0, cur1 = e0, e1
        for _u, pt in reversed(pts):
            if not mesh.has_edge(cur0, cur1):
                break
            ax, ay = mesh.xy(cur0)
            bx, by = mesh.xy(cur1)
            dx, dy = bx - ax, by - ay
            span = dx * dx + dy * dy
            if span <= 1e-28:
                break
            u = ((pt[0] - ax) * dx + (pt[1] - ay) * dy) / span
            u = min(1.0, max(0.0, u))
            if u <= 1e-8 or u >= 1.0 - 1e-8:
                continue
            vid = mesh._split_edge(cur0, cur1, pt, mesh._edge_height(cur0, cur1, u))
            # This hit is already an end of the edge. Skip it and keep going.
            # The same edge can still cross the outline at another point.
            if vid in (cur0, cur1):
                continue
            cur1 = vid


def _mark_fail(mesh: _Tin, reason: str) -> None:
    """Remember the first reason the current bump could not be cut in."""
    if not mesh._fail_reason:
        mesh._fail_reason = reason


def _strip_crossed(mesh: _Tin, a: int, b: int) -> Optional[List[int]]:
    """Triangles the open segment ``a-b`` crosses, in order.

    An original edge in that strip is a crossing that was not cut yet.
    That strip is refused so the original edge is not deleted.
    """
    start = None
    for ti in mesh._incident(a):
        if ti >= len(mesh.tris) or a not in mesh.tris[ti]:
            continue
        if b in mesh.tris[ti]:
            return []
        if not mesh._enters(ti, a, b):
            continue
        exit_e = mesh._exit_edge(ti, a, b, None)
        if exit_e is None:
            continue
        start = ti
        break
    if start is None:
        _mark_fail(
            mesh,
            "no triangle around the outline vertex contains the next vertex",
        )
        return None
    tris = [start]
    prev = None
    cur = start
    for _ in range(len(mesh.tris) + 2):
        exit_e = mesh._exit_edge(cur, a, b, prev)
        if exit_e is None:
            _mark_fail(mesh, "outline side left the triangle chain")
            return None
        e0, e1 = exit_e
        if b in (e0, e1) or _on_seg(mesh.xy(b), mesh.xy(e0), mesh.xy(e1), eps=1e-9):
            return tris
        key = _edge_key(e0, e1)
        if key in mesh._original_edges:
            _mark_fail(mesh, "original edge across the outline side")
            return None
        nxt = [ti for ti in mesh.edges.get(key, []) if ti != cur]
        if len(nxt) == 0:
            _mark_fail(mesh, "outline side left the mesh before the next vertex")
            return None
        if len(nxt) != 1:
            _mark_fail(mesh, "outline side left the triangle chain")
            return None
        prev = (e0, e1)
        cur = nxt[0]
        tris.append(cur)
    _mark_fail(mesh, "outline side left the triangle chain")
    return None


def _boundary_chain(mesh: _Tin, a: int, b: int, edges: Sequence[Tuple[int, int]], sign: float) -> Optional[List[int]]:
    """Vertices strictly between ``a`` and ``b`` along boundary edges of one side."""
    adj: Dict[int, List[int]] = {}
    for e0, e1 in edges:
        mid = (
            (mesh.lon[e0] + mesh.lon[e1]) * 0.5,
            (mesh.lat[e0] + mesh.lat[e1]) * 0.5,
        )
        if _orient(mesh.xy(a), mesh.xy(b), mid) * sign <= 1e-18:
            continue
        adj.setdefault(e0, []).append(e1)
        adj.setdefault(e1, []).append(e0)
    if a not in adj:
        return []
    prev = None
    cur = a
    chain: List[int] = []
    for _ in range(len(edges) + 2):
        if cur == b:
            return chain
        nxts = [n for n in adj.get(cur, []) if n != prev]
        if not nxts:
            return None
        nxt = min(nxts, key=lambda n: _hypot(mesh.xy(n), mesh.xy(b)))
        prev, cur = cur, nxt
        if cur != b:
            chain.append(cur)
    return None


def _retriangulate_strip(mesh: _Tin, a: int, b: int, tris: Sequence[int]) -> bool:
    """Replace the crossed triangles with a triangulation that uses edge ``a-b``.

    No vertex is added. Both sides are filled from the vertices already there.
    """
    drop = set(tris)
    count: Dict[Tuple[int, int], int] = {}
    for ti in tris:
        for e0, e1 in _tri_edges(mesh.tris[ti]):
            key = _edge_key(e0, e1)
            count[key] = count.get(key, 0) + 1
    boundary = [key for key, n in count.items() if n == 1]
    left = _boundary_chain(mesh, a, b, boundary, 1.0)
    right = _boundary_chain(mesh, a, b, boundary, -1.0)
    if left is None or right is None:
        _mark_fail(mesh, "strip banks did not connect")
        return False
    fresh: List[Tri] = []
    for seq in ([a, *left, b], [a, *right, b]):
        poly = [v for i, v in enumerate(seq) if i == 0 or v != seq[i - 1]]
        if len(poly) >= 2 and poly[0] == poly[-1]:
            poly.pop()
        if len(poly) < 3:
            continue
        fresh.extend(mesh._polygon_tris(poly))
    mesh.tris = [tri for i, tri in enumerate(mesh.tris) if i not in drop]
    mesh.tris.extend(fresh)
    mesh._rebuild()
    if not mesh.has_edge(a, b):
        _mark_fail(mesh, "strip banks did not connect")
        return False
    return True


def _open_constraint(mesh: _Tin, a: int, b: int) -> bool:
    """Make ``a-b`` an edge. Does not create a vertex."""
    if a == b or mesh.has_edge(a, b):
        return True
    for _ in range(4):
        flat = mesh._vertex_on_opposite(a)
        if flat is None:
            break
        if not mesh._stitch_on_edge(a, flat[0], flat[1]):
            break
    if mesh.has_edge(a, b):
        return True
    for ti in list(mesh._incident(a)):
        if ti >= len(mesh.tris) or a not in mesh.tris[ti]:
            continue
        others = [v for v in mesh.tris[ti] if v != a]
        if len(others) != 2:
            continue
        e0, e1 = others
        if b in (e0, e1):
            return mesh.has_edge(a, b)
        if _on_seg(mesh.xy(b), mesh.xy(e0), mesh.xy(e1), eps=1e-9):
            mesh._split_edge_at_vertex(e0, e1, b)
            if not mesh.has_edge(a, b):
                _mark_fail(mesh, "outline side left the triangle chain")
            return mesh.has_edge(a, b)
    strip = _strip_crossed(mesh, a, b)
    if strip is None:
        _mark_fail(mesh, "outline side left the triangle chain")
        return False
    if not strip:
        if not mesh.has_edge(a, b):
            _mark_fail(mesh, "outline side left the triangle chain")
        return mesh.has_edge(a, b)
    return _retriangulate_strip(mesh, a, b, strip)


def _link_outline(mesh: _Tin, a: int, b: int) -> List[int]:
    """Connect ``a`` to ``b`` along the outline. Crossings already on it are kept."""
    mids = mesh._verts_on_open_segment(a, b)
    nodes = [a, *mids, b]
    chain = [a]
    for u, v in zip(nodes, nodes[1:]):
        if not _open_constraint(mesh, u, v):
            return []
        chain.append(v)
    return chain


def _outline_loop(mesh: _Tin, ring: Sequence[XY]) -> Optional[List[int]]:
    """Cut ``ring`` into the mesh. Return the outline vertices, crossings included.

    New vertices are the polygon corners and the intersections of original
    mesh edges with the outline. Connecting those vertices does not add any.
    """
    ring = _dedupe_ring(_ccw_ring(ring))
    if len(ring) < 3:
        _mark_fail(mesh, "outline has fewer than 3 vertices")
        return None
    _split_outline_crossings(mesh, ring)
    ids: List[Optional[int]] = [mesh.insert(p, exact=True) for p in ring]
    if any(vid is None for vid in ids):
        _mark_fail(mesh, "corner lies outside the mesh")
        return None
    loop: List[int] = []
    n = len(ids)
    for i in range(n):
        a = ids[i]
        b = ids[(i + 1) % n]
        # Both corners are the same vertex. The edge has no length there.
        if a == b:
            if not loop or loop[-1] != a:
                loop.append(a)
            continue
        chain = _link_outline(mesh, a, b)
        if len(chain) < 2:
            _mark_fail(mesh, "outline side left the triangle chain")
            return None
        for vid in chain[:-1]:
            if not loop or loop[-1] != vid:
                loop.append(vid)
    if len(loop) >= 2 and loop[0] == loop[-1]:
        loop.pop()
    if len(loop) < 3:
        _mark_fail(mesh, "outline has fewer than 3 vertices")
        return None
    return loop


def _vid_on_loop(mesh: _Tin, p: XY, loop: Sequence[int]) -> Optional[int]:
    key = _xy_key(p)
    for vid in loop:
        if _xy_key(mesh.xy(vid)) == key:
            return vid
    best = None
    best_d = 1e-7
    for vid in loop:
        dist = _hypot(mesh.xy(vid), p)
        if dist < best_d:
            best = vid
            best_d = dist
    return best


def _graze_only(mesh: _Tin, tri: Tri, ring: Sequence[XY], limit_m: float = 0.002) -> bool:
    """True when ``tri`` leaves ``ring`` by less than ``limit_m`` metres.

    A clip vertex can sit a fraction of a millimetre outside the outline.
    That is still the inside of the bump, and dropping the triangle opens a hole.
    """
    pts = [mesh.xy(v) for v in tri]
    samples = list(pts)
    for i in range(3):
        samples.append(
            (
                (pts[i][0] + pts[(i + 1) % 3][0]) * 0.5,
                (pts[i][1] + pts[(i + 1) % 3][1]) * 0.5,
            )
        )
    mx, my = _metres_scale(sum(p[1] for p in pts) / 3.0)
    for p in samples:
        if _outside_ring(p, ring) and _dist_ring_m(p, ring, mx, my) > limit_m:
            return False
    return True


def _ring_area(ring: Sequence[XY]) -> float:
    """Signed double-area. Positive when ``ring`` is counter-clockwise."""
    if len(ring) < 3:
        return 0.0
    origin = ring[0]
    total = 0.0
    for i in range(1, len(ring) - 1):
        total += _orient(origin, ring[i], ring[i + 1])
    return total


def _outside_polys(tri: Sequence[XY], ring: Sequence[XY]) -> List[List[XY]]:
    """Polygons covering the part of triangle ``tri`` that lies outside ``ring``.

    The outline cut deletes every triangle inside the embedded loop. A triangle
    can sit inside that loop and still cover a strip outside the real bump,
    where the mesh outline and the shapefile disagree by about a centimetre.
    Deleting the whole triangle opens a hole there, and the next bump cannot
    place a vertex in it.
    """
    a, b, c = tri
    if _orient(a, b, c) < 0.0:
        b, c = c, b
    inner = _clip_to_tri(ring, a, b, c)
    if len(inner) < 3:
        return [[a, b, c]]
    if abs(_orient(a, b, c)) - abs(_ring_area(inner)) <= 1e-18:
        return []

    pts: List[XY] = []

    def vid(p: XY) -> int:
        for i, q in enumerate(pts):
            if _hypot(p, q) <= 1e-9:
                return i
        pts.append((float(p[0]), float(p[1])))
        return len(pts) - 1

    for p in (a, b, c):
        vid(p)
    for p in inner:
        vid(p)

    boundary = ((a, b), (b, c), (c, a))
    undirected = set()

    def add_edge(p: XY, q: XY) -> None:
        if _hypot(p, q) <= 1e-12:
            return
        i, j = vid(p), vid(q)
        if i == j:
            return
        undirected.add((i, j) if i < j else (j, i))

    for u, v in boundary:
        on = [p for p in pts if _on_seg(p, u, v, eps=1e-9)]
        on.sort(key=lambda p: _hypot(p, u))
        for p, q in zip(on, on[1:]):
            add_edge(p, q)
    m = len(inner)
    for i in range(m):
        p, q = inner[i], inner[(i + 1) % m]
        if any(
            _on_seg(p, u, v, eps=1e-9) and _on_seg(q, u, v, eps=1e-9)
            for u, v in boundary
        ):
            continue
        add_edge(p, q)

    adj: Dict[int, List[int]] = {}
    for i, j in undirected:
        adj.setdefault(i, []).append(j)
        adj.setdefault(j, []).append(i)
    for i, nbrs in adj.items():
        nbrs.sort(key=lambda j: math.atan2(pts[j][1] - pts[i][1], pts[j][0] - pts[i][0]))

    used = set()
    kept: List[List[XY]] = []
    for i, j in undirected:
        for start in ((i, j), (j, i)):
            if start in used:
                continue
            face: List[XY] = []
            u, v = start
            guard = 0
            while (u, v) not in used and guard < 64:
                used.add((u, v))
                face.append(pts[u])
                nbrs = adj.get(v, [])
                try:
                    k = nbrs.index(u)
                except ValueError:
                    break
                # Previous neighbour in CCW order: the face stays on the left.
                w = nbrs[(k - 1) % len(nbrs)]
                u, v = v, w
                guard += 1
            face = _dedupe_ring(face)
            if len(face) < 3 or _ring_area(face) <= 1e-22:
                continue
            cx = sum(p[0] for p in face) / len(face)
            cy = sum(p[1] for p in face) / len(face)
            if _bary_raw((cx, cy), (a, b, c)) is None:
                continue
            if not _outside_ring((cx, cy), ring):
                continue
            kept.append(face)
    return kept


def _bary_raw(p: XY, tri: Sequence[XY]) -> Optional[Tuple[float, float, float]]:
    a, b, c = tri
    area = _orient(a, b, c)
    if abs(area) <= 1e-24:
        return None
    w0 = _orient(b, c, p) / area
    w1 = _orient(c, a, p) / area
    w2 = _orient(a, b, p) / area
    if w0 < -1e-6 or w1 < -1e-6 or w2 < -1e-6:
        return None
    return w0, w1, w2


def _sample_z(mesh: _Tin, tri: Tri, p: XY) -> float:
    a, b, c = (mesh.xy(v) for v in tri)
    area = _orient(a, b, c)
    if abs(area) <= 1e-24:
        return mesh.alt[tri[0]]
    weights = (
        _orient(b, c, p) / area,
        _orient(c, a, p) / area,
        _orient(a, b, p) / area,
    )
    return mesh._interp(tri, weights)


def _paint_poly(mesh: _Tin, poly: Sequence[XY], src: Tri) -> List[Tri]:
    """Triangulate ``poly`` on ``mesh`` using heights from triangle ``src``."""
    out: List[Tri] = []
    for piece in _ear_pieces(poly):
        ids = [mesh._add(p, _sample_z(mesh, src, p), snap=False) for p in piece]
        if len(set(ids)) < 3:
            continue
        oriented = mesh._orient_tri((ids[0], ids[1], ids[2]))
        if abs(_orient(mesh.xy(oriented[0]), mesh.xy(oriented[1]), mesh.xy(oriented[2]))) <= 1e-22:
            continue
        out.append(oriented)
    return out


def _sticks_out(pts: Sequence[XY], ring: Sequence[XY]) -> bool:
    """True when ``pts`` covers ground outside ``ring``, past float dust."""
    if any(_outside_ring(p, ring) for p in pts):
        return True
    for i in range(3):
        mid = (
            (pts[i][0] + pts[(i + 1) % 3][0]) * 0.5,
            (pts[i][1] + pts[(i + 1) % 3][1]) * 0.5,
        )
        if _outside_ring(mid, ring):
            return True
    a, b, c = pts
    inner = _clip_to_tri(ring, a, b, c)
    if len(inner) < 3:
        return True
    mx, my = _metres_scale((a[1] + b[1] + c[1]) / 3.0)
    outside = abs(_orient(a, b, c)) - abs(_ring_area(inner))
    # Square metres. A centimetre-wide strip along a mesh edge is larger
    # than this; rounding error in the clip is not.
    return outside * mx * my * 0.5 > 1e-4


def _retain_outside(mesh: _Tin, tri: Tri, ring: Sequence[XY]) -> List[Tri]:
    """The part of ``tri`` outside ``ring``. The original triangle is not kept."""
    pts = [mesh.xy(v) for v in tri]
    if not _sticks_out(pts, ring):
        return []
    painted: List[Tri] = []
    for poly in _outside_polys(pts, ring):
        painted.extend(_paint_poly(mesh, poly, tri))
    if painted:
        return painted
    # A failed cut must not open a hole. The whole triangle stays.
    return [tri]


def _drop_covered(mesh: _Tin, ring: Sequence[XY], limit: Optional[Sequence[XY]] = None) -> None:
    """Remove triangles that lie inside ``ring``. Outside triangles stay.

    ``limit`` is the bump outline before it was snapped onto the mesh. A
    triangle cleared with the snapped loop can still cover ground outside
    that outline. That strip is written back so the next bump still has
    a surface to cut into.
    """
    kept: List[Tri] = []
    for tri in mesh.tris:
        if _face_escapes(mesh, tri, ring):
            kept.append(tri)
            continue
        cx = (mesh.lon[tri[0]] + mesh.lon[tri[1]] + mesh.lon[tri[2]]) / 3.0
        cy = (mesh.lat[tri[0]] + mesh.lat[tri[1]] + mesh.lat[tri[2]]) / 3.0
        if _pip(cx, cy, ring) or any(_strict_inside(mesh.xy(v), ring) for v in tri):
            if limit is not None:
                kept.extend(_retain_outside(mesh, tri, limit))
            continue
        kept.append(tri)
    mesh.tris = kept
    mesh._rebuild()


def _straight_fan_height(
    origin: XY, ends: Sequence[Tuple[XY, float]]
) -> Optional[float]:
    """Lowest height that keeps every pair of fan edges a valley.

    ``origin`` is the inner vertex's horizontal position. Each end is a fan
    edge's outer vertex and its height in metres. For one pair, the straight
    height is the one that makes those two edges as short as possible: below
    it the pair sags, above it the pair arches. The inner vertex takes the
    lowest of those heights, so one pair is straight and the rest stay valleys.
    """
    n = len(ends)
    if n < 2:
        return None
    mx, my = _metres_scale(origin[1])
    horiz: List[float] = []
    for p, _z in ends:
        horiz.append(math.hypot((p[0] - origin[0]) * mx, (p[1] - origin[1]) * my))
    best: Optional[float] = None
    for i in range(n):
        for j in range(i + 1, n):
            hi, hj = horiz[i], horiz[j]
            zi, zj = ends[i][1], ends[j][1]
            denom = hi + hj
            if denom <= 1e-9:
                z_star = min(zi, zj)
            else:
                # Horizontal distances weight the opposite end, which is where
                # the two edge lengths stop getting shorter.
                z_star = (zi * hj + zj * hi) / denom
            if best is None or z_star < best:
                best = z_star
    return best


def _fan_fill(
    mesh: _Tin,
    loop: Sequence[int],
    floor: float,
    limit: Sequence[XY],
    straight_fan: bool = False,
    bury: float = 0.0,
    skirt_top: Optional[float] = None,
) -> int:
    """Replace the bump interior with one fan vertex per convex piece.

    The fan reaches the outline vertices, so the polygon is covered once.
    Those vertices keep the height they already have. The new vertex sits at
    ``floor``, unless ``straight_fan`` is set, in which case each piece uses
    the straight fan height and then drops by ``bury``. Returns how many
    inner vertices were added.
    """
    ring = [mesh.xy(v) for v in loop]
    # Interior faces are removed. No new vertex is painted along the cut:
    # the outline vertices already exist.
    del limit
    _drop_covered(mesh, ring, None)
    pieces = _convex_pieces(ring)
    made = 0
    for piece in pieces:
        target = _kernel_point(piece)
        if target is None:
            target = _centroid(piece)
        if not (_in_kernel(target, piece) or _pip(target[0], target[1], piece)):
            continue
        seq: List[int] = []
        ok = True
        for p in piece:
            vid = _vid_on_loop(mesh, p, loop)
            if vid is None:
                ok = False
                break
            if not seq or seq[-1] != vid:
                seq.append(vid)
        if seq and seq[0] == seq[-1]:
            seq.pop()
        if not ok or len(seq) < 3:
            continue
        # Stay on the interior point. A nearby grid cell can fall inside or
        # outside the outline, which is a vertex the outline cut did not ask for.
        piece_floor = floor
        if straight_fan:
            ends: List[Tuple[XY, float]] = []
            for vid in seq:
                z = mesh.alt[vid]
                if skirt_top is not None and _is_skirt_height(z, skirt_top):
                    continue
                ends.append((mesh.xy(vid), z))
            base = _straight_fan_height(target, ends)
            if base is None:
                base = floor + bury
            piece_floor = base - bury
        center = mesh._add(target, piece_floor, snap=False)
        mesh.add_fan(center, seq, None)
        made += 1
    return made


def _drop_covered_dust(mesh: _Tin) -> None:
    """Drop a sliver whose centroid already lies inside a larger triangle."""
    if len(mesh.tris) < 2 or not mesh.lat:
        return
    mx, my = _metres_scale(mesh.lat[len(mesh.lat) // 2])

    def inside(p: XY, tri: Tri) -> bool:
        a, b, c = mesh.xy(tri[0]), mesh.xy(tri[1]), mesh.xy(tri[2])
        area = _orient(a, b, c)
        if abs(area) <= 1e-24:
            return False
        w0 = _orient(b, c, p) / area
        w1 = _orient(c, a, p) / area
        w2 = _orient(a, b, p) / area
        return min(w0, w1, w2) >= -1e-8

    areas: List[float] = []
    cents: List[XY] = []
    for tri in mesh.tris:
        a, b, c = mesh.xy(tri[0]), mesh.xy(tri[1]), mesh.xy(tri[2])
        areas.append(abs(_orient(a, b, c)) * mx * my * 0.5)
        cents.append(((a[0] + b[0] + c[0]) / 3.0, (a[1] + b[1] + c[1]) / 3.0))
    drop = set()
    for i, tri in enumerate(mesh.tris):
        if areas[i] > 0.05:
            continue
        for j, other in enumerate(mesh.tris):
            if i == j or areas[j] <= areas[i]:
                continue
            if inside(cents[i], other):
                drop.add(i)
                break
    if drop:
        mesh.tris = [tri for i, tri in enumerate(mesh.tris) if i not in drop]
        mesh._rebuild()


def flatten_tile_bumps(
    lons: Sequence[float],
    lats: Sequence[float],
    alts: Sequence[float],
    triangles: Sequence[Tri],
    bump_rings: Sequence[Sequence[XY]],
    bury_depth: float = 0.0,
    offset_down: Optional[float] = None,
    bounds: Optional[Tuple[float, float, float, float]] = None,
    mask_rings: Optional[Sequence[Sequence[XY]]] = None,
    straight_fan: bool = False,
) -> Tuple[List[float], List[float], List[float], List[Tri], Dict[str, int]]:
    """
    Cut bump outlines into the tile and fill each bump with fan vertices.

    An original edge that crosses an outline stops on that outline. The mesh
    outside the outline keeps its heights. Inside, each convex piece gets one
    new vertex at the lowest outline height, then ``bury_depth`` metres
    further. When ``straight_fan`` is set, that vertex instead uses the
    lowest straight height of its fan-edge pairs, then ``bury_depth``.
    ``offset_down`` is an alias for ``bury_depth``. ``bounds`` is
    ``(west, south, east, north)`` in degrees. ``mask_rings`` is accepted so
    older runs can still pass a road mask; the outline cut does not walk it.
    """
    if offset_down is not None:
        bury_depth = float(offset_down)
    depth = max(float(bury_depth), 0.0)
    stats = {
        "verts_in": len(lons),
        "tris_in": len(triangles),
        "splits": 0,
        "inner": 0,
        "reused": 0,
        "buried": 0,
        "failed": 0,
        "fail_reasons": [],
        "verts_out": len(lons),
        "tris_out": len(triangles),
    }
    rings = [_dedupe_ring(r) for r in bump_rings if len(r) >= 3]
    if not rings or not triangles:
        return list(lons), list(lats), list(alts), [tuple(t) for t in triangles], stats

    if bounds is None:
        west, east = min(lons), max(lons)
        south, north = min(lats), max(lats)
    else:
        west, south, east, north = bounds

    mesh = _Tin(lons, lats, alts, triangles)
    if bounds is not None:
        mesh.enable_grid(west, south, east, north)
    before = len(mesh.lon)
    mesh._original_edges = set()
    for tri in mesh.tris:
        for e0, e1 in _tri_edges(tri):
            mesh._original_edges.add(_edge_key(e0, e1))
    # Kept so a run that still passes the road mask does not fail on the argument.
    del mask_rings
    # New outline vertices (corners and crossings), moved after the cut onto
    # the nearest grid point outside their sides. The fan center is not here.
    outline_new: List[Tuple[int, Sequence[XY]]] = []

    for raw in rings:
        # The clip closes a polygon on the tile edge so its edges can be cut
        # into this tile.
        ring = _clip_rect(_dedupe_ring(raw), west, south, east, north)
        ring = _drop_vertices_on_edges(_dedupe_ring(ring))
        if len(ring) < 3:
            continue
        snap = mesh.snapshot()
        mesh._fail_reason = ""
        loop = _outline_loop(mesh, ring)
        if not loop:
            reason = mesh._fail_reason or "outline side left the triangle chain"
            mesh.restore(snap)
            stats["failed"] += 1
            stats["fail_reasons"].append(reason)
            continue
        heights = [mesh.alt[v] for v in loop]
        top = max(heights)
        ground = [z for z in heights if not _is_skirt_height(z, top)]
        floor = min(ground if ground else heights) - depth
        made = _fan_fill(
            mesh,
            loop,
            floor,
            ring,
            straight_fan=straight_fan,
            bury=depth,
            skirt_top=top,
        )
        if made == 0:
            mesh.restore(snap)
            stats["failed"] += 1
            stats["fail_reasons"].append("no interior fan vertex")
            continue
        stats["buried"] += made
        stats["inner"] += made
        for vid in loop:
            if vid >= before:
                outline_new.append((vid, ring))

    _drop_covered_dust(mesh)
    if mesh._grid is not None and outline_new:
        taken = {mesh._uv((mesh.lon[i], mesh.lat[i])) for i in range(before)}
        outline_ids = {vid for vid, _ring in outline_new}
        for i in range(before, len(mesh.lon)):
            if i not in outline_ids:
                taken.add(mesh._uv((mesh.lon[i], mesh.lat[i])))
        # Corners have the tighter wedge, so they take a grid cell first.
        pending: List[Tuple[int, int, Sequence[XY], List[Tuple[XY, XY]]]] = []
        for vid, ring in outline_new:
            if vid >= len(mesh.lon):
                continue
            edges = _outline_snap_edges(mesh.xy(vid), ring)
            pending.append((0 if len(edges) >= 2 else 1, vid, ring, edges))
        pending.sort(key=lambda item: (item[0], item[1]))
        for _rank, vid, ring, edges in pending:
            placed = _closest_outside_grid(
                mesh.xy(vid), ring, west, south, east, north, taken, edges
            )
            if placed is None:
                continue
            mesh.lon[vid] = placed[0]
            mesh.lat[vid] = placed[1]

    used = set()
    for tri in mesh.tris:
        used.update(tri)
    order = sorted(used)
    remap = {old: i for i, old in enumerate(order)}
    flons = [mesh.lon[i] for i in order]
    flats = [mesh.lat[i] for i in order]
    falts = [mesh.alt[i] for i in order]
    final = []
    for tri in mesh.tris:
        if not all(v in remap for v in tri):
            continue
        a, b, c = (remap[v] for v in tri)
        if len({a, b, c}) < 3:
            continue
        final.append((a, b, c))
    stats["verts_out"] = len(flons)
    stats["tris_out"] = len(final)
    stats["splits"] += 0
    # Boundary insertions are everything added that is not an inner vertex.
    added = len(mesh.lon) - before
    stats["splits"] = max(0, added - stats["inner"])
    return flons, flats, falts, final, stats


def _assert_fans(lons, lats, tris, ring, label: str) -> List[int]:
    """Every interior triangle uses an inner vertex, and those fans stay inside."""
    ring = _ccw_ring(list(ring))
    inners = [
        i
        for i, (x, y) in enumerate(zip(lons, lats))
        if _strict_inside((x, y), ring)
    ]
    assert inners, label
    for tri in tris:
        cx = sum(lons[v] for v in tri) / 3.0
        cy = sum(lats[v] for v in tri) / 3.0
        uses = any(v in inners for v in tri)
        if _strict_inside((cx, cy), ring):
            assert uses, (label, tri)
        if uses:
            assert _pip(cx, cy, ring), (label, tri, (cx, cy))
    return inners


def _assert_lowered(lons, lats, alts, ring, depth: float, label: str) -> None:
    """Inner vertices sit ``depth`` metres under the lowest boundary height."""
    ring = _ccw_ring(list(ring))
    n = len(ring)
    mx, my = _metres_scale(ring[0][1])
    inners = []
    support = []
    for i, (x, y, z) in enumerate(zip(lons, lats, alts)):
        p = (x, y)
        if _strict_inside(p, ring):
            if _dist_ring_m(p, ring, mx, my) > _SHELF_M + 0.05:
                inners.append(i)
            continue
        if any(_on_seg(p, ring[k], ring[(k + 1) % n]) for k in range(n)):
            support.append(z)
            continue
        assert z > -1e-6, (label, i, z)
    assert inners and support, label
    floor = min(support) - depth
    for i in inners:
        assert abs(alts[i] - floor) < 1e-6, (label, alts[i], floor)
    for z in support:
        assert z >= floor + depth - 1e-6, (label, z, floor)


def _triangle_of(lons, lats, tris, p: XY) -> Optional[Tri]:
    for tri in tris:
        a = (lons[tri[0]], lats[tri[0]])
        b = (lons[tri[1]], lats[tri[1]])
        c = (lons[tri[2]], lats[tri[2]])
        area = _orient(a, b, c)
        if abs(area) <= 1e-24:
            continue
        w0 = _orient(b, c, p) / area
        w1 = _orient(c, a, p) / area
        w2 = _orient(a, b, p) / area
        if w0 >= -1e-8 and w1 >= -1e-8 and w2 >= -1e-8:
            return tri
    return None


def _assert_single_ring(lons, lats, tris, ring, label: str) -> None:
    """Each face inside the polygon uses the outline, plus one fan vertex."""
    ring = _ccw_ring(list(ring))
    n = len(ring)

    def on_outline(p: XY) -> bool:
        return any(_on_seg(p, ring[k], ring[(k + 1) % n]) for k in range(n))

    for tri in tris:
        cx = sum(lons[v] for v in tri) / 3.0
        cy = sum(lats[v] for v in tri) / 3.0
        if not _strict_inside((cx, cy), ring):
            continue
        inside = []
        for v in tri:
            p = (lons[v], lats[v])
            if on_outline(p):
                continue
            assert _strict_inside(p, ring), (label, "face leaves the polygon", p)
            inside.append(p)
        assert len(inside) == 1, (label, "inset ring", tri, inside)


def self_test() -> None:
    lons = [0.0, 2.0, 2.0, 0.0, 1.0]
    lats = [0.0, 0.0, 2.0, 2.0, 1.0]
    alts = [0.0, 0.0, 0.0, 0.0, 5.0]
    tris = [(0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
    bump = [[(0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5)]]
    ol, oa, oz, ot, st = flatten_tile_bumps(lons, lats, alts, tris, bump)
    assert st["failed"] == 0, st
    assert st["buried"] >= 1, st
    corners = {(0.5, 0.5), (1.5, 0.5), (1.5, 1.5), (0.5, 1.5)}
    found = set()
    for x, y in zip(ol, oa):
        for c in corners:
            if _hypot((x, y), c) <= 1e-8:
                found.add(c)
    assert found == corners, found
    for x, y, z in zip(ol, oa, oz):
        if abs(x) < 1e-9 and abs(y) < 1e-9:
            assert abs(z) < 1e-6, z
    ring = bump[0]
    inners = [
        i
        for i, (x, y) in enumerate(zip(ol, oa))
        if _strict_inside((x, y), ring)
    ]
    assert inners, st
    _assert_lowered(ol, oa, oz, ring, 0.0, "square")
    inside_pt = (1.0, 1.0)
    tri = _triangle_of(ol, oa, ot, inside_pt)
    assert tri is not None and any(v in inners for v in tri), tri
    outside_pt = (0.1, 0.1)
    otri = _triangle_of(ol, oa, ot, outside_pt)
    assert otri is not None and all(v not in inners for v in otri), otri

    # This L is star-shaped: one interior point sees the whole polygon.
    n = 4
    glons = []
    glats = []
    galts = []
    for j in range(n + 1):
        for i in range(n + 1):
            glons.append(float(i))
            glats.append(float(j))
            galts.append(0.0)

    def gid(i, j):
        return j * (n + 1) + i

    gtris = []
    for j in range(n):
        for i in range(n):
            a, b, c, d = gid(i, j), gid(i + 1, j), gid(i + 1, j + 1), gid(i, j + 1)
            gtris.append((a, b, c))
            gtris.append((a, c, d))
    ell = [[
        (0.5, 0.5),
        (2.5, 0.5),
        (2.5, 1.5),
        (1.5, 1.5),
        (1.5, 2.5),
        (0.5, 2.5),
    ]]
    el, ea, ez, et, es = flatten_tile_bumps(glons, glats, galts, gtris, ell)
    assert es["failed"] == 0, es
    # The grid is flat, so lowering has nothing above the boundary to drop.
    assert all(abs(z) < 1e-6 for z in ez), ez

    # Convex quad inside one triangle: one new inner vertex, at the boundary height.
    ql, qa, qz, qt, qs = flatten_tile_bumps(
        [0.0, 4.0, 0.0],
        [0.0, 0.0, 4.0],
        [10.0, 10.0, 10.0],
        [(0, 1, 2)],
        [[(1.0, 0.4), (2.0, 0.5), (1.6, 1.2), (0.8, 1.0)]],
    )
    assert qs["failed"] == 0, qs
    assert qs["inner"] == 1, qs
    assert all(abs(z - 10.0) < 1e-6 for z in qz), qz
    # A concave arrow on flat ground keeps that ground.
    al, aa, az, at, ast = flatten_tile_bumps(
        [0.0, 4.0, 0.0, 4.0],
        [0.0, 0.0, 3.0, 3.0],
        [0.0, 0.0, 0.0, 0.0],
        [(0, 1, 2), (1, 3, 2)],
        [[(0.2, 0.2), (3.5, 1.5), (0.2, 2.8), (1.2, 1.5)]],
    )
    assert ast["failed"] == 0, ast
    assert all(abs(z) < 1e-6 for z in az), az
    # A U has an empty kernel, so each arm gets its own inner vertex.
    you = [[
        (0.5, 0.5),
        (3.5, 0.5),
        (3.5, 3.5),
        (2.4, 3.5),
        (2.4, 1.6),
        (1.6, 1.6),
        (1.6, 3.5),
        (0.5, 3.5),
    ]]
    ul, ua, uz, ut, us = flatten_tile_bumps(glons, glats, galts, gtris, you)
    assert us["failed"] == 0, us
    assert us["inner"] >= 2, us
    assert all(abs(z) < 1e-6 for z in uz), uz
    _assert_fans(ol, oa, ot, ring, "square")
    _assert_single_ring(ol, oa, ot, ring, "square")
    # A few-metre polygon in real longitude/latitude. The center has to be
    # computed from a local origin or it lands outside the polygon.
    geo = [
        (35.209332111440766, 32.189927433258674),
        (35.20925437412325, 32.18994140625),
        (35.209013131276464, 32.18994140625),
        (35.20915153752705, 32.189916987483414),
    ]
    gl, ga, gz, gt, gs = flatten_tile_bumps(
        [35.208, 35.211, 35.208],
        [32.189, 32.189, 32.191],
        [100.0, 100.0, 120.0],
        [(0, 1, 2)],
        [geo],
        bury_depth=0.0,
    )
    # The fan vertex sits near the boundary height, which is about 100 here.
    assert gs["failed"] == 0, gs
    assert min(gz) > 99.0, min(gz)
    _assert_single_ring(gl, ga, gt, geo, "geo")
    # A boundary vertex that lies on the straight edge between two others
    # has to stay in the mesh. Dropping it deletes the boundary edge and
    # the polygon on that edge cannot be inserted.
    seam = _Tin(
        [0.0, 0.0, 0.0, 1.0],
        [2.0, 1.0, 0.0, 1.5],
        [0.0, 0.0, 0.0, 0.0],
        [(0, 2, 3)],
    )
    stitched = seam._polygon_tris([0, 1, 2, 3])
    assert any(1 in tri for tri in stitched), stitched
    seam_edges = {_edge_key(t[i], t[(i + 1) % 3]) for t in stitched for i in range(3)}
    assert (0, 1) in seam_edges and (1, 2) in seam_edges, seam_edges
    # Same situation through the full insert: the closing edge is the mesh
    # boundary, and an earlier edge walks across a collinear boundary vertex.
    bl, ba, bz, bt, bs = flatten_tile_bumps(
        [0.0, 0.0, 0.0, 3.0],
        [0.0, 1.0, 2.0, 3.0],
        [5.0, 7.0, 6.0, 5.0],
        [(1, 2, 3), (0, 3, 1)],
        [[(0.0, 2.0), (0.4, 1.6), (0.3, 0.3), (0.0, 1.0)]],
        bury_depth=0.0,
    )
    assert bs["failed"] == 0, bs

    def z_at(xs, ys, zs, faces, p):
        tri = _triangle_of(xs, ys, faces, p)
        assert tri is not None, p
        a = (xs[tri[0]], ys[tri[0]])
        b = (xs[tri[1]], ys[tri[1]])
        c = (xs[tri[2]], ys[tri[2]])
        area = _orient(a, b, c)
        w0 = _orient(b, c, p) / area
        w1 = _orient(c, a, p) / area
        w2 = _orient(a, b, p) / area
        return w0 * zs[tri[0]] + w1 * zs[tri[1]] + w2 * zs[tri[2]]

    # A steep face outside the polygon must keep its height when a bump edge
    # crosses the neighbouring triangles. The interior is replaced by the fan.
    slons = [0.0, 4.0, 4.0, 0.0, 2.0]
    slats = [0.0, 0.0, 4.0, 4.0, 1.0]
    salts = [0.0, 0.0, 40.0, 40.0, 1.0]
    stris = [(0, 1, 4), (1, 2, 4), (2, 3, 4), (3, 0, 4)]
    outside_pt = (3.2, 2.5)
    pit = (1.2, 0.6)
    before_out = z_at(slons, slats, salts, stris, outside_pt)
    before_pit = z_at(slons, slats, salts, stris, pit)
    pl, pa, pz, pt, ps = flatten_tile_bumps(
        slons, slats, salts, stris,
        [[(0.4, 0.2), (2.2, 0.2), (2.2, 1.4), (0.4, 1.4)]],
    )
    assert ps["failed"] == 0, ps
    assert abs(z_at(pl, pa, pz, pt, outside_pt) - before_out) < 1e-6, (
        before_out, z_at(pl, pa, pz, pt, outside_pt)
    )
    pit_tri = _triangle_of(pl, pa, pt, pit)
    assert pit_tri is not None
    assert any(_strict_inside((pl[v], pa[v]), [(0.4, 0.2), (2.2, 0.2), (2.2, 1.4), (0.4, 1.4)]) for v in pit_tri)

    # One original triangle covers the bump and also leaves the road. The bump
    # side drops. The part outside the road stays at the original height.
    mlons = [0.0, 3.0, 1.5, 0.8]
    mlats = [0.0, 0.0, 3.0, 0.8]
    malts = [10.0, 10.0, 10.0, 30.0]
    mtris = [(0, 1, 3), (1, 2, 3), (2, 0, 3)]
    mbump = [[(0.7, 0.55), (1.0, 0.55), (1.0, 0.85), (0.7, 0.85)]]
    mmask = [[(-0.2, -0.2), (1.8, -0.2), (1.8, 2.2), (-0.2, 2.2)]]
    bump_pt = (0.8, 0.8)
    off_pt = (2.6, 0.3)
    before_bump = z_at(mlons, mlats, malts, mtris, bump_pt)
    before_off = z_at(mlons, mlats, malts, mtris, off_pt)
    xl, xa, xz, xt, xs = flatten_tile_bumps(
        mlons, mlats, malts, mtris, mbump, mask_rings=mmask
    )
    assert xs["failed"] == 0, xs
    assert xs["buried"] >= 1, xs
    after_bump = z_at(xl, xa, xz, xt, bump_pt)
    after_off = z_at(xl, xa, xz, xt, off_pt)
    assert after_bump < before_bump - 1.0, (before_bump, after_bump)
    assert abs(after_off - before_off) < 1e-4, (before_off, after_off)
    # Both samples are inside the bump, so the fan lowers them. A point
    # outside the outline keeps its height.
    slons = [0.0, 3.0, 0.0, 3.0]
    slats = [0.0, 0.0, 2.0, 2.0]
    salts = [9.0, 9.0, 15.0, 15.0]
    stris2 = [(0, 1, 2), (1, 3, 2)]
    sbump = [[(0.3, 0.4), (2.2, 0.4), (2.2, 1.4), (0.3, 1.4)]]
    smask = [[(-0.2, -0.2), (1.0, -0.2), (1.0, 2.2), (-0.2, 2.2)]]
    on_pt = (0.6, 0.9)
    out_pt = (1.8, 0.9)
    far_pt = (2.7, 1.8)
    before_on = z_at(slons, slats, salts, stris2, on_pt)
    before_out = z_at(slons, slats, salts, stris2, out_pt)
    before_far = z_at(slons, slats, salts, stris2, far_pt)
    yl, ya, yz, yt, ys = flatten_tile_bumps(
        slons, slats, salts, stris2, sbump, mask_rings=smask
    )
    assert ys["failed"] == 0, ys
    after_on = z_at(yl, ya, yz, yt, on_pt)
    after_out = z_at(yl, ya, yz, yt, out_pt)
    after_far = z_at(yl, ya, yz, yt, far_pt)
    assert after_on < before_on - 1e-3, (before_on, after_on)
    assert after_out < before_out - 1e-3, (before_out, after_out)
    assert abs(after_far - before_far) < 1e-4, (before_far, after_far)
    # A bump corner that falls on a skirt (height 0) must keep the terrain
    # height. The fan must not dig down to the skirt.
    sk_lons = [0.0, 4.0, 0.0, 2.0, 2.0]
    sk_lats = [1.0, 1.0, 4.0, 0.0, -1.0]
    sk_alts = [100.0, 100.0, 120.0, 0.0, 0.0]
    sk_tris = [(0, 1, 2), (0, 3, 4), (0, 4, 1)]
    sk_bump = [[(2.0, 0.2), (1.0, 1.5), (2.0, 2.5), (3.0, 1.5)]]
    outside_sk = (3.2, 1.2)
    before_sk = z_at(sk_lons, sk_lats, sk_alts, sk_tris, outside_sk)
    kl, ka, kz, kt, ks = flatten_tile_bumps(
        sk_lons, sk_lats, sk_alts, sk_tris, sk_bump,
    )
    assert ks["failed"] == 0, ks
    corner_z = z_at(kl, ka, kz, kt, (2.0, 0.2))
    inside_z = z_at(kl, ka, kz, kt, (2.0, 1.4))
    assert corner_z > 90.0, corner_z
    assert inside_z > 90.0, inside_z
    assert abs(z_at(kl, ka, kz, kt, outside_sk) - before_sk) < 1e-6
    # A mesh vertex already lies on an edge, so one triangle is flat. The
    # outline passes through that vertex and must keep going into the real
    # triangle. Splitting the edge at that vertex lets the cut finish.
    flat_lons = [0.0, 2.0, 1.0, 1.0]
    flat_lats = [0.0, 0.0, 1.0, 0.0]
    flat_alts = [5.0, 5.0, 8.0, 5.0]
    flat_tris = [(0, 1, 2), (0, 1, 3)]
    outside_hi = (1.0, 0.9)
    before_hi = z_at(flat_lons, flat_lats, flat_alts, flat_tris, outside_hi)
    fl, fa, fz, ft, fs = flatten_tile_bumps(
        flat_lons, flat_lats, flat_alts, flat_tris,
        [[(1.0, 0.0), (1.4, 0.15), (1.2, 0.55), (0.8, 0.45)]],
    )
    assert fs["failed"] == 0, fs
    assert fs["inner"] >= 1, fs
    assert abs(z_at(fl, fa, fz, ft, outside_hi) - before_hi) < 1e-6, (
        before_hi, z_at(fl, fa, fz, ft, outside_hi)
    )
    # Points that fall in one quantized-mesh cell are one vertex, and a
    # vertex placed on that grid stays on it. Triangles built there do not
    # overlap.
    grid = _Tin(
        [35.20, 35.22, 35.20],
        [32.18, 32.18, 32.20],
        [10.0, 10.0, 12.0],
        [(0, 1, 2)],
    )
    grid.enable_grid(35.20, 32.18, 35.22, 32.20)
    n_before = len([v for v in range(len(grid.lon)) if any(v in t for t in grid.tris)])
    ga = grid.insert((35.205, 32.185))
    gb = grid.insert((35.205 + 1e-8, 32.185 + 1e-8))
    assert ga is not None and ga == gb, (ga, gb)
    assert len(grid.lon) == n_before + 1 or len(grid.lon) >= n_before
    for x, y in zip(grid.lon, grid.lat):
        sx, sy = grid._snap((x, y))
        assert abs(sx - x) < 1e-12 and abs(sy - y) < 1e-12, (x, sx, y, sy)
    gwest, gsouth, geast, gnorth = 35.208, 32.189, 35.211, 32.191
    sl, sa, sz, stt, ss = flatten_tile_bumps(
        [35.208, 35.211, 35.208],
        [32.189, 32.189, 32.191],
        [100.0, 100.0, 120.0],
        [(0, 1, 2)],
        [geo],
        bounds=(gwest, gsouth, geast, gnorth),
    )
    assert ss["failed"] == 0, ss
    snapped = _Tin(sl, sa, sz, stt)
    snapped.enable_grid(gwest, gsouth, geast, gnorth)
    half_u = (geast - gwest) / _QM_Q * 0.5
    half_v = (gnorth - gsouth) / _QM_Q * 0.5
    for x, y, sx, sy in zip(sl, sa, snapped.lon, snapped.lat):
        # A crossing stays on the outline, so it can sit up to half a grid
        # cell off the point the terrain file will store.
        assert abs(x - sx) <= half_u + 1e-12, (x, sx)
        assert abs(y - sy) <= half_v + 1e-12, (y, sy)
    # An original edge crosses the outline at one point. The saved vertex is
    # the nearest grid point outside the bump, not the nearest grid point.
    cross_ring = [(1.5, 0.5), (2.2, 0.5), (2.2, 3.0), (1.5, 3.0)]
    cl, ca, cz, ct, cs = flatten_tile_bumps(
        [0.0, 4.0, 0.0, 4.0],
        [0.0, 0.0, 4.0, 4.0],
        [10.0, 10.0, 12.0, 12.0],
        [(0, 1, 2), (1, 3, 2)],
        [cross_ring],
        bounds=(0.0, 0.0, 4.0, 4.0),
    )
    assert cs["failed"] == 0, cs
    assert cs["inner"] >= 1, cs
    hit = [
        (x, y)
        for x, y in zip(cl, ca)
        if abs(x - 1.5) < 5e-4 and abs(y - 2.5) < 5e-4
    ]
    assert hit, "missing crossing"
    assert _outside_ring(hit[0], cross_ring), hit
    assert _closest_outside_grid(hit[0], cross_ring, 0.0, 0.0, 4.0, 4.0, set()) == hit[0]
    cn = len(cross_ring)

    def _off_outline(p: XY) -> float:
        mx, my = _metres_scale(p[1])
        return min(
            _dist_seg_m(p, cross_ring[i], cross_ring[(i + 1) % cn], mx, my)
            for i in range(cn)
        )

    for x, y in zip(cl, ca):
        p = (x, y)
        off = _off_outline(p)
        if _strict_inside(p, cross_ring):
            # The fan sits in the middle. An outline vertex stays outside.
            assert off > 2.0, (p, off)
            continue
        if off > 30.0:
            continue
        assert _outside_ring(p, cross_ring), (p, off)
    for tri in stt:
        cx = sum(sl[v] for v in tri) / 3.0
        cy = sum(sa[v] for v in tri) / 3.0
        hits = 0
        for other in stt:
            a = (sl[other[0]], sa[other[0]])
            b = (sl[other[1]], sa[other[1]])
            c = (sl[other[2]], sa[other[2]])
            area = _orient(a, b, c)
            if abs(area) <= 1e-24:
                continue
            w0 = _orient(b, c, (cx, cy)) / area
            w1 = _orient(c, a, (cx, cy)) / area
            w2 = _orient(a, b, (cx, cy)) / area
            if w0 >= -1e-8 and w1 >= -1e-8 and w2 >= -1e-8:
                hits += 1
        assert hits == 1, (tri, hits)
    # CD also passes through D, which is already its own end. That hit must
    # be skipped so the real crossing X on the same edge is still cut.
    qn = _QM_Q
    def _gpt(u: float, v: float) -> XY:
        return (u / qn, v / qn)
    cy = 16000.0
    cpt = _gpt(10000.0, cy)
    dpt = _gpt(10033.0, cy)
    mid = ((cpt[0] + dpt[0]) * 0.5, cpt[1])
    near_d = (dpt[0] - 5e-11, dpt[1] + 5e-11)
    end_ring = [
        (mid[0], mid[1] - 0.01),
        (mid[0], mid[1] + 0.01),
        (dpt[0] - 0.0002, mid[1] + 0.01),
        near_d,
        (dpt[0] - 0.0002, mid[1] - 0.01),
    ]
    el, ea, ez, et, es = flatten_tile_bumps(
        [
            cpt[0] - 0.02, dpt[0] + 0.02, dpt[0] + 0.02, cpt[0] - 0.02,
            cpt[0], dpt[0],
        ],
        [
            cpt[1] - 0.05, cpt[1] - 0.05, cpt[1] + 0.05, cpt[1] + 0.05,
            cpt[1], dpt[1],
        ],
        [10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
        [(0, 1, 5), (0, 5, 4), (4, 5, 2), (4, 2, 3)],
        [end_ring],
        bounds=(0.0, 0.0, 1.0, 1.0),
    )
    assert es["failed"] == 0, es
    end_hit = [
        (x, y)
        for x, y in zip(el, ea)
        if abs(x - mid[0]) < 2e-3 and abs(y - mid[1]) < 2e-3 and _outside_ring((x, y), end_ring)
    ]
    assert end_hit, "missing crossing beside an endpoint hit"
    # The closest grid point outside the bump can sit on the inner side of
    # one edge. A corner takes the closest grid point outside both edges.
    tw = 35.20
    ts = 32.18
    te = tw + 360.0 / 32768.0
    tn = ts + 180.0 / 16384.0
    tdu = (te - tw) / _QM_Q
    tdv = (tn - ts) / _QM_Q
    tcx = tw + tdu * 1000.9
    tcy = ts + tdv * 1000.1
    wedge_ring = [
        (tcx, tcy),
        (tcx + 20.0 * tdu, tcy),
        (tcx + 20.0 * tdu, tcy + 20.0 * tdv),
        (tcx, tcy + 20.0 * tdv),
    ]
    wedge_edges = _outline_snap_edges((tcx, tcy), wedge_ring)
    assert len(wedge_edges) == 2, wedge_edges
    wrong = (tw + tdu * 1001.0, ts + tdv * 1000.0)
    want = (tw + tdu * 1000.0, ts + tdv * 1000.0)
    assert _outside_ring(wrong, wedge_ring), wrong
    assert not all(_right_of(wrong, a, b) for a, b in wedge_edges)
    wedge_taken: set = set()
    wedge_hit = _closest_outside_grid(
        (tcx, tcy), wedge_ring, tw, ts, te, tn, wedge_taken, wedge_edges
    )
    assert wedge_hit == want, wedge_hit
    for a, b in wedge_edges:
        assert _right_of(wedge_hit, a, b), wedge_hit
    wmx, wmy = _metres_scale(tcy)
    wrong_d = math.hypot((wrong[0] - tcx) * wmx, (wrong[1] - tcy) * wmy)
    want_d = math.hypot((want[0] - tcx) * wmx, (want[1] - tcy) * wmy)
    assert wrong_d < want_d, (wrong_d, want_d)
    # A crossing keeps the closest grid point outside its own side.
    cross_p = ((wedge_ring[0][0] + wedge_ring[1][0]) * 0.5, wedge_ring[0][1])
    cross_edges = _outline_snap_edges(cross_p, wedge_ring)
    assert len(cross_edges) == 1, cross_edges
    cross_hit = _closest_outside_grid(
        cross_p, wedge_ring, tw, ts, te, tn, set(), cross_edges
    )
    assert cross_hit is not None
    assert _right_of(cross_hit, cross_edges[0][0], cross_edges[0][1]), cross_hit
    assert _outside_ring(cross_hit, wedge_ring), cross_hit
    # Both ends of a snapped side stay outside that side, so the side misses the bump.
    corner_taken: set = set()
    snapped_corners = []
    for corner in wedge_ring:
        cedges = _outline_snap_edges(corner, wedge_ring)
        cplace = _closest_outside_grid(
            corner, wedge_ring, tw, ts, te, tn, corner_taken, cedges
        )
        assert cplace is not None, corner
        for a, b in cedges:
            assert _right_of(cplace, a, b), (corner, cplace)
        snapped_corners.append(cplace)
    for i in range(len(snapped_corners)):
        a = snapped_corners[i]
        b = snapped_corners[(i + 1) % len(snapped_corners)]
        mid = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
        assert not _strict_inside(mid, wedge_ring), (a, b, mid)
    # Two fan edges of equal length. The straight height is halfway between
    # the ends, which is above the lower end.
    step = 10.0 / 111320.0
    straight = _straight_fan_height(
        (0.0, 0.0),
        [((step, 0.0), 10.0), ((-step, 0.0), 0.0)],
    )
    assert straight is not None and abs(straight - 5.0) < 1e-6, straight
    far = 100.0 / 111320.0
    near = 10.0 / 111320.0
    weighted = _straight_fan_height(
        (0.0, 0.0),
        [((far, 0.0), 0.0), ((0.0, near), 10.0)],
    )
    assert weighted is not None and abs(weighted - (1000.0 / 110.0)) < 1e-4, weighted
    fan_ring = [(0.2, 0.2), (0.8, 0.2), (0.2, 0.8)]
    fan_lons = [0.0, 1.0, 0.0]
    fan_lats = [0.0, 0.0, 1.0]
    fan_alts = [0.0, 10.0, 10.0]
    fan_tris = [(0, 1, 2)]
    ll, la, lz, lt, ls = flatten_tile_bumps(
        fan_lons, fan_lats, fan_alts, fan_tris, [fan_ring]
    )
    assert ls["failed"] == 0, ls
    hl, ha, hz, ht, hs = flatten_tile_bumps(
        fan_lons, fan_lats, fan_alts, fan_tris, [fan_ring], straight_fan=True
    )
    assert hs["failed"] == 0, hs

    def _fan_inner(xs, ys, zs):
        ids = [
            i
            for i, (x, y) in enumerate(zip(xs, ys))
            if _strict_inside((x, y), fan_ring)
        ]
        assert len(ids) == 1, ids
        return ids[0]

    low_i = _fan_inner(ll, la, lz)
    high_i = _fan_inner(hl, ha, hz)

    def _ring_ends(xs, ys, zs):
        found = []
        for x, y, z in zip(xs, ys, zs):
            if _dist_ring_m((x, y), fan_ring, *_metres_scale(y)) < 0.05:
                found.append(((x, y), z))
        return found

    low_ends = _ring_ends(ll, la, lz)
    assert abs(lz[low_i] - min(z for _p, z in low_ends)) < 1e-6, lz[low_i]
    high_ends = _ring_ends(hl, ha, hz)
    expect = _straight_fan_height((hl[high_i], ha[high_i]), high_ends)
    lowest_end = min(z for _p, z in high_ends)
    assert expect is not None and expect > lowest_end + 1e-3, (expect, lowest_end)
    assert abs(hz[high_i] - expect) < 1e-6, (hz[high_i], expect)
    print(
        "qm_bump_flatten_core self_test ok",
        st, es, qs, ast, us, gs, bs, ps, xs, ys,
    )


if __name__ == "__main__":
    self_test()
