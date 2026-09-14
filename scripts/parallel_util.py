# -*- coding: utf-8 -*-
"""Process-pool helpers for QGIS Processing scripts (Windows-safe spawn)."""

from __future__ import annotations

import multiprocessing as mp
import os
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable, List, Optional, Sequence


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


def ensure_worker_python() -> Optional[str]:
    """
    Make ProcessPoolExecutor spawn real ``python.exe``.

    When algorithms run under ``qgis_process.exe``, ``sys.executable`` is that
    binary. Workers then get launched as ``qgis_process.exe -c ...``, which
    fails with ``Command -c not known!``. Point multiprocessing at the QGIS
    ``python.exe`` instead.
    """
    exe = Path(sys.executable).resolve() if sys.executable else None
    if exe is not None and "python" in exe.name.lower() and exe.is_file():
        return str(exe)

    candidates: List[Path] = []
    if exe is not None:
        bin_dir = exe.parent
        candidates.append(bin_dir / "python.exe")
        # qgis_process lives in apps/qgis-ltr/bin on some installs.
        for up in (bin_dir, bin_dir.parent, bin_dir.parent.parent, bin_dir.parent.parent.parent):
            candidates.append(up / "bin" / "python.exe")
            candidates.extend(sorted(up.glob("apps/Python*/python.exe")))

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
        if cand.is_file():
            mp.set_executable(str(cand))
            return str(cand)
    return None


def _pool_initializer(scripts_root: Optional[str]) -> None:
    if not scripts_root:
        return
    if scripts_root not in sys.path:
        sys.path.insert(0, scripts_root)


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

    worker_py = ensure_worker_python()
    if not worker_py:
        if feedback is not None and hasattr(feedback, "pushWarning"):
            feedback.pushWarning(
                "Could not locate python.exe for worker processes; "
                "falling back to serial execution."
            )
        elif feedback is not None and hasattr(feedback, "pushInfo"):
            feedback.pushInfo(
                "No worker python.exe found — running serially."
            )
        return map_in_processes(
            fn,
            tasks,
            workers=1,
            scripts_root=scripts_root,
            feedback=feedback,
            progress_label=progress_label,
        )

    if feedback is not None and hasattr(feedback, "pushInfo"):
        feedback.pushInfo(f"Worker interpreter: {worker_py}")

    results: List[Optional[Any]] = [None] * len(tasks)
    done = 0
    n = len(tasks)
    with ProcessPoolExecutor(
        max_workers=workers,
        initializer=_pool_initializer,
        initargs=(scripts_root,),
    ) as pool:
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
