"""Inclusive minute-interval helpers."""

from __future__ import annotations

Interval = tuple[int, int]

DAY_START = 0
DAY_END = 1439


def merge_intervals(intervals: list[Interval] | tuple[Interval, ...]) -> list[Interval]:
    """Merge overlapping or adjacent inclusive intervals."""
    ordered = sorted(intervals)
    merged: list[Interval] = []
    for start, end in ordered:
        if not merged or start > merged[-1][1] + 1:
            merged.append((start, end))
        else:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
    return merged


def complement_intervals(
    intervals: list[Interval] | tuple[Interval, ...],
    lo: int = DAY_START,
    hi: int = DAY_END,
) -> list[Interval]:
    """Return the parts of [lo, hi] not covered by ``intervals``."""
    result: list[Interval] = []
    cursor = lo
    for start, end in merge_intervals(intervals):
        start = max(start, lo)
        end = min(end, hi)
        if end < lo or start > hi:
            continue
        if start > cursor:
            result.append((cursor, start - 1))
        cursor = max(cursor, end + 1)
    if cursor <= hi:
        result.append((cursor, hi))
    return result
