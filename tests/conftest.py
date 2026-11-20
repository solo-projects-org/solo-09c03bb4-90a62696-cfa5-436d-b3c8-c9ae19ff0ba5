"""Shared fixtures for the archive and source tests."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.collector import Collector  # noqa: E402
from app.db import Database  # noqa: E402
from app.models import Notice, SourceResponse  # noqa: E402
from source.store import SourceState  # noqa: E402

DATASETS = ROOT / "datasets"


class StubSource:
    """Synchronous adapter around ``SourceState`` for collector tests."""

    def __init__(self, state: SourceState) -> None:
        self.state = state
        self.version_calls = 0
        self.search_calls: list[tuple[int, int]] = []
        self.fail_on_search = False
        self.fail_on_version = False

    def day_version(self, day):
        self.version_calls += 1
        if self.fail_on_version:
            from app.errors import SourceError

            raise SourceError("version endpoint down")
        import asyncio

        return asyncio.run(self.state.day_version(day))

    def search(self, day, start_minute, end_minute):
        if self.fail_on_search:
            from app.errors import SourceError

            raise SourceError("search endpoint down")
        import asyncio

        self.search_calls.append((start_minute, end_minute))
        version, page = asyncio.run(self.state.search(day, start_minute, end_minute))
        notices = tuple(
            Notice(
                announcement_no=n.announcement_no,
                title=n.title,
                published_at=n.published_at,
                published_minute=n.published_minute,
            )
            for n in page
        )
        return SourceResponse(
            day_version=version,
            notices=notices,
            start_minute=start_minute,
            end_minute=end_minute,
        )


@pytest.fixture()
def source_state() -> SourceState:
    return SourceState(DATASETS, "v1")


@pytest.fixture()
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "archive.sqlite3")
    database.init()
    return database


@pytest.fixture()
def stub(source_state: SourceState) -> StubSource:
    return StubSource(source_state)


@pytest.fixture()
def collector(db: Database, stub: StubSource) -> Collector:
    return Collector(db, stub)
