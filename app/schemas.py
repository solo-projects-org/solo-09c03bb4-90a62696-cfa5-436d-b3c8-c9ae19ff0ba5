"""HTTP schemas for the archive service."""

from __future__ import annotations

from pydantic import BaseModel, Field

from common.times import minute_label


class RangeOut(BaseModel):
    start_minute: int
    end_minute: int
    from_time: str
    to_time: str


class ManualRangeOut(RangeOut):
    reason: str


class NoticeOut(BaseModel):
    announcement_no: str
    title: str
    published_at: str
    published_minute: int


class CollectRequest(BaseModel):
    day: str = Field(
        description="Archive day in YYYY-MM-DD (UTC)",
        examples=["2026-11-20"],
    )
    max_queries: int | None = Field(
        default=None,
        ge=1,
        le=6,
        description="Queries allowed in this round; defaults to the service cap of 6",
        examples=[6],
    )

    model_config = {
        "json_schema_extra": {"example": {"day": "2026-11-20", "max_queries": 6}}
    }


class CollectResponse(BaseModel):
    day: str
    status: str
    day_version: str | None
    queries_used: int
    version_reset: bool
    collected_ranges: list[RangeOut]
    manual_ranges: list[RangeOut]
    pending_ranges: list[RangeOut]
    notices_collected: int
    detail: str | None = None


class DayCoverageOut(BaseModel):
    day: str
    day_version: str | None
    complete: bool
    notices: list[NoticeOut]
    completed_ranges: list[RangeOut]
    pending_ranges: list[RangeOut]
    manual_ranges: list[ManualRangeOut]


def range_out(start: int, end: int) -> RangeOut:
    return RangeOut(
        start_minute=start,
        end_minute=end,
        from_time=minute_label(start),
        to_time=minute_label(end),
    )
