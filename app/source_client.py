"""HTTP client for the disclosure source."""

from __future__ import annotations

from datetime import date

import httpx

from app.errors import InvalidSourceResponse, SourceError
from app.models import Notice, SourceResponse
from common.times import parse_published_at


class SourceClient:
    """Thin, strict client. Every call validates the response contract."""

    def __init__(
        self,
        base_url: str,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            timeout=timeout,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def day_version(self, day: date) -> str:
        path = f"/v1/days/{day.isoformat()}/version"
        payload = self._get(path)
        version = payload.get("day_version")
        if not isinstance(version, str) or not version.strip():
            raise InvalidSourceResponse("source day_version is missing or empty")
        if payload.get("day") != day.isoformat():
            raise InvalidSourceResponse("source returned a different day")
        return version.strip()

    def search(self, day: date, start_minute: int, end_minute: int) -> SourceResponse:
        path = f"/v1/days/{day.isoformat()}/notices"
        payload = self._get(
            path,
            params={"start_minute": start_minute, "end_minute": end_minute},
        )

        version = payload.get("day_version")
        if not isinstance(version, str) or not version.strip():
            raise InvalidSourceResponse("source day_version is missing or empty")
        if payload.get("day") != day.isoformat():
            raise InvalidSourceResponse("source returned a different day")

        notices_raw = payload.get("notices")
        if not isinstance(notices_raw, list):
            raise InvalidSourceResponse("source notices must be a list")
        if len(notices_raw) > 5:
            raise InvalidSourceResponse("source returned more than five notices")

        notices: list[Notice] = []
        seen: set[str] = set()
        for entry in notices_raw:
            if not isinstance(entry, dict):
                raise InvalidSourceResponse("source notice must be an object")
            announcement_no = entry.get("announcement_no")
            title = entry.get("title")
            published_at = entry.get("published_at")
            if not isinstance(announcement_no, str) or not announcement_no.strip():
                raise InvalidSourceResponse("source notice has no announcement_no")
            if not isinstance(title, str) or not title.strip():
                raise InvalidSourceResponse("source notice has no title")
            if announcement_no in seen:
                raise InvalidSourceResponse(
                    "source returned a duplicate announcement_no in one page"
                )
            seen.add(announcement_no)
            try:
                normalised, minute = parse_published_at(published_at or "", day)
            except ValueError as exc:
                raise InvalidSourceResponse(
                    f"source notice {announcement_no} has a bad published_at: {exc}"
                ) from exc
            if not start_minute <= minute <= end_minute:
                raise InvalidSourceResponse(
                    "source returned a notice outside the requested interval"
                )
            notices.append(
                Notice(
                    announcement_no=announcement_no.strip(),
                    title=title.strip(),
                    published_at=normalised,
                    published_minute=minute,
                )
            )

        return SourceResponse(
            day_version=version.strip(),
            notices=tuple(notices),
            start_minute=start_minute,
            end_minute=end_minute,
        )

    def _get(self, path: str, params: dict[str, object] | None = None) -> dict:
        try:
            response = self._client.get(path, params=params)
        except httpx.HTTPError as exc:
            raise SourceError(f"source request failed: {exc}") from exc
        if response.status_code != 200:
            raise SourceError(
                f"source returned HTTP {response.status_code} for {path}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise InvalidSourceResponse("source returned non-JSON body") from exc
        if not isinstance(payload, dict):
            raise InvalidSourceResponse("source returned a non-object body")
        return payload
