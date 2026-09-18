"""Command Code (commandcode.ai) usage provider.

Endpoints (internal API, same routes the ``command-code`` CLI uses):
  ``GET {base}/alpha/billing/credits``        — window usage + credit balance
  ``GET {base}/alpha/billing/subscriptions``  — plan id / status / period end

Auth: ``Authorization: Bearer <api_key>`` (Command Code Studio → API keys).
Personal plans send no ``orgId`` query parameter.

Credits response shape (no envelope):
  {
    "credits": {"monthlyCredits": 69.95, ...},
    "windowLimits": {
      "fiveHour": {"used": 0.05, "cap": 14, "exceeded": false,
                   "resetAt": 1789716175624},
      "weekly":   {"used": 0.05, "cap": 35, "exceeded": false,
                   "resetAt": 1790302975624}
    }
  }

(Values above are illustrative; ``resetAt`` is epoch milliseconds.)

``credits.monthlyCredits`` is the *monthly remainder* in dollars (verified:
plan total − remaining == fiveHour.used).  The monthly row therefore needs the
plan's total, which comes from ``subscriptions``: ``planId`` is matched
longest-prefix against the CLI's built-in table (:data:`PLAN_MONTHLY_CREDITS`)
and only an ``active`` subscription yields the row — an unknown or inactive
plan is silently skipped rather than guessed.  A subscription lookup failure
degrades to the two window rows plus a ``warning``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from llm_usage.models import (
    PlatformResult,
    UsageEntry,
    compute_percent,
    compute_remaining,
)

DEFAULT_BASE_URL = "https://api.commandcode.ai"
TIMEOUT = 10.0

# Fixed window order: 5小时 → 每周 (payload keys → labels).
WINDOWS = (("fiveHour", "5小时"), ("weekly", "每周"))

# Monthly credit allowance per plan (same table the CLI ships); matched by
# longest ``planId`` prefix so ``individual-pro-v1`` wins over ``individual-pro``.
PLAN_MONTHLY_CREDITS: dict[str, float] = {
    "individual-go": 10.0,
    "individual-goat": 70.0,
    "individual-pro": 30.0,
    "individual-pro-v1": 80.0,
    "individual-provider": 15.0,
    "individual-max": 150.0,
    "individual-ultra": 300.0,
    "teams-pro": 40.0,
}
_MONTHLY_KEYS_BY_LENGTH = sorted(PLAN_MONTHLY_CREDITS, key=len, reverse=True)

SUBSCRIPTION_WARNING = "订阅信息获取失败，已跳过每月额度"


def _as_float(value: Any) -> float | None:
    """Coerce a JSON number to float; booleans and non-numbers → ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _parse_reset_time(value: Any) -> str | None:
    """Pass ISO strings through; convert epoch seconds/millis to UTC ISO."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        ts = float(value)
        if ts > 1e12:  # epoch millis
            ts /= 1000.0
        return (
            datetime.fromtimestamp(ts, tz=UTC)
            .isoformat()
            .replace("+00:00", "Z")
        )
    text = str(value).strip()
    return text or None


def _plan_monthly_credits(plan_id: Any) -> float | None:
    """Monthly credit allowance for a plan id (longest-prefix match)."""
    if not isinstance(plan_id, str):
        return None
    normalized = plan_id.strip().lower().replace("_", "-")
    for key in _MONTHLY_KEYS_BY_LENGTH:
        if normalized.startswith(key):
            return PLAN_MONTHLY_CREDITS[key]
    return None


def _parse_usage_payload(
    payload: dict[str, Any],
    subscription: dict[str, Any] | None,
    platform: str,
) -> list[UsageEntry]:
    """Turn the credits payload (+ subscription) into UsageEntry list.

    Rows come in the fixed 5小时 → 每周 → 每月 order.  A window is emitted only
    when both ``used`` and a positive ``cap`` are present — this covers
    ``limited: false`` and missing fields.  The monthly row additionally
    requires an ``active`` subscription whose ``planId`` is in the tier table.
    """
    entries: list[UsageEntry] = []

    window_limits = payload.get("windowLimits")
    if not isinstance(window_limits, dict):
        window_limits = {}
    for key, label in WINDOWS:
        window = window_limits.get(key)
        if not isinstance(window, dict):
            continue
        used = _as_float(window.get("used"))
        cap = _as_float(window.get("cap"))
        if used is None or cap is None or cap <= 0:
            continue
        entries.append(
            UsageEntry(
                platform=platform,
                label=label,
                used=used,
                limit=cap,
                remaining=compute_remaining(used, cap),
                percent=compute_percent(used, cap),
                reset_at=_parse_reset_time(window.get("resetAt")),
                unit="$",
            )
        )

    credits = payload.get("credits")
    monthly_remaining = (
        _as_float(credits.get("monthlyCredits"))
        if isinstance(credits, dict)
        else None
    )
    if monthly_remaining is not None and isinstance(subscription, dict):
        plan_total = (
            _plan_monthly_credits(subscription.get("planId"))
            if subscription.get("status") == "active"
            else None
        )
        if plan_total is not None and plan_total > 0:
            # Bonus/granted credits can push the remainder above the plan
            # total; clamp so the row never renders a negative "used".
            used = max(plan_total - monthly_remaining, 0.0)
            entries.append(
                UsageEntry(
                    platform=platform,
                    label="每月",
                    used=used,
                    limit=plan_total,
                    remaining=compute_remaining(used, plan_total),
                    percent=compute_percent(used, plan_total),
                    reset_at=_parse_reset_time(
                        subscription.get("currentPeriodEnd")
                    ),
                    unit="$",
                )
            )

    return entries


class CommandCodeProvider:
    """Command Code live provider."""

    name = "commandcode"
    display_name = "Command Code"
    is_manual = False

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def fetch(self, config: dict[str, Any]) -> PlatformResult:
        display_name = config.get("display_name") or self.display_name
        platform_key = config.get("_platform_key", self.name)
        api_key = config.get("api_key")
        if not api_key:
            return PlatformResult(platform_key, display_name, error="未配置")

        base = (config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"}

        client = self._client or httpx.Client(timeout=TIMEOUT)
        own_client = self._client is None
        try:
            resp = client.get(f"{base}/alpha/billing/credits", headers=headers)
            if resp.status_code == 401:
                return PlatformResult(
                    platform_key, display_name,
                    error="认证失败(401)",
                )
            if resp.status_code != 200:
                return PlatformResult(
                    platform_key, display_name,
                    error=f"请求失败(HTTP {resp.status_code})",
                )
            payload = resp.json()

            # Best-effort plan lookup: without it the window rows still render.
            subscription: dict[str, Any] | None = None
            try:
                sub_resp = client.get(
                    f"{base}/alpha/billing/subscriptions", headers=headers
                )
                if sub_resp.status_code == 200:
                    body = sub_resp.json()
                    data = body.get("data") if isinstance(body, dict) else None
                    if isinstance(data, dict):
                        subscription = data
            except (httpx.HTTPError, ValueError):
                subscription = None
            warning = None if subscription is not None else SUBSCRIPTION_WARNING

            entries = _parse_usage_payload(payload, subscription, platform_key)
            if not entries:
                return PlatformResult(
                    platform_key, display_name, error="响应中未找到用量数据"
                )
            return PlatformResult(
                platform_key, display_name, entries=entries, warning=warning
            )
        except httpx.HTTPError:
            return PlatformResult(
                platform_key, display_name, error="网络错误"
            )
        finally:
            if own_client:
                client.close()
