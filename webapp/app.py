# -*- coding: utf-8 -*-
"""
Local browser UI for QGIS Processing tools.

Run:  python webapp/app.py
Open: http://127.0.0.1:8080
"""

from __future__ import annotations

import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import quote

_WEBAPP_DIR = Path(__file__).resolve().parent
if str(_WEBAPP_DIR) not in sys.path:
    sys.path.insert(0, str(_WEBAPP_DIR))

from nicegui import app, ui

from catalog_loader import (
    DEFAULT_DATABASE,
    OUTPUT_TIERS,
    VECTOR_OUTPUT_EXT,
    default_map_id,
    default_output_spec,
    domain_tiers,
    ensure_database_layout,
    library_choices,
    list_maps,
    load_catalog,
    map_root,
    output_name_stem,
    resolve_output_path,
    resolve_pipeline,
    tool_by_id,
    tools_for_tab,
)
from qgis_runner import find_qgis_process, run_queue
import user_defaults

CATALOG = load_catalog()
ensure_database_layout(DEFAULT_DATABASE, CATALOG)
app.add_static_files("/static", str(_WEBAPP_DIR / "static"))

SECRET_ICONS_DIR = _WEBAPP_DIR / "catalog" / "Secret Icons"
app.add_static_files("/secret-icons", str(SECRET_ICONS_DIR))


def _secret_icon_url(filename: str) -> str:
    return "/secret-icons/" + quote(filename)


# Inside-joke MapleStory tab icons (from webapp/catalog/Secret Icons)
MAPLE_ICONS = {
    "trees": {"src": _secret_icon_url("MS Stump.gif"), "name": "Stump", "px": 65},
    "buildings": {"src": _secret_icon_url("MS Zakum.gif"), "name": "Zakum", "px": 63},
    "roads": {"src": _secret_icon_url("MS Trucker.gif"), "name": "Trucker", "px": 42},
    "water": {"src": _secret_icon_url("MS Pianus.png"), "name": "Pianus", "px": 63},
    "common": {"src": _secret_icon_url("MS Mushmom.gif"), "name": "Mushmom", "px": 42},
}


def _maple_meta(tab_id: str) -> Dict[str, Any]:
    return MAPLE_ICONS[tab_id]


def _maple_img_html(tab_id: str, *, px: Optional[int] = None) -> str:
    meta = _maple_meta(tab_id)
    size = int(px if px is not None else meta["px"])
    src = meta["src"]
    name = meta["name"]
    return (
        f'<img class="qt-maple-icon" src="{src}" alt="{name}" title="{name}" '
        f'width="{size}" height="{size}" '
        f'style="width:{size}px;height:{size}px;max-width:{size}px;max-height:{size}px;'
        f'object-fit:contain;display:inline-block;flex-shrink:0;" />'
    )


# Session-ish state (single-user local app)
state: Dict[str, Any] = {
    "database": str(DEFAULT_DATABASE),  # Database root
    "map": None,  # set on Configure before tool params show
    "selected": set(),
    "page": "select",
    "values": {},
    "qgis_bat": None,
    "log_lines": [],
    "maple_mode": False,
    "run_progress": {
        "overall_pct": 0.0,
        "job_pct": 0.0,
        "job_index": 0,
        "job_count": 0,
        "job_label": "",
        "stage": "",
        "running": False,
        "done": False,
        "ok": None,
        "sub_cur": None,
        "sub_total": None,
        "sub_unit": "",
        "sub_detail": "",
    },
}

# Input params whose values are paths under Database/<map>/…
# (Outputs keep tier/name; only the map folder prefix changes.)
_MAP_SCOPED_TYPES = frozenset({"vector_file", "folder"})


def _map_chosen() -> bool:
    return bool(state.get("map"))


def _reset_map_scoped_values() -> None:
    """Reset only map-tied input file/folder fields; keep numbers, enums, outputs."""
    for tool in CATALOG.get("tools") or []:
        tid = tool["id"]
        vals = state["values"].get(tid)
        if not isinstance(vals, dict):
            continue
        for p in tool.get("params") or []:
            if p.get("type") not in _MAP_SCOPED_TYPES:
                continue
            pid = p["id"]
            if "default" in p:
                vals[pid] = p["default"]
            else:
                vals[pid] = None


def _set_active_map(mid: Optional[str]) -> None:
    """Switch active map; reset only map-scoped file fields when leaving a map."""
    mid = (mid or "").strip() or None
    prev = state.get("map")
    if mid == prev:
        return
    state["map"] = mid
    if prev:
        # Leaving a map: drop paths that pointed under the old map folder
        _reset_map_scoped_values()
    if mid:
        ensure_database_layout(_db_root(), CATALOG, map_id=mid)
    render_body.refresh()


def render_map_picker() -> None:
    """Map selector — first configure input; scopes all library file lists."""
    maps = list_maps(_db_root(), CATALOG)
    map_labels = {m["id"]: m["label"] for m in maps}
    map_ids = [m["id"] for m in maps]
    current = state.get("map") if state.get("map") in map_ids else None

    with ui.column().classes("w-full gap-1"):
        ui.label("Map *").classes("text-caption text-grey-5")
        map_sel = (
            ui.select(
                options=map_labels,
                value=current,
                label=None,
            )
            .props(_field_props() + " clearable")
            .classes("w-full")
        )

        def on_map(e) -> None:
            mid = e.value
            if mid == state.get("map"):
                return
            _set_active_map(str(mid) if mid else None)

        map_sel.on_value_change(on_map)


