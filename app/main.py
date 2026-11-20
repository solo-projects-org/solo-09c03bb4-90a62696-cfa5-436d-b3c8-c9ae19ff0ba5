"""Composition root and FastAPI app for the archive service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, HTTPException

from app.collector import Collector, DayBusy, RoundResult
from app.config import settings
from app.db import Database
from app.schemas import (
    CollectRequest,
    CollectResponse,
    DayCoverageOut,
    ManualRangeOut,
    NoticeOut,
    range_out,
)
from app.source_client import SourceClient
from common.times import parse_archive_date


class AppState:
    def __init__(self, db: Database, client: SourceClient, collector: Collector) -> None:
        self.db = db
        self.client = client
        self.collector = collector


def build_state() -> AppState:
    db = Database(settings.db_path)
    db.init()
    client = SourceClient(settings.source_base_url, settings.source_timeout_seconds)
    collector = Collector(db, client, settings.max_round_queries)
    return AppState(db=db, client=client, collector=collector)


def set_state_for_tests(state: AppState) -> None:
    app.state.archive = state


@asynccontextmanager
async def lifespan(app: FastAPI):
    if not getattr(app.state, "archive", None):
        app.state.archive = build_state()
    try:
        yield
    finally:
        state: AppState | None = getattr(app.state, "archive", None)
        if state is not None:
            state.client.close()


app = FastAPI(
    title="Environmental disclosure archive",
    version="1.0.0",
    description=(
        "Bounded continuation collection over a versioned disclosure source. "
        "Rounds verify the day version first, then search at most six minute "
        "intervals. Fewer than five results closes an interval; a full page "
        "splits it; a full single minute is left for manual review."
    ),
    lifespan=lifespan,
)


def archive() -> AppState:
    state: AppState | None = getattr(app.state, "archive", None)
    if state is None:
        state = build_state()
        app.state.archive = state
    return state


def _parse_day(day: str) -> date:
    try:
        return parse_archive_date(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _round_payload(result: RoundResult) -> CollectResponse:
    return CollectResponse(
        day=result.day.isoformat(),
        status=result.status,
        day_version=result.day_version,
        queries_used=result.queries_used,
        version_reset=result.version_reset,
        collected_ranges=[
            range_out(r.start_minute, r.end_minute) for r in result.collected_ranges
        ],
        manual_ranges=[
            range_out(r.start_minute, r.end_minute) for r in result.manual_ranges
        ],
        pending_ranges=[
            range_out(r.start_minute, r.end_minute) for r in result.pending_ranges
        ],
        notices_collected=result.notices_collected,
        detail=result.detail,
    )


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/collection/rounds", response_model=CollectResponse)
def run_round(payload: CollectRequest) -> CollectResponse:
    day = _parse_day(payload.day)
    state = archive()
    max_queries = payload.max_queries or settings.max_round_queries
    try:
        result = state.collector.run_round(day, max_queries)
    except DayBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if result.status == "source_error" and result.queries_used == 0 and result.detail:
        raise HTTPException(
            status_code=502, detail={"day": payload.day, "error": result.detail}
        )
    return _round_payload(result)


@app.get("/api/days/{day}", response_model=DayCoverageOut)
def day_coverage(day: str) -> DayCoverageOut:
    parsed = _parse_day(day)
    state = archive()
    record = state.db.get_day_record(parsed)
    completed, manual_merged = state.db.coverage(record)
    pending = state.db.pending_ranges(record)
    return DayCoverageOut(
        day=day,
        day_version=record.day_version,
        complete=not pending and not manual_merged,
        notices=[
            NoticeOut(
                announcement_no=n.announcement_no,
                title=n.title,
                published_at=n.published_at,
                published_minute=n.published_minute,
            )
            for n in record.notices
        ],
        completed_ranges=[range_out(r.start_minute, r.end_minute) for r in completed],
        pending_ranges=[range_out(r.start_minute, r.end_minute) for r in pending],
        manual_ranges=[
            ManualRangeOut(
                **range_out(r.start_minute, r.end_minute).model_dump(),
                reason=reason,
            )
            for r, reason in record.manual
        ],
    )


@app.get("/api/days/{day}/version")
def version_view(day: str) -> dict[str, str | None]:
    parsed = _parse_day(day)
    state = archive()
    record = state.db.get_day_record(parsed)
    return {"day": day, "day_version": record.day_version}


__all__ = [
    "app",
    "AppState",
    "build_state",
    "set_state_for_tests",
]
