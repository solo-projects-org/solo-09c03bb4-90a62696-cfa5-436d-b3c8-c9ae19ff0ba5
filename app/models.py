"""Domain models shared between the source, the collector and the API."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Notice:
    """A single disclosure notice as returned by the source."""

    announcement_no: str
    title: str
    published_at: str
    published_minute: int

    def as_dict(self) -> dict[str, object]:
        return {
            "announcement_no": self.announcement_no,
            "title": self.title,
            "published_at": self.published_at,
            "published_minute": self.published_minute,
        }


@dataclass(frozen=True)
class SourceResponse:
    """One page of source results, with no total count by design."""

    day_version: str
    notices: tuple[Notice, ...]
    start_minute: int
    end_minute: int


@dataclass(frozen=True)
class RangeSpec:
    """Inclusive minute interval to collect, in minute-of-day units."""

    start_minute: int
    end_minute: int

    def __post_init__(self) -> None:
        if not 0 <= self.start_minute <= self.end_minute <= 1439:
            raise ValueError("range must satisfy 0 <= start <= end <= 1439")

    @property
    def size(self) -> int:
        return self.end_minute - self.start_minute + 1

    def as_dict(self) -> dict[str, int]:
        return {"start_minute": self.start_minute, "end_minute": self.end_minute}
