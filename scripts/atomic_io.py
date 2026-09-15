# -*- coding: utf-8 -*-
"""
Atomic file outputs for Processing scripts.

Write to a sibling ``*.partial`` path, then ``os.replace`` into the final
location only after a successful finish. On cancel/error the partial is
deleted so the named output is never left half-written.

Note: a hard kill (Task Manager / kill -9) can still leave a ``*.partial``
orphan; the final path itself stays untouched until replace succeeds.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union

Parameters = Dict[str, Any]


def _parse_qgs_property_repr(text: str) -> Optional[str]:
    """Extract static payload from ``<QgsProperty: static (...)>`` reprs."""
    import re

    text = text.strip()
    if "QgsProperty" not in text:
        return None
    # static (VALUE) — VALUE may contain spaces / drive paths
    m = re.search(r"static\s*\((.*)\)\s*>?\s*$", text, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    return m.group(1).strip()


def _unwrap_qgs_property(value: Any) -> Any:
    """
    Resolve a QgsProperty to its static destination string.

    ``str(QgsProperty)`` looks like ``<QgsProperty: static (TEMPORARY_OUTPUT)>``
    and must never be used as a filesystem path.
    """
    type_name = type(value).__name__
    if type_name != "QgsProperty":
        # Already stringified somewhere upstream
        if isinstance(value, str) and "QgsProperty" in value:
            parsed = _parse_qgs_property_repr(value)
            return parsed if parsed is not None else None
        return value

    # Prefer staticValue() — valueAsString()/value() often need an expression context.
    try:
        if hasattr(value, "isStatic") and callable(value.isStatic) and value.isStatic():
            return value.staticValue()
    except Exception:
        pass
    try:
        static_val = value.staticValue()
        if static_val is not None and str(static_val).strip() != "":
            return static_val
    except Exception:
        pass
    try:
        as_str = value.valueAsString()
        if as_str and "QgsProperty" not in str(as_str):
            return as_str
    except Exception:
        pass

    parsed = _parse_qgs_property_repr(str(value))
    return parsed  # may be None — caller must not Path() the property


def _sink_destination(value: Any) -> Any:
    """
    Unwrap QGIS output destinations to a path / TEMPORARY_OUTPUT / memory URI.

    Toolbox FeatureSink values are often ``QgsProcessingOutputLayerDefinition``
    and/or ``QgsProperty``. Their ``str(...)`` forms are not filesystem paths.
    """
    if value is None:
        return None

    # Unwrap nested wrappers a few times (definition → property → string).
    for _ in range(4):
        if value is None:
            return None
        type_name = type(value).__name__
        if type_name == "QgsProcessingOutputLayerDefinition" or (
            hasattr(value, "sink") and "OutputLayerDefinition" in type_name
        ):
            value = getattr(value, "sink", value)
            continue
        if type_name == "QgsProperty":
            value = _unwrap_qgs_property(value)
            continue
        if isinstance(value, str) and "QgsProperty" in value:
            value = _unwrap_qgs_property(value)
            continue
        # Unknown wrapper whose repr is a QgsProperty (defensive).
        if not isinstance(value, (str, Path, bytes, int, float, bool)):
            text = str(value)
            if text.startswith("<QgsProperty:") or "QgsProperty: static" in text:
                value = _parse_qgs_property_repr(text)
                continue
        break

    if value is not None and type(value).__name__ == "QgsProperty":
        return None
    if isinstance(value, str) and (
        "QgsProperty" in value or "QgsProcessingOutputLayerDefinition" in value
    ):
        return None
    return value


def _as_path(value: Any) -> Optional[Path]:
    value = _sink_destination(value)
    if value is None:
        return None
    if isinstance(value, Path):
        text = str(value)
    else:
        text = str(value).strip()
    if not text:
        return None
    # Never treat QGIS object reprs as paths.
    if "QgsProcessingOutputLayerDefinition" in text or "QgsProperty" in text:
        return None
    # Memory / temporary Processing destinations — not filesystem paths.
    lower = text.lower()
    if lower in {"memory:", "temporary_output"} or lower.startswith("memory:"):
        return None
    if text.startswith("memory://"):
        return None
    # Layer ids / URI schemes without a normal path
    if "://" in text and not (text[1:3] == ":\\" or text.startswith("\\\\")):
        # e.g. postgres:// — leave alone
        if not text.lower().startswith("file:"):
            return None
    return Path(text)


def partial_path_for(final: Path) -> Path:
    """
    Sibling path used while writing (file or directory).

    For files with a suffix, insert ``.partial`` *before* the extension
    (``out.gpkg`` → ``out.partial.gpkg``). OGR/GeoPackage often appends the
    driver extension to the given path; writing to ``out.gpkg.partial`` would
    create ``out.gpkg.partial.gpkg`` and break finalize.
    """
    if final.suffix:
        return final.with_name(final.stem + ".partial" + final.suffix)
    return final.with_name(final.name + ".partial")


def _existing_temp_candidates(temp: Path, final: Path) -> list[Path]:
    """Paths that may hold the written partial (legacy + current layouts)."""
    cands = [temp]
    # Legacy: final=out.gpkg → temp was out.gpkg.partial, OGR wrote out.gpkg.partial.gpkg
    if final.suffix:
        legacy = final.with_name(final.name + ".partial")
        cands.append(legacy)
        cands.append(Path(str(legacy) + final.suffix))
        cands.append(Path(str(temp) + final.suffix))
    # De-dupe while preserving order
    seen = set()
    out: list[Path] = []
    for p in cands:
        key = str(p).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


@dataclass
class AtomicOutput:
    """Tracks a pending atomic publish from temp → final."""

    final: Path
    temp: Path
    is_dir: bool = False
    _done: bool = False

    def _resolve_temp(self) -> Path:
        if self.is_dir:
            return self.temp
        for cand in _existing_temp_candidates(self.temp, self.final):
            if cand.is_file():
                return cand
        return self.temp

    def finalize(self) -> Path:
        if self._done:
            return self.final
        self.final.parent.mkdir(parents=True, exist_ok=True)
        # Close handles before replace (caller must drop sink refs first).
        if self.is_dir:
            if self.final.exists():
                shutil.rmtree(self.final, ignore_errors=False)
            # replace() works for dirs on Windows only when dest absent
            os.rename(str(self.temp), str(self.final))
        else:
            written = self._resolve_temp()
            if not written.is_file():
                raise FileNotFoundError(
                    f"Atomic partial missing (looked for {self.temp} and variants)"
                )
            _publish_file_with_retries(written, self.final)
            # Clean any leftover sibling partials from alternate naming.
            for cand in _existing_temp_candidates(self.temp, self.final):
                if cand != self.final and cand.is_file():
                    try:
                        cand.unlink(missing_ok=True)
                    except OSError:
                        pass
        self._done = True
        return self.final

    def abandon(self) -> None:
        if self._done:
            return
        try:
            if self.is_dir:
                if self.temp.is_dir():
                    shutil.rmtree(self.temp, ignore_errors=True)
            else:
                for cand in _existing_temp_candidates(self.temp, self.final):
                    try:
                        if cand.is_file():
                            cand.unlink(missing_ok=True)
                        for side in cand.parent.glob(cand.name + ".*"):
                            try:
                                side.unlink(missing_ok=True)
                            except OSError:
                                pass
                    except OSError:
                        pass
        finally:
            self._done = True


def _is_sqlite_container(path: Path) -> bool:
    return path.suffix.lower() in {".gpkg", ".sqlite", ".db"}


def _remove_vector_path(path: Path) -> None:
    """Delete a vector file and common sidecars (shapefile / gpkg rtree)."""
    if path.suffix.lower() == ".shp":
        stem = path.with_suffix("")
        for ext in (
            ".shp",
            ".shx",
            ".dbf",
            ".prj",
            ".cpg",
            ".qpj",
            ".sbn",
            ".sbx",
        ):
            try:
                stem.with_suffix(ext).unlink(missing_ok=True)
            except OSError:
                pass
        return
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
    for side in path.parent.glob(path.name + ".*"):
        try:
            side.unlink(missing_ok=True)
        except OSError:
            pass


def begin_atomic_file_output(
    parameters: Parameters,
    key: str = "OUTPUT",
) -> Tuple[Parameters, Optional[AtomicOutput]]:
    """
    Redirect a filesystem file OUTPUT to ``<name>.partial`` for writing.

    Returns updated parameters (copy) and an AtomicOutput handle, or
    (original parameters, None) when OUTPUT is not a plain file path.

    Shapefile outputs skip atomic rename: OGR writes several sidecars, and
    FeatureSink keeps handles open that block a clean multi-file rename.
    """
    final = _as_path(parameters.get(key))
    if final is None:
        # Temporary / memory / unrecognized — leave QGIS destination as-is.
        return parameters, None

    # Shapefile: write directly to the final path (multi-file format).
    # Normalize to a plain path string so parameterAsSink never sees a
    # QgsProcessingOutputLayerDefinition repr.
    if final.suffix.lower() == ".shp":
        _remove_vector_path(final)
        for cand in _existing_temp_candidates(partial_path_for(final), final):
            _remove_vector_path(cand)
        new_params = dict(parameters)
        new_params[key] = str(final)
        return new_params, None

    temp = partial_path_for(final)
    # Remove any leftover partials from current or legacy naming.
    for cand in _existing_temp_candidates(temp, final):
        if cand.exists():
            if cand.is_dir():
                shutil.rmtree(cand, ignore_errors=True)
            else:
                _remove_vector_path(cand)

    temp.parent.mkdir(parents=True, exist_ok=True)
    new_params = dict(parameters)
    new_params[key] = str(temp)
    return new_params, AtomicOutput(final=final, temp=temp, is_dir=False)


def begin_atomic_dir_output(
    final_dir: Union[str, Path],
) -> AtomicOutput:
    """
    Prepare a directory OUTPUT that will be published only on success.

    Work happens in ``<final>.partial/``; on finalize it is renamed to ``final``.
    """
    final = Path(final_dir)
    temp = partial_path_for(final)
    if temp.exists():
        shutil.rmtree(temp, ignore_errors=True)
    if final.exists() and final.is_dir() and any(final.iterdir()):
        raise FileExistsError(
            f"Output folder exists and is not empty: {final}"
        )
    # Do not create ``temp`` here — copytree/robocopy create the destination.
    temp.parent.mkdir(parents=True, exist_ok=True)
    return AtomicOutput(final=final, temp=temp, is_dir=True)


def _release_dest_layer(context: Any, dest_id: Any) -> None:
    """
    Drop Processing's open output layer so Windows can rename the GeoPackage.

    ``parameterAsSink`` keeps a QgsVectorLayer on the partial path (plus an
    SQLite rtree sidecar). Renaming while that layer is alive → WinError 32.
    """
    if context is None or dest_id is None:
        return
    dest_id = str(dest_id)
    try:
        details = context.layersToLoadOnCompletion()
        if isinstance(details, dict) and dest_id in details:
            details.pop(dest_id, None)
    except Exception:
        pass
    try:
        store = context.temporaryLayerStore()
    except Exception:
        store = None
    if store is None:
        return
    try:
        layer = store.mapLayer(dest_id)
        if layer is not None:
            store.removeMapLayer(dest_id)
    except Exception:
        pass


def _flush_gpkg_handles(path: Path) -> None:
    """Best-effort GDAL flush if the file is still in the GDAL cache."""
    try:
        from osgeo import gdal

        ds = gdal.OpenEx(str(path), gdal.OF_UPDATE | gdal.OF_VECTOR)
        if ds is not None:
            ds.FlushCache()
            ds = None
    except Exception:
        pass


def _sqlite_backup_publish(written: Path, final: Path) -> None:
    """Safe publish for GeoPackage: SQLite online backup (not byte-copy)."""
    import sqlite3

    if final.exists():
        final.unlink()
    src = sqlite3.connect(str(written))
    try:
        dst = sqlite3.connect(str(final))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    check = sqlite3.connect(str(final))
    try:
        row = check.execute("PRAGMA integrity_check").fetchone()
        if not row or row[0] != "ok":
            raise RuntimeError(f"Published GeoPackage failed integrity_check: {row}")
    finally:
        check.close()
    try:
        written.unlink(missing_ok=True)
    except OSError:
        pass


def _publish_file_with_retries(written: Path, final: Path) -> None:
    """Rename partial → final. Never byte-copy a live GeoPackage/SQLite DB."""
    import gc

    last_exc: Optional[BaseException] = None
    for attempt in range(60):
        try:
            os.replace(str(written), str(final))
            return
        except OSError as exc:
            winerr = getattr(exc, "winerror", None)
            locked = (
                isinstance(exc, PermissionError)
                or winerr == 32
                or "being used" in str(exc).lower()
            )
            if not locked:
                raise
            last_exc = exc
            if attempt == 0:
                _flush_gpkg_handles(written)
            if attempt in (5, 15, 30):
                gc.collect()
            # Byte-copy of a locked GeoPackage corrupts SQLite (invalid rootpage).
            # Use the SQLite backup API once the DB is readable.
            if attempt >= 10 and _is_sqlite_container(written):
                try:
                    _sqlite_backup_publish(written, final)
                    return
                except Exception as backup_exc:
                    last_exc = backup_exc
            time.sleep(0.2 if attempt < 20 else 0.4)
    if last_exc is not None:
        raise last_exc
    raise PermissionError(f"Could not publish {written} → {final}")

def finish_or_abandon(
    handle: Optional[AtomicOutput],
    *,
    ok: bool,
    sink: Any = None,
    context: Any = None,
    dest_id: Any = None,
) -> Optional[str]:
    """
    Close sink, then finalize or abandon.

    Returns the final path string when ok and an atomic handle was used.
    When ``handle`` is None (temporary output / shapefile), the Processing
    ``dest_id`` layer is left alone so QGIS can add it to the project.
    Pass ``context`` + ``dest_id`` only for atomic GeoPackage renames that
    need the file unlocked on Windows.
    """
    import gc

    # Drop sink so writers flush / release handles.
    if sink is not None:
        try:
            del sink
        except Exception:
            pass
        sink = None

    if handle is None:
        # Temporary layer or direct file write — keep dest_id registered for
        # layersToLoadOnCompletion / temporaryLayerStore.
        gc.collect()
        return None

    # Atomic rename: Processing still holds the partial path open via dest_id.
    _release_dest_layer(context, dest_id)
    gc.collect()
    # GeoPackage / rtree often keep a short-lived lock after close.
    time.sleep(0.5)

    if ok:
        return str(handle.finalize())
    handle.abandon()
    return None
