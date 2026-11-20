"""Conditional GET (ETag / If-None-Match) for ``GET /api/days/{day}``.

These tests cover the correctness rules:

* a matching ``If-None-Match`` yields ``304`` without reading details or writing;
* every committed change to the day representation invalidates the old tag;
* writes that do *not* change the representation (frontier splits, rollbacks,
  other days, restarts) leave the tag valid;
* the tag on a ``200`` always describes the same committed snapshot as its body;
* an older instance writing to the shared SQLite file during a rolling upgrade
  still invalidates tags known to the new instance.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import source.main as source_main
from app.collector import Collector
from app.db import Database
from app.etag import if_none_match_matches, tag_for_state
from app.main import AppState, app as archive_app, set_state_for_tests
from app.models import Notice, SourceResponse
from app.source_client import SourceClient
from tests.conftest import DATASETS, StubSource
from tests.test_api import SyncASGITransport
from source.store import SourceState


# --------------------------------------------------------------------- helpers


def _raw_conn(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _legacy_add_notice(path: Path, day: str, no: str, minute: int) -> None:
    """Mimic an *older* archive instance writing to the shared database.

    These statements are exactly what the pre-ETag code issued: they know
    nothing about ``day_revisions`` or triggers.
    """
    conn = _raw_conn(path)
    try:
        conn.execute(
            "INSERT INTO notices(day, announcement_no, title, published_at, "
            "published_minute, day_version) VALUES (?, ?, ?, ?, ?, ?)",
            (
                day,
                no,
                "旧实例写入",
                f"{day}T{minute // 60:02d}:{minute % 60:02d}:00Z",
                minute,
                f"{day}.v1",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def _legacy_complete_range(path: Path, day: str, start: int, end: int) -> None:
    conn = _raw_conn(path)
    try:
        conn.execute(
            "INSERT INTO completed_ranges(day, start_minute, end_minute, "
            "day_version, created_at) VALUES (?, ?, ?, ?, ?)",
            (day, start, end, f"{day}.v1", "2026-11-20T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


def _legacy_split_frontier(path: Path, day: str, start: int, end: int) -> None:
    """An old instance splitting a pending interval: representation unchanged."""
    conn = _raw_conn(path)
    try:
        conn.execute(
            "DELETE FROM frontier_ranges WHERE day = ? AND start_minute = ? "
            "AND end_minute = ?",
            (day, start, end),
        )
        conn.execute(
            "INSERT INTO frontier_ranges(day, start_minute, end_minute, created_at) "
            "VALUES (?, ?, ?, ?)",
            (day, start, (start + end) // 2, "2026-11-20T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO frontier_ranges(day, start_minute, end_minute, created_at) "
            "VALUES (?, ?, ?, ?)",
            (day, (start + end) // 2 + 1, end, "2026-11-20T00:00:00+00:00"),
        )
        conn.commit()
    finally:
        conn.close()


class _EmptyClosingSource(StubSource):
    """First search is full (split), later searches close an empty interval."""

    def __init__(self, state: SourceState) -> None:
        super().__init__(state)
        self.first_call = True

    def search(self, day, start_minute, end_minute):
        if self.first_call and (start_minute, end_minute) == (0, 1439):
            self.first_call = False
            return SourceResponse(
                day_version=f"{day.isoformat()}.v1",
                notices=tuple(
                    Notice(
                        announcement_no=f"FULL-{i}",
                        title=f"满页 {i}",
                        published_at=f"{day.isoformat()}T00:0{i}:00Z",
                        published_minute=i,
                    )
                    for i in range(5)
                ),
                start_minute=start_minute,
                end_minute=end_minute,
            )
        self.search_calls.append((start_minute, end_minute))
        return SourceResponse(
            day_version=f"{day.isoformat()}.v1",
            notices=(),
            start_minute=start_minute,
            end_minute=end_minute,
        )


@pytest.fixture()
def source_state() -> SourceState:
    state = SourceState(DATASETS, "v1")
    source_main.set_state(state)
    return state


@pytest.fixture()
def archive_client(tmp_path: Path, source_state: SourceState):
    db = Database(tmp_path / "etag.sqlite3")
    db.init()
    client = SourceClient(
        "http://source", 5.0, transport=SyncASGITransport(source_main.app)
    )
    state = AppState(db=db, client=client, collector=Collector(db, client))
    set_state_for_tests(state)
    with TestClient(archive_app) as test_client:
        yield test_client
    client.close()


def collect(client: TestClient, day: str, max_queries: int | None = None) -> dict:
    payload: dict[str, object] = {"day": day}
    if max_queries is not None:
        payload["max_queries"] = max_queries
    response = client.post("/api/collection/rounds", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def get_day(client: TestClient, day: str, etag: str | None = None) -> httpx.Response:
    headers = {"If-None-Match": etag} if etag is not None else {}
    return client.get(f"/api/days/{day}", headers=headers)


# ----------------------------------------------------------- header behaviour


def test_200_carries_strong_etag_and_json_body(archive_client: TestClient) -> None:
    collect(archive_client, "2026-11-19")
    response = get_day(archive_client, "2026-11-19")

    assert response.status_code == 200
    tag = response.headers["ETag"]
    assert tag.startswith('"') and tag.endswith('"') and not tag.startswith("W/")
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.headers["Cache-Control"] == "no-cache"
    body = response.json()
    assert body["day"] == "2026-11-19"
    assert body["complete"] is True


def test_matching_if_none_match_returns_304_with_same_tag(
    archive_client: TestClient,
) -> None:
    collect(archive_client, "2026-11-19")
    first = get_day(archive_client, "2026-11-19")
    tag = first.headers["ETag"]

    second = get_day(archive_client, "2026-11-19", tag)

    assert second.status_code == 304
    assert second.headers["ETag"] == tag
    assert second.content == b""
    assert "content-length" not in second.headers or second.headers["Content-Length"] == "0"


def test_weak_tag_and_tag_lists_match(archive_client: TestClient) -> None:
    collect(archive_client, "2026-11-19")
    tag = get_day(archive_client, "2026-11-19").headers["ETag"]

    assert get_day(archive_client, "2026-11-19", f"W/{tag}").status_code == 304
    assert (
        get_day(archive_client, "2026-11-19", f'"other", {tag}').status_code == 304
    )
    assert get_day(archive_client, "2026-11-19", "*").status_code == 304
    assert get_day(archive_client, "2026-11-19", '"other"').status_code == 200


def test_unparseable_if_none_match_is_ignored(archive_client: TestClient) -> None:
    collect(archive_client, "2026-11-19")
    tag = get_day(archive_client, "2026-11-19").headers["ETag"]
    # Unquoted garbage must not accidentally match the quoted tag.
    assert get_day(archive_client, "2026-11-19", tag.strip('"')).status_code == 200


def test_304_skips_detail_queries_and_writes(
    archive_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    collect(archive_client, "2026-11-19")
    tag = get_day(archive_client, "2026-11-19").headers["ETag"]

    state: AppState = archive_app.state.archive
    db_path = state.db.path
    watcher = _raw_conn(db_path)
    try:
        before = watcher.execute("PRAGMA data_version").fetchone()[0]
        revision_before = watcher.execute(
            "SELECT revision FROM day_revisions WHERE day = '2026-11-19'"
        ).fetchone()[0]

        def forbidden(_day):
            raise AssertionError("304 must not read notices or interval details")

        monkeypatch.setattr(state.db, "get_day_record", forbidden)

        response = get_day(archive_client, "2026-11-19", tag)

        assert response.status_code == 304
        # No other connection committed, so nothing was written.
        assert watcher.execute("PRAGMA data_version").fetchone()[0] == before
        assert (
            watcher.execute(
                "SELECT revision FROM day_revisions WHERE day = '2026-11-19'"
            ).fetchone()[0]
            == revision_before
        )
    finally:
        watcher.close()


def test_unseen_day_has_stable_tag_and_revalidates(archive_client: TestClient) -> None:
    first = get_day(archive_client, "2026-11-21")
    assert first.status_code == 200
    assert first.json()["day_version"] is None
    assert first.json()["complete"] is False
    assert first.json()["pending_ranges"] == [
        {"start_minute": 0, "end_minute": 1439, "from_time": "00:00", "to_time": "23:59"}
    ]
    tag = first.headers["ETag"]

    assert get_day(archive_client, "2026-11-21").headers["ETag"] == tag
    assert get_day(archive_client, "2026-11-21", tag).status_code == 304


# ----------------------------------------------------- invalidation semantics


def test_first_collection_invalidates_unseen_tag(archive_client: TestClient) -> None:
    unseen_tag = get_day(archive_client, "2026-11-18").headers["ETag"]

    collect(archive_client, "2026-11-18")

    after = get_day(archive_client, "2026-11-18", unseen_tag)
    assert after.status_code == 200
    assert after.headers["ETag"] != unseen_tag
    assert after.json()["day_version"] == "2026-11-18.v1"


def test_notice_change_invalidates_tag(archive_client: TestClient) -> None:
    collect(archive_client, "2026-11-19")
    old_tag = get_day(archive_client, "2026-11-19").headers["ETag"]

    _legacy_add_notice(
        archive_app.state.archive.db.path, "2026-11-19", "HB-NEW-1", 10 * 60
    )

    changed = get_day(archive_client, "2026-11-19", old_tag)
    assert changed.status_code == 200
    assert changed.headers["ETag"] != old_tag
    assert "HB-NEW-1" in {n["announcement_no"] for n in changed.json()["notices"]}


def test_same_version_empty_interval_commit_invalidates_tag(
    db: Database, source_state: SourceState
) -> None:
    """Closing an empty pending interval changes pending_ranges -> new tag."""
    source = _EmptyClosingSource(source_state)
    collector = Collector(db, source)
    day = date(2026, 11, 19)

    first = collector.run_round(day, max_queries=1)
    assert first.status == "partial"
    assert first.day_version == "2026-11-19.v1"
    tag_before = _tag_for(db, day)

    second = collector.run_round(day, max_queries=1)
    assert second.status == "partial"
    assert second.day_version == "2026-11-19.v1"  # same day version
    assert second.notices_collected == 0  # confirmed an empty interval

    tag_after = _tag_for(db, day)
    assert tag_after != tag_before


def test_same_version_next_settlement_invalidates_cached_tag(
    archive_client: TestClient,
) -> None:
    """End-to-end: same version, empty completion, conditional GET revalidates."""
    collect(archive_client, "2026-11-20", max_queries=2)
    before = get_day(archive_client, "2026-11-20")
    old_tag = before.headers["ETag"]

    # Keep collecting the same version until the next round settles a range.
    result = collect(archive_client, "2026-11-20", max_queries=1)
    assert result["day_version"] == before.json()["day_version"]
    assert result["collected_ranges"], "a range must have been settled"

    after = get_day(archive_client, "2026-11-20", old_tag)
    assert after.status_code == 200
    assert after.headers["ETag"] != old_tag


def test_frontier_split_keeps_tag_valid(db: Database, source_state: SourceState) -> None:
    """Only splitting the pending interval must keep the cached tag valid."""
    collector = Collector(db, StubSource(source_state))
    day = date(2026, 11, 20)

    first = collector.run_round(day, max_queries=1)  # 5 notices -> split
    assert first.status == "partial"
    tag_before = _tag_for(db, day)
    record_before = db.get_day_record(day)
    assert record_before.frontier

    collector.run_round(day, max_queries=1)  # another split
    record_after = db.get_day_record(day)
    assert [f.spec for f in record_after.frontier] != [f.spec for f in record_before.frontier]
    assert _tag_for(db, day) == tag_before


def test_other_day_update_keeps_tag_valid(archive_client: TestClient) -> None:
    collect(archive_client, "2026-11-19")
    tag = get_day(archive_client, "2026-11-19").headers["ETag"]

    collect(archive_client, "2026-11-20", max_queries=2)
    collect(archive_client, "2026-11-21")

    assert get_day(archive_client, "2026-11-19", tag).status_code == 304


def test_restart_keeps_tag_valid(tmp_path: Path, source_state: SourceState) -> None:
    path = tmp_path / "restart.sqlite3"
    db = Database(path)
    db.init()
    collector = Collector(db, StubSource(source_state))
    day = date(2026, 11, 20)
    for _ in range(20):
        result = collector.run_round(day)
        if result.status in {"collected", "needs_manual_review"}:
            break
    tag = _tag_for(db, day)

    restarted = Database(path)
    restarted.init()
    assert _tag_for(restarted, day) == tag
    # A re-read of the whole record agrees with the metadata-only lookup.
    assert restarted.get_day_record(day).revision == db.get_day_record(day).revision


def test_rolled_back_write_keeps_tag_valid(
    db: Database, source_state: SourceState
) -> None:
    collector = Collector(db, StubSource(source_state))
    day = date(2026, 11, 19)
    collector.run_round(day)
    tag_before = _tag_for(db, day)
    revision_before = db.get_day_record(day).revision

    conn = _raw_conn(db.path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT INTO completed_ranges(day, start_minute, end_minute, "
            "day_version, created_at) VALUES (?, ?, ?, ?, ?)",
            ("2026-11-19", 700, 700, "2026-11-19.v1", "2026-11-20T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT INTO notices(day, announcement_no, title, published_at, "
            "published_minute, day_version) VALUES (?, ?, ?, ?, ?, ?)",
            ("2026-11-19", "ROLLBACK", "回滚", "2026-11-19T11:40:00Z", 700,
             "2026-11-19.v1"),
        )
        # Inside the transaction the token has already moved...
        inside = conn.execute(
            "SELECT revision FROM day_revisions WHERE day = '2026-11-19'"
        ).fetchone()[0]
        assert inside > revision_before
        conn.rollback()
    finally:
        conn.close()

    # ...but after the rollback the cached tag is still valid.
    assert db.get_day_record(day).revision == revision_before
    assert _tag_for(db, day) == tag_before


def test_version_change_invalidates_tag(archive_client: TestClient, source_state) -> None:
    collect(archive_client, "2026-11-19")
    old_tag = get_day(archive_client, "2026-11-19").headers["ETag"]

    import asyncio

    asyncio.run(source_state.switch_version("v2"))
    result = collect(archive_client, "2026-11-19")
    assert result["version_reset"] is True

    changed = get_day(archive_client, "2026-11-19", old_tag)
    assert changed.status_code == 200
    assert changed.headers["ETag"] != old_tag
    assert changed.json()["day_version"] == "2026-11-19.v2"


# --------------------------------------------------- snapshot / body coherence


def test_commit_between_metadata_read_and_body_read_is_coherent(
    archive_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit landing between the metadata lookup and the body read must not
    pair a tag from one snapshot with a body from another."""
    collect(archive_client, "2026-11-19")
    old_tag = get_day(archive_client, "2026-11-19").headers["ETag"]
    state: AppState = archive_app.state.archive
    day = date(2026, 11, 19)

    # The cheap metadata lookup runs first and sees the pre-commit state; a
    # legacy instance then commits right before the body is read.
    real_get = state.db.get_day_record

    def get_after_commit(requested: date):
        _legacy_complete_range(state.db.path, "2026-11-19", 700, 700)
        _legacy_add_notice(state.db.path, "2026-11-19", "RACED-1", 700)
        return real_get(requested)

    monkeypatch.setattr(state.db, "get_day_record", get_after_commit)

    response = get_day(archive_client, "2026-11-19", '"unrelated"')
    assert response.status_code == 200
    body = response.json()
    tag = response.headers["ETag"]

    # The tag describes the snapshot that produced the body.
    committed_revision = real_get(day).revision
    assert tag == _tag_for_revision(day, committed_revision)
    assert "RACED-1" in {n["announcement_no"] for n in body["notices"]}
    assert 700 in {n["published_minute"] for n in body["notices"]}

    # Revalidating that tag finds the same committed snapshot -> 304.
    monkeypatch.undo()

    assert get_day(archive_client, "2026-11-19", tag).status_code == 304
    assert get_day(archive_client, "2026-11-19", old_tag).status_code == 200