def render_header() -> None:
    with ui.header().classes("qt-header items-center justify-between"):
        with ui.row().classes("qt-brand q-ml-sm items-center"):
            ui.label("QG").classes("qt-mark")
            with ui.column().classes("gap-0"):
                ui.label("QGIS Tools").classes("qt-brand-title")
                ui.label("local process console").classes("qt-brand-sub")
        with ui.row().classes("items-center q-mr-md gap-2"):
            ui.label("DB_ROOT").classes("qt-brand-sub")
            db_input = (
                ui.input(value=state["database"])
                .props(_field_props())
                .classes("w-72")
            )

            def apply_db() -> None:
                state["database"] = db_input.value.strip() or str(DEFAULT_DATABASE)
                ensure_database_layout(_db_root(), CATALOG)
                maps_now = list_maps(_db_root(), CATALOG)
                ids = [m["id"] for m in maps_now]
                if state.get("map") and state.get("map") not in ids:
                    state["map"] = None
                    _reset_map_scoped_values()
                ui.notify(f"Database → {state['database']}", type="positive")
                render_body.refresh()

            db_input.on("keydown.enter", apply_db)
            ui.button(icon="folder_open", on_click=apply_db).props(
                "flat dense round color=teal-4"
            ).tooltip("Apply database root")


PAGE_META = {
    "select": {"step": 1, "kicker": "pipeline // select", "title": "Choose processing modules"},
    "configure": {"step": 2, "kicker": "pipeline // configure", "title": "Wire inputs & parameters"},
    "run": {"step": 3, "kicker": "pipeline // execute", "title": "qgis_process queue"},
}


def _db_root() -> Path:
    return Path(state["database"])


def _db() -> Path:
    """Active map folder: Database/<map>/."""
    mid = state.get("map")
    if not mid:
        raise ValueError("Choose a map before resolving Database paths.")
    return map_root(_db_root(), str(mid))


def _try_find_qgis() -> str:
    try:
        p = find_qgis_process()
        state["qgis_bat"] = str(p)
        return str(p)
    except FileNotFoundError as exc:
        state["qgis_bat"] = None
        return str(exc)


def _toggle_tool(tool_id: str, checked: bool) -> None:
    """Update selection without rebuilding the page."""
    tool = tool_by_id(CATALOG, tool_id)
    if tool and tool.get("placeholder"):
        return
    selected = state["selected"]
    if checked:
        selected.add(tool_id)
    else:
        selected.discard(tool_id)

    card = (state.get("_tool_cards") or {}).get(tool_id)
    if card is not None:
        if checked:
            card.classes(add="is-selected")
        else:
            card.classes(remove="is-selected")

    count_el = state.get("_selected_count_el")
    if count_el is not None:
        count_el.set_text(f"{len(selected)} selected")

    queue_refresh = state.get("_queue_panel_refresh")
    if callable(queue_refresh):
        queue_refresh()


def _init_defaults_for(tool: Dict[str, Any]) -> None:
    tid = tool["id"]
    if tid in state["values"]:
        # Migrate legacy full-path outputs / extensioned names to {tier, stem}
        for p in tool.get("params", []):
            if p.get("type") not in ("vector_output", "folder_output"):
                continue
            cur = state["values"][tid].get(p["id"])
            if isinstance(cur, str):
                state["values"][tid][p["id"]] = default_output_spec(p)
            elif isinstance(cur, dict) and "name" in cur:
                cur = dict(cur)
                cur["name"] = output_name_stem(str(cur.get("name") or ""))
                if not cur["name"]:
                    cur["name"] = default_output_spec(p)["name"]
                state["values"][tid][p["id"]] = cur
        return
    vals: Dict[str, Any] = {}
    for p in tool.get("params", []):
        pid = p["id"]
        ptype = p["type"]
        if ptype in ("vector_output", "folder_output"):
            vals[pid] = default_output_spec(p)
        elif user_defaults.has(tid, pid):
            vals[pid] = user_defaults.get(tid, pid)
        elif "default" in p:
            vals[pid] = p["default"]
        else:
            vals[pid] = None
    state["values"][tid] = vals


def _can_set_default(param: Dict[str, Any]) -> bool:
    """Params that ship with a catalog default can save a user override."""
    return "default" in param and param.get("type") not in (
        "vector_file",
        "folder",
        "vector_output",
        "folder_output",
    )


def _set_as_default_button(tool_id: str, param: Dict[str, Any]) -> None:
    if not _can_set_default(param):
        return

    def on_click(tool=tool_id, p=param) -> None:
        pid = p["id"]
        vals = state["values"].get(tool) or {}
        if pid not in vals:
            ui.notify("Enter a value first.", type="warning")
            return
        val = vals[pid]
        ptype = p["type"]
        if ptype != "boolean" and val is None:
            ui.notify("Enter a value first.", type="warning")
            return
        if ptype == "integer":
            val = int(val)
        elif ptype == "number":
            val = float(val)
        elif ptype == "boolean":
            val = bool(val)
        elif ptype == "enum":
            val = int(val)
        elif ptype == "text":
            val = str(val if val is not None else "")
        user_defaults.set_default(tool, pid, val)
        ui.notify(f"Default saved for “{p.get('label') or pid}”.", type="positive")

    ui.button("Set as default", on_click=on_click).props(
        "qt-set-default flat dense no-caps"
    ).props("color=teal-4").tooltip(
        "Remember this value as the default next time this tool is configured"
    )


def _resolve_param_value(tool: Dict[str, Any], param: Dict[str, Any], raw_val: Any) -> Any:
    if param.get("type") in ("vector_output", "folder_output"):
        return resolve_output_path(_db(), CATALOG, param, raw_val)
    return raw_val


def _selected_tools() -> List[Dict[str, Any]]:
    ordered_ids, _wires = resolve_pipeline(CATALOG, state["selected"])
    out: List[Dict[str, Any]] = []
    for tid in ordered_ids:
        tool = tool_by_id(CATALOG, tid)
        if tool and not tool.get("placeholder"):
            out.append(tool)
    return out


def _pipeline_wires() -> Dict[str, Dict[str, tuple]]:
    _ordered, wires = resolve_pipeline(CATALOG, state["selected"])
    return wires


