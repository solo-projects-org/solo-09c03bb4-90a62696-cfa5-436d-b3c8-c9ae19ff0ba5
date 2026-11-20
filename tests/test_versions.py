"""Day-version retraction: newer versions win, late old responses lose."""

from __future__ import annotations

import threading
from datetime import date

import pytest

from app.collector import Collector
from app.db import Database, StaleVersion
from app.errors import SourceError
from app.models import Notice, SourceResponse
from tests.conftest import StubSource

DENSE_DAY = date(2026, 11, 20)


class ScriptedSource(StubSource):
    """A stub whose search responses can be overridden per call."""

    def __init__(self, state) -> None:
        super().__init__(state)
        self.script: list[SourceResponse] = []
        self.on_search = None

    def search(self, day, start_minute, end_minute):
        self.search_calls.append((start_minute, end_minute))
        if self.script:
            response = self.script.pop(0)
            if self.on_search is not None:
                self.on_search(response)
            return response
        return super().search(day, start_minute, end_minute)


def page(version: str, notices: list[tuple[str, str, int]]) -> SourceResponse:
    return SourceResponse(
        day_version=version,
        notices=tuple(
            Notice(
                announcement_no=no,
                title=title,
                published_at=f"2026-11-20T{minute // 60:02d}:{minute % 60:02d}:00Z",
                published_minute=minute,
            )
            for no, title, minute in notices
        ),
        start_minute=0,
        end_minute=1439,
    )


def test_newer_version_mid_round_retracts_old_data(
    db: Database, source_state
) -> None:
    source = ScriptedSource(source_state)
    collector = Collector(db, source)

    # Leave the dense day partially collected under v1: split first, then close
    # the morning half with a short page.
    first = collector.run_round(DENSE_DAY, max_queries=3)
    assert first.status == "partial"
    assert first.queries_used == 3
    record_before = db.get_day_record(DENSE_DAY)
    assert record_before.day_version == "2026-11-20.v1"
    assert record_before.completed  # at least one settled range exists
    assert record_before.notices == []  # 00:00-05:59 genuinely has no notices

    # Mid-round the source returns a page stamped with v2.
    source.script = [
        page(
            "2026-11-20.v2",
            [("HB-EIA-2026-1120-01", "修订版", 552)],
        )
    ]
    # The version endpoint still says v1, so the mismatch is detected in-search.
    source.day_version = lambda day: "2026-11-20.v1"  # type: ignore[assignment]
    result = collector.run_round(DENSE_DAY)

    assert result.status == "version_changed"
    assert result.version_reset is True
    assert result.day_version == "2026-11-20.v2"

    record = db.get_day_record(DENSE_DAY)
    assert record.day_version == "2026-11-20.v2"
    # Old v1 notices and coverage are gone; nothing from v1 survives.
    assert record.notices == []
    assert record.completed == []
    assert record.frontier == []


def test_version_check_retracts_before_searching(db: Database, source_state) -> None:
    collector = Collector(db, source_state and StubSource(source_state))

    collector.run_round(date(2026, 11, 19))
    assert len(db.get_day_record(date(2026, 11, 19)).notices) == 3

    # Switch the whole source to v2; the next round's pre-flight check sees it.
    import asyncio

    asyncio.run(source_state.switch_version("v2"))
    result = collector.run_round(date(2026, 11, 19))

    assert result.version_reset is True
    record = db.get_day_record(date(2026, 11, 19))
    assert record.day_version == "2026-11-19.v2"
    # v2 has four notices on this day; the round restarted from scratch.
    assert result.status in {"collected", "needs_manual_review"}
    assert len(record.notices) == 4


