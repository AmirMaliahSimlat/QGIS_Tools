# -*- coding: utf-8 -*-
"""Load tool catalog and scan Database/<map>/… folders."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Union
import re
import shutil

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = Path(__file__).resolve().parent / "catalog" / "tools.yaml"
DEFAULT_DATABASE = REPO_ROOT / "Database"

VECTOR_EXTS = {".shp", ".gpkg", ".geojson", ".json", ".gml"}
VECTOR_OUTPUT_EXT = ".shp"
RASTER_EXTS = {".tif", ".tiff"}
RASTER_OUTPUT_EXT = ".tif"
OUTPUT_TIERS = ("final", "staging", "tests")
ALL_TIERS = ("source", "staging", "tests", "final")
# Prefer finished products, then intermediates, then imports, then experiments.
INPUT_TIERS = ("final", "staging", "source", "tests")
# Domains without source/ (tool outputs only).
STAGE_TIERS = ("staging", "tests", "final")
# Catalog keys that use Cesium quantized-mesh folder picking.
MESH_LIBRARY_KEYS = frozenset({"quantized_mesh"})

# Legacy flat folders under Database/ (pre-map layout) — not treated as maps.
_RESERVED_TOP = frozenset(
    {
        "buildings",
        "trees",
        "roads",
        "water",
        "mesh",
        "elevation",
        "imagery",
        "outputs",
    }
)


def load_catalog(path: Optional[Path] = None) -> Dict[str, Any]:
    with open(path or CATALOG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def catalog_maps(catalog: Dict[str, Any]) -> List[Dict[str, str]]:
    maps = catalog.get("maps") or []
    out: List[Dict[str, str]] = []
    for m in maps:
        if isinstance(m, str):
            out.append({"id": m, "label": m.replace("_", " ")})
        else:
            mid = str(m.get("id") or "").strip()
            if not mid:
                continue
            out.append({"id": mid, "label": str(m.get("label") or mid.replace("_", " "))})
    return out


def list_maps(database_root: Path, catalog: Dict[str, Any]) -> List[Dict[str, str]]:
    """Catalog maps plus any extra map folders discovered on disk."""
    by_id = {m["id"]: m for m in catalog_maps(catalog)}
    if database_root.is_dir():
        for p in sorted(database_root.iterdir()):
            if not p.is_dir() or p.name.startswith("."):
                continue
            if p.name in _RESERVED_TOP:
                continue
            if p.name not in by_id:
                by_id[p.name] = {"id": p.name, "label": p.name.replace("_", " ")}
    return list(by_id.values())


def map_root(database_root: Path, map_id: str) -> Path:
    return (database_root / map_id).resolve()


def default_map_id(catalog: Dict[str, Any]) -> str:
    maps = catalog_maps(catalog)
    return maps[0]["id"] if maps else "Default"


def domain_rel(catalog: Dict[str, Any], domain_key: str) -> Optional[str]:
    domains = catalog.get("domains") or catalog.get("library_folders") or {}
    return domains.get(domain_key)


def domain_root(map_dir: Path, catalog: Dict[str, Any], domain_key: str) -> Optional[Path]:
    rel = domain_rel(catalog, domain_key)
    if not rel:
        return None
    return (map_dir / rel).resolve()


def source_domain_keys(catalog: Dict[str, Any]) -> frozenset:
    keys = catalog.get("source_domains")
    if keys is None:
        # Legacy: every domain had source/
        domains = catalog.get("domains") or catalog.get("library_folders") or {}
        return frozenset(domains.keys())
    return frozenset(str(k) for k in keys)


def domain_tiers(catalog: Dict[str, Any], domain_key: str) -> tuple:
    """Tiers that exist for a domain (source only for import domains)."""
    if domain_key in source_domain_keys(catalog):
        return tuple(catalog.get("tiers") or ALL_TIERS)
    return STAGE_TIERS


def tier_path(map_dir: Path, catalog: Dict[str, Any], domain_key: str, tier: str) -> Optional[Path]:
    root = domain_root(map_dir, catalog, domain_key)
    if root is None:
        return None
    allowed = domain_tiers(catalog, domain_key)
    if tier not in allowed and tier == "source":
        return None
    return (root / tier).resolve()


def ensure_database_layout(
    database_root: Path,
    catalog: Dict[str, Any],
    *,
    map_id: Optional[str] = None,
) -> None:
    """Create Database/<map>/{domains}/{tiers}/ — source/ only for source_domains."""
    database_root.mkdir(parents=True, exist_ok=True)
    domains = catalog.get("domains") or catalog.get("library_folders") or {}
    map_ids = [map_id] if map_id else [m["id"] for m in catalog_maps(catalog)]
    if not map_ids:
        map_ids = [default_map_id(catalog)]
    for mid in map_ids:
        base = map_root(database_root, mid)
        for key, rel in domains.items():
            for tier in domain_tiers(catalog, key):
                (base / rel / tier).mkdir(parents=True, exist_ok=True)


def sanitize_map_id(name: str) -> str:
    """Folder-safe map id: trim, spaces→_, strip Windows-illegal chars."""
    text = (name or "").strip()
    text = re.sub(r'[<>:"/\\|?*]', "", text)
    text = re.sub(r"\s+", "_", text)
    text = text.strip("._")
    return text


def create_map(
    database_root: Path,
    catalog: Dict[str, Any],
    name: str,
) -> str:
    """
    Create Database/<map_id>/ with the full domain/tier skeleton.

    Returns the map id. Raises ValueError if the name is empty/invalid.
    """
    mid = sanitize_map_id(name)
    if not mid:
        raise ValueError("Enter a map name.")
    ensure_database_layout(database_root, catalog, map_id=mid)
    return mid


def import_dest_dir(
    map_dir: Path,
    catalog: Dict[str, Any],
    library_key: str,
) -> Optional[Path]:
    """
    Folder where UI imports land for a library input.

    Prefer ``source/`` when the domain has it (raw imports); otherwise ``staging/``.
    """
    root = domain_root(map_dir, catalog, library_key)
    if root is None:
        return None
    allowed = domain_tiers(catalog, library_key)
    tier = "source" if "source" in allowed else "staging"
    if tier not in allowed:
        tier = allowed[0] if allowed else "staging"
    dest = (root / tier).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    return dest


def _primary_imported_path(copied: Sequence[Path], *, prefer_folder: bool = False) -> Optional[Path]:
    if not copied:
        return None
    if prefer_folder:
        # Caller may pass a single directory path.
        for p in copied:
            if p.is_dir():
                return p
    preferred_exts = (".shp", ".gpkg", ".geojson", ".json", ".gml", ".tif", ".tiff")
    by_ext = {p.suffix.lower(): p for p in copied if p.is_file()}
    for ext in preferred_exts:
        if ext in by_ext:
            return by_ext[ext]
    files = [p for p in copied if p.is_file()]
    return files[0] if files else Path(copied[0])


def import_paths_to_library(
    map_dir: Path,
    catalog: Dict[str, Any],
    library_key: str,
    sources: Sequence[Union[str, Path]],
    *,
    as_folder: bool = False,
) -> Dict[str, Any]:
    """
    Copy selected files (or one folder) into the library import destination.

    Never overwrites: existing names are skipped.
    Returns {dest, copied, skipped, primary}.
    """
    dest = import_dest_dir(map_dir, catalog, library_key)
    if dest is None:
        raise ValueError(f"Unknown library “{library_key}”.")

    copied: List[Path] = []
    skipped: List[str] = []

    for raw in sources:
        src = Path(raw)
        if not src.exists():
            skipped.append(src.name or str(src))
            continue
        if as_folder or src.is_dir():
            target = dest / src.name
            if target.exists():
                skipped.append(src.name)
                continue
            shutil.copytree(src, target)
            copied.append(target)
            continue
        if not src.is_file():
            skipped.append(src.name)
            continue
        target = dest / src.name
        if target.exists():
            skipped.append(src.name)
            continue
        shutil.copy2(src, target)
        copied.append(target)

    primary = _primary_imported_path(copied, prefer_folder=as_folder)
    return {
        "dest": dest,
        "copied": copied,
        "skipped": skipped,
        "primary": primary,
    }


def list_vector_files(folder: Path) -> List[Path]:
    if not folder.is_dir():
        return []
    found: List[Path] = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() not in VECTOR_EXTS:
            continue
        if p.suffix.lower() in {".shp", ".gpkg", ".geojson", ".json", ".gml"}:
            found.append(p)
    return found


def list_raster_files(folder: Path) -> List[Path]:
    if not folder.is_dir():
        return []
    found: List[Path] = []
    for p in sorted(folder.rglob("*")):
        if not p.is_file():
            continue
        if p.suffix.lower() in RASTER_EXTS:
            found.append(p)
    return found


def _subfolder_sort_key(path: Path, *, numeric: bool = False, reverse_numeric: bool = False):
    name = path.name
    if numeric or reverse_numeric:
        try:
            n = int(name)
            # reverse_numeric: best (highest) LOD first → sort key negated
            return (0, -n if reverse_numeric else n)
        except ValueError:
            return (1, name.lower())
    return (0, name.lower())


def list_subfolders(
    folder: Path,
    *,
    numeric: bool = False,
    reverse_numeric: bool = False,
) -> List[Path]:
    if not folder.is_dir():
        return []
    folders = [p for p in folder.iterdir() if p.is_dir()]
    return sorted(
        folders,
        key=lambda p: _subfolder_sort_key(
            p, numeric=numeric, reverse_numeric=reverse_numeric
        ),
    )


def _has_xy_terrain(folder: Path) -> bool:
    """True if folder looks like one LOD: {x}/{y}.terrain."""
    if not folder.is_dir():
        return False
    try:
        for x_dir in folder.iterdir():
            if not x_dir.is_dir() or not x_dir.name.isdigit():
                continue
            for terrain in x_dir.glob("*.terrain"):
                if terrain.stem.isdigit():
                    return True
    except OSError:
        return False
    return False


def _is_mesh_tileset(folder: Path) -> bool:
    """True if folder holds one or more numeric LOD dirs with tiles."""
    if not folder.is_dir():
        return False
    try:
        for child in folder.iterdir():
            if child.is_dir() and child.name.isdigit() and _has_xy_terrain(child):
                return True
    except OSError:
        return False
    return False


def _input_choice_label(tier: str, rest: str = "", *, folder: bool = False) -> str:
    """
    UI label for a Database pick.

    source tier: hide file/folder names — just ``source`` (optional ``/LOD``).
    staging/tests/final: ``tier/name``.
    """
    rest = (rest or "").replace("\\", "/").strip("/")
    if tier == "source":
        if rest.isdigit():
            label = f"source/{rest}"
        elif not rest:
            label = "source"
        else:
            # Multiple source assets: stem only, no extension
            label = f"source ({Path(rest).stem})"
    else:
        label = f"{tier}/{rest}" if rest else tier
    if folder and not label.endswith("/") and label != "source":
        label += "/"
    return label


def list_mesh_choices(
    tier_dir: Path,
    tier: str,
    *,
    mesh_pick: str = "lod",
) -> List[Dict[str, str]]:
    """
    Mesh library picks under a tier folder.

    Layout:
      {tier}/{tileset}/{lod}/{x}/{y}.terrain   (preferred)
      {tier}/{lod}/{x}/{y}.terrain             (legacy single-LOD at tier)

    mesh_pick:
      lod      — highest LOD only (auto-picked; other levels hidden)
      tileset  — full tileset root (all LODs)
    """
    choices: List[Dict[str, str]] = []
    if not tier_dir.is_dir():
        return choices

    pick = (mesh_pick or "lod").strip().lower()
    children = list_subfolders(tier_dir)

    if pick == "tileset":
        tilesets = [c for c in children if _is_mesh_tileset(c)]
        for child in tilesets:
            if tier == "source" and len(tilesets) == 1:
                rest = ""
            elif tier == "source":
                rest = child.name
            else:
                rest = child.name
            choices.append(
                {
                    "label": _input_choice_label(tier, rest, folder=True),
                    "path": str(child),
                }
            )
        return choices

    # Default: highest LOD only (one choice per tileset / legacy root).
    tilesets = [c for c in children if _is_mesh_tileset(c)]
    legacy_lods = [
        c
        for c in children
        if _has_xy_terrain(c) and c.name.isdigit()
    ]
    if legacy_lods:
        best = max(legacy_lods, key=lambda p: int(p.name))
        rest = "" if tier == "source" else best.name
        choices.append(
            {
                "label": _input_choice_label(tier, rest, folder=True),
                "path": str(best),
            }
        )

    for child in tilesets:
        lods = [
            c
            for c in child.iterdir()
            if c.is_dir() and c.name.isdigit() and _has_xy_terrain(c)
        ]
        if not lods:
            continue
        best = max(lods, key=lambda p: int(p.name))
        hide_tileset = tier == "source" and len(tilesets) == 1
        if hide_tileset:
            rest = ""
        elif tier == "source":
            rest = child.name
        else:
            # staging/tests/final: tileset name only (LOD is implied = highest)
            rest = child.name
        choices.append(
            {
                "label": _input_choice_label(tier, rest, folder=True),
                "path": str(best),
            }
        )

    return choices


def library_choices(
    map_dir: Path,
    catalog: Dict[str, Any],
    library_key: str,
    kind: str,
    *,
    tiers: Optional[Sequence[str]] = None,
    mesh_pick: Optional[str] = None,
) -> List[Dict[str, str]]:
    """Return [{label, path}, ...] scanning domain tiers under the active map."""
    root = domain_root(map_dir, catalog, library_key)
    if root is None:
        return []
    allowed = domain_tiers(catalog, library_key)
    if tiers is not None:
        scan_tiers = [t for t in tiers if t in allowed]
    else:
        preferred = list(catalog.get("input_tiers") or INPUT_TIERS)
        scan_tiers = [t for t in preferred if t in allowed]
        for t in allowed:
            if t not in scan_tiers:
                scan_tiers.append(t)
    choices: List[Dict[str, str]] = []
    for tier in scan_tiers:
        tier_dir = root / tier
        if library_key in MESH_LIBRARY_KEYS and kind in ("folder", "folder_output"):
            choices.extend(
                list_mesh_choices(
                    tier_dir,
                    tier,
                    mesh_pick=mesh_pick or "lod",
                )
            )
        elif kind in ("folder", "folder_output"):
            folders = list_subfolders(tier_dir)
            for p in folders:
                if tier == "source" and len(folders) == 1:
                    rest = ""
                else:
                    rest = p.name
                choices.append(
                    {
                        "label": _input_choice_label(tier, rest, folder=True),
                        "path": str(p),
                    }
                )
        elif kind in ("raster_file", "raster_output"):
            files = list_raster_files(tier_dir)
            for p in files:
                if tier == "source" and len(files) == 1:
                    rest = ""
                elif tier == "source":
                    rest = p.stem
                else:
                    rest = p.relative_to(tier_dir).as_posix()
                choices.append(
                    {
                        "label": _input_choice_label(tier, rest),
                        "path": str(p),
                    }
                )
        else:
            files = list_vector_files(tier_dir)
            for p in files:
                if tier == "source" and len(files) == 1:
                    rest = ""
                elif tier == "source":
                    rest = p.stem  # name only, no type — becomes source (stem)
                else:
                    rest = p.relative_to(tier_dir).as_posix()
                choices.append(
                    {
                        "label": _input_choice_label(tier, rest),
                        "path": str(p),
                    }
                )
    return choices


def output_name_stem(name: str) -> str:
    """Strip directory bits and a known vector/raster extension from a name."""
    raw = (name or "").strip().replace("\\", "/").lstrip("/")
    if not raw:
        return ""
    base = Path(raw).name
    suffix = Path(base).suffix.lower()
    if suffix in VECTOR_EXTS or suffix in RASTER_EXTS:
        return Path(base).stem
    return base


def finalize_output_filename(param: Dict[str, Any], name: str) -> str:
    """
    Turn a user-facing name (no type) into the on-disk file/folder name.

    vector_output → .shp; raster_output → .tif; folder_output → bare folder name.
    """
    stem = output_name_stem(name)
    if not stem:
        stem = output_name_stem(str(param.get("default_name") or "output")) or "output"
    if param.get("type") == "folder_output":
        return stem
    if param.get("type") == "raster_output":
        return stem + RASTER_OUTPUT_EXT
    # vector_output and any other file sinks from the UI
    return stem + VECTOR_OUTPUT_EXT


_SHP_EXISTENCE_EXTS = (
    ".shp",
    ".shx",
    ".dbf",
    ".prj",
    ".cpg",
    ".qpj",
    ".sbn",
    ".sbx",
    ".qmd",
)


def output_destination_taken(path: Union[str, Path], *, is_folder: bool = False) -> bool:
    """True if saving here would collide with an existing file/folder (no overwrite)."""
    p = Path(path)
    if is_folder:
        return p.exists()
    if p.suffix.lower() == ".shp":
        stem = p.with_suffix("")
        return any(stem.with_suffix(ext).exists() for ext in _SHP_EXISTENCE_EXTS)
    return p.exists()


def default_output_spec(
    param: Dict[str, Any],
    *,
    tier: Optional[str] = None,
) -> Dict[str, str]:
    chosen = tier if tier in ("staging", "final") else None
    if chosen is None:
        raw = str(param.get("default_tier") or "final")
        chosen = raw if raw in ("staging", "final") else "final"
    return {
        "tier": chosen,
        "name": output_name_stem(str(param.get("default_name") or "output")),
    }


def resolve_output_path(
    map_dir: Path,
    catalog: Dict[str, Any],
    param: Dict[str, Any],
    spec: Any,
) -> str:
    """Turn {tier, name} into Database/<map>/<domain>/<tier>/<name[.shp]>."""
    domain_key = param.get("domain") or param.get("library") or "outputs"
    if isinstance(spec, dict):
        tier = str(spec.get("tier") or "staging")
        name = str(spec.get("name") or param.get("default_name") or "output").strip()
    elif isinstance(spec, str) and spec:
        p = Path(spec)
        if p.is_absolute() or len(p.parts) > 1:
            return str(p)
        name = spec
        tier = "staging"
    else:
        d = default_output_spec(param)
        tier, name = d["tier"], d["name"]

    if tier not in OUTPUT_TIERS:
        tier = "staging"
    filename = finalize_output_filename(param, name)

    dest_dir = tier_path(map_dir, catalog, domain_key, tier)
    if dest_dir is None:
        dest_dir = (map_dir / "misc" / tier).resolve()
    dest_dir.mkdir(parents=True, exist_ok=True)
    return str((dest_dir / filename).resolve())


def tools_for_tab(catalog: Dict[str, Any], tab_id: str) -> List[Dict[str, Any]]:
    return [t for t in catalog.get("tools", []) if tab_id in t.get("tabs", [])]


def tool_by_id(catalog: Dict[str, Any], tool_id: str) -> Optional[Dict[str, Any]]:
    for t in catalog.get("tools", []):
        if t["id"] == tool_id:
            return t
    return None


def primary_output_param(tool: Dict[str, Any]) -> Optional[str]:
    for p in tool.get("params", []):
        if p.get("type") in ("vector_output", "folder_output"):
            return p["id"]
    return None


def resolve_pipeline(
    catalog: Dict[str, Any],
    selected_ids: set,
) -> tuple[List[str], Dict[str, Dict[str, tuple[str, str]]]]:
    ordered: List[str] = []
    wires: Dict[str, Dict[str, tuple[str, str]]] = {}
    placed: set = set()

    for pipeline in catalog.get("pipelines") or []:
        steps = pipeline.get("steps") or []
        selected_steps = [s for s in steps if s.get("id") in selected_ids]
        prev_tool_id: Optional[str] = None
        prev_out: Optional[str] = None
        for step in selected_steps:
            tid = step["id"]
            tool = tool_by_id(catalog, tid)
            if not tool:
                continue
            ordered.append(tid)
            placed.add(tid)
            step_wires: Dict[str, tuple[str, str]] = {}
            for param_id, src in (step.get("wire") or {}).items():
                if src == "previous" and prev_tool_id and prev_out:
                    step_wires[param_id] = (prev_tool_id, prev_out)
            if step_wires:
                wires[tid] = step_wires
            prev_tool_id = tid
            prev_out = primary_output_param(tool)

    for t in catalog.get("tools", []):
        tid = t["id"]
        if tid in selected_ids and tid not in placed:
            ordered.append(tid)

    return ordered, wires