def _build_jobs() -> List[Dict[str, Any]]:
    if not _map_chosen():
        raise ValueError("Choose a map before running.")
    jobs = []
    wires = _pipeline_wires()
    produced: Dict[str, Dict[str, object]] = {}  # tool_id -> {param_id: path}

    for tool in _selected_tools():
        _init_defaults_for(tool)
        tid = tool["id"]
        raw = dict(state["values"][tid])

        # Auto-wire inputs from earlier steps in the chain
        for param_id, (src_tool, src_param) in (wires.get(tid) or {}).items():
            src_val = None
            if src_tool in produced and src_param in produced[src_tool]:
                src_val = produced[src_tool][src_param]
            else:
                src_raw = (state["values"].get(src_tool) or {}).get(src_param)
                src_tool_def = tool_by_id(CATALOG, src_tool)
                src_param_def = None
                if src_tool_def:
                    src_param_def = next(
                        (p for p in src_tool_def.get("params", []) if p["id"] == src_param),
                        None,
                    )
                if src_param_def is not None:
                    src_val = _resolve_param_value(src_tool_def, src_param_def, src_raw)
                else:
                    src_val = src_raw
            if not src_val:
                src_name = (tool_by_id(CATALOG, src_tool) or {}).get(
                    "display_name", src_tool
                )
                raise ValueError(
                    f"{tool['display_name']}: expected output from prior step "
                    f"“{src_name}” ({src_param})"
                )
            raw[param_id] = src_val

        params: Dict[str, object] = {}
        for p in tool.get("params", []):
            pid = p["id"]
            val = raw.get(pid)
            if pid in (wires.get(tid) or {}):
                # already a resolved path string
                if val is None or val == "" or val == "(none)":
                    raise ValueError(f"{tool['display_name']}: missing wired {p['label']}")
                params[pid] = val
                continue
            if p.get("type") in ("vector_output", "folder_output"):
                path = resolve_output_path(_db(), CATALOG, p, val)
                params[pid] = path
                continue
            if val is None or val == "" or val == "(none)":
                if p.get("required"):
                    raise ValueError(f"{tool['display_name']}: missing {p['label']}")
                continue
            params[pid] = val

        produced[tid] = {}
        for p in tool.get("params", []):
            if p.get("type") in ("vector_output", "folder_output"):
                if p["id"] in params:
                    produced[tid][p["id"]] = params[p["id"]]

        jobs.append(
            {
                "algorithm": tool["algorithm"],
                "params": params,
                "label": tool["display_name"],
            }
        )
    return jobs


def _field_props() -> str:
    return "outlined dense dark color=teal-4"


def _dual_icon_tab(tab: Dict[str, Any]):
    """Tab with Material + Maple sprites stacked above the label; CSS crossfades."""
    material = tab["icon"]
    label = tab["label"]
    maple_img = _maple_img_html(tab["id"])
    with ui.tab(tab["id"], label=" ") as tab_el:
        ui.html(
            f'<span class="qt-tab-stack">'
            f'  <span class="qt-tab-icon-slot">'
            f'    <span class="qt-tab-icon-normal material-icons" aria-hidden="true">{material}</span>'
            f'    <span class="qt-tab-icon-secret">{maple_img}</span>'
            f'  </span>'
            f'  <span class="qt-tab-caption">{label}</span>'
            f"</span>",
            sanitize=False,
        )
    return tab_el


def _set_secret_mode(enabled: bool) -> None:
    """Toggle Secret Mode without rebuilding the page."""
    state["maple_mode"] = bool(enabled)
    tabs_el = state.get("_tabs_el")
    if tabs_el is not None:
        if enabled:
            tabs_el.classes(add="qt-secret")
        else:
            tabs_el.classes(remove="qt-secret")
    else:
        # Fallback if tabs not mounted yet
        ui.run_javascript(
            "const t=document.querySelector('.qt-tabs');"
            f"if(t)t.classList.toggle('qt-secret',{str(bool(enabled)).lower()});"
        )


SECRET_QUESTION = "How many classes were available in KMS beta release?"
SECRET_ANSWER = "3"


def render_maple_toggle() -> None:
    """Small opt-in — Material icons remain the default (gated by security question)."""
    with ui.row().classes("items-center gap-2 qt-maple-toggle-row"):
        maple = (
            ui.switch("Secret Mode", value=bool(state.get("maple_mode")))
            .props("color=orange-5 dense keep-color")
            .classes("qt-maple-toggle")
        )

        def _sync_switch() -> None:
            """Force switch UI to match maple_mode (source of truth)."""
            state["_ignore_maple_switch"] = True
            maple.value = bool(state.get("maple_mode"))
            ui.timer(0.35, lambda: state.pop("_ignore_maple_switch", None), once=True)

        def _unlock() -> None:
            if state.get("_secret_unlocking"):
                return
            ans = (answer.value or "").strip()
            if ans != SECRET_ANSWER:
                ui.notify("Incorrect.", type="negative")
                return
            state["_secret_unlocking"] = True
            state["_secret_unlock_guard"] = True
            _set_secret_mode(True)
            dialog.close()
            # Dialog close / Enter can desync the switch — sync now and again shortly.
            _sync_switch()
            ui.timer(0.05, _sync_switch, once=True)
            ui.timer(0.2, _sync_switch, once=True)
            ui.timer(
                0.6,
                lambda: (
                    state.pop("_secret_unlocking", None),
                    state.pop("_secret_unlock_guard", None),
                ),
                once=True,
            )

        with ui.dialog().props("persistent") as dialog, ui.card().classes(
            "qt-panel q-pa-md"
        ).style("min-width: 22rem"):
            ui.label("Security question").classes("qt-meta-label")
            ui.label(SECRET_QUESTION).classes("text-body1 q-mb-md")
            answer = (
                ui.input(placeholder="Answer")
                .props(_field_props() + " autofocus")
                .classes("w-full")
            )

            def on_enter() -> None:
                _unlock()

            answer.on("keydown.enter", on_enter)
            with ui.row().classes("w-full justify-end gap-2 q-mt-md"):
                ui.button("Cancel", on_click=dialog.close).classes("qt-btn-ghost").props(
                    "flat no-caps"
                )
                ui.button("Unlock", on_click=_unlock).classes("qt-btn-primary").props(
                    "unelevated no-caps"
                )

        def on_maple(e) -> None:
            if state.get("_ignore_maple_switch") or state.get("_secret_unlocking"):
                # Keep visuals aligned with real mode during programmatic updates
                if bool(e.value) != bool(state.get("maple_mode")):
                    maple.value = bool(state.get("maple_mode"))
                return
            wanted = bool(e.value)
            active = bool(state.get("maple_mode"))
            if wanted == active:
                return
            if not wanted:
                # Enter-unlock often emits a spurious "off" — don't kill secret mode
                if active and state.get("_secret_unlock_guard"):
                    _sync_switch()
                    return
                _set_secret_mode(False)
                return
            # User asked to enable — keep switch off until the question is answered
            state["_ignore_maple_switch"] = True
            maple.value = False
            ui.timer(0.15, lambda: state.pop("_ignore_maple_switch", None), once=True)
            answer.value = ""
            dialog.open()

        maple.on_value_change(on_maple)


