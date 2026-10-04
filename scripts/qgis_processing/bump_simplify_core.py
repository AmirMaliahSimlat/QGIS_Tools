# -*- coding: utf-8 -*-
"""
Simplify bump rings to convex hulls, then optionally drop corners by
extending edges.

Touching exact polygons are dissolved by the caller before this runs.
Nearby rings then share one hull when that hull lies inside the road.
A ring whose own hull leaves the road keeps its bends only where a
shortcut would leave the road or uncover the bump.
"""

from __future__ import annotations

import math
from typing import Callable, List, Optional, Sequence, Tuple

from road_clip_simplify_core import convex_hull

XY = Tuple[float, float]
Ring = List[XY]
CoversFn = Callable[[Sequence[XY]], bool]

_EPS = 1e-6
# A new edge can fall a couple of centimetres short of an exact bump vertex
# because of rounding. That still covers the bump; the road margin is larger.
_COVER_EPS_M = 0.02
# Bumps farther apart than this are not paired directly. A chain of closer
# bumps can still become one hull when each combined hull stays in the road.
_MERGE_GAP_M = 200.0


def _hypot(a: XY, b: XY) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _line_intersect(a: XY, b: XY, c: XY, d: XY) -> Optional[XY]:
    """Intersection of infinite lines AB and CD, or None when parallel."""
    rx, ry = b[0] - a[0], b[1] - a[1]
    sx, sy = d[0] - c[0], d[1] - c[1]
    den = rx * sy - ry * sx
    if abs(den) < 1e-12:
        return None
    qpx, qpy = c[0] - a[0], c[1] - a[1]
    t = (qpx * sy - qpy * sx) / den
    return (a[0] + t * rx, a[1] + t * ry)


def _param(a: XY, b: XY, p: XY) -> Optional[float]:
    """Parameter of P on line AB. 0 at A, 1 at B, >1 beyond B."""
    dx, dy = b[0] - a[0], b[1] - a[1]
    if abs(dx) >= abs(dy):
        if abs(dx) < 1e-18:
            return None
        return (p[0] - a[0]) / dx
    if abs(dy) < 1e-18:
        return None
    return (p[1] - a[1]) / dy


def _tri_area(a: XY, b: XY, c: XY) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_boundary(p: XY, ring: Sequence[XY], eps: float = 1e-4) -> bool:
    n = len(ring)
    for i in range(n):
        ax, ay = ring[i]
        bx, by = ring[(i + 1) % n]
        vx, vy = bx - ax, by - ay
        wx, wy = p[0] - ax, p[1] - ay
        span = vx * vx + vy * vy
        if span < 1e-24:
            if math.hypot(wx, wy) <= eps:
                return True
            continue
        t = max(0.0, min(1.0, (wx * vx + wy * vy) / span))
        if math.hypot(wx - t * vx, wy - t * vy) <= eps:
            return True
    return False


def _inside(p: XY, ring: Sequence[XY], eps: float = 1e-4) -> bool:
    """True when P is inside or on the boundary of a simple ring."""
    if len(ring) < 3:
        return False
    if _on_boundary(p, ring, eps):
        return True
    x, y = p
    hit = False
    n = len(ring)
    for i in range(n):
        x1, y1 = ring[i]
        x2, y2 = ring[(i + 1) % n]
        if (y1 > y) == (y2 > y):
            continue
        xint = x1 + (y - y1) / (y2 - y1) * (x2 - x1)
        if x < xint:
            hit = not hit
    return hit


def _as_ccw(pts: Sequence[XY]) -> list:
    """Return ``pts`` in counter-clockwise order so mask tests see a filled patch."""
    area = 0.0
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        area += x1 * y2 - x2 * y1
    if area < 0.0:
        return list(reversed(pts))
    return list(pts)


def _point_height(a: XY, b: XY, c: XY) -> float:
    """Distance from B to the line AC."""
    base = _hypot(a, c)
    if base < 1e-12:
        return _hypot(a, b)
    return abs(_tri_area(a, b, c)) / base


