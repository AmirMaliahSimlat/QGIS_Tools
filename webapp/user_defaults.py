# -*- coding: utf-8 -*-
"""Persisted per-tool parameter defaults (overrides catalog defaults)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULTS_PATH = Path(__file__).resolve().parent / "user_defaults.json"

# tool_id -> param_id -> value
_cache: Optional[Dict[str, Dict[str, Any]]] = None


def _load() -> Dict[str, Dict[str, Any]]:
    global _cache
    if _cache is not None:
        return _cache
    if DEFAULTS_PATH.is_file():
        try:
            raw = json.loads(DEFAULTS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _cache = {
                    str(tid): dict(params)
                    for tid, params in raw.items()
                    if isinstance(params, dict)
                }
                return _cache
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    _cache = {}
    return _cache


def reload() -> None:
    global _cache
    _cache = None
    _load()


def get(tool_id: str, param_id: str) -> Any:
    """Return saved default, or raise KeyError if unset."""
    return _load()[tool_id][param_id]


def has(tool_id: str, param_id: str) -> bool:
    return param_id in _load().get(tool_id, {})


def effective(tool_id: str, param: Dict[str, Any]) -> Any:
    """User override if set, else catalog ``default`` (KeyError if neither)."""
    pid = param["id"]
    if has(tool_id, pid):
        return get(tool_id, pid)
    if "default" in param:
        return param["default"]
    raise KeyError(pid)


def set_default(tool_id: str, param_id: str, value: Any) -> None:
    data = _load()
    bucket = data.setdefault(tool_id, {})
    bucket[param_id] = value
    DEFAULTS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