def render_steps() -> None:
    step = PAGE_META[state["page"]]["step"]
    labels = [("01", "Select"), ("02", "Configure"), ("03", "Run")]
    with ui.element("div").classes("qt-steps"):
        for i, (num, label) in enumerate(labels, start=1):
            cls = "qt-step"
            if i == step:
                cls += " is-active"
            elif i < step:
                cls += " is-done"
            ui.label(f"{num} · {label}").classes(cls)


def render_select_page() -> None:
    def go_configure() -> None:
        if not state["selected"]:
            ui.notify("Select at least one tool.", type="warning")
            return
        for tool in _selected_tools():
            _init_defaults_for(tool)
        state["page"] = "configure"
        render_body.refresh()

    @ui.refreshable
    def queue_panel() -> None:
        ordered = _selected_tools()
        ui.label("run queue").classes("qt-meta-label q-mb-sm")
        if not ordered:
            ui.label("No tools selected").classes("qt-hint q-mb-md")
        else:
            with ui.element("div").classes("qt-queue-list q-mb-md"):
                for i, tool in enumerate(ordered, start=1):
                    with ui.element("div").classes("qt-queue-item"):
                        ui.label(f"{i:02d}").classes("qt-queue-num")
                        ui.label(tool["display_name"]).classes("qt-queue-name")

        ui.button(
            "Continue to Process",
            icon="arrow_forward",
            on_click=go_configure,
        ).classes("qt-btn-primary w-full").props("unelevated no-caps")

    state["_queue_panel_refresh"] = queue_panel.refresh

    with ui.row().classes("w-full items-center justify-between q-mb-sm no-wrap"):
        ui.label("Armed modules appear with a teal edge. Queue as many as you need.").classes(
            "qt-lede"
        )
        count_el = ui.label(f"{len(state['selected'])} selected").classes("qt-brand-sub")
        state["_selected_count_el"] = count_el

    with ui.row().classes("w-full qt-select-layout no-wrap items-start gap-4"):
        with ui.column().classes("qt-select-main flex-grow"):
            with ui.row().classes(
                "w-full items-end no-wrap gap-3 q-mb-sm qt-tabs-bar"
            ):
                tabs_cls = "qt-tabs"
                if state["maple_mode"]:
                    tabs_cls += " qt-secret"
                with ui.tabs().classes(tabs_cls).props("align=left dense") as tabs:
                    state["_tabs_el"] = tabs
                    tab_refs = {}
                    for tab in CATALOG.get("tabs", []):
                        tab_refs[tab["id"]] = _dual_icon_tab(tab)
                render_maple_toggle()

            state["_tool_cards"] = {}
            with ui.tab_panels(tabs, value=CATALOG["tabs"][0]["id"]).classes(
                "w-full"
            ):
                for tab in CATALOG.get("tabs", []):
                    with ui.tab_panel(tab_refs[tab["id"]]):
                        tools = tools_for_tab(CATALOG, tab["id"])
                        if not tools:
                            ui.label("No tools in this lane yet.").classes("qt-lede")
                            continue
                        for i, tool in enumerate(tools):
                            tid = tool["id"]
                            is_placeholder = bool(tool.get("placeholder"))
                            checked = (not is_placeholder) and tid in state["selected"]
                            card_cls = "qt-tool-card w-full q-mb-sm q-pa-md"
                            if checked:
                                card_cls += " is-selected"
                            if is_placeholder:
                                card_cls += " is-placeholder"
                            with ui.card().classes(card_cls) as card:
                                if not is_placeholder:
                                    state["_tool_cards"][tid] = card
                                with ui.column().classes("gap-1 w-full"):
                                    if is_placeholder:
                                        with ui.row().classes(
                                            "items-center gap-2 no-wrap"
                                        ):
                                            ui.label(tool["display_name"]).classes(
                                                "text-body1"
                                            )
                                            ui.label("coming soon").classes(
                                                "qt-badge-soon"
                                            )
                                    else:
                                        cb = ui.checkbox(
                                            text=tool["display_name"],
                                            value=checked,
                                            on_change=lambda e, tool_id=tid: _toggle_tool(
                                                tool_id, e.value
                                            ),
                                        ).props("color=teal-4 dense")
                                        cb.tooltip(tool.get("description") or "")
                                        ui.label(f"script:{tid}").classes(
                                            "qt-tool-id q-ml-lg"
                                        )
                                if tool.get("description"):
                                    ui.label(tool["description"]).classes(
                                        "qt-tool-desc q-mt-xs"
                                    )

        with ui.card().classes("qt-panel qt-select-queue q-pa-md"):
            queue_panel()