def _segments_cross(a: XY, b: XY, c: XY, d: XY) -> bool:
    """True when AB and CD cross properly, not only at a shared endpoint."""
    o1 = _tri_area(a, b, c)
    o2 = _tri_area(a, b, d)
    o3 = _tri_area(c, d, a)
    o4 = _tri_area(c, d, b)
    return o1 * o2 < 0.0 and o3 * o4 < 0.0


def _shortcut_clear(ring: Sequence[XY], i: int) -> bool:
    """The edge that skips vertex ``i`` does not cut another edge."""
    n = len(ring)
    a = ring[(i - 1) % n]
    c = ring[(i + 1) % n]
    ax0, ax1 = (a[0], c[0]) if a[0] <= c[0] else (c[0], a[0])
    ay0, ay1 = (a[1], c[1]) if a[1] <= c[1] else (c[1], a[1])
    for j in range(n):
        k = (j + 1) % n
        if j in ((i - 1) % n, i) or k in ((i - 1) % n, i):
            continue
        u = ring[j]
        v = ring[k]
        if max(u[0], v[0]) < ax0 or min(u[0], v[0]) > ax1:
            continue
        if max(u[1], v[1]) < ay0 or min(u[1], v[1]) > ay1:
            continue
        if _segments_cross(a, c, u, v):
            return False
    return True


def _drop_bends(ring: Sequence[XY], covers: CoversFn) -> Ring:
    """
    Drop corners of a bump whose convex hull leaves the road.

    A corner goes when the shortcut still contains that corner (a dent or a
    collinear stub) and the shortcut triangle lies inside the road. The exact
    bump therefore stays covered, without using the hull that steps outside.
    """
    current = list(ring)
    guard = len(current)
    for _ in range(guard):
        n = len(current)
        if n <= 4:
            break
        drop = set()
        blocked = set()
        for i in range(n):
            if i in blocked or (i - 1) % n in blocked or (i + 1) % n in blocked:
                continue
            a = current[(i - 1) % n]
            b = current[i]
            c = current[(i + 1) % n]
            acx, acy = c[0] - a[0], c[1] - a[1]
            span = acx * acx + acy * acy
            along = 0.5
            if span > 1e-18:
                along = ((b[0] - a[0]) * acx + (b[1] - a[1]) * acy) / span
            collinear = _point_height(a, b, c) <= 1e-3 and -1e-6 <= along <= 1.0 + 1e-6
            if not collinear:
                trial = current[:i] + current[i + 1 :]
                if not _inside(b, trial):
                    continue
                if not _shortcut_clear(current, i):
                    continue
                if not covers(_as_ccw((a, b, c))):
                    continue
            drop.add(i)
            blocked.add(i)
            blocked.add((i - 1) % n)
            blocked.add((i + 1) % n)
        if not drop:
            break
        current = [pt for i, pt in enumerate(current) if i not in drop]
    return current


def _replace_span(ring: Sequence[XY], start: int, span: int, new_pts: Sequence[XY]) -> Ring:
    """Replace ``span`` vertices beginning at ``start`` (wrapping) with ``new_pts``."""
    n = len(ring)
    skip = {(start + t) % n for t in range(span)}
    out: Ring = []
    for j in range(n):
        if j == start:
            out.extend(new_pts)
        if j not in skip:
            out.append(ring[j])
    return out


def _span_geometry(ring: Sequence[XY], i: int, k: int):
    """
    Edge before vertex i and the edge after a run of ``k`` vertices.

    Returns (B, D, X, s_need) when those edges meet beyond the run and every
    intermediate vertex lies between chord BD and that meeting point.
    """
    n = len(ring)
    if k < 1 or k > n - 4:
        return None
    a = ring[(i - 2) % n]
    b = ring[(i - 1) % n]
    mids = [ring[(i + t) % n] for t in range(k)]
    d = ring[(i + k) % n]
    e = ring[(i + k + 1) % n]
    cross = _line_intersect(a, b, d, e)
    if cross is None:
        return None
    t_ab = _param(a, b, cross)
    t_ed = _param(e, d, cross)
    if t_ab is None or t_ed is None:
        return None
    if t_ab <= 1.0 + _EPS or t_ed <= 1.0 + _EPS:
        return None
    area_x = _tri_area(b, cross, d)
    if abs(area_x) < 1e-8:
        return None
    s_need = 0.0
    for mid in mids:
        share = _tri_area(b, mid, d) / area_x
        if share <= _EPS or share >= 1.0 - _EPS:
            return None
        if share > s_need:
            s_need = share
    return b, d, cross, s_need


