"""Bounded collection rounds for one archive day.

A round is deliberately bounded:

1. Verify the day version before doing any work.
2. Search at most ``max_round_queries`` times (hard cap of six).
3. Fewer than five notices closes the whole searched interval.
4. A full page splits the interval; a full single minute goes to manual review.

Every split/close/manual decision is written to SQLite before the next query,
so an interrupted round resumes from the persisted frontier.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from app.db import (
    Database,
    DayMissing,
    FrontierItem,
    StaleVersion,
    VersionChanged,
)
from app.errors import InvalidSourceResponse, SourceError
from app.models import RangeSpec
from app.source_client import SourceClient
from common.versions import compare_versions

PAGE_LIMIT = 5
DEFAULT_MAX_QUERIES = 6
MANUAL_REASON = "one-minute interval still returned five notices; needs manual review"

RoundStatus = Literal[
    "collected",
    "partial",
    "needs_manual_review",
    "source_error",
    "stale_response",
    "version_changed",
]


@dataclass
class RoundResult:
    day: date
    status: RoundStatus
    day_version: str | None
    queries_used: int = 0
    collected_ranges: list[RangeSpec] = field(default_factory=list)
    manual_ranges: list[RangeSpec] = field(default_factory=list)
    pending_ranges: list[RangeSpec] = field(default_factory=list)
    notices_collected: int = 0
    version_reset: bool = False
    detail: str | None = None


class DayBusy(RuntimeError):
    """A collection round for this day is already running."""


class Collector:
    """Runs bounded collection rounds against a source."""

    def __init__(
        self,
        db: Database,
        client: SourceClient,
        max_round_queries: int = DEFAULT_MAX_QUERIES,
    ) -> None:
        if not 1 <= max_round_queries <= DEFAULT_MAX_QUERIES:
            raise ValueError(f"max_round_queries must be within 1..{DEFAULT_MAX_QUERIES}")
        self.db = db
        self.client = client
        self.max_round_queries = max_round_queries
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _day_lock(self, day: date) -> threading.Lock:
        key = day.isoformat()
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._locks[key] = lock
            return lock

    def run_round(self, day: date, max_queries: int | None = None) -> RoundResult:
        if max_queries is None:
            max_queries = self.max_round_queries
        if not 1 <= max_queries <= self.max_round_queries:
            raise ValueError(f"max_queries must be within 1..{self.max_round_queries}")

        lock = self._day_lock(day)
        if not lock.acquire(blocking=False):
            raise DayBusy(f"a collection round for {day.isoformat()} is already running")
        try:
            return self._run_round_locked(day, max_queries)
        finally:
            lock.release()

    # ------------------------------------------------------------------ internals

    def _run_round_locked(self, day: date, max_queries: int) -> RoundResult:
        # Step 1: verify the day version before touching any interval.
        try:
            expected_version = self.client.day_version(day)
        except (SourceError, InvalidSourceResponse) as exc:
            record = self.db.get_day_record(day)
            return RoundResult(
                day=day,
                status="source_error",
                day_version=record.day_version,
                pending_ranges=self.db.pending_ranges(record),
                detail=str(exc),
            )

        try:
            transition = self.db.sync_version(day, expected_version)
        except StaleVersion as exc:
            record = self.db.get_day_record(day)
            return RoundResult(
                day=day,
                status="stale_response",
                day_version=record.day_version,
                pending_ranges=self.db.pending_ranges(record),
                detail=str(exc),
            )

        result = RoundResult(
            day=day,
            status="collected",
            day_version=expected_version,
            version_reset=transition == "reset",
        )

        record = self.db.get_day_record(day)
        frontier = record.frontier
        if not frontier:
            if self.db.pending_ranges(record):
                try:
                    frontier = self.db.seed_frontier(day)
                except (StaleVersion, VersionChanged, DayMissing) as exc:
                    result.status = "version_changed"
                    result.detail = str(exc)
                    result.pending_ranges = self.db.pending_ranges(record)
                    return result
            else:
                # Everything is already settled: nothing to search this round.
                result.status = (
                    "needs_manual_review" if record.manual else "collected"
                )
                return result

        queries_used = 0
        while frontier and queries_used < max_queries:
            item = frontier[0]
            outcome = self._step(day, item, expected_version, result)
            queries_used += 1
            result.queries_used = queries_used

            if outcome == "abort":
                record = self.db.get_day_record(day)
                result.pending_ranges = self.db.pending_ranges(record)
                return result

            record = self.db.get_day_record(day)
            frontier = record.frontier

        record = self.db.get_day_record(day)
        result.pending_ranges = self.db.pending_ranges(record)
        if result.pending_ranges or frontier:
            result.status = "partial"
        elif record.manual:
            result.status = "needs_manual_review"
        else:
            result.status = "collected"
        return result

    def _step(
        self,
        day: date,
        item: FrontierItem,
        expected_version: str,
        result: RoundResult,
    ) -> Literal["continue", "abort"]:
        target = item.spec
        try:
            response = self.client.search(day, target.start_minute, target.end_minute)
        except (SourceError, InvalidSourceResponse) as exc:
            result.status = "source_error"
            result.detail = str(exc)
            return "abort"

        comparison = compare_versions(response.day_version, expected_version)
        if comparison > 0:
            # A newer day version appeared mid-round: retract and stop.
            try:
                self.db.reset_to_version(day, response.day_version)
            except StaleVersion as exc:
                # Another writer already advanced past this response.
                result.status = "stale_response"
                result.detail = str(exc)
                return "abort"
            result.status = "version_changed"
            result.version_reset = True
            result.day_version = response.day_version
            result.detail = (
                f"day version advanced from {expected_version} to "
                f"{response.day_version}; old coverage retracted"
            )
            return "abort"
        if comparison < 0:
            # A late response from an older version must never be persisted.
            result.status = "stale_response"
            result.detail = (
                f"discarded late response for day version {response.day_version}"
            )
            return "abort"

        try:
            if len(response.notices) < PAGE_LIMIT:
                self.db.complete_item(
                    day, expected_version, item, list(response.notices)
                )
                result.collected_ranges.append(target)
                result.notices_collected += len(response.notices)
                return "continue"

            if target.size == 1:
                self.db.manual_item(day, expected_version, item, MANUAL_REASON)
                result.manual_ranges.append(target)
                return "continue"

            mid = (target.start_minute + target.end_minute) // 2
            self.db.split_item(
                day,
                expected_version,
                item,
                RangeSpec(target.start_minute, mid),
                RangeSpec(mid + 1, target.end_minute),
            )
            return "continue"
        except VersionChanged as exc:
            result.status = "version_changed"
            result.version_reset = True
            result.day_version = exc.new_version
            result.detail = str(exc)
            return "abort"
        except (StaleVersion, DayMissing) as exc:
            result.status = "stale_response"
            result.detail = str(exc)
            return "abort"