def _param_widget(
    tool_id: str,
    param: Dict[str, Any],
    *,
    wired_from: Optional[tuple] = None,
) -> None:
    vals = state["values"][tool_id]
    pid = param["id"]
    ptype = param["type"]
    label = param["label"]
    required = param.get("required", False)
    star = " *" if required else ""

    if wired_from:
        src_tool_id, src_param = wired_from
        src_tool = tool_by_id(CATALOG, src_tool_id) or {}
        src_name = src_tool.get("display_name", src_tool_id)
        src_param_def = next(
            (p for p in src_tool.get("params", []) if p["id"] == src_param),
            None,
        )
        src_raw = (state["values"].get(src_tool_id) or {}).get(src_param)
        if src_param_def is not None:
            src_path = resolve_output_path(_db(), CATALOG, src_param_def, src_raw)
        else:
            src_path = src_raw or "(set in prior step)"
        with ui.column().classes("w-full q-mb-sm"):
            ui.label(f"{label}{star}").classes("text-caption text-grey-5")
            ui.label(f"← auto from “{src_name}”").classes("qt-meta-label")
            ui.label(str(src_path)).classes("qt-meta")
        return

    if ptype in ("vector_file", "folder"):
        lib = param.get("library")
        choices = (
            library_choices(
                _db(),
                CATALOG,
                lib,
                ptype,
                mesh_pick=param.get("mesh_pick"),
            )
            if lib
            else []
        )
        options = ["(none)"] + [c["label"] for c in choices]
        path_by_label = {c["label"]: c["path"] for c in choices}
        current_path = vals.get(pid)
        current_label = "(none)"
        for lab, path in path_by_label.items():
            if path == current_path:
                current_label = lab
                break

        with ui.row().classes("w-full items-center no-wrap gap-2"):
            sel = (
                ui.select(
                    options=options,
                    value=current_label if current_label in options else "(none)",
                    label=label + star,
                )
                .props(_field_props())
                .classes("flex-grow")
            )

            def on_sel(e, tool=tool_id, key=pid, mapping=path_by_label) -> None:
                lab = e.value
                state["values"][tool][key] = None if lab in (None, "(none)") else mapping.get(lab)

            sel.on_value_change(on_sel)
            ui.button(icon="refresh", on_click=render_body.refresh).props(
                "flat dense round color=teal-4"
            ).tooltip("Refresh folder list")

        if not choices:
            domain = (CATALOG.get("domains") or {}).get(lib, lib)
            tier_hint = "|".join(domain_tiers(CATALOG, lib)) if lib else "working|tests"
            ui.label(
                f"empty · Database/{state.get('map')}/{domain}/{{{tier_hint}}} — drop assets, then refresh"
            ).classes("qt-hint q-mb-sm")
        return

    if ptype in ("vector_output", "folder_output"):
        spec = vals.get(pid)
        if not isinstance(spec, dict):
            spec = default_output_spec(param)
            vals[pid] = spec
        else:
            # Keep stored name as stem only (strip legacy .gpkg etc.)
            fixed = dict(spec)
            fixed["name"] = output_name_stem(str(fixed.get("name") or "")) or default_output_spec(
                param
            )["name"]
            vals[pid] = fixed
            spec = fixed
        tier = spec.get("tier") or "working"
        name = spec.get("name") or default_output_spec(param)["name"]
        name_label = "Folder name" if ptype == "folder_output" else "Name"
        resolved = resolve_output_path(_db(), CATALOG, param, spec)

        with ui.column().classes("w-full q-mb-sm gap-1"):
            ui.label(label + star).classes("text-caption text-grey-5")
            with ui.row().classes("w-full items-center no-wrap gap-2"):
                tier_sel = (
                    ui.select(
                        options=list(OUTPUT_TIERS),
                        value=tier if tier in OUTPUT_TIERS else "working",
                        label="Save to",
                    )
                    .props(_field_props())
                    .classes("w-40")
                )
                name_inp = (
                    ui.input(label=name_label, value=name)
                    .props(_field_props())
                    .classes("flex-grow")
                )
                if ptype == "vector_output":
                    ui.label(VECTOR_OUTPUT_EXT).classes("qt-meta q-mt-sm")

                def on_tier(e, tool=tool_id, key=pid) -> None:
                    cur = state["values"][tool].get(key)
                    if not isinstance(cur, dict):
                        cur = default_output_spec(param)
                    cur = dict(cur)
                    cur["tier"] = e.value
                    state["values"][tool][key] = cur
                    path_lbl.set_text(resolve_output_path(_db(), CATALOG, param, cur))

                def on_name(e, tool=tool_id, key=pid) -> None:
                    cur = state["values"][tool].get(key)
                    if not isinstance(cur, dict):
                        cur = default_output_spec(param)
                    cur = dict(cur)
                    cur["name"] = output_name_stem(e.value or "")
                    state["values"][tool][key] = cur
                    path_lbl.set_text(resolve_output_path(_db(), CATALOG, param, cur))

                tier_sel.on_value_change(on_tier)
                name_inp.on_value_change(on_name)
            path_lbl = ui.label(resolved).classes("qt-meta")
        return

    if ptype == "boolean":
        try:
            fallback = user_defaults.effective(tool_id, param)
        except KeyError:
            fallback = False
        with ui.row().classes("w-full items-center no-wrap gap-2 q-mb-sm"):
            sw = ui.switch(
                label + star,
                value=bool(vals[pid] if pid in vals else fallback),
            ).props("color=teal-4 dense").classes("flex-grow")

            def on_bool(e, tool=tool_id, key=pid) -> None:
                state["values"][tool][key] = bool(e.value)

            sw.on_value_change(on_bool)
            _set_as_default_button(tool_id, param)
        return

    if ptype == "enum":
        opts = param.get("options") or []
        try:
            fallback = user_defaults.effective(tool_id, param)
        except KeyError:
            fallback = 0
        idx = int(vals.get(pid, fallback) or 0)
        idx = max(0, min(idx, len(opts) - 1)) if opts else 0
        with ui.row().classes("w-full items-center no-wrap gap-2 q-mb-sm"):
            sel = (
                ui.select(
                    options=opts,
                    value=opts[idx] if opts else None,
                    label=label + star,
                )
                .props(_field_props())
                .classes("flex-grow")
            )

            def on_enum(e, tool=tool_id, key=pid, options=opts) -> None:
                try:
                    state["values"][tool][key] = options.index(e.value)
                except ValueError:
                    state["values"][tool][key] = 0

            sel.on_value_change(on_enum)
            _set_as_default_button(tool_id, param)
        return

    if ptype in ("number", "integer"):
        try:
            fallback = user_defaults.effective(tool_id, param)
        except KeyError:
            fallback = 0
        val = vals.get(pid, fallback)
        kwargs = {}
        if "min" in param:
            kwargs["min"] = param["min"]
        if "max" in param:
            kwargs["max"] = param["max"]
        # Floats: step=any avoids HTML5 "(value-min) % step == 0" false invalids
        # (e.g. min=0.01 + step=0.1 rejects 1.5 and 10).
        with ui.row().classes("w-full items-center no-wrap gap-2 q-mb-sm"):
            if ptype == "integer":
                num = (
                    ui.number(label=label + star, value=val, step=1, **kwargs)
                    .props(_field_props())
                    .classes("flex-grow")
                )
            else:
                num = (
                    ui.number(label=label + star, value=val, **kwargs)
                    .props(_field_props() + " step=any")
                    .classes("flex-grow")
                )

            def on_num(e, tool=tool_id, key=pid, as_int=ptype == "integer") -> None:
                v = e.value
                if v is None:
                    state["values"][tool][key] = None
                    return
                state["values"][tool][key] = int(v) if as_int else float(v)

            num.on_value_change(on_num)
            _set_as_default_button(tool_id, param)
        return

    if ptype == "text":
        try:
            fallback = user_defaults.effective(tool_id, param)
        except KeyError:
            fallback = ""
        cur = vals[pid] if pid in vals and vals[pid] is not None else fallback
        with ui.row().classes("w-full items-center no-wrap gap-2 q-mb-sm"):
            inp = (
                ui.input(label=label + star, value=str(cur))
                .props(_field_props())
                .classes("flex-grow")
            )

            def on_text(e, tool=tool_id, key=pid) -> None:
                state["values"][tool][key] = e.value

            inp.on_value_change(on_text)
            _set_as_default_button(tool_id, param)
        return

    ui.label(f"Unsupported param type: {ptype}").classes("text-negative")


