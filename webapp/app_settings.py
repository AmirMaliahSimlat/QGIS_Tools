# -*- coding: utf-8 -*-
"""Persisted local UI settings (machine paths, etc.)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

SETTINGS_PATH = Path(__file__).resolve().parent / "app_settings.json"

# Relative to the QGIS install folder (e.g. C:\\Program Files\\QGIS 3.44.12).
QGIS_PROCESS_REL = Path("bin") / "qgis_process-qgis-ltr.bat"

_cache: Optional[Dict[str, Any]] = None


def _load() -> Dict[str, Any]:
    global _cache
    if _cache is not None:
        return _cache
    if SETTINGS_PATH.is_file():
        try:
            raw = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _cache = dict(raw)
                return _cache
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    _cache = {}
    return _cache


def reload() -> None:
    global _cache
    _cache = None
    _load()


def get(key: str, default: Any = None) -> Any:
    return _load().get(key, default)


def set_value(key: str, value: Any) -> None:
    data = _load()
    if value is None or value == "":
        data.pop(key, None)
    else:
        data[key] = value
    SETTINGS_PATH.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def qgis_process_bat_from_root(root: str | Path) -> Path:
    """Install folder → bin\\qgis_process-qgis-ltr.bat."""
    return (Path(root) / QGIS_PROCESS_REL).resolve()


def root_from_qgis_process_bat(bat: str | Path) -> Path:
    """Infer install folder from a …/bin/qgis_process*.bat path."""
    path = Path(bat)
    if path.parent.name.lower() == "bin":
        return path.parent.parent.resolve()
    return path.parent.resolve()


def get_qgis_root() -> Optional[str]:
    raw = get("qgis_root")
    if raw is None:
        # Migrate older full-.bat setting if present.
        legacy = get("qgis_process_bat")
        if legacy:
            try:
                root = root_from_qgis_process_bat(str(legacy))
                set_qgis_root(str(root))
                set_value("qgis_process_bat", None)
                return str(root)
            except OSError:
                return None
        return None
    text = str(raw).strip()
    return text or None


def set_qgis_root(path: Optional[str]) -> None:
    set_value("qgis_root", (path or "").strip() or None)
    # Drop legacy key once we store the folder.
    if get("qgis_process_bat") is not None:
        set_value("qgis_process_bat", None)
