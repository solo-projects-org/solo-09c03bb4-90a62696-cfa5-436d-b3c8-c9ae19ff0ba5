"""FastAPI app for the local disclosure source."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from common.times import parse_archive_date
from source.store import SourceState

DATA_DIR = Path(os.environ.get("SOURCE_DATA_DIR", "/data/datasets"))
INITIAL_VERSION = os.environ.get("SOURCE_DATASET", "v1")

app = FastAPI(
    title="Local disclosure source (demo)",
    version="1.0.0",
    description=(
        "Minute-interval search over a versioned local dataset. Mirrors the "
        "upstream contract: at most five results, no total count, responses "
        "carry the day version."
    ),
)

_state: SourceState | None = None


def get_state() -> SourceState:
    global _state
    if _state is None:
        _state = SourceState(DATA_DIR, INITIAL_VERSION)
    return _state


def set_state(state: SourceState) -> None:
    """Test hook: inject a prepared state without touching the filesystem."""
    global _state
    _state = state


class NoticeOut(BaseModel):
    announcement_no: str
    title: str
    published_at: str


class SearchOut(BaseModel):
    day: str
    day_version: str
    start_minute: int
    end_minute: int
    notices: list[NoticeOut]


class VersionOut(BaseModel):
    day: str
    day_version: str


class SwitchIn(BaseModel):
    version: str


class SwitchOut(BaseModel):
    active_version: str
    available_versions: list[str]


class DelayIn(BaseModel):
    delay_ms: int = Field(ge=0, le=60000)
    requests: int = Field(ge=0, le=100)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/v1/days/{day}/version", response_model=VersionOut)
async def day_version(day: str) -> VersionOut:
    state = get_state()
    try:
        parsed = parse_archive_date(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    try:
        version = await state.day_version(parsed)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no data for {day}") from exc
    return VersionOut(day=day, day_version=version)


@app.get("/v1/days/{day}/notices", response_model=SearchOut)
async def search_notices(
    day: str,
    start_minute: int = Query(ge=0, le=1439),
    end_minute: int = Query(ge=0, le=1439),
) -> SearchOut:
    state = get_state()
    try:
        parsed = parse_archive_date(day)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if start_minute > end_minute:
        raise HTTPException(
            status_code=422, detail="start_minute must not exceed end_minute"
        )
    try:
        version, notices = await state.search(parsed, start_minute, end_minute)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=f"no data for {day}") from exc
    return SearchOut(
        day=day,
        day_version=version,
        start_minute=start_minute,
        end_minute=end_minute,
        notices=[NoticeOut(**notice.as_dict()) for notice in notices],
    )


@app.get("/v1/admin/version", response_model=SwitchOut)
async def show_version() -> SwitchOut:
    state = get_state()
    return SwitchOut(
        active_version=state.active_version,
        available_versions=state.available_versions(),
    )


@app.post("/v1/admin/version", response_model=SwitchOut)
async def switch_version(payload: SwitchIn) -> SwitchOut:
    state = get_state()
    try:
        active = await state.switch_version(payload.version)
    except KeyError as exc:
        raise HTTPException(
            status_code=404,
            detail=f"unknown version {payload.version}; "
            f"available: {', '.join(state.available_versions())}",
        ) from exc
    return SwitchOut(active_version=active, available_versions=state.available_versions())


@app.post("/v1/admin/delay")
async def configure_delay(payload: DelayIn) -> dict[str, int]:
    state = get_state()
    await state.configure_delay(payload.delay_ms, payload.requests)
    return {"delay_ms": payload.delay_ms, "requests": payload.requests}


@app.get("/v1/admin/delay")
async def show_delay() -> dict[str, int]:
    return await get_state().fault_state()


def create_app(data_dir: Path | None = None, initial_version: str | None = None) -> FastAPI:
    """Factory used by tests to point the source at a specific dataset."""
    global DATA_DIR, INITIAL_VERSION
    if data_dir is not None:
        DATA_DIR = Path(data_dir)
    if initial_version is not None:
        INITIAL_VERSION = initial_version
    set_state(SourceState(DATA_DIR, INITIAL_VERSION))
    return app


__all__ = ["app", "create_app", "set_state", "get_state"]