def render_pipeline_nodes(tools: List[Dict[str, Any]]) -> None:
    """Visual run-order as connected nodes."""
    with ui.element("div").classes("qt-pipeline q-mb-md"):
        ui.label("run order").classes("qt-meta-label q-mb-sm")
        with ui.element("div").classes("qt-pipeline-track"):
            for i, tool in enumerate(tools):
                if i > 0:
                    ui.element("div").classes("qt-pipeline-edge")
                with ui.element("div").classes("qt-pipeline-node"):
                    ui.label(f"{i + 1:02d}").classes("qt-pipeline-step")
                    ui.label(tool["display_name"]).classes("qt-pipeline-title")


def render_configure_page() -> None:
    selected = _selected_tools()
    wires = _pipeline_wires()

    with ui.card().classes("qt-panel w-full q-mb-md q-pa-md"):
        ui.label("Map").classes("text-h6 q-mb-sm")
        render_map_picker()

    if not _map_chosen():
        with ui.row().classes("w-full justify-between qt-footer-actions"):
            def back_early() -> None:
                state["page"] = "select"
                render_body.refresh()

            ui.button("Back", icon="arrow_back", on_click=back_early).classes(
                "qt-btn-ghost"
            ).props("flat no-caps")
        return

    if len(selected) > 1:
        render_pipeline_nodes(selected)
    elif len(selected) == 1:
        # Still show a single node so the page has clear context
        render_pipeline_nodes(selected)

    for step_i, tool in enumerate(selected, start=1):
        _init_defaults_for(tool)
        tool_wires = wires.get(tool["id"]) or {}
        with ui.card().classes("qt-panel w-full q-mb-md q-pa-md"):
            with ui.row().classes("items-center justify-between w-full q-mb-sm"):
                ui.label(f"{step_i}. {tool['display_name']}").classes("text-h6")
                ui.label(tool["algorithm"]).classes("qt-tool-id")
            if tool.get("description"):
                ui.label(tool["description"]).classes("qt-lede q-mb-md")
            for param in tool.get("params", []):
                _param_widget(
                    tool["id"],
                    param,
                    wired_from=tool_wires.get(param["id"]),
                )

    with ui.row().classes("w-full justify-between qt-footer-actions"):
        def back() -> None:
            state["page"] = "select"
            render_body.refresh()

        def run() -> None:
            if not _map_chosen():
                ui.notify("Choose a map first", type="warning")
                return
            state["page"] = "run"
            state["log_lines"] = []
            state["run_progress"] = {
                "overall_pct": 0.0,
                "job_pct": 0.0,
                "job_index": 0,
                "job_count": 0,
                "job_label": "Starting…",
                "stage": "Opening run console…",
                "running": True,
                "done": False,
                "ok": None,
                "sub_cur": None,
                "sub_total": None,
                "sub_unit": "",
                "sub_detail": "",
                "_started": False,
            }
            render_body.refresh()

        ui.button("Back", icon="arrow_back", on_click=back).classes("qt-btn-ghost").props(
            "flat no-caps"
        )
        ui.button("Run queue", icon="play_arrow", on_click=run).classes("qt-btn-primary").props(
            "unelevated no-caps"
        )


log_box: Optional[ui.log] = None
_progress_bar = None
_progress_sub_bar = None
_progress_sub_row = None
_progress_job_lbl = None
_progress_stage_lbl = None
_progress_pct_lbl = None
_progress_sub_lbl = None
_log_cursor = 0


def _append_log(line: str) -> None:
    state["log_lines"].append(line)


def _set_run_progress(update: Dict[str, object]) -> None:
    rp = state.get("run_progress")
    if not isinstance(rp, dict):
        rp = {}
        state["run_progress"] = rp
    if "stage" in update and update.get("stage") != rp.get("stage"):
        # Only reset local clock when the textual stage base changes.
        new_stage = str(update.get("stage") or "")
        old_stage = str(rp.get("stage") or "")
        new_base = new_stage.split(" · ")[0]
        old_base = old_stage.split(" · ")[0]
        if new_base != old_base:
            rp["stage_t0"] = time.time()
    if "elapsed_s" not in update and rp.get("stage_t0"):
        update = dict(update)
        update["elapsed_s"] = max(0, int(time.time() - float(rp["stage_t0"])))
    rp.update(update)


