# -*- coding: utf-8 -*-
"""Process-pool helpers for QGIS Processing scripts (Windows-safe spawn)."""

from __future__ import annotations

import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from typing import Any, Callable, Iterable, List, Optional, Sequence


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


def _pool_initializer(scripts_root: Optional[str]) -> None:
    if not scripts_root:
        return
    import sys

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
