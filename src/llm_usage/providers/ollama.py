"""Ollama Cloud usage provider.

Endpoint: ``GET https://ollama.com/api/balance``
Auth:     ``Authorization: Bearer <api_key>``

The legacy ``/api/usage`` endpoint no longer carries limits (only request
counts, and cost/token counts for non-legacy plans), so the balance endpoint
is the source for quota data.  Its response has two shapes:

Legacy plans (session/weekly limits still apply)::

  {
    "included": {
      "session": {"remaining_percent": 75, "resets_at": "2026-10-01T07:00:00Z"},
      "weekly":  {"remaining_percent": 40, "resets_at": "2026-10-05T00:00:00Z"}
    },
    "purchased": {"balance_usd": 25}
  }

Credits-based plans (monthly pool)::

  {
    "included": {
      "balance_usd": 72.5,
      "allowance_usd": 100,
      "period": {"from": "2026-09-15T09:30:00Z", "until": "2026-10-15T09:30:00Z"}
    },
    "purchased": {"balance_usd": 25}
  }

``remaining_percent`` is a 0-100 "percent remaining", so the reported percent
is ``100 - remaining_percent``.  Neither shape returns absolute used/limit
for the windows — legacy rows are percent-only; credit rows derive used/limit
from the USD balances.
"""

from __future__ import annotations

from typing import Any

import httpx

from llm_usage.models import (
    PlatformResult,
    UsageEntry,
    compute_remaining,
)

DEFAULT_BASE_URL = "https://ollama.com/api"
TIMEOUT = 10.0


def _number(value: Any) -> float | None:
    """Coerce to float, tolerating JSON string numbers; None when invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _clamp_percent(value: float) -> float:
    """Round to one decimal, clamped to 0-100 (upstream anomalies stay in range)."""
    return round(max(min(value, 100.0), 0.0), 1)


def _parse_balance_payload(
    payload: dict[str, Any], platform: str
) -> list[UsageEntry]:
    """Turn an Ollama ``/api/balance`` payload into UsageEntry list.

    Dispatches on the ``included`` shape: ``session``/``weekly`` → legacy
    percent-only rows; ``balance_usd``/``allowance_usd`` → one 每月 row in
    USD derived from the remaining/allowance balance.
    """
    included = payload.get("included")
    if not isinstance(included, dict):
        return []
    entries: list[UsageEntry] = []

    for window_key, label in [("session", "5小时"), ("weekly", "每周")]:
        window = included.get(window_key)
        if not isinstance(window, dict):
            continue
        remaining_percent = _number(window.get("remaining_percent"))
        if remaining_percent is None:
            continue
        resets_at = window.get("resets_at")
        entries.append(
            UsageEntry(
                platform=platform,
                label=label,
                used=0.0,
                limit=None,
                remaining=None,
                percent=_clamp_percent(100.0 - remaining_percent),
                reset_at=resets_at if isinstance(resets_at, str) else None,
                unit="%",
            )
        )

    balance = _number(included.get("balance_usd"))
    allowance = _number(included.get("allowance_usd"))
    if balance is not None and allowance is not None:
        used = max(allowance - balance, 0.0)
        period = included.get("period")
        reset_at = None
        if isinstance(period, dict) and isinstance(period.get("until"), str):
            reset_at = period["until"]
        entries.append(
            UsageEntry(
                platform=platform,
                label="每月",
                used=round(used, 2),
                limit=allowance,
                remaining=compute_remaining(used, allowance),
                percent=(
                    _clamp_percent(used / allowance * 100.0) if allowance else None
                ),
                reset_at=reset_at,
                unit="$",
            )
        )

    return entries


class OllamaProvider:
    """Ollama Cloud live provider."""

    name = "ollama"
    display_name = "Ollama Cloud"
    is_manual = False

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def fetch(self, config: dict[str, Any]) -> PlatformResult:
        display_name = config.get("display_name") or self.display_name
        platform_key = config.get("_platform_key", self.name)
        api_key = config.get("api_key")
        if not api_key:
            return PlatformResult(platform_key, display_name, error="未配置")

        base_url = (config.get("base_url") or DEFAULT_BASE_URL).rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"}

        client = self._client or httpx.Client(timeout=TIMEOUT)
        own_client = self._client is None
        try:
            resp = client.get(f"{base_url}/balance", headers=headers)
            if resp.status_code == 401:
                return PlatformResult(
                    platform_key, display_name,
                    error="认证失败(401)：请检查 API key",
                )
            if resp.status_code != 200:
                return PlatformResult(
                    platform_key, display_name,
                    error=f"请求失败(HTTP {resp.status_code})",
                )
            data = resp.json()
            entries = _parse_balance_payload(data, platform_key)
            if not entries:
                return PlatformResult(
                    platform_key, display_name, error="响应中未找到用量数据"
                )
            return PlatformResult(platform_key, display_name, entries=entries)
        except httpx.HTTPError:
            return PlatformResult(
                platform_key, display_name, error="网络错误"
            )
        finally:
            if own_client:
                client.close()
