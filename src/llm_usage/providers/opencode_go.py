"""OpenCode Go usage provider.

Endpoint: ``GET https://opencode.ai/zen/go/v1/usage``
Auth:     ``Authorization: Bearer <api_key>``

Response:
  {
    "usage": {
      "rolling":  {"status": "ok", "percent": 3,  "resetsAt": "2026-08-19T04:23:00.867Z"},
      "weekly":   {"status": "ok", "percent": 25, "resetsAt": "2026-08-24T00:00:00.867Z"},
      "monthly":  {"status": "ok", "percent": 12, "resetsAt": "2026-09-17T17:37:53.867Z"}
    }
  }

``percent`` is a 0-100 integer.  No absolute used/limit returned — only
percentage and reset time.  Plan limits are $12 (5h) / $30 (weekly) /
$60 (monthly); we derive ``used`` from percent × limit for display.
"""

from __future__ import annotations

from typing import Any

import httpx

from llm_usage.models import (
    PlatformResult,
    UsageEntry,
    compute_remaining,
)

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
TIMEOUT = 10.0

# Plan limits in USD
PLAN_LIMITS = {"rolling": 12.0, "weekly": 30.0, "monthly": 60.0}
WINDOW_LABELS = {"rolling": "5小时", "weekly": "每周", "monthly": "每月"}


class OpenCodeGoProvider:
    """OpenCode Go live provider."""

    name = "opencode-go"
    display_name = "OpenCode Go"
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
            resp = client.get(f"{base_url}/usage", headers=headers)
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
            usage = data.get("usage", {})

            entries: list[UsageEntry] = []
            for window in ("rolling", "weekly", "monthly"):
                w = usage.get(window, {})
                percent = w.get("percent")
                reset_at = w.get("resetsAt")
                if percent is None:
                    continue
                percent_f = float(percent)
                limit = PLAN_LIMITS[window]
                used = round(percent_f / 100.0 * limit, 2)

                entries.append(
                    UsageEntry(
                        platform=platform_key,
                        label=WINDOW_LABELS[window],
                        used=used,
                        limit=limit,
                        remaining=compute_remaining(used, limit),
                        percent=percent_f,
                        reset_at=reset_at,
                        unit="$",
                    )
                )

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