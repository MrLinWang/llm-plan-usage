"""Kimi Code (Moonshot Coding Plan) usage provider.

Endpoint: ``GET {base_url}/usages``  (404 → fallback ``/usage``)
Auth:     ``Authorization: Bearer <sk-kimi-xxx>``

Response shape (verified against the live API, 2026-08):
  {
    "limits": [{
      "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
      "detail": {"limit": "100", "used": "68", "remaining": "32",
                 "resetTime": "<ISO or epoch-ms>"}
    }],
    "usage": {"limit": "100", "used": "27", "remaining": "73",
              "resetTime": "<ISO or epoch-ms>"}    # weekly
  }
Legacy flat ``limits[]`` items (``{"name", "limit", "remaining", ...}``)
are also accepted. Numbers may be JSON strings. ``used = limit - remaining``.
"""

from __future__ import annotations

from typing import Any

import httpx

from llm_usage.models import (
    PlatformResult,
    UsageEntry,
    compute_percent,
    compute_remaining,
)

DEFAULT_BASE_URL = "https://api.kimi.com/coding/v1"
TIMEOUT = 10.0


def _kimi_error_hint(status: int) -> str:
    """Human-readable Chinese hint for common Kimi API errors."""
    if status == 401:
        return (
            "认证失败(401)：请确认使用的是 Kimi Code 控制台的 sk-kimi-xxx 密钥，"
            "而非开放平台 key。"
        )
    if status == 404:
        return "端点未找到(404)：请检查 base_url 配置是否正确。"
    return f"请求失败(HTTP {status})"


def _parse_reset_time(value: Any) -> str | None:
    """Parse a resetTime that may be epoch-millis (int/float) or an ISO string."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        # epoch milliseconds → ISO 8601
        from datetime import datetime, timezone

        return datetime.fromtimestamp(value / 1000.0, tz=timezone.utc).isoformat()
    if isinstance(value, str):
        return value
    return None


def _window_label(window: Any) -> str | None:
    """Derive a Chinese label from a ``{"duration", "timeUnit"}`` window."""
    if not isinstance(window, dict):
        return None
    duration = window.get("duration")
    if not isinstance(duration, (int, float)):
        return None
    unit = window.get("timeUnit")
    if unit == "TIME_UNIT_MINUTE":
        minutes = duration
    elif unit == "TIME_UNIT_HOUR":
        minutes = duration * 60
    elif unit == "TIME_UNIT_DAY":
        minutes = duration * 1440
    else:
        return None
    if minutes % 1440 == 0:
        return f"{int(minutes // 1440)}天"
    if minutes % 60 == 0:
        return f"{int(minutes // 60)}小时"
    return f"{int(minutes)}分钟"


def _parse_usage_payload(
    payload: dict[str, Any], platform: str
) -> list[UsageEntry]:
    """Turn the Kimi JSON payload into UsageEntry list.

    - ``limits[]`` → per-window entries (typically the 5h window).
    - ``usage`` → the weekly window entry.
    """
    entries: list[UsageEntry] = []

    for item in payload.get("limits", []) or []:
        # Live API nests the numbers under "detail"; legacy shape was flat.
        detail = item.get("detail")
        data = detail if isinstance(detail, dict) else item
        limit = data.get("limit")
        remaining = data.get("remaining")
        if limit is None or remaining is None:
            # not enough data; skip
            continue
        limit_f = float(limit)
        remaining_f = float(remaining)
        used = limit_f - remaining_f
        label = str(
            item.get("name") or _window_label(item.get("window")) or "窗口"
        )
        entries.append(
            UsageEntry(
                platform=platform,
                label=label,
                used=used,
                limit=limit_f,
                remaining=compute_remaining(used, limit_f),
                percent=compute_percent(used, limit_f),
                reset_at=_parse_reset_time(data.get("resetTime")),
                unit="%",
            )
        )

    usage = payload.get("usage")
    if isinstance(usage, dict):
        limit = usage.get("limit")
        remaining = usage.get("remaining")
        if limit is not None and remaining is not None:
            limit_f = float(limit)
            remaining_f = float(remaining)
            used = limit_f - remaining_f
            entries.append(
                UsageEntry(
                    platform=platform,
                    label="每周",
                    used=used,
                    limit=limit_f,
                    remaining=compute_remaining(used, limit_f),
                    percent=compute_percent(used, limit_f),
                    reset_at=_parse_reset_time(usage.get("resetTime")),
                    unit="%",
                )
            )

    return entries


class KimiProvider:
    """Kimi Code live provider."""

    name = "kimi"
    display_name = "Kimi Code"
    is_manual = False

    def __init__(self, client: httpx.Client | None = None) -> None:
        # ``client`` lets tests inject an httpx.MockTransport.
        self._client = client

    def fetch(self, config: dict[str, Any]) -> PlatformResult:
        display_name = config.get("display_name") or self.display_name
        api_key = config.get("api_key")
        if not api_key:
            return PlatformResult(self.name, display_name, error="未配置")

        base_url = config.get("base_url") or DEFAULT_BASE_URL
        base_url = base_url.rstrip("/")
        headers = {"Authorization": f"Bearer {api_key}"}

        client = self._client or httpx.Client(timeout=TIMEOUT)
        own_client = self._client is None
        try:
            # Try /usages first; 404 → fallback to /usage
            resp = client.get(f"{base_url}/usages", headers=headers)
            if resp.status_code == 404:
                resp = client.get(f"{base_url}/usage", headers=headers)
            if resp.status_code != 200:
                return PlatformResult(
                    self.name, display_name, error=_kimi_error_hint(resp.status_code)
                )
            payload = resp.json()
            entries = _parse_usage_payload(payload, self.name)
            if not entries:
                return PlatformResult(
                    self.name, display_name, error="响应中未找到用量数据"
                )
            return PlatformResult(self.name, display_name, entries=entries)
        except httpx.HTTPError as exc:
            return PlatformResult(
                self.name, display_name, error=f"网络错误：{exc}"
            )
        finally:
            if own_client:
                client.close()