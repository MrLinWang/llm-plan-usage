"""Unified data models shared by all providers and display layer."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class UsageEntry:
    """A single usage window/limit for one platform.

    Attributes:
        platform: provider key, e.g. ``"kimi"``, ``"volcengine-coding"``.
        label: window label shown to user, e.g. ``"5小时"``, ``"每周"``, ``"余额用量"``.
        used: amount consumed so far.
        limit: total allowed; ``None`` means unlimited / unknown. For a wallet
            balance row, this is the optional user-configured display maximum.
        remaining: amount left; ``None`` if not computable.
        percent: ``used / limit * 100`` (0–100); ``None`` when limit is unknown.
            Wallet balance rows use ``(limit - remaining) / limit * 100`` and
            keep ``used=0`` as a placeholder, displayed as unknown spend.
        reset_at: when the window resets — ISO 8601 UTC (``Z`` or offset
            suffix; naive values are treated as UTC); ``None`` if N/A.  Only
            display converts it to local time; stored/JSON values stay UTC.
        reset_kind: semantics of ``reset_at``: ``"expires"`` = a plan /
            subscription expiry (e.g. the gateway's 每月 row) rather than a
            quota refresh; ``None`` = window reset.  Display reads it to say
            「已到期」 instead of 「已重置」 once the timestamp has passed.
        unit: free-form display unit, e.g. ``"%"``, ``"$"``, ``"次"``,
            ``"tokens"``, ``"分钟"``, ``"credits"``, ``"AFP"``.
        is_manual: True for manually-entered values (no live API fetch).
        plan: billing-plan / group name used to partition the platform's rows
            (multi-credential platforms: each credential is its own plan; the
            Sub2API: each configured key group).  ``None`` = no
            partitioning (a plain single-source platform).
            ``fetch_all`` tags expanded credentials with the slot name and
            otherwise keeps a provider-set plan.
        key_breakdown: optional per-key usage detail for entries aggregated
            from multiple API keys (e.g. a gateway's shared-quota groups).
            Each item is a dict: ``{"number": int, "name": str | None,
            "used": float | None, "ok": bool, "error": str | None}`` where
            ``name`` is the configured per-key display name (``None`` = fall
            back to ``#N``).  ``None`` for entries that are not a multi-key
            aggregate.
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
    plan: str | None = None
    key_breakdown: list[dict] | None = None
    reset_kind: str | None = None


@dataclass
class PlatformResult:
    """The aggregated result for one platform: either entries or an error."""

    platform: str
    display_name: str
    entries: list[UsageEntry] = field(default_factory=list)
    error: str | None = None
    warning: str | None = None

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