def _candidate(ring: Sequence[XY], i: int, k: int, use_point: bool):
    """New ring and the added patch, or None."""
    geom = _span_geometry(ring, i, k)
    if geom is None:
        return None
    b, d, cross, s_need = geom
    n = len(ring)
    start = (i - 1) % n
    span = k + 2
    if use_point:
        new_pts = [cross]
        patch = [b, cross, d]
    else:
        bx = b[0] + s_need * (cross[0] - b[0])
        by = b[1] + s_need * (cross[1] - b[1])
        dx = d[0] + s_need * (cross[0] - d[0])
        dy = d[1] + s_need * (cross[1] - d[1])
        b2 = (bx, by)
        d2 = (dx, dy)
        if _hypot(b2, d2) < 1e-4:
            return None
        new_pts = [b2, d2]
        patch = [b, b2, d2, d]
    patch = _as_ccw(patch)
    updated = _replace_span(ring, start, span, new_pts)
    if len(updated) < 3:
        return None
    removed = span - len(new_pts)
    if removed < 1:
        return None
    return updated, patch, removed


def _cut_at(ring, i, k, hull_pts, covers):
    """Largest accepted extension at this corner and span, or None."""
    for use_point in (True, False):
        built = _candidate(ring, i, k, use_point)
        if built is None:
            continue
        updated, patch, removed = built
        if removed < 1:
            continue
        if not covers(patch):
            continue
        if not all(_inside(p, updated, _COVER_EPS_M) for p in hull_pts):
            continue
        return removed, updated
    return None