def test_late_old_response_cannot_restore_retracted_data(
    db: Database, source_state
) -> None:
    """The core ordering hazard: an in-flight v1 page lands after v2 is known."""
    source = ScriptedSource(source_state)
    collector = Collector(db, source)

    # A late v1 page arrives after the archive has already moved to v2.
    db.sync_version(DENSE_DAY, "2026-11-20.v1")
    db.sync_version(DENSE_DAY, "2026-11-20.v2")

    source.script = [
        page("2026-11-20.v1", [("OLD-1", "旧版公告", 100)]),
        page("2026-11-20.v2", [("NEW-1", "新版公告", 100)]),
        page("2026-11-20.v2", [("NEW-2", "新版公告二", 101)]),
    ]
    # Force the version endpoint to report v2 (the current truth).
    source.day_version = lambda day: "2026-11-20.v2"  # type: ignore[assignment]

    result = collector.run_round(DENSE_DAY)

    assert result.status == "stale_response"
    record = db.get_day_record(DENSE_DAY)
    assert record.day_version == "2026-11-20.v2"
    # The retracted v1 page is not present even though it was the only page.
    assert "OLD-1" not in {n.announcement_no for n in record.notices}
    assert record.notices == []
    assert record.completed == []


def test_stale_write_rejected_inside_transaction(db: Database) -> None:
    from app.db import FrontierItem

    db.sync_version(DENSE_DAY, "2026-11-20.v1")
    db.seed_frontier(DENSE_DAY)
    item = db.get_day_record(DENSE_DAY).frontier[0]

    # Someone else advances the day version before our write lands.
    db.reset_to_version(DENSE_DAY, "2026-11-20.v2")

    with pytest.raises(StaleVersion):
        db.complete_item(
            DENSE_DAY,
            "2026-11-20.v1",
            item,
            [Notice("X", "旧", "2026-11-20T01:00:00Z", 60)],
        )


def test_stale_version_endpoint_does_not_roll_back(
    db: Database, source_state
) -> None:
    """If the source itself regresses, the archive keeps its newer data."""
    collector = Collector(db, StubSource(source_state))
    import asyncio

    asyncio.run(source_state.switch_version("v2"))
    collector.run_round(date(2026, 11, 19))
    assert db.get_day_record(date(2026, 11, 19)).day_version == "2026-11-19.v2"

    from app.source_client import SourceClient

    class StaleClient(StubSource):
        def day_version(self, day):
            return "2026-11-19.v1"

    stale = Collector(db, StaleClient(source_state))
    result = stale.run_round(date(2026, 11, 19))

    assert result.status == "stale_response"
    record = db.get_day_record(date(2026, 11, 19))
    assert record.day_version == "2026-11-19.v2"
    assert len(record.notices) == 4


def test_concurrent_rounds_are_rejected(db: Database, source_state) -> None:
    from app.collector import DayBusy

    entered = threading.Event()
    release = threading.Event()
    source = ScriptedSource(source_state)
    collector = Collector(db, source)

    def slow_search(day, start, end):
        entered.set()
        release.wait(5)
        return StubSource.search(source, day, start, end)

    source.search = slow_search  # type: ignore[assignment]

    thread = threading.Thread(target=lambda: collector.run_round(date(2026, 11, 19)))
    thread.start()
    assert entered.wait(5)
    with pytest.raises(DayBusy):
        collector.run_round(date(2026, 11, 19))
    release.set()
    thread.join(timeout=5)
    assert not thread.is_alive()


def test_source_error_mid_round_preserves_earlier_progress(
    db: Database, source_state
) -> None:
    """Progress made before a failure is durable, not rolled back."""

    class FailSecond(StubSource):
        def __init__(self, state) -> None:
            super().__init__(state)
            self.calls = 0

        def search(self, day, start_minute, end_minute):
            self.calls += 1
            if self.calls == 1:
                # Real full page: the collector splits the day.
                return StubSource.search(self, day, start_minute, end_minute)
            raise SourceError("connection reset")

        def day_version(self, day):
            return "2026-11-20.v1"

    source = FailSecond(source_state)
    collector = Collector(db, source)
    result = collector.run_round(date(2026, 11, 20))

    assert result.status == "source_error"
    record = db.get_day_record(DENSE_DAY)
    # The first page was full, so the split frontier survives the failure.
    assert record.frontier
    assert result.pending_ranges
    assert [r for r in record.completed] == []