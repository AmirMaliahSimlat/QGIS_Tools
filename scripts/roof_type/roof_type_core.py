# -*- coding: utf-8 -*-
"""Pick roof_type from complete vs partial zone overlaps."""

from __future__ import annotations

from typing import Optional, Sequence


def choose_roof_type(complete_types: Sequence[int], partial_types: Sequence[int], rng) -> Optional[int]:
    """
    complete_types: roof_type of zones that fully contain the footprint.
    partial_types: roof_type of zones that only partially overlap.

    Complete zones win over partial. Multiple distinct types in the winning
    set are chosen uniformly at random.
    """
    if complete_types:
        unique = sorted(set(int(t) for t in complete_types))
        if len(unique) == 1:
            return unique[0]
        return rng.choice(unique)
    if partial_types:
        unique = sorted(set(int(t) for t in partial_types))
        if len(unique) == 1:
            return unique[0]
        return rng.choice(unique)
    return None
