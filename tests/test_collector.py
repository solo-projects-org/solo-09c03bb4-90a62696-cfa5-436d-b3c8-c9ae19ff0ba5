"""Collector behaviour: interval splitting, manuals and version safety."""

from __future__ import annotations

from datetime import date

from app.collector import Collector
from app.db import Database
from tests.conftest import StubSource


def test_sparse_day_completes_in_one_query(collector: Collector, stub: StubSource) -> None:
    day = date(2026, 11, 19)
    result = collector.run_round(day)

    assert result.status == "collected"
    assert result.queries_used == 1
    assert result.pending_ranges == []
    assert result.notices_collected == 3
    assert stub.search_calls == [(0, 1439)]


def test_empty_day_completes(collector: Collector, db: Database) -> None:
    result = collector.run_round(date(2026, 11, 18))

    assert result.status == "collected"
    assert result.queries_used == 1
    assert result.notices_collected == 0
    record = db.get_day_record(date(2026, 11, 18))
    assert record.notices == []
    assert db.pending_ranges(record) == []


def test_dense_day_splits_bounded_then_settles(collector: Collector, db: Database) -> None:
    day = date(2026, 11, 20)
    rounds = 0
    statuses = []
    while rounds < 20:
        result = collector.run_round(day)
        statuses.append(result.status)
        rounds += 1
        # Never exceed the per-round cap.
        assert result.queries_used <= 6
        if result.status in {"collected", "needs_manual_review"}:
            break

    assert statuses[-1] == "needs_manual_review"
    record = db.get_day_record(day)
    # The 09:12 minute holds five notices, so by contract it is handed to a
    # human and its page is *not* archived. Everything else is archived.
    assert len(record.notices) == 4
    assert {n.announcement_no for n in record.notices} == {
        "HB-EIA-2026-1120-06",
        "HB-EIA-2026-1120-07",
        "HB-EIA-2026-1120-08",
        "HB-EIA-2026-1120-09",
    }
    # 09:12 = 9*60+12 = 552; the single full minute is the only manual range.
    assert [(r.start_minute, r.end_minute) for r, _ in record.manual] == [(552, 552)]
    assert db.pending_ranges(record) == []


def test_full_minute_keeps_its_page_unarchived(
    collector: Collector, db: Database
) -> None:
    """A manual minute must never be silently stored as if it were complete."""
    day = date(2026, 11, 20)
    for _ in range(20):
        result = collector.run_round(day)
        if result.status in {"collected", "needs_manual_review"}:
            break

    record = db.get_day_record(day)
    manual_minutes = {r.start_minute for r, _ in record.manual}
    archived_minutes = {n.published_minute for n in record.notices}
    assert not (manual_minutes & archived_minutes)
    reasons = {reason for _, reason in record.manual}
    assert reasons == {
        "one-minute interval still returned five notices; needs manual review"
    }


def test_manual_range_is_reported_as_pending_for_humans(
    collector: Collector, db: Database
) -> None:
    day = date(2026, 11, 20)
    for _ in range(20):
        result = collector.run_round(day)
        if result.status in {"collected", "needs_manual_review"}:
            break

    record = db.get_day_record(day)
    completed, merged_manual = db.coverage(record)
    # Complete coverage plus the manual minute covers the whole day...
    total = sum(r.end_minute - r.start_minute + 1 for r in completed)
    total += sum(r.end_minute - r.start_minute + 1 for r in merged_manual)
    assert total == 1440
    # ...but the day is not "complete" because the manual minute is unverified.
    assert db.pending_ranges(record) == []
    assert merged_manual


def test_round_respects_max_queries(collector: Collector, stub: StubSource) -> None:
    result = collector.run_round(date(2026, 11, 20), max_queries=2)

    assert result.queries_used == 2
    assert result.status == "partial"
    assert len(stub.search_calls) == 2
    # The unseen remainder is still reported as pending, not assumed empty.
    assert result.pending_ranges


def test_version_checked_before_search(collector: Collector, stub: StubSource) -> None:
    collector.run_round(date(2026, 11, 19))

    assert stub.version_calls == 1
    assert stub.search_calls


def test_source_failure_keeps_state_for_retry(
    collector: Collector, stub: StubSource, db: Database
) -> None:
    stub.fail_on_version = True
    result = collector.run_round(date(2026, 11, 19))

    assert result.status == "source_error"
    assert result.queries_used == 0
    assert db.get_day_record(date(2026, 11, 19)).day_version is None

    stub.fail_on_version = False
    retry = collector.run_round(date(2026, 11, 19))
    assert retry.status == "collected"
    assert retry.notices_collected == 3


def test_search_failure_retries_pending(
    collector: Collector, stub: StubSource, db: Database
) -> None:
    stub.fail_on_search = True
    first = collector.run_round(date(2026, 11, 20))
    assert first.status == "source_error"
    assert first.pending_ranges

    stub.fail_on_search = False
    second = collector.run_round(date(2026, 11, 20), max_queries=1)
    assert second.status == "partial"
    assert second.queries_used == 1


def test_restart_resumes_frontier(tmp_path, source_state) -> None:
    """A fresh Database/Collector over the same file continues, not restarts."""
    from tests.conftest import StubSource as Stub

    path = tmp_path / "resume.sqlite3"
    first_db = Database(path)
    first_db.init()
    first = Collector(first_db, Stub(source_state))
    first.run_round(date(2026, 11, 20), max_queries=2)

    pending_before = first_db.pending_ranges(first_db.get_day_record(date(2026, 11, 20)))
    assert pending_before

    # Simulate a process restart: brand-new objects over the same file.
    second_db = Database(path)
    second_db.init()
    second = Collector(second_db, Stub(source_state))
    result = second.run_round(date(2026, 11, 20), max_queries=1)

    pending_after = second_db.pending_ranges(second_db.get_day_record(date(2026, 11, 20)))
    # The frontier advanced rather than searching (0, 1439) all over again.
    assert pending_after != pending_before
    assert result.queries_used == 1
