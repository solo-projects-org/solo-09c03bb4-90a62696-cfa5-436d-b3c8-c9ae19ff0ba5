"""SQLite persistence for per-day coverage, frontier work and notices.

Two kinds of state are stored for a day:

* *settled* state: completed ranges, manual ranges and their notices;
* *frontier* state: the sub-intervals still to be searched.

Splitting a full page only creates frontier rows, so a round that runs out of
its query budget still makes durable progress and a restart continues from the
same frontier instead of re-walking the whole day.

A third, derived piece of state supports ``GET /api/days/{day}`` entity tags:
``day_revisions`` holds a monotonically increasing per-day token.  SQLite
triggers bump it for exactly those committed writes that change the
representation the endpoint returns (day version, notices, completed ranges
and manual ranges).  Frontier-only changes — splitting an interval — do not
bump it, because the frontier is not part of that representation.

Because the triggers live in the database file, an *older* archive instance
that keeps writing to the same SQLite file (rolling upgrade) bumps the token
too: the new instance sees the old instance's commits and invalidates tags that
clients cached before them.  Triggers are transactional, so a rolled-back write
does not bump the token either.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from app.intervals import complement_intervals, merge_intervals
from app.models import Notice, RangeSpec
from common.versions import compare_versions


class VersionChanged(RuntimeError):
    """The day moved to a newer version while a write was in flight."""

    def __init__(self, day: str, new_version: str) -> None:
        super().__init__(f"{day} moved to newer day version {new_version}")
        self.day = day
        self.new_version = new_version


class StaleVersion(RuntimeError):
    """A write produced by an older day version was rejected."""

    def __init__(self, day: str, attempted: str, current: str) -> None:
        super().__init__(
            f"stale day version for {day}: attempted {attempted}, current {current}"
        )
        self.day = day
        self.attempted = attempted
        self.current = current


class DayMissing(RuntimeError):
    """Internal guard: a write referenced a day without a version row."""


@dataclass
class FrontierItem:
    id: int
    start_minute: int
    end_minute: int

    @property
    def spec(self) -> RangeSpec:
        return RangeSpec(self.start_minute, self.end_minute)


@dataclass
class DayRecord:
    day: date
    day_version: str | None
    completed: list[RangeSpec] = field(default_factory=list)
    manual: list[tuple[RangeSpec, str]] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)
    frontier: list[FrontierItem] = field(default_factory=list)
    # Monotonic per-day revision token. ``None`` when the day has no row.
    revision: int | None = None


SCHEMA = """
CREATE TABLE IF NOT EXISTS days (
    day TEXT PRIMARY KEY,
    day_version TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS completed_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL,
    day_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_completed_ranges_day
    ON completed_ranges(day);

CREATE TABLE IF NOT EXISTS manual_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL,
    reason TEXT NOT NULL,
    day_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_manual_ranges_day
    ON manual_ranges(day);

CREATE TABLE IF NOT EXISTS frontier_ranges (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    day TEXT NOT NULL,
    start_minute INTEGER NOT NULL,
    end_minute INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(day, start_minute, end_minute)
);

CREATE INDEX IF NOT EXISTS idx_frontier_ranges_day
    ON frontier_ranges(day, start_minute);

CREATE TABLE IF NOT EXISTS notices (
    day TEXT NOT NULL,
    announcement_no TEXT NOT NULL,
    title TEXT NOT NULL,
    published_at TEXT NOT NULL,
    published_minute INTEGER NOT NULL,
    day_version TEXT NOT NULL,
    PRIMARY KEY (day, announcement_no)
);

-- Per-day revision token backing the ETag of GET /api/days/{day}.  The token
-- only moves for writes that change what that endpoint returns.
CREATE TABLE IF NOT EXISTS day_revisions (
    day TEXT PRIMARY KEY,
    revision INTEGER NOT NULL
);

-- Each trigger below records one committed write to a table that is part of
-- the day representation.  A new ``days`` row also creates the day.
CREATE TRIGGER IF NOT EXISTS days_revision_after_insert
AFTER INSERT ON days
BEGIN
    INSERT INTO day_revisions(day, revision) VALUES (NEW.day, 1)
    ON CONFLICT(day) DO UPDATE SET revision = day_revisions.revision + 1;
END;

-- ``updated_at``-only touches deliberately do not bump the token.
CREATE TRIGGER IF NOT EXISTS days_revision_after_version_update
AFTER UPDATE OF day_version ON days
WHEN NEW.day_version IS NOT OLD.day_version
BEGIN
    INSERT INTO day_revisions(day, revision) VALUES (NEW.day, 1)
    ON CONFLICT(day) DO UPDATE SET revision = day_revisions.revision + 1;
END;

-- Settled ranges, manual ranges and notices are part of the representation.
-- ``frontier_ranges`` intentionally has no trigger: the frontier is derived
-- from settled ranges and is not part of the day representation, so splitting
-- a pending interval (or re-seeding the frontier) must not invalidate a tag.
"""

# Per-day revision triggers for the tables that contribute to the JSON body.
# Generated rather than hand-written so every table gets the same treatment.
_REVISION_TRIGGER_TABLES = ("completed_ranges", "manual_ranges", "notices")


def _revision_triggers() -> str:
    statements: list[str] = []
    for table in _REVISION_TRIGGER_TABLES:
        for action, row in (("INSERT", "NEW"), ("DELETE", "OLD"), ("UPDATE", "NEW")):
            statements.append(
                f"""
CREATE TRIGGER IF NOT EXISTS {table}_revision_after_{action.lower()}
AFTER {action} ON {table}
BEGIN
    INSERT INTO day_revisions(day, revision) VALUES ({row}.day, 1)
    ON CONFLICT(day) DO UPDATE SET revision = day_revisions.revision + 1;
END;"""
            )
    return "".join(statements)


SCHEMA = SCHEMA + _revision_triggers()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """One short-lived SQLite connection per operation keeps this thread-safe."""

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._connect()
        try:
            conn.executescript(SCHEMA)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.commit()
            # Backfill revision rows for days created before revision tracking
            # existed.  ``OR IGNORE`` keeps an already-tracked day's counter.
            conn.execute(
                "INSERT OR IGNORE INTO day_revisions(day, revision) "
                "SELECT day, 1 FROM days"
            )
            conn.commit()
        finally:
            conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    # ------------------------------------------------------------------ reads

    def get_day_record(self, day: date) -> DayRecord:
        """Read one day as a single consistent snapshot.

        The revision token, the version row and the ranges/notices are read
        inside one deferred transaction.  In WAL mode that pins the snapshot at
        the first read, so a concurrent commit can never produce a response
        whose ETag describes a different state than its body.
        """
        day_text = day.isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT day_version FROM days WHERE day = ?", (day_text,)
            ).fetchone()
            if row is None:
                conn.commit()
                return DayRecord(day=day, day_version=None)

            revision_row = conn.execute(
                "SELECT revision FROM day_revisions WHERE day = ?", (day_text,)
            ).fetchone()
            revision = revision_row["revision"] if revision_row is not None else None

            completed = [
                RangeSpec(r["start_minute"], r["end_minute"])
                for r in conn.execute(
                    "SELECT start_minute, end_minute FROM completed_ranges "
                    "WHERE day = ? ORDER BY start_minute, end_minute",
                    (day_text,),
                )
            ]
            manual = [
                (RangeSpec(r["start_minute"], r["end_minute"]), r["reason"])
                for r in conn.execute(
                    "SELECT start_minute, end_minute, reason FROM manual_ranges "
                    "WHERE day = ? ORDER BY start_minute, end_minute",
                    (day_text,),
                )
            ]
            frontier = [
                FrontierItem(
                    id=r["id"],
                    start_minute=r["start_minute"],
                    end_minute=r["end_minute"],
                )
                for r in conn.execute(
                    "SELECT id, start_minute, end_minute FROM frontier_ranges "
                    "WHERE day = ? ORDER BY start_minute, end_minute, id",
                    (day_text,),
                )
            ]
            notices = [
                Notice(
                    announcement_no=r["announcement_no"],
                    title=r["title"],
                    published_at=r["published_at"],
                    published_minute=r["published_minute"],
                )
                for r in conn.execute(
                    "SELECT announcement_no, title, published_at, published_minute "
                    "FROM notices WHERE day = ? "
                    "ORDER BY published_at, announcement_no",
                    (day_text,),
                )
            ]
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return DayRecord(
            day=day,
            day_version=row["day_version"],
            completed=completed,
            manual=manual,
            notices=notices,
            frontier=frontier,
            revision=revision,
        )

    def day_revision(self, day: date) -> tuple[bool, int | None]:
        """Cheap existence/revision lookup used for conditional requests.

        Returns ``(exists, revision)``.  It touches only the two small metadata
        tables, never notices or range details, and performs no write, so a
        matching ``If-None-Match`` can be answered with ``304`` cheaply.
        """
        day_text = day.isoformat()
        conn = self._connect()
        try:
            conn.execute("BEGIN")
            row = conn.execute(
                "SELECT 1 FROM days WHERE day = ?", (day_text,)
            ).fetchone()
            if row is None:
                conn.commit()
                return (False, None)
            revision_row = conn.execute(
                "SELECT revision FROM day_revisions WHERE day = ?", (day_text,)
            ).fetchone()
            conn.commit()
            return (True, revision_row["revision"] if revision_row is not None else None)
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------- version gate

    def sync_version(self, day: date, source_version: str) -> str:
        """Reconcile the stored version with the source.

        Returns ``initialized``, ``reset`` or ``unchanged``.  A source that
        reports an older version than the archive raises ``StaleVersion`` so the
        archive never rolls backwards.
        """
        day_text = day.isoformat()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT day_version FROM days WHERE day = ?", (day_text,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO days(day, day_version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (day_text, source_version, now, now),
                )
                conn.commit()
                return "initialized"
            current = row["day_version"]
            comparison = compare_versions(source_version, current)
            if comparison < 0:
                raise StaleVersion(day_text, source_version, current)
            if comparison == 0:
                conn.commit()
                return "unchanged"
            self._reset_locked(conn, day_text, source_version, now)
            conn.commit()
            return "reset"
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def reset_to_version(self, day: date, new_version: str) -> bool:
        """Retract old announcements and coverage if ``new_version`` is newer."""
        day_text = day.isoformat()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT day_version FROM days WHERE day = ?", (day_text,)
            ).fetchone()
            if row is None:
                conn.execute(
                    "INSERT INTO days(day, day_version, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (day_text, new_version, now, now),
                )
                conn.commit()
                return True
            current = row["day_version"]
            comparison = compare_versions(new_version, current)
            if comparison < 0:
                raise StaleVersion(day_text, new_version, current)
            if comparison == 0:
                conn.commit()
                return False
            self._reset_locked(conn, day_text, new_version, now)
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _reset_locked(
        conn: sqlite3.Connection, day_text: str, version: str, now: str
    ) -> None:
        conn.execute("DELETE FROM notices WHERE day = ?", (day_text,))
        conn.execute("DELETE FROM completed_ranges WHERE day = ?", (day_text,))
        conn.execute("DELETE FROM manual_ranges WHERE day = ?", (day_text,))
        conn.execute("DELETE FROM frontier_ranges WHERE day = ?", (day_text,))
        conn.execute(
            "UPDATE days SET day_version = ?, updated_at = ? WHERE day = ?",
            (version, now, day_text),
        )

    def _guard_version_locked(
        self, conn: sqlite3.Connection, day_text: str, expected: str
    ) -> None:
        """Reject or absorb a version race inside an open write transaction."""
        row = conn.execute(
            "SELECT day_version FROM days WHERE day = ?", (day_text,)
        ).fetchone()
        if row is None:
            raise DayMissing(f"day {day_text} has no version row")
        current = row["day_version"]
        comparison = compare_versions(expected, current)
        if comparison == 0:
            return
        if comparison < 0:
            raise StaleVersion(day_text, expected, current)
        # The worker learned about a newer version than the archive has.
        self._reset_locked(conn, day_text, expected, _utc_now())
        raise VersionChanged(day_text, expected)

    # -------------------------------------------------------------- frontier

    def seed_frontier(self, day: date) -> list[FrontierItem]:
        """Create frontier rows covering everything not yet settled.

        Idempotent: does nothing when a frontier already exists or the whole day
        is settled.
        """
        record = self.get_day_record(day)
        if record.day_version is None:
            raise DayMissing(f"day {day.isoformat()} has no version row")
        if record.frontier:
            return record.frontier
        occupied = [(r.start_minute, r.end_minute) for r in record.completed]
        occupied.extend((r.start_minute, r.end_minute) for r, _ in record.manual)
        gaps = complement_intervals(occupied)
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._guard_version_locked(conn, day.isoformat(), record.day_version)
            conn.execute("DELETE FROM frontier_ranges WHERE day = ?", (day.isoformat(),))
            conn.executemany(
                "INSERT OR IGNORE INTO frontier_ranges("
                "day, start_minute, end_minute, created_at) VALUES (?, ?, ?, ?)",
                [(day.isoformat(), start, end, now) for start, end in gaps],
            )
            conn.commit()
        except (VersionChanged, StaleVersion):
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
        return self.get_day_record(day).frontier

    def _settle_locked(
        self,
        conn: sqlite3.Connection,
        day_text: str,
        expected_version: str,
        item: FrontierItem,
    ) -> None:
        self._guard_version_locked(conn, day_text, expected_version)
        deleted = conn.execute(
            "DELETE FROM frontier_ranges WHERE id = ? AND day = ?",
            (item.id, day_text),
        ).rowcount
        if deleted != 1:
            raise DayMissing(
                f"frontier item {item.id} vanished for {day_text}"
            )

    # ----------------------------------------------------------------- writes

    def complete_item(
        self,
        day: date,
        expected_version: str,
        item: FrontierItem,
        notices: list[Notice],
    ) -> None:
        """Close a frontier interval, storing its notices atomically."""
        day_text = day.isoformat()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._settle_locked(conn, day_text, expected_version, item)
            for notice in notices:
                conn.execute(
                    "INSERT OR REPLACE INTO notices("
                    "day, announcement_no, title, published_at, "
                    "published_minute, day_version) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        day_text,
                        notice.announcement_no,
                        notice.title,
                        notice.published_at,
                        notice.published_minute,
                        expected_version,
                    ),
                )
            conn.execute(
                "INSERT INTO completed_ranges("
                "day, start_minute, end_minute, day_version, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (day_text, item.start_minute, item.end_minute, expected_version, now),
            )
            conn.commit()
        except (VersionChanged, StaleVersion):
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def split_item(
        self,
        day: date,
        expected_version: str,
        item: FrontierItem,
        left: RangeSpec,
        right: RangeSpec,
    ) -> None:
        """Replace a full interval with two smaller frontier intervals."""
        day_text = day.isoformat()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._settle_locked(conn, day_text, expected_version, item)
            conn.executemany(
                "INSERT OR IGNORE INTO frontier_ranges("
                "day, start_minute, end_minute, created_at) VALUES (?, ?, ?, ?)",
                [
                    (day_text, left.start_minute, left.end_minute, now),
                    (day_text, right.start_minute, right.end_minute, now),
                ],
            )
            conn.commit()
        except (VersionChanged, StaleVersion):
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def manual_item(
        self,
        day: date,
        expected_version: str,
        item: FrontierItem,
        reason: str,
    ) -> None:
        """Move a single-minute interval that stayed full to manual review."""
        day_text = day.isoformat()
        now = _utc_now()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._settle_locked(conn, day_text, expected_version, item)
            conn.execute(
                "INSERT INTO manual_ranges("
                "day, start_minute, end_minute, reason, day_version, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    day_text,
                    item.start_minute,
                    item.end_minute,
                    reason,
                    expected_version,
                    now,
                ),
            )
            conn.commit()
        except (VersionChanged, StaleVersion):
            conn.commit()
            raise
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------------ views

    def coverage(self, record: DayRecord) -> tuple[list[RangeSpec], list[RangeSpec]]:
        covered = merge_intervals(
            [(r.start_minute, r.end_minute) for r in record.completed]
        )
        manual = merge_intervals(
            [(r.start_minute, r.end_minute) for r, _ in record.manual]
        )
        return (
            [RangeSpec(start, end) for start, end in covered],
            [RangeSpec(start, end) for start, end in manual],
        )

    def pending_ranges(self, record: DayRecord) -> list[RangeSpec]:
        """Minute intervals that no completed or manual range covers.

        The persisted frontier is always the complement of the settled ranges,
        so either representation yields the same merged answer; the complement
        is used directly because it also works before the frontier is seeded.
        """
        occupied = [(r.start_minute, r.end_minute) for r in record.completed]
        occupied.extend((r.start_minute, r.end_minute) for r, _ in record.manual)
        return [
            RangeSpec(start, end) for start, end in complement_intervals(occupied)
        ]
