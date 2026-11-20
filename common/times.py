"""UTC date/time helpers shared by the disclosure source and the archive."""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

DATE_PATTERN = r"^\d{4}-\d{2}-\d{2}$"
DAY_MINUTES = 24 * 60

_date_re = re.compile(DATE_PATTERN)


def parse_archive_date(value: str) -> date:
    """Parse a strict YYYY-MM-DD string, rejecting impossible calendar dates."""
    if not isinstance(value, str) or not _date_re.fullmatch(value):
        raise ValueError("date must use the YYYY-MM-DD format")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("date must be a real calendar date") from exc


def parse_published_at(value: str, expected_day: date) -> tuple[str, int]:
    """Normalise an ISO-8601 timestamp to UTC and return (iso, minute-of-day).

    Naive timestamps are interpreted as UTC. Timestamps that fall on another
    date are rejected because a per-day source must not leak notices across days.
    """
    if not isinstance(value, str) or not value.strip():
        raise ValueError("published_at must be a non-empty ISO-8601 timestamp")
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("published_at is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    if parsed.date() != expected_day:
        raise ValueError("published_at does not belong to the requested date")
    minute = parsed.hour * 60 + parsed.minute
    if not 0 <= minute < DAY_MINUTES:
        raise ValueError("published_at minute is outside the day")
    normalised = parsed.isoformat().replace("+00:00", "Z")
    return normalised, minute


def minute_label(minute: int) -> str:
    """Render a minute-of-day as HH:MM for humans."""
    if not 0 <= minute < DAY_MINUTES:
        raise ValueError("minute must be within 0..1439")
    return f"{minute // 60:02d}:{minute % 60:02d}"
