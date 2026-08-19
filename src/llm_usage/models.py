"""Unified data models shared by all providers and display layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UsageEntry:
    """A single usage window/limit for one platform.

    Attributes:
        platform: provider key, e.g. ``"kimi"``, ``"volcengine-coding"``.
        label: window label shown to user, e.g. ``"5小时"``, ``"每周"``, ``"余额"``.
        used: amount consumed so far.
        limit: total allowed; ``None`` means unlimited / unknown.
        remaining: amount left; ``None`` if not computable.
        percent: ``used / limit * 100`` (0–100); ``None`` when limit is unknown.
        reset_at: when the window resets — ISO 8601 UTC (``Z`` or offset
            suffix; naive values are treated as UTC); ``None`` if N/A.  Only
            display converts it to local time; stored/JSON values stay UTC.
        unit: display unit: ``"%"``, ``"次"``, ``"$"``, ``"tokens"``.
        is_manual: True for manually-entered values (no live API fetch).
    """

    platform: str
    label: str
    used: float
    limit: float | None
    remaining: float | None
    percent: float | None
    reset_at: str | None
    unit: str
    is_manual: bool = False


@dataclass
class PlatformResult:
    """The aggregated result for one platform: either entries or an error."""

    platform: str
    display_name: str
    entries: list[UsageEntry] = field(default_factory=list)
    error: str | None = None

    @property
    def ok(self) -> bool:
        """True when this platform fetched successfully (no error)."""
        return self.error is None


def compute_remaining(used: float, limit: float | None) -> float | None:
    """Return ``limit - used`` or ``None`` when limit is unknown/unlimited."""
    if limit is None:
        return None
    return max(limit - used, 0.0)


def compute_percent(used: float, limit: float | None) -> float | None:
    """Return ``used / limit * 100`` clamped to 0–100, or ``None`` if no limit."""
    if limit is None or limit == 0:
        return None
    return round(max(min(used / limit * 100.0, 100.0), 0.0), 1)