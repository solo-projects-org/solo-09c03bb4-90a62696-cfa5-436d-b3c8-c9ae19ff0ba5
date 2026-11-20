"""Day-version comparison.

Day versions are opaque strings in general, but they embed a day and a
revision (the local source uses ``2026-11-20.v2``).  Comparison is *natural*:
the string is split into digit and non-digit chunks, digits compare as numbers
and the rest compares as text.  That orders ``2026-11-19.v3`` before
``2026-11-20.v1`` while still ranking ``.v10`` above ``.v9``.
"""

from __future__ import annotations

import re

_CHUNKS = re.compile(r"(\d+|\D+)")


def _chunks(version: str) -> list[str]:
    return _CHUNKS.findall(version.strip())


def compare_versions(left: str, right: str) -> int:
    """Return -1, 0 or 1 as ``left`` is older than, equal to or newer than ``right``."""
    if left == right:
        return 0
    for a, b in zip(_chunks(left), _chunks(right)):
        if a == b:
            continue
        if a.isdigit() and b.isdigit():
            return 1 if int(a) > int(b) else -1
        # Compare mixed chunks as text so a separator change still yields a
        # stable total order instead of an exception.
        return 1 if a > b else -1
    left_len = len(_chunks(left))
    right_len = len(_chunks(right))
    if left_len == right_len:
        return 1 if left > right else -1
    return 1 if left_len > right_len else -1
