"""Composition root and FastAPI app for the archive service."""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date

from fastapi import FastAPI, Header, HTTPException, Response

from app.collector import Collector, DayBusy, RoundResult
from app.config import settings
from app.db import Database
from app.etag import if_none_match_matches, tag_for_state, tag_for_unseen_day
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


@app.get(
    "/api/days/{day}",
    response_model=DayCoverageOut,
    responses={
        200: {
            "description": "Day coverage",
            "headers": {
                "ETag": {
                    "description": (
                        "Strong entity tag for this committed day state. Send it "
                        "back as If-None-Match to receive 304 when unchanged."
                    ),
                    "schema": {"type": "string"},
                }
            },
        },
        304: {"description": "The cached representation is still current"},
    },
)
def day_coverage(
    day: str,
    response: Response,
    if_none_match: str | None = Header(default=None, alias="If-None-Match"),
) -> DayCoverageOut | Response:
    parsed = _parse_day(day)
    state = archive()

    # Cheap path: classify the day from metadata only.  A day that has never
    # been collected still has a well-defined representation, so it gets a
    # stable tag too and can be answered with 304.
    exists, revision = state.db.day_revision(parsed)
    if exists:
        current_tag = tag_for_state(day, revision)
    else:
        current_tag = tag_for_unseen_day(day)

    if if_none_match_matches(if_none_match, current_tag):
        return Response(
            status_code=304,
            headers={"ETag": current_tag, "Cache-Control": "no-cache"},
        )

    record = state.db.get_day_record(parsed)

    # The body and its tag must come from one committed snapshot.  Derive the
    # final tag from the record just read, never from the earlier metadata-only
    # lookup (a day may have appeared in between).  When the snapshot differs
    # from what the client holds, the body itself is the revalidation answer.
    snapshot_tag = (
        tag_for_unseen_day(day)
        if record.day_version is None
        else tag_for_state(day, record.revision)
    )
    if snapshot_tag != current_tag and if_none_match_matches(
        if_none_match, snapshot_tag
    ):
        # The body we just loaded is exactly what the client already has.
        return Response(
            status_code=304,
            headers={"ETag": snapshot_tag, "Cache-Control": "no-cache"},
        )
    current_tag = snapshot_tag

    completed, manual_merged = state.db.coverage(record)
    pending = state.db.pending_ranges(record)
    body = DayCoverageOut(
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

    # The representation can change at any committed write, so caches must
    # revalidate.  Revalidation is cheap: a match costs two small metadata
    # reads and no body materialisation.
    response.headers["ETag"] = current_tag
    response.headers["Cache-Control"] = "no-cache"
    return body


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
