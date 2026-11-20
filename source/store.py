"""In-memory dataset store for the local disclosure source.

The source models an upstream publishing system: data is grouped into named
dataset versions, each day has its own day version string, and searches are
capped at five notices without exposing a total.  The active dataset can be
switched at runtime (or via ``SOURCE_DATASET``) so the archive's retraction
behaviour can be demonstrated.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from common.times import parse_archive_date, parse_published_at

PAGE_LIMIT = 5


@dataclass(frozen=True)
class SourceNotice:
    announcement_no: str
    title: str
    published_at: str
    published_minute: int
    sort_key: datetime

    def as_dict(self) -> dict[str, object]:
        return {
            "announcement_no": self.announcement_no,
            "title": self.title,
            "published_at": self.published_at,
        }


@dataclass
class Dataset:
    label: str
    days: dict[str, list[SourceNotice]] = field(default_factory=dict)

    def day_version(self, day: str) -> str:
        return f"{day}.{self.label}"


class DatasetError(RuntimeError):
    """A dataset file is missing or violates the source contract."""


def load_dataset(path: Path) -> Dataset:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DatasetError(f"dataset file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise DatasetError(f"dataset file is not valid JSON: {path}: {exc}") from exc

    label = str(raw.get("label", "")).strip()
    if not label:
        raise DatasetError(f"dataset {path.name} is missing a label")
    days_raw = raw.get("days")
    if not isinstance(days_raw, dict):
        raise DatasetError(f"dataset {path.name} is missing a days object")

    days: dict[str, list[SourceNotice]] = {}
    for day_text, entries in days_raw.items():
        try:
            day = parse_archive_date(day_text)
        except ValueError as exc:
            raise DatasetError(f"dataset {path.name}: invalid day {day_text}") from exc
        if not isinstance(entries, list):
            raise DatasetError(f"dataset {path.name}: day {day_text} must be a list")
        notices: list[SourceNotice] = []
        seen: set[str] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise DatasetError(
                    f"dataset {path.name}: day {day_text} has a non-object notice"
                )
            announcement_no = str(entry.get("announcement_no", "")).strip()
            title = str(entry.get("title", "")).strip()
            if not announcement_no or not title:
                raise DatasetError(
                    f"dataset {path.name}: day {day_text} notice needs "
                    "announcement_no and title"
                )
            if announcement_no in seen:
                raise DatasetError(
                    f"dataset {path.name}: duplicate announcement_no "
                    f"{announcement_no} on {day_text}"
                )
            seen.add(announcement_no)
            try:
                published_at, minute = parse_published_at(
                    str(entry.get("published_at", "")), day
                )
            except ValueError as exc:
                raise DatasetError(
                    f"dataset {path.name}: {day_text} {announcement_no}: {exc}"
                ) from exc
            sort_key = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
            notices.append(
                SourceNotice(
                    announcement_no=announcement_no,
                    title=title,
                    published_at=published_at,
                    published_minute=minute,
                    sort_key=sort_key,
                )
            )
        notices.sort(key=lambda n: (n.sort_key, n.announcement_no))
        days[day_text] = notices
    return Dataset(label=label, days=days)


class SourceState:
    """Thread-safe holder for the active dataset and demo faults."""

    def __init__(self, data_dir: Path, initial_version: str) -> None:
        self._data_dir = Path(data_dir)
        self._lock = asyncio.Lock()
        self._datasets = {
            path.stem: load_dataset(path)
            for path in sorted(self._data_dir.glob("*.json"))
        }
        if not self._datasets:
            raise DatasetError(f"no dataset files found in {self._data_dir}")
        if initial_version not in self._datasets:
            raise DatasetError(
                f"unknown dataset version {initial_version!r}; "
                f"available: {', '.join(sorted(self._datasets))}"
            )
        self._active = initial_version
        self._delay_ms = 0
        self._delay_remaining = 0

    @property
    def active_version(self) -> str:
        return self._active

    def available_versions(self) -> list[str]:
        return sorted(self._datasets)

    async def active_dataset(self) -> Dataset:
        async with self._lock:
            return self._datasets[self._active]

    async def day_version(self, day: date) -> str:
        day_text = day.isoformat()
        async with self._lock:
            dataset = self._datasets[self._active]
            if day_text not in dataset.days:
                raise KeyError(day_text)
            return dataset.day_version(day_text)

    async def search(
        self, day: date, start_minute: int, end_minute: int
    ) -> tuple[str, list[SourceNotice]]:
        """Snapshot the matching page, then honour any configured delay.

        The delay is applied *after* the snapshot, which is what lets tests
        produce a genuinely late response from an older day version.
        """
        day_text = day.isoformat()
        async with self._lock:
            dataset = self._datasets[self._active]
            if day_text not in dataset.days:
                raise KeyError(day_text)
            version = dataset.day_version(day_text)
            matches = [
                notice
                for notice in dataset.days[day_text]
                if start_minute <= notice.published_minute <= end_minute
            ]
            delay_ms = 0
            if self._delay_remaining > 0:
                delay_ms = self._delay_ms
                self._delay_remaining -= 1
        if delay_ms:
            await asyncio.sleep(delay_ms / 1000)
        return version, matches[:PAGE_LIMIT]

    async def switch_version(self, version: str) -> str:
        async with self._lock:
            if version not in self._datasets:
                raise KeyError(version)
            self._active = version
            return self._active

    async def configure_delay(self, delay_ms: int, requests: int) -> None:
        async with self._lock:
            self._delay_ms = delay_ms
            self._delay_remaining = requests

    async def fault_state(self) -> dict[str, int]:
        async with self._lock:
            return {
                "delay_ms": self._delay_ms,
                "requests_remaining": self._delay_remaining,
            }
