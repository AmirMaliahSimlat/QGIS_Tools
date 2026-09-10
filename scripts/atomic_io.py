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


def _as_path(value: Any) -> Optional[Path]:
    if value is None:
        return None
    if isinstance(value, Path):
        text = str(value)
    else:
        text = str(value).strip()
    if not text:
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
    """Sibling path used while writing (file or directory)."""
    return final.with_name(final.name + ".partial")


@dataclass
class AtomicOutput:
    """Tracks a pending atomic publish from temp → final."""

    final: Path
    temp: Path
    is_dir: bool = False
    _done: bool = False

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
            # Retry briefly — GeoPackage may still be flushing.
            last_exc: Optional[BaseException] = None
            for _ in range(10):
                try:
                    os.replace(str(self.temp), str(self.final))
                    last_exc = None
                    break
                except PermissionError as exc:
                    last_exc = exc
                    time.sleep(0.15)
            if last_exc is not None:
                raise last_exc
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
                if self.temp.is_file():
                    self.temp.unlink(missing_ok=True)
                # Sidecar leftovers (rare)
                for side in self.temp.parent.glob(self.temp.name + ".*"):
                    try:
                        side.unlink(missing_ok=True)
                    except OSError:
                        pass
        finally:
            self._done = True


def begin_atomic_file_output(
    parameters: Parameters,
    key: str = "OUTPUT",
) -> Tuple[Parameters, Optional[AtomicOutput]]:
    """
    Redirect a filesystem file OUTPUT to ``<name>.partial`` for writing.

    Returns updated parameters (copy) and an AtomicOutput handle, or
    (original parameters, None) when OUTPUT is not a plain file path.
    """
    final = _as_path(parameters.get(key))
    if final is None:
        return parameters, None

    temp = partial_path_for(final)
    if temp.exists():
        if temp.is_dir():
            shutil.rmtree(temp, ignore_errors=True)
        else:
            temp.unlink(missing_ok=True)

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


def finish_or_abandon(
    handle: Optional[AtomicOutput],
    *,
    ok: bool,
    sink: Any = None,
) -> Optional[str]:
    """
    Close sink (if given), then finalize or abandon.

    Returns the final path string when ok and handle was used.
    """
    # Drop sink so Windows releases the GeoPackage lock.
    if sink is not None:
        try:
            del sink
        except Exception:
            pass

    if handle is None:
        return None
    if ok:
        return str(handle.finalize())
    handle.abandon()
    return None
