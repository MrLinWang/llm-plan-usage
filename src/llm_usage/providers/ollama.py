"""Ollama Cloud usage provider.

Endpoint: ``GET https://ollama.com/api/usage``
Auth:     ``Authorization: Bearer <api_key>``

Response:
  {
    "activity": {"cost": "0.00000", "period": {...}, "models": []},
    "limits": {
      "session": {"usage": 0.333, "models": [{...}]},
      "weekly":  {"usage": 0.136, "models": [{...}]}
    }
  }

``usage`` is a 0-1 float (0.333 = 33.3%).  No reset time or absolute limit
returned — only percentage.  We map session → "5小时" and weekly → "每周".
"""

from __future__ import annotations

from typing import Any

import httpx

from llm_usage.models import (
    PlatformResult,
    UsageEntry,
)

DEFAULT_BASE_URL = "https://ollama.com/api"
TIMEOUT = 10.0


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
            limits = data.get("limits", {})
            entries: list[UsageEntry] = []

            for window_key, label in [("session", "5小时"), ("weekly", "每周")]:
                window = limits.get(window_key, {})
                usage = window.get("usage")
                if usage is None:
                    continue
                # usage is 0-1; convert to 0-100 percent
                percent = round(float(usage) * 100, 1)
                entries.append(
                    UsageEntry(
                        platform=platform_key,
                        label=label,
                        used=0.0,
                        limit=None,
                        remaining=None,
                        percent=percent,
                        reset_at=None,
                        unit="%",
                    )
                )

            if not entries:
                return PlatformResult(
                    platform_key, display_name, error="响应中未找到用量数据"
                )
            return PlatformResult(platform_key, display_name, entries=entries)
        except httpx.HTTPError as exc:
            return PlatformResult(
                platform_key, display_name, error=f"网络错误：{exc}"
            )
        finally:
            if own_client:
                client.close()