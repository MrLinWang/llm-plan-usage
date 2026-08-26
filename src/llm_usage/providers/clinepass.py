"""ClinePass usage provider.

Endpoint: ``GET {root}/api/v1/users/me/plan/usage-limits``
Auth:     ``Authorization: Bearer <api_key>``  (Cline API key from
          app.cline.bot → Settings → API Keys)

Response shape ({success, data, error} envelope; dashboard-derived endpoint,
verified against the live API by third-party integrations):
  {
    "success": true,
    "data": {
      "limits": [
        {"type": "five_hour", "percentUsed": 42.5, "resetsAt": "...Z"},
        {"type": "weekly",    "percentUsed": 12.0, "resetsAt": "..."},
        {"type": "monthly",   "percentUsed":  3.1, "resetsAt": "..."}
      ]
    }
  }

Percent-only: ClinePass is a flat monthly subscription and the backend returns
server-computed ``percentUsed`` per window with no absolute quota — entries
carry ``limit=None`` (same as Ollama Cloud).  Unknown window types are
skipped.  ``base_url`` accepts the bare host, ``.../api`` or ``.../api/v1``
forms; all normalize onto the same versioned root.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from llm_usage.models import (
    PlatformResult,
    UsageEntry,
)

DEFAULT_BASE_URL = "https://api.cline.bot/api"
TIMEOUT = 10.0

# Documented ClinePass windows:
# docs.cline.bot/getting-started/clinepass#usage
WINDOW_LABELS = {"five_hour": "5小时", "weekly": "每周", "monthly": "每月"}


def _api_root(base_url: str) -> str:
    """Normalize a base URL to the host-level root before ``/api/v1``."""
    root = base_url.strip().rstrip("/")
    if root.endswith("/api/v1"):
        root = root[: -len("/api/v1")]
    if root.endswith("/api"):
        root = root[: -len("/api")]
    return root


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


def _parse_percent(value: Any) -> float | None:
    """Coerce percentUsed (number or numeric string), clamped to 0–100."""
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(min(pct, 100.0), 0.0), 1)


def _parse_usage_payload(
    payload: dict[str, Any], platform: str
) -> list[UsageEntry]:
    """Turn the usage-limits payload into UsageEntry list.

    Windows are emitted in the fixed 5小时 → 每周 → 每月 order regardless of
    the API's item order; unknown window types are skipped.
    """
    data = payload.get("data")
    limits = data.get("limits") if isinstance(data, dict) else None
    by_type: dict[str, dict[str, Any]] = {}
    if isinstance(limits, list):
        for item in limits:
            if isinstance(item, dict) and isinstance(item.get("type"), str):
                by_type.setdefault(item["type"], item)

    entries: list[UsageEntry] = []
    for window_type, label in WINDOW_LABELS.items():
        item = by_type.get(window_type)
        if item is None:
            continue
        percent = _parse_percent(item.get("percentUsed"))
        if percent is None:
            continue
        entries.append(
            UsageEntry(
                platform=platform,
                label=label,
                used=0.0,
                limit=None,
                remaining=None,
                percent=percent,
                reset_at=_parse_reset_time(item.get("resetsAt")),
                unit="%",
            )
        )
    return entries


class ClinePassProvider:
    """ClinePass live provider."""

    name = "clinepass"
    display_name = "ClinePass"
    is_manual = False

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def fetch(self, config: dict[str, Any]) -> PlatformResult:
        display_name = config.get("display_name") or self.display_name
        platform_key = config.get("_platform_key", self.name)
        api_key = config.get("api_key")
        if not api_key:
            return PlatformResult(platform_key, display_name, error="未配置")

        root = _api_root(config.get("base_url") or DEFAULT_BASE_URL)
        url = f"{root}/api/v1/users/me/plan/usage-limits"
        headers = {"Authorization": f"Bearer {api_key}"}

        client = self._client or httpx.Client(timeout=TIMEOUT)
        own_client = self._client is None
        try:
            resp = client.get(url, headers=headers)
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
            entries = _parse_usage_payload(resp.json(), platform_key)
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
