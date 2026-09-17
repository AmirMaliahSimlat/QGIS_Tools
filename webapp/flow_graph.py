# -*- coding: utf-8 -*-
"""Hardcoded run-order graph (DAG) for the configure / run stages."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from catalog_loader import tool_by_id

Edge = Dict[str, str]  # from, from_param, to, to_param

_OUT_TYPES = frozenset({"vector_output", "folder_output", "raster_output"})
_IN_TYPES = frozenset({"vector_file", "folder", "raster_file"})
_COMPAT = {
    "vector_output": frozenset({"vector_file"}),
    "folder_output": frozenset({"folder"}),
    "raster_output": frozenset({"raster_file"}),
}

# Fixed pipelines. Each node is (tool_id, input_param_or_None).
# When tools are skipped, we wire the nearest selected upstream producer whose
# output domain matches the consumer input (no footprint→points jumps).
_WIRE_CHAINS: Tuple[Tuple[Tuple[str, Optional[str]], ...], ...] = (
    (
        ("layers_alignment", None),
        ("water_outline_points", "INPUT_POLYGONS"),
    ),
    (
        ("layers_alignment", None),
        ("tree_mask_to_points", "INPUT_POLYGONS"),
        ("thin_tree_points", "INPUT_POINTS"),
        ("sample_tree_rgb", "INPUT_POINTS"),
    ),
    (
        ("layers_alignment", None),
        ("building_altitude_and_height", "INPUT_BUILDINGS"),
        ("assign_roof_type", "INPUT_BUILDINGS"),
    ),
    # polygon_mask_points (roads) stays alone — no chain entry.
)


def output_ports(tool: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [p for p in tool.get("params") or [] if p.get("type") in _OUT_TYPES]


def input_ports(tool: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [p for p in tool.get("params") or [] if p.get("type") in _IN_TYPES]


def ports_compatible(out_param: Dict[str, Any], in_param: Dict[str, Any]) -> bool:
    """True when structural kinds match and domain/library file types match."""
    out_t = out_param.get("type")
    in_t = in_param.get("type")
    if in_t not in _COMPAT.get(out_t, ()):
        return False
    out_key = port_domain_key(out_param)
    in_key = port_domain_key(in_param)
    if out_key or in_key:
        return bool(out_key) and out_key == in_key
    return True


def port_kind(param: Dict[str, Any]) -> str:
    """Short kind label for UI (vector / folder / raster)."""
    t = param.get("type") or ""
    if t.startswith("vector"):
        return "vector"
    if t.startswith("folder"):
        return "folder"
    if t.startswith("raster"):
        return "raster"
    return t or "file"


def port_domain_key(param: Dict[str, Any]) -> Optional[str]:
    """Catalog domain/library key for a file port, if any."""
    key = param.get("domain") or param.get("library")
    return str(key) if key else None


def port_file_type(
    catalog: Dict[str, Any],
    param: Dict[str, Any],
) -> str:
    """
    Human file-type name for Order-stage ports (e.g. \"Tree points\").

    Prefer domain_labels, then domain path, then param label.
    """
    key = port_domain_key(param)
    if key:
        labels = catalog.get("domain_labels") or {}
        if key in labels:
            return str(labels[key])
        domains = catalog.get("domains") or {}
        rel = domains.get(key)
        if isinstance(rel, str) and rel.strip():
            return rel.replace("\\", "/").replace("/", " ").replace("_", " ").title()
        return key.replace("_", " ").title()
    return str(param.get("label") or param.get("id") or "?")


def param_by_id(tool: Dict[str, Any], param_id: str) -> Optional[Dict[str, Any]]:
    for p in tool.get("params") or []:
        if p.get("id") == param_id:
            return p
    return None


def would_create_cycle(
    edges: Iterable[Edge],
    new_edge: Edge,
) -> bool:
    """True if adding new_edge would introduce a directed cycle."""
    src, dst = new_edge["from"], new_edge["to"]
    if src == dst:
        return True
    succ: Dict[str, Set[str]] = defaultdict(set)
    for e in edges:
        if e["to"] == new_edge["to"] and e["to_param"] == new_edge["to_param"]:
            continue  # replaced by add_edge
        succ[e["from"]].add(e["to"])
    succ[src].add(dst)
    # Reachability from dst back to src
    seen: Set[str] = set()
    stack = [dst]
    while stack:
        u = stack.pop()
        if u == src:
            return True
        if u in seen:
            continue
        seen.add(u)
        stack.extend(succ.get(u) or ())
    return False


def validate_edge(
    catalog: Dict[str, Any],
    edges: Iterable[Edge],
    new_edge: Edge,
) -> Optional[str]:
    """
    Return an error message if the edge is invalid, else None.

    Rules: distinct tools, same file type (domain/library), vector↔vector /
    folder↔folder, no cycles.
    """
    src_id, dst_id = new_edge.get("from"), new_edge.get("to")
    if not src_id or not dst_id:
        return "Incomplete connection."
    if src_id == dst_id:
        return "Cannot link a tool to itself."
    src_tool = tool_by_id(catalog, src_id)
    dst_tool = tool_by_id(catalog, dst_id)
    if not src_tool or not dst_tool:
        return "Unknown tool in connection."
    sparam = param_by_id(src_tool, new_edge.get("from_param") or "")
    dparam = param_by_id(dst_tool, new_edge.get("to_param") or "")
    if sparam is None or sparam.get("type") not in _OUT_TYPES:
        return "Source must be a file output port."
    if dparam is None or dparam.get("type") not in _IN_TYPES:
        return "Target must be a file input port."
    if not ports_compatible(sparam, dparam):
        src_ft = port_file_type(catalog, sparam)
        dst_ft = port_file_type(catalog, dparam)
        if port_kind(sparam) != port_kind(dparam):
            return (
                f"Incompatible types: {port_kind(sparam)} output cannot feed "
                f"{port_kind(dparam)} input."
            )
        return f"Incompatible file types: {src_ft} cannot feed {dst_ft}."
    if would_create_cycle(edges, new_edge):
        return "That link would create a cycle."
    return None


def default_edges_from_catalog(
    catalog: Dict[str, Any],
    selected_ids: Set[str],
) -> List[Edge]:
    """No automatic wiring — tools start disconnected."""
    return []


def pipeline_groups(
    catalog: Dict[str, Any],
    selected_ids: Set[str],
) -> List[Dict[str, Any]]:
    """
    Selected tools grouped by catalog pipeline (unrelated chains stay apart).

    Orphans (not in any pipeline) each become their own group, in catalog order.
    """
    groups: List[Dict[str, Any]] = []
    placed: Set[str] = set()
    for pipeline in catalog.get("pipelines") or []:
        ids = [
            s["id"]
            for s in (pipeline.get("steps") or [])
            if s.get("id") in selected_ids
        ]
        if not ids:
            continue
        groups.append(
            {
                "id": pipeline.get("id") or "pipeline",
                "label": pipeline.get("label") or pipeline.get("id") or "Pipeline",
                "tool_ids": ids,
            }
        )
        placed.update(ids)
    for tool in catalog.get("tools") or []:
        tid = tool["id"]
        if tid in selected_ids and tid not in placed and not tool.get("placeholder"):
            groups.append(
                {
                    "id": tid,
                    "label": tool.get("display_name") or tid,
                    "tool_ids": [tid],
                }
            )
    return groups


def prune_edges(edges: Iterable[Edge], selected_ids: Set[str]) -> List[Edge]:
    out: List[Edge] = []
    seen = set()
    for e in edges:
        frm, to = e.get("from"), e.get("to")
        if frm not in selected_ids or to not in selected_ids:
            continue
        key = (frm, e.get("from_param"), to, e.get("to_param"))
        if key in seen:
            continue
        seen.add(key)
        out.append(
            {
                "from": str(frm),
                "from_param": str(e.get("from_param") or ""),
                "to": str(to),
                "to_param": str(e.get("to_param") or ""),
            }
        )
    return out


def hardcoded_edges(
    catalog: Dict[str, Any],
    selected_ids: Set[str],
) -> List[Edge]:
    """
    Build fixed pipeline wires for the current selection.

    For each chain, keep selected tools in order. For every consumer, attach the
    nearest upstream selected producer with a compatible domain/library type.
    """
    selected_ids = set(selected_ids)
    edges: List[Edge] = []
    seen: Set[Tuple[str, str, str, str]] = set()

    for chain in _WIRE_CHAINS:
        nodes = [(tid, ip) for tid, ip in chain if tid in selected_ids]
        if len(nodes) < 2:
            continue
        for i, (to_id, to_param) in enumerate(nodes):
            if not to_param:
                continue
            to_tool = tool_by_id(catalog, to_id)
            in_param = param_by_id(to_tool, to_param) if to_tool else None
            if not to_tool or in_param is None:
                continue
            for j in range(i - 1, -1, -1):
                from_id, _ = nodes[j]
                from_tool = tool_by_id(catalog, from_id)
                if not from_tool:
                    continue
                for out_param in output_ports(from_tool):
                    if not ports_compatible(out_param, in_param):
                        continue
                    key = (from_id, out_param["id"], to_id, to_param)
                    if key in seen:
                        break
                    seen.add(key)
                    edges.append(
                        {
                            "from": from_id,
                            "from_param": out_param["id"],
                            "to": to_id,
                            "to_param": to_param,
                        }
                    )
                    break
                else:
                    continue
                break
    return edges


def sync_flow_edges(
    catalog: Dict[str, Any],
    selected_ids: Set[str],
    edges: List[Edge],
    seeded_tools: Set[str],
) -> Tuple[List[Edge], Set[str]]:
    """Replace user wires with the hardcoded selection-aware graph."""
    selected_ids = set(selected_ids)
    return hardcoded_edges(catalog, selected_ids), set(selected_ids)


def wires_from_edges(
    edges: Iterable[Edge],
) -> Dict[str, Dict[str, Tuple[str, str]]]:
    """tool_id -> {input_param: (src_tool, src_output_param)}."""
    wires: Dict[str, Dict[str, Tuple[str, str]]] = {}
    for e in edges:
        wires.setdefault(e["to"], {})[e["to_param"]] = (e["from"], e["from_param"])
    return wires


def pipeline_output_tiers(
    catalog: Dict[str, Any],
    selected_ids: Set[str],
    edges: Iterable[Edge],
) -> Dict[Tuple[str, str], str]:
    """
    Map (tool_id, output_param_id) → ``staging`` | ``final`` for the selection.

    If an output is wired into a selected downstream tool that itself produces
    the same domain/file type, the upstream output defaults to ``staging``.
    Otherwise it defaults to ``final``. ``tests`` is never returned.
    """
    selected_ids = set(selected_ids)
    intermediate: Set[Tuple[str, str]] = set()

    for e in edges:
        from_id = e.get("from") or ""
        to_id = e.get("to") or ""
        from_param_id = e.get("from_param") or ""
        if from_id not in selected_ids or to_id not in selected_ids:
            continue
        from_tool = tool_by_id(catalog, from_id)
        to_tool = tool_by_id(catalog, to_id)
        if not from_tool or not to_tool:
            continue
        out_param = param_by_id(from_tool, from_param_id)
        if out_param is None:
            continue
        domain = port_domain_key(out_param)
        if not domain:
            continue
        if any(port_domain_key(op) == domain for op in output_ports(to_tool)):
            intermediate.add((from_id, from_param_id))

    result: Dict[Tuple[str, str], str] = {}
    for tid in selected_ids:
        tool = tool_by_id(catalog, tid)
        if not tool or tool.get("placeholder"):
            continue
        for op in output_ports(tool):
            key = (tid, str(op.get("id") or ""))
            if not key[1]:
                continue
            result[key] = "staging" if key in intermediate else "final"
    return result


def _undirected_components(
    tool_ids: List[str],
    edges: Iterable[Edge],
) -> List[List[str]]:
    ids = list(tool_ids)
    id_set = set(ids)
    adj: Dict[str, Set[str]] = {t: set() for t in ids}
    for e in edges:
        a, b = e["from"], e["to"]
        if a in id_set and b in id_set:
            adj[a].add(b)
            adj[b].add(a)
    seen: Set[str] = set()
    comps: List[List[str]] = []
    for t in ids:
        if t in seen:
            continue
        stack = [t]
        seen.add(t)
        comp = []
        while stack:
            u = stack.pop()
            comp.append(u)
            for v in adj[u]:
                if v not in seen:
                    seen.add(v)
                    stack.append(v)
        # Preserve catalog/pipeline relative order inside the component.
        order = {x: i for i, x in enumerate(ids)}
        comp.sort(key=lambda x: order[x])
        comps.append(comp)
    return comps


def topo_sort_component(
    tool_ids: List[str],
    edges: Iterable[Edge],
) -> List[str]:
    """Kahn topo-sort; falls back to given order on cycles."""
    id_set = set(tool_ids)
    indeg = {t: 0 for t in tool_ids}
    succ: Dict[str, List[str]] = defaultdict(list)
    for e in edges:
        a, b = e["from"], e["to"]
        if a in id_set and b in id_set and a != b:
            succ[a].append(b)
            indeg[b] += 1
    q = deque([t for t in tool_ids if indeg[t] == 0])
    out: List[str] = []
    while q:
        u = q.popleft()
        out.append(u)
        for v in succ[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    if len(out) != len(tool_ids):
        # Cycle — keep stable original order for leftovers.
        left = [t for t in tool_ids if t not in out]
        out.extend(left)
    return out


def run_order_groups(
    catalog: Dict[str, Any],
    selected_ids: Set[str],
    edges: Iterable[Edge],
    manual_order: Optional[List[str]] = None,
) -> List[Dict[str, Any]]:
    """
    Separate run-order groups by connectivity; order tools (and groups) by
    ``manual_order`` when provided, else catalog pipeline order. Edge
    constraints still win via topo preference.
    """
    selected_ids = set(selected_ids)
    base = pipeline_groups(catalog, selected_ids)
    tool_label: Dict[str, str] = {}
    for g in base:
        for tid in g["tool_ids"]:
            tool_label[tid] = g["label"]

    preferred = sync_manual_order(catalog, selected_ids, manual_order)
    preferred = stabilize_order(preferred, edges)

    comps = _undirected_components(preferred, edges)
    pos = {t: i for i, t in enumerate(preferred)}
    comps.sort(key=lambda c: min(pos.get(t, 10**9) for t in c))

    groups: List[Dict[str, Any]] = []
    for i, comp in enumerate(comps, start=1):
        ordered = stabilize_order(
            sorted(comp, key=lambda t: pos.get(t, 10**9)),
            edges,
        )
        labels = []
        for tid in ordered:
            lab = tool_label.get(tid)
            if lab and lab not in labels:
                labels.append(lab)
        groups.append(
            {
                "id": f"run-{i}",
                "label": " + ".join(labels) if labels else f"Run {i}",
                "tool_ids": ordered,
            }
        )
    return groups


def flat_run_order(
    catalog: Dict[str, Any],
    selected_ids: Set[str],
    edges: Iterable[Edge],
    manual_order: Optional[List[str]] = None,
) -> List[str]:
    """All tools in execution order (DAG layers, left→right then top→bottom)."""
    layers, _cols = dag_layout(catalog, selected_ids, edges, manual_order)
    return [tid for row in layers for tid in row]


def dag_layers(
    catalog: Dict[str, Any],
    selected_ids: Set[str],
    edges: Iterable[Edge],
    manual_order: Optional[List[str]] = None,
) -> List[List[str]]:
    """Layer lists only (see ``dag_layout`` for column alignment)."""
    layers, _cols = dag_layout(catalog, selected_ids, edges, manual_order)
    return layers


def dag_layout(
    catalog: Dict[str, Any],
    selected_ids: Set[str],
    edges: Iterable[Edge],
    manual_order: Optional[List[str]] = None,
) -> Tuple[List[List[str]], Dict[str, float]]:
    """
    Layered DAG with vertical alignment:

    - Rank 0 = roots; Rank(v) = 1 + max(parent ranks)
    - Children of one parent are left→right in that parent's output-port order
    - Children are tightly packed (normal adjacent gaps) and centered on the parent:
      odd count → middle child under the parent;
      even count → mid-gap between the middle pair sits under the parent
      (parent stays on the roots row; children may use half-columns)
    """
    preferred = sync_manual_order(catalog, selected_ids, manual_order)
    if not preferred:
        return [], {}
    edges = list(edges)
    order = stabilize_order(preferred, edges)
    id_set = set(order)
    preds: Dict[str, Set[str]] = {t: set() for t in order}
    succs: Dict[str, Set[str]] = {t: set() for t in order}
    inbound: Dict[str, List[Edge]] = defaultdict(list)
    for e in edges:
        a, b = e["from"], e["to"]
        if a in id_set and b in id_set and a != b:
            preds[b].add(a)
            succs[a].add(b)
            inbound[b].append(e)

    out_index_cache: Dict[Tuple[str, str], int] = {}
    fallback = {t: i for i, t in enumerate(preferred)}

    def out_index(tool_id: str, param_id: str) -> int:
        key = (tool_id, param_id)
        if key in out_index_cache:
            return out_index_cache[key]
        tool = tool_by_id(catalog, tool_id)
        idx = 10**6
        if tool:
            for i, p in enumerate(output_ports(tool)):
                if p.get("id") == param_id:
                    idx = i
                    break
        out_index_cache[key] = idx
        return idx

    def edge_out_index(child: str, parent: str) -> int:
        best = 10**6
        for e in inbound.get(child) or ():
            if e["from"] == parent:
                best = min(best, out_index(parent, e.get("from_param") or ""))
        return best

    rank: Dict[str, int] = {t: 0 for t in order}
    for t in order:
        if preds[t]:
            rank[t] = 1 + max(rank[p] for p in preds[t])

    max_r = max(rank.values()) if rank else 0
    layers: List[List[str]] = [[] for _ in range(max_r + 1)]
    for t in order:
        layers[rank[t]].append(t)
    for layer in layers:
        layer.sort(key=lambda t: fallback.get(t, 0))

    x: Dict[str, float] = {}
    for i, t in enumerate(layers[0]):
        x[t] = float(i)

    def _pack_siblings(parent: str, kids: List[str]) -> None:
        """Center kids under parent; never move the parent (avoids root-row collisions)."""
        n = len(kids)
        if n == 0:
            return
        kids = sorted(
            kids,
            key=lambda t: (edge_out_index(t, parent), fallback.get(t, 0)),
        )
        px = x.get(parent, 0.0)
        # Consecutive slots, mid-gap (even) or middle kid (odd) under parent.
        start = px - (n - 1) / 2.0
        for i, t in enumerate(kids):
            x[t] = start + float(i)

    def _ideals_for_layer(layer: List[str]) -> Dict[str, float]:
        ideals: Dict[str, float] = {}
        by_parent: Dict[str, List[str]] = defaultdict(list)
        multi: List[str] = []
        for t in layer:
            ps = preds.get(t) or set()
            if len(ps) == 1:
                by_parent[next(iter(ps))].append(t)
            else:
                multi.append(t)
        for p, kids in by_parent.items():
            kids_sorted = sorted(
                kids,
                key=lambda t: (edge_out_index(t, p), fallback.get(t, 0)),
            )
            n = len(kids_sorted)
            px = x.get(p, 0.0)
            start = px - (n - 1) / 2.0
            for i, t in enumerate(kids_sorted):
                ideals[t] = start + float(i)
        for t in multi:
            ps = [x[p] for p in (preds.get(t) or ()) if p in x]
            if ps:
                ois = [
                    out_index(e["from"], e.get("from_param") or "")
                    for e in (inbound.get(t) or ())
                    if e["from"] in x
                ]
                nudge = (min(ois) * 0.01) if ois else 0.0
                ideals[t] = (sum(ps) / len(ps)) + nudge
            else:
                ideals[t] = float(fallback.get(t, 0))
        return ideals

    def _place_layer(layer: List[str]) -> None:
        ideals = _ideals_for_layer(layer)
        items = sorted(
            layer,
            key=lambda t: (ideals.get(t, 0.0), fallback.get(t, 0)),
        )
        placed: List[float] = []
        for t in items:
            ideal = ideals.get(t, float(fallback.get(t, 0)))
            if not placed:
                px = ideal
            else:
                px = max(ideal, placed[-1] + 1.0)
            x[t] = px
            placed.append(px)
        by_parent: Dict[str, List[str]] = defaultdict(list)
        for t in layer:
            ps = preds.get(t) or set()
            if len(ps) == 1:
                by_parent[next(iter(ps))].append(t)
        for p, kids in by_parent.items():
            _pack_siblings(p, kids)
        # Resolve collisions without yanking parents off the root row.
        items = sorted(layer, key=lambda t: (x[t], fallback.get(t, 0)))
        placed = []
        for t in items:
            px = x[t]
            if placed:
                px = max(px, placed[-1] + 1.0)
            x[t] = px
            placed.append(px)
        layer[:] = sorted(layer, key=lambda t: (x[t], fallback.get(t, 0)))

    for r in range(1, len(layers)):
        _place_layer(layers[r])

    cols: Dict[str, float] = {t: float(x[t]) for row in layers for t in row}
    if cols:
        shift = min(cols.values())
        if shift:
            for t in cols:
                cols[t] -= shift

    layers = [row for row in layers if row]
    return layers, cols



def sync_manual_order(
    catalog: Dict[str, Any],
    selected_ids: Set[str],
    order: Optional[Iterable[str]],
) -> List[str]:
    """Keep user order for still-selected tools; append new ones in catalog order."""
    selected_ids = set(selected_ids)
    defaults: List[str] = []
    for g in pipeline_groups(catalog, selected_ids):
        defaults.extend(g["tool_ids"])
    out: List[str] = []
    seen: Set[str] = set()
    for tid in order or ():
        if tid in selected_ids and tid not in seen:
            out.append(tid)
            seen.add(tid)
    for tid in defaults:
        if tid not in seen:
            out.append(tid)
            seen.add(tid)
    return out


def stabilize_order(
    order: List[str],
    edges: Iterable[Edge],
) -> List[str]:
    """
    Topo-sort respecting edges; among ready nodes pick the earliest in ``order``.
    """
    if not order:
        return []
    id_set = set(order)
    indeg = {t: 0 for t in order}
    succ: Dict[str, List[str]] = defaultdict(list)
    for e in edges:
        a, b = e["from"], e["to"]
        if a in id_set and b in id_set and a != b:
            succ[a].append(b)
            indeg[b] += 1
    pos = {t: i for i, t in enumerate(order)}
    remaining = set(order)
    out: List[str] = []
    while remaining:
        ready = [t for t in remaining if indeg[t] == 0]
        if not ready:
            out.extend(sorted(remaining, key=lambda t: pos[t]))
            break
        ready.sort(key=lambda t: pos[t])
        u = ready[0]
        out.append(u)
        remaining.remove(u)
        for v in succ[u]:
            indeg[v] -= 1
    return out


def order_respects_edges(
    order: List[str],
    edges: Iterable[Edge],
) -> Optional[str]:
    """Error message if any producer is placed after its consumer."""
    pos = {t: i for i, t in enumerate(order)}
    for e in edges:
        a, b = e["from"], e["to"]
        if a in pos and b in pos and pos[a] >= pos[b]:
            return (
                f"“{a}” feeds “{b}”, so it must stay above it in the run order."
            )
    return None


def move_tool_in_order(
    order: List[str],
    tool_id: str,
    delta: int,
    edges: Iterable[Edge],
) -> Tuple[List[str], Optional[str]]:
    """
    Swap ``tool_id`` with the neighbor at ``delta`` (-1 up, +1 down).
    Rejects moves that would put a consumer above its producer.
    """
    if tool_id not in order:
        return list(order), "Tool is not in the run order."
    i = order.index(tool_id)
    j = i + int(delta)
    if j < 0 or j >= len(order):
        return list(order), None
    new_order = list(order)
    new_order[i], new_order[j] = new_order[j], new_order[i]
    err = order_respects_edges(new_order, edges)
    if err:
        return list(order), err
    return new_order, None


def ensure_order_for_edge(
    order: List[str],
    new_edge: Edge,
) -> List[str]:
    """After linking, ensure producer sits above consumer."""
    src, dst = new_edge["from"], new_edge["to"]
    if src not in order or dst not in order or src == dst:
        return list(order)
    out = list(order)
    si, di = out.index(src), out.index(dst)
    if si < di:
        return out
    out.pop(si)
    di = out.index(dst)
    out.insert(di, src)
    return out


def edge_key(e: Edge) -> Tuple[str, str, str, str]:
    return (e["from"], e["from_param"], e["to"], e["to_param"])


def add_edge(edges: List[Edge], new_edge: Edge) -> List[Edge]:
    """Add edge; replace any existing wire into the same input port."""
    edges = [
        e
        for e in edges
        if not (e["to"] == new_edge["to"] and e["to_param"] == new_edge["to_param"])
    ]
    edges.append(
        {
            "from": new_edge["from"],
            "from_param": new_edge["from_param"],
            "to": new_edge["to"],
            "to_param": new_edge["to_param"],
        }
    )
    return edges


def remove_edge(edges: List[Edge], target: Edge) -> List[Edge]:
    key = edge_key(target)
    return [e for e in edges if edge_key(e) != key]
