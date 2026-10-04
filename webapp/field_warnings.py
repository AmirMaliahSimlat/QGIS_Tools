# -*- coding: utf-8 -*-
"""Detect input attributes that tools will replace in a new output copy."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

import shapefile

# Shapefile / DBF stores at most 10 characters per field name.
_DBF_NAME_LEN = 10


def vector_field_names(path: Path) -> List[str]:
    """Return attribute names for a vector file (shp / geojson)."""
    path = Path(path)
    if not path.is_file():
        return []
    suf = path.suffix.lower()
    if suf == ".shp":
        reader = shapefile.Reader(str(path))
        try:
            return [str(f[0]) for f in reader.fields[1:]]
        finally:
            reader.close()
    if suf in {".geojson", ".json"}:
        data = json.loads(path.read_text(encoding="utf-8"))
        feats = data.get("features") or []
        names: Set[str] = set()
        for feat in feats[:50]:
            props = feat.get("properties") or {}
            names.update(str(k) for k in props.keys())
        return sorted(names)
    return []


def _name_keys(name: str) -> Set[str]:
    n = name.strip()
    if not n:
        return set()
    keys = {n.lower(), n[:_DBF_NAME_LEN].lower()}
    return keys


def matching_fields(
    existing: Sequence[str], written: Sequence[str]
) -> List[str]:
    """
    Return catalog field names that already appear on the layer.

    Matches full names and DBF-truncated forms (e.g. long names shortened to 10 chars).
    """
    existing_keys: Set[str] = set()
    for name in existing:
        existing_keys |= _name_keys(str(name))
    hits: List[str] = []
    for name in written:
        if _name_keys(name) & existing_keys:
            hits.append(name)
    return hits


def format_warning_lines(warnings: Iterable[Dict[str, Any]]) -> List[str]:
    lines: List[str] = []
    for w in warnings:
        fields = ", ".join(w.get("fields") or [])
        tool = w.get("tool_name") or w.get("tool_id") or "tool"
        src = w.get("input_path") or "(input)"
        lines.append(f"• {tool}: {fields}")
        lines.append(f"    input: {src}")
    return lines