# ------------------------------------------------------- rolling-upgrade / old DB


def test_old_instance_writes_invalidate_new_instance_tags(
    archive_client: TestClient,
) -> None:
    collect(archive_client, "2026-11-19")
    old_tag = get_day(archive_client, "2026-11-19").headers["ETag"]
    db_path = archive_app.state.archive.db.path

    # The old instance keeps collecting on the same file with plain SQL.
    _legacy_add_notice(db_path, "2026-11-19", "HB-OLD-INSTANCE", 11 * 60)
    _legacy_complete_range(db_path, "2026-11-19", 660, 660)

    changed = get_day(archive_client, "2026-11-19", old_tag)
    assert changed.status_code == 200
    assert "HB-OLD-INSTANCE" in {n["announcement_no"] for n in changed.json()["notices"]}


def test_old_instance_frontier_split_does_not_invalidate(
    archive_client: TestClient,
) -> None:
    collect(archive_client, "2026-11-20", max_queries=1)
    old_tag = get_day(archive_client, "2026-11-20").headers["ETag"]
    db_path = archive_app.state.archive.db.path

    conn = _raw_conn(db_path)
    try:
        row = conn.execute(
            "SELECT start_minute, end_minute FROM frontier_ranges "
            "WHERE day = '2026-11-20' ORDER BY start_minute LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row is not None

    _legacy_split_frontier(db_path, "2026-11-20", row[0], row[1])
    assert get_day(archive_client, "2026-11-20", old_tag).status_code == 304


def test_legacy_database_is_migrated_and_tracked(tmp_path: Path) -> None:
    """A pre-ETag database gets a revision row and triggers on first start."""
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    try:
        conn.executescript(
            """
            CREATE TABLE days (
                day TEXT PRIMARY KEY, day_version TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE completed_ranges (
                id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
                start_minute INTEGER NOT NULL, end_minute INTEGER NOT NULL,
                day_version TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE manual_ranges (
                id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
                start_minute INTEGER NOT NULL, end_minute INTEGER NOT NULL,
                reason TEXT NOT NULL, day_version TEXT NOT NULL,
                created_at TEXT NOT NULL);
            CREATE TABLE frontier_ranges (
                id INTEGER PRIMARY KEY AUTOINCREMENT, day TEXT NOT NULL,
                start_minute INTEGER NOT NULL, end_minute INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(day, start_minute, end_minute));
            CREATE TABLE notices (
                day TEXT NOT NULL, announcement_no TEXT NOT NULL,
                title TEXT NOT NULL, published_at TEXT NOT NULL,
                published_minute INTEGER NOT NULL, day_version TEXT NOT NULL,
                PRIMARY KEY (day, announcement_no));
            INSERT INTO days VALUES
                ('2026-11-19', '2026-11-19.v1', 'x', 'x');
            INSERT INTO completed_ranges
                (day, start_minute, end_minute, day_version, created_at)
                VALUES ('2026-11-19', 0, 1439, '2026-11-19.v1', 'x');
            """
        )
        conn.commit()
    finally:
        conn.close()

    db = Database(path)
    db.init()
    record = db.get_day_record(date(2026, 11, 19))
    assert record.revision == 1
    tag_before = _tag_for(db, date(2026, 11, 19))

    # Old-instance-style writes now bump the backfilled counter via triggers.
    _legacy_add_notice(path, "2026-11-19", "LEGACY-1", 60)
    assert db.get_day_record(date(2026, 11, 19)).revision > 1
    assert _tag_for(db, date(2026, 11, 19)) != tag_before

    # Re-running init (e.g. the new instance restarting) is idempotent.
    db.init()
    assert db.get_day_record(date(2026, 11, 19)).revision > 1


def test_init_is_idempotent_for_revisions(archive_client: TestClient) -> None:
    collect(archive_client, "2026-11-19")
    state: AppState = archive_app.state.archive
    revision = state.db.get_day_record(date(2026, 11, 19)).revision
    state.db.init()
    assert state.db.get_day_record(date(2026, 11, 19)).revision == revision


# -------------------------------------------------------------- helper / units


def _tag_for(db: Database, day: date) -> str:
    exists, revision = db.day_revision(day)
    assert exists
    return _tag_for_revision(day, revision)


def _tag_for_revision(day: date, revision: int | None) -> str:
    return tag_for_state(day.isoformat(), revision)


def test_if_none_match_parser_units() -> None:
    assert if_none_match_matches(None, '"a"') is False
    assert if_none_match_matches("", '"a"') is False
    assert if_none_match_matches('"a"', '"a"') is True
    assert if_none_match_matches('W/"a"', '"a"') is True
    assert if_none_match_matches('"b", "a"', '"a"') is True
    assert if_none_match_matches(" * ", '"a"') is True
    assert if_none_match_matches('"a', '"a"') is False
    assert if_none_match_matches("a", '"a"') is False
    assert if_none_match_matches('"A"', '"a"') is False


def test_openapi_documents_etag(archive_client: TestClient) -> None:
    schema = archive_client.get("/openapi.json").json()
    responses = schema["paths"]["/api/days/{day}"]["get"]["responses"]
    assert "ETag" in responses["200"]["headers"]
    assert "304" in responses


def test_idle_rounds_do_not_invalidate_tag(
    db: Database, source_state: SourceState
) -> None:
    """A settled day re-checked by the collector writes nothing, so its tag holds."""
    collector = Collector(db, StubSource(source_state))
    day = date(2026, 11, 19)
    for _ in range(20):
        result = collector.run_round(day)
        if result.status in {"collected", "needs_manual_review"}:
            break
    tag_before = _tag_for(db, day)

    for _ in range(5):
        result = collector.run_round(day)
        assert result.queries_used == 0

    assert _tag_for(db, day) == tag_before


def test_frontier_reseed_does_not_invalidate_tag(
    db: Database, source_state: SourceState
) -> None:
    """The frontier is derived state: dropping and re-seeding it keeps the tag."""
    collector = Collector(db, StubSource(source_state))
    day = date(2026, 11, 20)
    collector.run_round(day, max_queries=1)
    tag_before = _tag_for(db, day)
    pending_before = db.pending_ranges(db.get_day_record(day))

    conn = _raw_conn(db.path)
    try:
        conn.execute("DELETE FROM frontier_ranges WHERE day = ?", (day.isoformat(),))
        conn.commit()
    finally:
        conn.close()
    db.seed_frontier(day)

    assert db.get_day_record(day).frontier
    assert db.pending_ranges(db.get_day_record(day)) == pending_before
    assert _tag_for(db, day) == tag_before