def _sync_run_ui() -> None:
    """Pull background log/progress into widgets (timer on run page)."""
    global _log_cursor
    if state.get("page") != "run":
        return
    lines = state.get("log_lines") or []
    if log_box is not None and _log_cursor < len(lines):
        for line in lines[_log_cursor:]:
            log_box.push(line)
        _log_cursor = len(lines)

    rp = state.get("run_progress") or {}
    overall = float(rp.get("overall_pct") or 0.0)
    job_i = int(rp.get("job_index") or 0)
    job_n = int(rp.get("job_count") or 0)
    label = str(rp.get("job_label") or "")
    stage = str(rp.get("stage") or "")
    job_pct = float(rp.get("job_pct") or 0.0)

    if _progress_bar is not None:
        _progress_bar.value = max(0.0, min(1.0, overall / 100.0))
    if _progress_pct_lbl is not None:
        _progress_pct_lbl.set_text(f"{overall:.0f}%")
    if _progress_job_lbl is not None:
        if job_n:
            _progress_job_lbl.set_text(f"Job {job_i}/{job_n} — {label}")
        else:
            _progress_job_lbl.set_text(label or "Queue")

    sub_total = rp.get("sub_total")
    sub_cur = rp.get("sub_cur")
    sub_unit = str(rp.get("sub_unit") or "items")
    sub_detail = str(rp.get("sub_detail") or "")
    if _progress_sub_row is not None:
        try:
            show_sub = bool(sub_total) and int(sub_total) > 0
        except (TypeError, ValueError):
            show_sub = False
        _progress_sub_row.set_visibility(show_sub)
        if show_sub:
            total_i = int(sub_total)
            cur_i = max(0, min(int(sub_cur or 0), total_i))
            frac = cur_i / total_i if total_i else 0.0
            if _progress_sub_bar is not None:
                _progress_sub_bar.value = max(0.0, min(1.0, frac))
            if _progress_sub_lbl is not None:
                unit_lbl = sub_unit[:1].upper() + sub_unit[1:] if sub_unit else "Items"
                text = f"{unit_lbl}  {cur_i:,} / {total_i:,}  ({100.0 * frac:.1f}%)"
                if sub_detail:
                    text = f"{text}  ·  {sub_detail}"
                _progress_sub_lbl.set_text(text)

    if _progress_stage_lbl is not None:
        detail = stage
        elapsed = rp.get("elapsed_s")
        if elapsed is None and rp.get("stage_t0"):
            try:
                elapsed = max(0, int(time.time() - float(rp["stage_t0"])))
            except (TypeError, ValueError):
                elapsed = None
        # Prefer clean stage + sub-bar; don't paste long polygon lines here.
        if job_pct and stage and f"{int(job_pct)}%" not in stage and not sub_total:
            detail = f"{stage}  ·  job {job_pct:.0f}%"
        elif job_pct and not stage:
            detail = f"job {job_pct:.0f}%"
        if elapsed is not None and "·" not in detail and not rp.get("done"):
            detail = f"{detail} · {int(elapsed)}s"
        _progress_stage_lbl.set_text(detail)

    if rp.get("done") and rp.get("running"):
        # Final notify once
        rp["running"] = False
        if rp.get("ok"):
            ui.notify("Queue complete", type="positive")
        elif rp.get("ok") is False:
            ui.notify("Failed — see console", type="negative")


def _start_run() -> None:
    """Kick off the queue in a background thread (never block the UI)."""
    rp = state.get("run_progress") or {}
    if rp.get("_worker_alive"):
        return
    rp["_worker_alive"] = True

    def worker() -> None:
        try:
            _set_run_progress(
                {
                    "job_label": "Queue",
                    "stage": "Finding qgis_process…",
                }
            )
            bat = find_qgis_process()
            state["qgis_bat"] = str(bat)
            _set_run_progress({"stage": "Building job list…"})
            jobs = _build_jobs()
            if not jobs:
                _append_log("No jobs to run.")
                _set_run_progress(
                    {
                        "running": True,  # cleared by sync notify
                        "done": True,
                        "ok": True,
                        "job_label": "Queue",
                        "stage": "Nothing to run",
                        "overall_pct": 100.0,
                    }
                )
                return
            _append_log(f"> binary  {bat}")
            _append_log(f"> queued  {len(jobs)} job(s)")
            _append_log("---")
            _set_run_progress(
                {
                    "running": True,
                    "done": False,
                    "ok": None,
                    "job_count": len(jobs),
                    "job_index": 0,
                    "job_label": "Queue",
                    "stage": (
                        "Launching QGIS (first start can take a while)…"
                    ),
                    "overall_pct": 0.0,
                }
            )

            def log(msg: str) -> None:
                _append_log(msg)

            def on_progress(info: Dict[str, object]) -> None:
                _set_run_progress(dict(info))

            results = run_queue(jobs, bat=bat, log=log, progress=on_progress)
            ok = bool(results) and all(r.ok for r in results)
            if ok:
                _append_log("---")
                _append_log("status  OK — all jobs finished")
            else:
                _append_log("---")
                _append_log("status  FAILED — queue halted")
            _set_run_progress(
                {
                    "done": True,
                    "ok": ok,
                    "overall_pct": 100.0
                    if ok
                    else float(
                        (state.get("run_progress") or {}).get("overall_pct") or 0
                    ),
                    "stage": "Complete" if ok else "Failed",
                    "running": True,
                }
            )
        except Exception as exc:
            _append_log(f"ERROR: {exc}")
            _append_log(traceback.format_exc())
            _set_run_progress(
                {
                    "done": True,
                    "ok": False,
                    "running": True,
                    "job_label": "Queue",
                    "stage": f"ERROR: {exc}",
                }
            )
        finally:
            rp = state.get("run_progress")
            if isinstance(rp, dict):
                rp["_worker_alive"] = False

    threading.Thread(target=worker, daemon=True).start()


