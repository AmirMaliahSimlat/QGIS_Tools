# -*- coding: utf-8 -*-
"""Uniform random subset of feature indices."""

from __future__ import annotations

import random
from typing import Optional, Set


def sample_uniform_indices(
    n: int,
    keep_count: Optional[int] = None,
    keep_fraction: float = 0.4,
    seed: Optional[int] = None,
) -> Set[int]:
    """Return a set of indices in ``range(n)`` to keep."""
    if n <= 0:
        return set()
    if keep_count is None:
        k = int(round(n * keep_fraction))
    else:
        k = int(keep_count)
    k = max(0, min(k, n))
    if k == 0:
        return set()
    if k == n:
        return set(range(n))
    rng = random.Random(seed)
    return set(rng.sample(range(n), k))
