"""Integration test for the late-response hazard, with real source latency.

Scenario:

1. Collector A starts a v1 round; the source takes a v1 snapshot and then
   deliberately delays the response.
2. While A is waiting, the dataset is switched to v2 and collector B completes a
   v2 round against the same SQLite file, retracting the v1 data.
3. A's late v1 page finally arrives.  It must be rejected and must not restore
   the retracted notices or coverage.
"""

from __future__ import annotations

import asyncio
import threading
import time
from datetime import date
from pathlib import Path

import httpx
import pytest

import source.main as source_main
from app.collector import Collector
from app.db import Database
from app.source_client import SourceClient
from tests.conftest import DATASETS
from source.store import SourceState

DAY = date(2026, 11, 19)


class SyncASGITransport(httpx.BaseTransport):
    def __init__(self, asgi_app) -> None:
        self._asgi_app = asgi_app

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        target = request.url.raw_path.decode("ascii")

        async def run() -> httpx.Response:
            transport = httpx.ASGITransport(app=self._asgi_app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://source"
            ) as http:
                return await http.request(
                    request.method,
                    target,
                    headers=request.headers,
                    content=request.content,
                )

        buffered = asyncio.run(run())
        return httpx.Response(
            status_code=buffered.status_code,
            headers=buffered.headers,
            content=buffered.content,
            request=request,
        )


@pytest.fixture()
def source_state() -> SourceState:
    state = SourceState(DATASETS, "v1")
    source_main.set_state(state)
    return state


@pytest.fixture()
def make_client(source_state: SourceState):
    clients: list[SourceClient] = []

    def factory() -> SourceClient:
        client = SourceClient(
            "http://source", 30.0, transport=SyncASGITransport(source_main.app)
        )
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


def _switch_to_v2() -> None:
    with httpx.Client(
        base_url="http://source", transport=SyncASGITransport(source_main.app)
    ) as http:
        assert http.post("/v1/admin/version", json={"version": "v2"}).status_code == 200


def test_late_v1_page_cannot_restore_retracted_data(
    tmp_path: Path, source_state: SourceState, make_client
) -> None:
    db = Database(tmp_path / "race.sqlite3")
    db.init()

    # Make exactly one search slow.  The snapshot is taken before the sleep.
    asyncio.run(source_state.configure_delay(delay_ms=600, requests=1))

    collector_a = Collector(db, make_client())
    collector_b = Collector(db, make_client())
    result_a: dict[str, object] = {}

    def run_a() -> None:
        result_a["round"] = collector_a.run_round(DAY)

    worker = threading.Thread(target=run_a)
    worker.start()

    # Wait until A's search has taken its snapshot (the delay counter is spent).
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if asyncio.run(source_state.fault_state())["requests_remaining"] == 0:
            break
        time.sleep(0.01)
    else:
        pytest.fail("collector A never reached the source search")

    # Now move the world to v2 and let B finish a full v2 round.
    _switch_to_v2()
    result_b = collector_b.run_round(DAY)
    assert result_b.status == "collected"
    assert result_b.version_reset is True
    assert result_b.notices_collected == 4

    record_v2 = db.get_day_record(DAY)
    assert record_v2.day_version == "2026-11-19.v2"
    assert {n.announcement_no for n in record_v2.notices} == {
        "HB-EIA-2026-1119-01",
        "HB-EIA-2026-1119-02",
        "HB-EIA-2026-1119-03",
        "HB-EIA-2026-1119-04",
    }

    worker.join(timeout=10)
    assert not worker.is_alive()

    # A's late v1 page was rejected rather than written.
    late = result_a["round"]
    assert late.status in {"stale_response", "version_changed"}
    record = db.get_day_record(DAY)
    assert record.day_version == "2026-11-19.v2"
    assert {n.announcement_no for n in record.notices} == {
        "HB-EIA-2026-1119-01",
        "HB-EIA-2026-1119-02",
        "HB-EIA-2026-1119-03",
        "HB-EIA-2026-1119-04",
    }
    # The v1 revision text must not have overwritten the v2 title.
    titles = {n.announcement_no: n.title for n in record.notices}
    assert titles["HB-EIA-2026-1119-01"] == "云岭风电场二期工程环境影响报告书公示（修订版）"


def test_delay_hook_does_not_apply_to_version_check(source_state: SourceState) -> None:
    """Only searches consume the delay, so the pre-flight check stays fast."""
    asyncio.run(source_state.configure_delay(delay_ms=100, requests=1))
    started = time.monotonic()
    asyncio.run(source_state.day_version(DAY))
    assert time.monotonic() - started < 0.05
    assert asyncio.run(source_state.fault_state())["requests_remaining"] == 1
