"""End-to-end API tests: the archive app talking to the real source app."""

from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

import source.main as source_main
from app.db import Database
from app.main import AppState, set_state_for_tests
from app.main import app as archive_app
from app.collector import Collector
from app.source_client import SourceClient
from source.store import SourceState

DATASETS = Path(__file__).resolve().parent.parent / "datasets"


class SyncASGITransport(httpx.BaseTransport):
    """Drive an ASGI app from the archive's synchronous HTTP client.

    The response is fully buffered so it satisfies httpx's sync transport
    contract; the fixture scope keeps each request isolated.
    """

    def __init__(self, asgi_app) -> None:
        self._asgi_app = asgi_app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        target = request.url.raw_path.decode("ascii")

        async def run() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://source"
            ) as client:
                return await client.request(
                    request.method,
                    target,
                    headers=request.headers,
                    content=request.content,
                )

        result = asyncio.run(run())
        return httpx.Response(
            status_code=result.status_code,
            headers=result.headers,
            content=result.content,
            request=request,
        )


@pytest.fixture()
def source_app(source_state: SourceState):
    source_main.set_state(source_state)
    return source_main.app


@pytest.fixture()
def archive_client(tmp_path: Path, source_app):
    db = Database(tmp_path / "api.sqlite3")
    db.init()
    client = SourceClient("http://source", timeout=5.0, transport=SyncASGITransport(source_app))
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


def test_swagger_and_openapi_available(archive_client: TestClient) -> None:
    assert archive_client.get("/docs").status_code == 200
    schema = archive_client.get("/openapi.json").json()
    assert "/api/collection/rounds" in schema["paths"]
    assert "/api/days/{day}" in schema["paths"]


def test_collect_then_query_sparse_day(archive_client: TestClient) -> None:
    result = collect(archive_client, "2026-11-19")
    assert result["status"] == "collected"
    assert result["queries_used"] == 1
    assert result["notices_collected"] == 3

    day = archive_client.get("/api/days/2026-11-19").json()
    assert day["day_version"] == "2026-11-19.v1"
    assert day["complete"] is True
    assert day["pending_ranges"] == []
    assert day["manual_ranges"] == []
    assert day["completed_ranges"] == [
        {
            "start_minute": 0,
            "end_minute": 1439,
            "from_time": "00:00",
            "to_time": "23:59",
        }
    ]
    assert {n["announcement_no"] for n in day["notices"]} == {
        "HB-EIA-2026-1119-01",
        "HB-EIA-2026-1119-02",
        "HB-EIA-2026-1119-03",
    }


def test_dense_day_shows_manual_and_pending(archive_client: TestClient) -> None:
    collect(archive_client, "2026-11-20", max_queries=2)
    partial = archive_client.get("/api/days/2026-11-20").json()
    assert partial["complete"] is False
    assert partial["pending_ranges"], "unsearched intervals must be listed"

    for _ in range(20):
        result = collect(archive_client, "2026-11-20")
        if result["status"] in {"collected", "needs_manual_review"}:
            break

    day = archive_client.get("/api/days/2026-11-20").json()
    assert day["complete"] is False  # manual minute still needs a human
    assert day["pending_ranges"] == []
    assert len(day["manual_ranges"]) == 1
    assert day["manual_ranges"][0] == {
        "start_minute": 552,
        "end_minute": 552,
        "from_time": "09:12",
        "to_time": "09:12",
        "reason": "one-minute interval still returned five notices; needs manual review",
    }


def test_round_cap_is_enforced_by_api(archive_client: TestClient) -> None:
    response = archive_client.post(
        "/api/collection/rounds", json={"day": "2026-11-20", "max_queries": 7}
    )
    assert response.status_code == 422


def test_invalid_day_is_rejected(archive_client: TestClient) -> None:
    assert archive_client.get("/api/days/2026-02-30").status_code == 422
    assert archive_client.post(
        "/api/collection/rounds", json={"day": "2026/11/20"}
    ).status_code == 422


def test_unknown_day_is_a_source_error(archive_client: TestClient) -> None:
    response = archive_client.post(
        "/api/collection/rounds", json={"day": "2025-01-01"}
    )
    assert response.status_code == 502


def test_version_switch_retracts_and_recollects(
    archive_client: TestClient, source_app
) -> None:
    first = collect(archive_client, "2026-11-19")
    assert first["status"] == "collected"

    switched = archive_client.get("/v1/admin/version")
    assert switched.status_code == 404  # sanity: admin lives on the source

    switch = httpx.Client(
        base_url="http://source", transport=SyncASGITransport(source_app)
    ).post("/v1/admin/version", json={"version": "v2"})
    assert switch.status_code == 200

    result = collect(archive_client, "2026-11-19")
    assert result["version_reset"] is True

    day = archive_client.get("/api/days/2026-11-19").json()
    assert day["day_version"] == "2026-11-19.v2"
    assert len(day["notices"]) == 4
    assert day["complete"] is True
    titles = {n["announcement_no"]: n["title"] for n in day["notices"]}
    assert titles["HB-EIA-2026-1119-01"] == "云岭风电场二期工程环境影响报告书公示（修订版）"


def test_restart_resumes_pending_work(
    archive_client: TestClient, tmp_path: Path, source_app
) -> None:
    collect(archive_client, "2026-11-20", max_queries=2)
    before = archive_client.get("/api/days/2026-11-20").json()
    assert before["pending_ranges"]

    # Simulate a restart: new Database/Collector/AppState over the same file.
    db = Database(tmp_path / "api.sqlite3")
    db.init()
    client = SourceClient("http://source", 5.0, transport=SyncASGITransport(source_app))
    state = AppState(db=db, client=client, collector=Collector(db, client))
    set_state_for_tests(state)
    try:
        after = archive_client.get("/api/days/2026-11-20").json()
        assert after["pending_ranges"] == before["pending_ranges"]
        resumed = collect(archive_client, "2026-11-20", max_queries=1)
        assert resumed["queries_used"] == 1
        assert resumed["status"] == "partial"
    finally:
        client.close()


def test_version_endpoint_reports_archive_version(archive_client: TestClient) -> None:
    collect(archive_client, "2026-11-18")
    body = archive_client.get("/api/days/2026-11-18/version").json()
    assert body == {"day": "2026-11-18", "day_version": "2026-11-18.v1"}
