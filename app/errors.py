"""Error types that cross module boundaries."""

from __future__ import annotations


class SourceError(RuntimeError):
    """The disclosure source could not be reached or answered with an error."""


class InvalidSourceResponse(ValueError):
    """The disclosure source returned data that violates the agreed contract."""


class VersionConflict(RuntimeError):
    """A stale write tried to touch a day that already moved to another version."""


class SourceUnavailable(RuntimeError):
    """The source version check failed before a collection round could start."""
