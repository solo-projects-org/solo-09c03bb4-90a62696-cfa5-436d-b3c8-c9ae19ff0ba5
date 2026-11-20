"""Entity tags for conditional ``GET /api/days/{day}`` requests.

The archive exposes a *strong* ETag derived from a per-day revision token that
is stored in SQLite and bumped by database triggers whenever the committed,
query-visible state of a day changes (see ``app.db``).

Two properties matter:

* The tag is computed from metadata alone, so a matching ``If-None-Match`` can
  be answered with ``304`` without loading notices or interval details.
* The tag is derived from the same snapshot that produced the body, so a
  ``200`` never pairs a tag with a body from a different committed state.

The representation prefix is part of the tag, so changing the JSON contract
also invalidates tags that clients cached under an older service build.
"""

from __future__ import annotations

import secrets

REPRESENTATION_VERSION = "eia1"


def tag_for_state(day: str, revision: int | None) -> str:
    """Return the quoted strong entity tag for a day's revision token.

    ``revision=None`` means the day exists but its revision row is missing
    (a legacy database opened before the migration ran).  A fresh random tag is
    returned in that case, which is safe: the response is simply never a 304.
    """
    if revision is None:
        suffix = f"r{secrets.token_hex(16)}"
    else:
        suffix = f"r{revision}"
    return f'"{REPRESENTATION_VERSION}-{day}-{suffix}"'


def tag_for_unseen_day(day: str) -> str:
    """Stable tag for a day that has never been collected.

    Such a day still has a defined representation (no version, whole day
    pending), so its tag may be cached and revalidated like any other.
    """
    return f'"{REPRESENTATION_VERSION}-{day}-unseen"'


def _normalise_member(member: str) -> str | None:
    """Return the opaque quoted tag for one ``If-None-Match`` list member.

    ``W/`` prefixes are stripped because ``If-None-Match`` uses the weak
    comparison function even when the server emits strong tags.
    """
    value = member.strip()
    if value == "*":
        return "*"
    if value[:2].upper() == "W/":
        value = value[2:].strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value
    return None


def if_none_match_matches(header: str | None, current_tag: str) -> bool:
    """Return whether an ``If-None-Match`` header matches ``current_tag``.

    Supports ``*``, weak tags (``W/"..."``) and comma-separated lists.
    Malformed list members are ignored, as RFC 9110 requires.
    """
    if not header:
        return False
    for member in header.split(","):
        opaque = _normalise_member(member)
        if opaque == "*":
            return True
        if opaque is not None and opaque == current_tag:
            return True
    return False