def _extend(ring: Sequence[XY], hull_pts: Sequence[XY], covers: CoversFn) -> Ring:
    """
    Drop corners while the ring still covers ``hull_pts`` and ``covers``
    accepts each added patch.

    At each corner the longest span whose extension still lies in the road
    is kept. A curve often rejects both a single corner and the full run,
    while a span in between meets inside the spare road.
    """
    current = list(ring)
    guard = len(current)
    for _ in range(guard):
        n = len(current)
        if n < 5:
            break
        step_i = 1 if n <= 220 else max(1, n // 48)
        step_k = 1 if n <= 220 else max(1, (n - 4) // 80)
        best = None
        for i in range(0, n, step_i):
            found = None
            found_k = 0
            k = n - 4
            while k >= 1:
                got = _cut_at(current, i, k, hull_pts, covers)
                if got is not None:
                    found = got
                    found_k = k
                    break
                k -= step_k
            if found is not None and step_k > 1:
                upper = min(n - 4, found_k + step_k - 1)
                for k in range(upper, found_k, -1):
                    got = _cut_at(current, i, k, hull_pts, covers)
                    if got is not None:
                        found = got
                        break
            if found is not None and (best is None or found[0] > best[0]):
                best = found
        if best is None:
            break
        current = best[1]
    return current


def _bbox(ring: Sequence[XY]) -> Tuple[float, float, float, float]:
    xs = [p[0] for p in ring]
    ys = [p[1] for p in ring]
    return min(xs), min(ys), max(xs), max(ys)


def _bbox_gap(a, b) -> float:
    dx = max(0.0, b[0] - a[2], a[0] - b[2])
    dy = max(0.0, b[1] - a[3], a[1] - b[3])
    return math.hypot(dx, dy)


def _nearby_pairs(indexes: Sequence[int], boxes, gap: float):
    """Index pairs whose bounding boxes are at most ``gap`` apart."""
    cell = max(float(gap), 1.0)
    grid = {}
    for i in indexes:
        box = boxes[i]
        ix0 = int(math.floor((box[0] - gap) / cell))
        iy0 = int(math.floor((box[1] - gap) / cell))
        ix1 = int(math.floor((box[2] + gap) / cell))
        iy1 = int(math.floor((box[3] + gap) / cell))
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                grid.setdefault((ix, iy), []).append(i)
    seen = set()
    for i in indexes:
        box = boxes[i]
        ix0 = int(math.floor((box[0] - gap) / cell))
        iy0 = int(math.floor((box[1] - gap) / cell))
        ix1 = int(math.floor((box[2] + gap) / cell))
        iy1 = int(math.floor((box[3] + gap) / cell))
        for ix in range(ix0, ix1 + 1):
            for iy in range(iy0, iy1 + 1):
                for j in grid.get((ix, iy), ()):
                    if j <= i:
                        continue
                    key = (i, j)
                    if key in seen:
                        continue
                    seen.add(key)
                    dist = _bbox_gap(boxes[i], boxes[j])
                    if dist <= gap + 1e-6:
                        yield dist, i, j


def simplify_bump_set(
    rings: Sequence[Sequence[XY]],
    covers: CoversFn,
    extend: bool = False,
    merge_gap: float = _MERGE_GAP_M,
    on_pairs=None,
    on_extend=None,
) -> List[Tuple[Ring, str, List[int]]]:
    """
    Hull every ring, join nearby hulls that still fit in the road, then
    optionally extend edges.

    Returns ``(ring, status, member_indexes)`` for each output part.
    ``status`` is ``hull``, ``merged``, ``extended``, or ``original``.
    """
    prepared = []
    for ring in rings:
        src = [(float(p[0]), float(p[1])) for p in ring]
        if len(src) >= 2 and _hypot(src[0], src[-1]) <= 1e-9:
            src = src[:-1]
        if len(src) < 3:
            prepared.append({"src": src, "hull": None})
            continue
        hull = convex_hull(src)
        if len(hull) >= 3 and covers(hull):
            prepared.append({"src": src, "hull": hull, "shortened": False})
            continue
        reduced = _drop_bends(src, covers)
        hull2 = convex_hull(reduced)
        if (
            len(hull2) >= 3
            and len(hull2) <= len(reduced)
            and covers(hull2)
            and all(_inside(p, hull2) for p in src)
        ):
            prepared.append({"src": src, "hull": hull2, "shortened": True})
        else:
            prepared.append(
                {
                    "src": reduced,
                    "hull": None,
                    "shortened": len(reduced) < len(src),
                    "full": src,
                }
            )

    n = len(prepared)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    cluster_pts: List[Optional[Ring]] = [
        list(item["hull"]) if item["hull"] is not None else None for item in prepared
    ]
    boxes = []
    for item in prepared:
        pts = item["hull"] if item["hull"] is not None else item["src"]
        boxes.append(_bbox(pts) if len(pts) >= 1 else (0.0, 0.0, 0.0, 0.0))

    mergeable = [i for i in range(n) if prepared[i]["hull"] is not None]
    pairs = list(_nearby_pairs(mergeable, boxes, max(0.0, float(merge_gap))))
    pairs.sort()
    tried = set()
    total_pairs = max(len(pairs), 1)

    def report_pair(pi: int) -> None:
        if on_pairs is not None:
            on_pairs(pi + 1, total_pairs)

    for pi, (_dist, i, j) in enumerate(pairs):
        ri, rj = find(i), find(j)
        if ri == rj or cluster_pts[ri] is None or cluster_pts[rj] is None:
            if pi % 100 == 0 or pi + 1 == len(pairs):
                report_pair(pi)
            continue
        key = (ri, rj) if ri < rj else (rj, ri)
        if key in tried:
            if pi % 100 == 0 or pi + 1 == len(pairs):
                report_pair(pi)
            continue
        tried.add(key)
        # Each road test can take a while once hulls are large. Count it
        # before the test so the bar moves for that pair, not 25 later.
        report_pair(pi)
        hull = convex_hull(cluster_pts[ri] + cluster_pts[rj])
        if len(hull) < 3 or not covers(hull):
            continue
        cluster_pts[ri] = hull
        parent[rj] = ri
        cluster_pts[rj] = None

    roots = [i for i in range(n) if find(i) == i]
    out: List[Tuple[Ring, str, List[int]]] = []
    for ei, i in enumerate(roots):
        if on_extend is not None:
            on_extend(ei + 1, len(roots))
        members = [k for k in range(n) if find(k) == i]
        if cluster_pts[i] is None:
            ring = list(prepared[i]["src"])
            status = "extended" if prepared[i].get("shortened") else "original"
            full = prepared[i].get("full") or ring
            if extend and status == "extended" and len(ring) >= 5:
                extended = _extend(ring, full, covers)
                if len(extended) < len(ring):
                    ring = extended
            out.append((ring, status, members))
            continue
        hull = cluster_pts[i]
        status = "merged" if len(members) > 1 else "hull"
        if extend and len(hull) >= 5:
            extended = _extend(hull, hull, covers)
            if len(extended) < len(hull):
                hull = extended
                status = "extended"
        out.append((hull, status, members))
    return out


def simplify_bump_ring(
    ring: Sequence[XY],
    covers: CoversFn,
    extend: bool = False,
) -> Tuple[Ring, str]:
    """
    Return ``(ring, status)``.

    ``status`` is ``\"hull\"``, ``\"extended\"``, or ``\"original\"``.
    ``covers(ring)`` is true when that ring lies inside the road mask.
    """
    src = [(float(p[0]), float(p[1])) for p in ring]
    if len(src) >= 2 and _hypot(src[0], src[-1]) <= 1e-9:
        src = src[:-1]
    if len(src) < 3:
        return src, "original"
    hull = convex_hull(src)
    if len(hull) < 3 or not covers(hull):
        reduced = _drop_bends(src, covers)
        hull2 = convex_hull(reduced)
        if (
            len(hull2) >= 3
            and len(hull2) <= len(reduced)
            and covers(hull2)
            and all(_inside(p, hull2) for p in src)
        ):
            reduced = hull2
        if extend and len(reduced) >= 5 and len(reduced) < len(src):
            extended = _extend(reduced, src, covers)
            if len(extended) < len(reduced):
                reduced = extended
        if len(reduced) < len(src):
            return reduced, "extended"
        return src, "original"
    if not extend or len(hull) < 5:
        return hull, "hull"
    extended = _extend(hull, hull, covers)
    if len(extended) < len(hull):
        return extended, "extended"
    return hull, "hull"


def _self_test() -> None:
    def square(x0, y0, x1, y1):
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    def contains_ring(container):
        def covers(ring):
            return all(_inside(p, container) for p in ring)
        return covers

    road = square(0.0, 0.0, 100.0, 40.0)
    notch = [(10, 10), (30, 10), (30, 18), (18, 18), (18, 25), (10, 25)]
    hull_only, status = simplify_bump_ring(notch, contains_ring(road), extend=False)
    assert status == "hull", status
    assert len(hull_only) == 5, hull_only
    assert (18.0, 18.0) not in [(round(p[0], 6), round(p[1], 6)) for p in hull_only]
    assert all(_inside(p, hull_only) for p in notch)
    assert contains_ring(road)(hull_only)

    def ngon(cx, cy, r, n):
        return [
            (
                cx + r * math.cos(2.0 * math.pi * i / n),
                cy + r * math.sin(2.0 * math.pi * i / n),
            )
            for i in range(n)
        ]

    octa = ngon(50.0, 20.0, 4.0, 8)
    plain, plain_status = simplify_bump_ring(octa, contains_ring(road), extend=False)
    assert plain_status == "hull" and len(plain) == 8
    shrunk, shrunk_status = simplify_bump_ring(octa, contains_ring(road), extend=True)
    assert shrunk_status == "extended", shrunk_status
    assert len(shrunk) < 8, shrunk
    assert all(_inside(p, shrunk) for p in octa)
    assert contains_ring(road)(shrunk)

    # Extensions that leave the octagon itself are rejected.
    tight, tight_status = simplify_bump_ring(octa, contains_ring(octa), extend=True)
    assert tight_status == "hull" and len(tight) == 8, (tight_status, tight)

    # A fine curve: the one-vertex patch is a sliver and the full run meets
    # outside the road. A medium span still fits and drops the chain.
    curve = []
    for i in range(28):
        ang = math.radians(15.0 + 70.0 * i / 27.0)
        curve.append((200.0 * math.cos(ang), 200.0 * math.sin(ang)))
    curve.append((80.0, 40.0))

    def curve_covers(ring):
        for x, y in ring:
            if math.hypot(x, y) > 200.55:
                return False
        area = 0.0
        for i, (x1, y1) in enumerate(ring):
            x2, y2 = ring[(i + 1) % len(ring)]
            area += x1 * y2 - x2 * y1
        return abs(area) >= 40.0

    assert curve_covers(curve)
    curved, curved_status = simplify_bump_ring(curve, curve_covers, extend=True)
    assert curved_status == "extended", curved_status
    assert len(curved) < 20, len(curved)
    assert all(_inside(p, curved) for p in curve)
    assert curve_covers(curved)

    # Hull crosses the missing corner of an L, so the original ring is kept.
    ell = [(0, 0), (30, 0), (30, 8), (8, 8), (8, 30), (0, 30)]
    blob = ngon(8.0, 8.0, 6.0, 8)
    kept, kept_status = simplify_bump_ring(blob, contains_ring(ell), extend=True)
    assert kept_status == "original", kept_status
    assert len(kept) == len(blob)

    def square_at(x0, y0, x1, y1):
        return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]

    wide = square_at(0.0, 0.0, 120.0, 40.0)
    left = square_at(10.0, 10.0, 40.0, 30.0)
    right = square_at(55.0, 12.0, 90.0, 28.0)
    joined = simplify_bump_set([left, right], contains_ring(wide), extend=False)
    assert len(joined) == 1, len(joined)
    assert joined[0][1] == "merged", joined[0][1]
    assert all(_inside(p, joined[0][0]) for p in left + right)
    assert contains_ring(wide)(joined[0][0])

    def covers_ell(ring):
        if not all(_inside(p, ell) for p in ring):
            return False
        for i, a in enumerate(ring):
            b = ring[(i + 1) % len(ring)]
            mid = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
            if not _inside(mid, ell):
                return False
        return True

    arm_a = square_at(1.0, 16.0, 6.0, 26.0)
    arm_b = square_at(16.0, 1.0, 26.0, 6.0)
    apart = simplify_bump_set([arm_a, arm_b], covers_ell, extend=False)
    assert len(apart) == 2, len(apart)

    # Hull of this L-shaped bump cuts the missing corner, so it is rejected.
    # Extra samples along the edges drop back off, leaving the six corners.
    def covers_poly(container):
        def covers(ring):
            if not all(_inside(p, container) for p in ring):
                return False
            for i, a in enumerate(ring):
                b = ring[(i + 1) % len(ring)]
                mid = ((a[0] + b[0]) * 0.5, (a[1] + b[1]) * 0.5)
                if not _inside(mid, container):
                    return False
            return True
        return covers

    corner = [(2.0, 2.0), (22.0, 2.0), (22.0, 6.0), (7.0, 6.0), (7.0, 22.0), (2.0, 22.0)]
    dense = []
    for i, a in enumerate(corner):
        b = corner[(i + 1) % len(corner)]
        for k in range(6):
            t = k / 6.0
            dense.append((a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t))
    bent, bent_status = simplify_bump_ring(dense, covers_poly(ell), extend=False)
    assert bent_status == "extended", bent_status
    assert len(bent) <= 8, bent
    assert all(_inside(p, bent) for p in dense)
    assert covers_poly(ell)(bent)
    print("bump_simplify_core self_test ok")


if __name__ == "__main__":
    _self_test()
