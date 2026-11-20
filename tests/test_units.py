"""Unit tests for the small interval and version helpers."""

from __future__ import annotations

from datetime import date

import pytest

from app.intervals import complement_intervals, merge_intervals
from app.models import RangeSpec
from common.times import minute_label, parse_archive_date, parse_published_at
from common.versions import compare_versions


def test_merge_intervals_joins_adjacent_and_overlapping() -> None:
    assert merge_intervals([(5, 9), (10, 12), (1, 3)]) == [(1, 3), (5, 12)]
    assert merge_intervals([(1, 10), (2, 3)]) == [(1, 10)]
    assert merge_intervals([]) == []


def test_complement_of_empty_set_is_whole_day() -> None:
    assert complement_intervals([]) == [(0, 1439)]


def test_complement_ignores_out_of_range_noise() -> None:
    assert complement_intervals([(-100, 10), (500, 600), (2000, 3000)]) == [
        (11, 499),
        (601, 1439),
    ]


def test_complement_splits_around_a_hole() -> None:
    assert complement_intervals([(0, 551), (553, 1439)]) == [(552, 552)]


def test_range_spec_rejects_impossible_ranges() -> None:
    with pytest.raises(ValueError):
        RangeSpec(10, 9)
    with pytest.raises(ValueError):
        RangeSpec(-1, 10)
    with pytest.raises(ValueError):
        RangeSpec(0, 1440)
    assert RangeSpec(0, 1439).size == 1440
    assert RangeSpec(9 * 60 + 12, 9 * 60 + 12).size == 1


def test_compare_versions_uses_numeric_suffix() -> None:
    assert compare_versions("2026-11-20.10", "2026-11-20.9") == 1
    assert compare_versions("2026-11-20.9", "2026-11-20.10") == -1
    assert compare_versions("2026-11-20.1", "2026-11-20.1") == 0


def test_compare_versions_orders_across_days() -> None:
    assert compare_versions("2026-11-20.v1", "2026-11-19.v9") == 1
    assert compare_versions("2026-11-19.v9", "2026-11-20.v1") == -1
    assert compare_versions("2026-12-01.v1", "2026-11-30.v7") == 1


def test_compare_versions_falls_back_to_strings() -> None:
    assert compare_versions("charlie", "bravo") == 1
    assert compare_versions("bravo", "charlie") == -1


def test_parse_archive_date_is_strict() -> None:
    assert parse_archive_date("2026-11-20") == date(2026, 11, 20)
    for bad in ["2026-11-31", "2026-2-3", "20-11-2026", "2026-02-30", ""]:
        with pytest.raises(ValueError):
            parse_archive_date(bad)


def test_parse_published_at_normalises_timezones() -> None:
    day = date(2026, 11, 20)
    iso, minute = parse_published_at("2026-11-20T17:12:00+08:00", day)
    assert iso == "2026-11-20T09:12:00Z"
    assert minute == 9 * 60 + 12
    _, naive = parse_published_at("2026-11-20T09:12:00", day)
    assert naive == 9 * 60 + 12


def test_parse_published_at_rejects_other_days() -> None:
    with pytest.raises(ValueError):
        parse_published_at("2026-11-21T00:00:00Z", date(2026, 11, 20))


def test_minute_label() -> None:
    assert minute_label(0) == "00:00"
    assert minute_label(552) == "09:12"
    assert minute_label(1439) == "23:59"
    with pytest.raises(ValueError):
        minute_label(1440)
