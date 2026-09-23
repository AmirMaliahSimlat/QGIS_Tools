# -*- coding: utf-8 -*-
"""Process-pool helpers for QGIS Processing scripts (Windows-safe spawn)."""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence, Tuple


def resolve_workers(requested: int, cap: int = 8) -> int:
    """
    Resolve worker count.

    ``requested``: 0 = auto (min(cpu_count, cap)), 1+ = exact count (at least 1).
    """
    n = int(requested)
    if n <= 0:
        cpu = os.cpu_count() or 1
        return max(1, min(int(cpu), int(cap)))
    return max(1, n)


def _is_real_python(path: Path) -> bool:
    """True for a standalone python.exe — not qgis-bin / qgis_process."""
    try:
        if not path.is_file():
            return False
    except OSError:
        return False
    name = path.name.lower()
    # Never treat the QGIS application as a worker interpreter.
    if name.startswith("qgis") or name.startswith("qgis-") or "qgis_process" in name:
        return False
    return name.startswith("python") and name.endswith(".exe")


def ensure_worker_python() -> Optional[str]:
    """
    Locate a real ``python.exe`` for ProcessPoolExecutor workers.

    When algorithms run under ``qgis_process.exe`` or ``qgis-bin.exe``,
    ``sys.executable`` is that binary. Spawning workers with it either fails
    (``Command -c not known!``) or opens **empty QGIS windows** that look like
    missing layers. Point multiprocessing at QGIS's ``python.exe`` instead.
    """
    exe = Path(sys.executable).resolve() if sys.executable else None
    if exe is not None and _is_real_python(exe):
        return str(exe)

    candidates: List[Path] = []
    if exe is not None:
        bin_dir = exe.parent
        candidates.append(bin_dir / "python.exe")
        candidates.append(bin_dir / "python3.exe")
        # qgis_process / qgis-bin live in apps/qgis*/bin or bin/
        for up in (
            bin_dir,
            bin_dir.parent,
            bin_dir.parent.parent,
            bin_dir.parent.parent.parent,
        ):
            candidates.append(up / "bin" / "python.exe")
            candidates.extend(sorted(up.glob("apps/Python*/python.exe")))
            candidates.extend(sorted(up.glob("apps/Python*/python3.exe")))

    # Explicit QGIS env vars when present
    for env_key in ("QGIS_PREFIX_PATH", "OSGEO4W_ROOT", "PYTHONHOME"):
        root = os.environ.get(env_key)
        if not root:
            continue
        root_p = Path(root)
        candidates.append(root_p / "bin" / "python.exe")
        candidates.extend(sorted(root_p.glob("apps/Python*/python.exe")))

    prog = Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    if prog.is_dir():
        for qgis_dir in sorted(prog.glob("QGIS*"), reverse=True):
            candidates.append(qgis_dir / "bin" / "python.exe")
            candidates.extend(sorted(qgis_dir.glob("apps/Python*/python.exe")))

    seen = set()
    for cand in candidates:
        try:
            key = str(cand.resolve()).lower()
        except OSError:
            continue
        if key in seen:
            continue
        seen.add(key)
        if _is_real_python(cand):
            return str(cand.resolve())
    return None


def spawn_context_for_workers(
    feedback=None,
) -> Tuple[Optional[Any], Optional[str]]:
    """
    Build a spawn multiprocessing context bound to real ``python.exe``.

    Returns ``(mp_context, python_path)`` or ``(None, None)`` if workers
    cannot be started safely (caller should run serially).
    """
    worker_py = ensure_worker_python()
    if not worker_py:
        if feedback is not None and hasattr(feedback, "pushWarning"):
            feedback.pushWarning(
                "Could not locate python.exe for worker processes; "
                "falling back to serial execution. "
                "(Using qgis-bin/qgis_process as workers opens empty QGIS "
                "windows and breaks the run.)"
            )
        return None, None

    # Bind BOTH the spawn context and the global setter — some Python/QGIS
    # builds only honour one of them when the parent is not python.exe.
    try:
        ctx = mp.get_context("spawn")
    except ValueError:
        ctx = mp.get_context()
    try:
        ctx.set_executable(worker_py)
    except Exception:
        pass
    try:
        mp.set_executable(worker_py)
    except Exception:
        pass

    if feedback is not None and hasattr(feedback, "pushInfo"):
        parent = sys.executable or "(unknown)"
        feedback.pushInfo(
            f"Worker interpreter: {worker_py} (parent was {parent})"
        )
    return ctx, worker_py


def _pool_initializer(scripts_root: Optional[str]) -> None:
    if not scripts_root:
        return
    # os.pathsep joins several roots (tool folder + scripts/) so workers can
    # import both the local module and shared helpers.
    for part in str(scripts_root).split(os.pathsep):
        if part and part not in sys.path:
            sys.path.insert(0, part)


def map_in_processes(
    fn: Callable[[Any], Any],
    tasks: Sequence[Any],
    workers: int,
    scripts_root: Optional[str] = None,
    feedback=None,
    progress_label: str = "Parallel work",
) -> List[Any]:
    """
    Map ``fn`` over ``tasks`` in a process pool.

    When ``workers == 1`` or there is at most one task, runs in-process
    (no pool). Returns results in **task order**.

    Workers never receive QGIS layers — only picklable Python data the parent
    already extracted (paths, coordinates, snapshots).
    """
    if not tasks:
        return []

    workers = max(1, int(workers))
    if workers == 1 or len(tasks) == 1:
        out: List[Any] = []
        n = len(tasks)
        for i, task in enumerate(tasks):
            if feedback is not None and hasattr(feedback, "isCanceled"):
                if feedback.isCanceled():
                    break
            out.append(fn(task))
            if feedback is not None and (
                i % 25 == 0 or i + 1 == n
            ):
                if hasattr(feedback, "setProgressText"):
                    feedback.setProgressText(
                        f"{progress_label}: {i + 1}/{n}"
                    )
        return out

    ctx, worker_py = spawn_context_for_workers(feedback=feedback)
    if not worker_py or ctx is None:
        return map_in_processes(
            fn,
            tasks,
            workers=1,
            scripts_root=scripts_root,
            feedback=feedback,
            progress_label=progress_label,
        )

    results: List[Optional[Any]] = [None] * len(tasks)
    done = 0
    n = len(tasks)
    pool_kwargs = dict(
        max_workers=workers,
        initializer=_pool_initializer,
        initargs=(scripts_root,),
    )
    # mp_context is Python 3.11+ / required so qgis-bin parents don't respawn QGIS.
    try:
        pool = ProcessPoolExecutor(mp_context=ctx, **pool_kwargs)
    except TypeError:
        pool = ProcessPoolExecutor(**pool_kwargs)

    with pool:
        future_map = {
            pool.submit(fn, task): idx for idx, task in enumerate(tasks)
        }
        for fut in as_completed(future_map):
            if feedback is not None and hasattr(feedback, "isCanceled"):
                if feedback.isCanceled():
                    for pending in future_map:
                        pending.cancel()
                    try:
                        pool.shutdown(wait=False, cancel_futures=True)
                    except TypeError:
                        pool.shutdown(wait=False)
                    break
            idx = future_map[fut]
            results[idx] = fut.result()
            done += 1
            if feedback is not None and (
                done % 10 == 0 or done == n
            ):
                if hasattr(feedback, "setProgressText"):
                    feedback.setProgressText(
                        f"{progress_label}: {done}/{n}"
                    )
    return list(results)