def render_run_page() -> None:
    global log_box, _progress_bar, _progress_sub_bar, _progress_sub_row
    global _progress_job_lbl, _progress_stage_lbl, _progress_pct_lbl, _progress_sub_lbl
    global _log_cursor

    _log_cursor = 0
    ui.label("Running tools via headless QGIS…").classes("qt-lede q-mb-sm")

    with ui.card().classes("qt-panel w-full q-mb-md q-pa-md"):
        with ui.row().classes("w-full items-center justify-between no-wrap gap-2"):
            _progress_job_lbl = ui.label(
                str((state.get("run_progress") or {}).get("job_label") or "Starting…")
            ).classes("text-body1")
            _progress_pct_lbl = ui.label("0%").classes("qt-meta")
        _progress_bar = (
            ui.linear_progress(value=0.0, show_value=False)
            .props("color=teal-4 track-color=grey-9 rounded")
            .classes("w-full q-mt-sm qt-run-progress")
        )
        _progress_stage_lbl = ui.label(
            str((state.get("run_progress") or {}).get("stage") or "…")
        ).classes("qt-meta q-mt-sm")
        with ui.column().classes("w-full q-mt-sm gap-1") as _progress_sub_row:
            _progress_sub_lbl = ui.label("").classes("qt-meta")
            _progress_sub_bar = (
                ui.linear_progress(value=0.0, show_value=False)
                .props("color=cyan-4 track-color=grey-9 rounded")
                .classes("w-full qt-run-sub-progress")
            )
        _progress_sub_row.set_visibility(False)

    log_box = ui.log(max_lines=4000).classes("qt-log w-full h-80 text-sm")
    for line in state["log_lines"]:
        log_box.push(line)
        _log_cursor = len(state["log_lines"])

    ui.timer(0.25, _sync_run_ui)

    # Start the worker from the run page itself (survives body refresh).
    rp = state.get("run_progress") or {}
    if rp.get("running") and not rp.get("_started") and not rp.get("done"):
        rp["_started"] = True
        ui.timer(0.05, _start_run, once=True)

    with ui.row().classes("w-full justify-between qt-footer-actions"):
        def again() -> None:
            if (state.get("run_progress") or {}).get("running") and not (
                state.get("run_progress") or {}
            ).get("done"):
                ui.notify("Queue still running…", type="warning")
                return
            state["page"] = "configure"
            render_body.refresh()

        def home() -> None:
            if (state.get("run_progress") or {}).get("running") and not (
                state.get("run_progress") or {}
            ).get("done"):
                ui.notify("Queue still running…", type="warning")
                return
            state["page"] = "select"
            render_body.refresh()

        ui.button("Back to configure", icon="arrow_back", on_click=again).classes(
            "qt-btn-ghost"
        ).props("flat no-caps")
        ui.button("Select tools", icon="home", on_click=home).classes("qt-btn-ghost").props(
            "flat no-caps"
        )


@ui.refreshable
def render_body() -> None:
    meta = PAGE_META[state["page"]]
    with ui.element("div").classes("qt-hero"):
        ui.label(meta["kicker"]).classes("qt-kicker")
        ui.label(meta["title"]).classes("qt-title")
    render_steps()
    page = state["page"]
    if page == "select":
        render_select_page()
    elif page == "configure":
        render_configure_page()
    else:
        render_run_page()


@ui.page("/")
def index() -> None:
    ui.dark_mode(True)
    ui.colors(
        primary="#2dd4bf",
        secondary="#38bdf8",
        accent="#2dd4bf",
        dark="#070b12",
        positive="#34d399",
        negative="#fb7185",
        info="#38bdf8",
        warning="#fbbf24",
    )
    ui.add_head_html('<link rel="stylesheet" href="/static/theme.css?v=sub-progress-1">')
    ui.add_head_html(
        '<meta name="theme-color" content="#070b12">'
        '<style>body{margin:0}</style>'
    )
    # Critical tab crossfade (also in theme.css) — keeps working even if CSS is cached
    ui.add_css(
        """
        .qt-tabs-bar { display: flex; width: 100%; align-items: flex-end; justify-content: flex-start; gap: 0.75rem; }
        .qt-tabs-bar .qt-tabs { flex: 1 1 auto; min-width: 0; width: auto !important; max-width: 100%; }
        .qt-tabs-bar .qt-maple-toggle-row { flex: 0 0 auto; margin-left: auto; align-self: flex-end; margin-bottom: 0.65rem; }
        .qt-tabs .q-tabs__content { justify-content: flex-start !important; }
        .qt-tabs .q-tab { min-height: 100px !important; padding-top: 8px !important; padding-bottom: 8px !important; }
        .qt-tabs .q-tab__content { flex-direction: column !important; justify-content: center; }
        .qt-tabs .q-tab__label { font-size: 0 !important; width: 0 !important; height: 0 !important; overflow: hidden !important; margin: 0 !important; padding: 0 !important; }
        .qt-tab-stack { display: inline-flex !important; flex-direction: column !important; align-items: center !important; justify-content: center !important; gap: 0.3rem !important; line-height: 1.1; }
        .qt-tab-icon-slot { position: relative !important; width: 65px !important; height: 65px !important; display: flex !important; align-items: center !important; justify-content: center !important; }
        .qt-tab-icon-normal, .qt-tab-icon-secret { position: absolute !important; inset: 0 !important; display: flex !important; align-items: center !important; justify-content: center !important; transition: opacity 0.28s ease, transform 0.28s ease !important; }
        .qt-tab-icon-normal { font-size: 28px !important; opacity: 1 !important; transform: scale(1); color: inherit; }
        .qt-tab-icon-secret { opacity: 0 !important; transform: scale(0.88); pointer-events: none !important; }
        .qt-tabs.qt-secret .qt-tab-icon-normal { opacity: 0 !important; transform: scale(0.88); pointer-events: none !important; }
        .qt-tabs.qt-secret .qt-tab-icon-secret { opacity: 1 !important; transform: scale(1); pointer-events: auto !important; }
        .qt-tab-caption { font-weight: 500; font-size: 0.9rem; color: inherit; }
        """
    )
    render_header()
    with ui.column().classes("w-full max-w-6xl mx-auto q-pa-md"):
        ui.label(
            f"Tools write under {DEFAULT_DATABASE}/<map>/… Pick the map on Configure. "
            "Runs via qgis_process — Desktop stays closed."
        ).classes("qt-lede q-mb-sm")
        render_body()


if __name__ in {"__main__", "__mp_main__"}:
    _try_find_qgis()
    ui.run(
        title="QGIS Tools · Process Console",
        host="127.0.0.1",
        port=8080,
        reload=False,
        show=True,
        favicon="🛰️",
        dark=True,
    )
