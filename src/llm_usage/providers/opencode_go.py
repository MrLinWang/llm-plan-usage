"""OpenCode Go usage provider.

Endpoint: ``GET https://opencode.ai/zen/go/v1/usage``
Auth:     ``Authorization: Bearer <oc_sk_...>`` (OpenCode Console API key)

Response:
  {
    "usage": {
      "rolling":  {"status": "ok", "percent": 3,  "resetsAt": "2026-08-19T04:23:00.867Z"},
      "weekly":   {"status": "ok", "percent": 25, "resetsAt": "2026-08-24T00:00:00.867Z"},
      "monthly":  {"status": "ok", "percent": 12, "resetsAt": "2026-09-17T17:37:53.867Z"}
    }
  }

``percent`` is 0–100 and already relative to the member's plan allowance.
OpenCode Go's allowance is **model-dependent** (Go / Go Plus / Go Teams each
assign a different monthly dollar limit per model; the 5-hour window is 20% of
that model's monthly limit and weekly is 50%), so the response deliberately
carries no single absolute used/limit.  Rows are therefore percent-only, the
same shape as ClinePass/Ollama legacy windows — do not invent a dollar pool
from a plan table.

Console API keys can expire (Never / 1 day / 7 days / 30 days / custom); a 401
with ``error.code == "api_key_expired"`` is surfaced as a static regeneration
hint.  ``403`` means the workspace/member is not subscribed to Go.
"""

from __future__ import annotations

from typing import Any

import httpx

from llm_usage.models import (
    PlatformResult,
    UsageEntry,
)

DEFAULT_BASE_URL = "https://opencode.ai/zen/go/v1"
TIMEOUT = 10.0

WINDOW_LABELS = {"rolling": "5小时", "weekly": "每周", "monthly": "每月"}
EXPIRED_HINT = "API key 已过期，请在 OpenCode Console 重新生成"


def _percent(value: Any) -> float | None:
    """Coerce a 0–100 percent (number or numeric string), clamped."""
    if isinstance(value, bool):
        return None
    try:
        pct = float(value)
    except (TypeError, ValueError):
        return None
    return round(max(min(pct, 100.0), 0.0), 1)


def _unauthorized_message(resp: httpx.Response) -> str:
    """Static Chinese hint; recognize the API's api_key_expired code."""
    try:
        body = resp.json()
    except ValueError:
        body = None
    error = body.get("error") if isinstance(body, dict) else None
    code = error.get("code") if isinstance(error, dict) else None
    if code == "api_key_expired":
        return EXPIRED_HINT
    return "认证失败(401)：请检查 API key"


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
        workspace_id = config.get("workspace_id") or config.get("org_id")
        if workspace_id:
            headers["x-opencode-org-id"] = str(workspace_id)

        client = self._client or httpx.Client(timeout=TIMEOUT)
        own_client = self._client is None
        try:
            resp = client.get(f"{base_url}/usage", headers=headers)
            if resp.status_code == 401:
                return PlatformResult(
                    platform_key, display_name, error=_unauthorized_message(resp)
                )
            if resp.status_code == 403:
                return PlatformResult(
                    platform_key, display_name, error="未订阅 OpenCode Go"
                )
            if resp.status_code != 200:
                return PlatformResult(
                    platform_key, display_name,
                    error=f"请求失败(HTTP {resp.status_code})",
                )
            data = resp.json()
            usage = data.get("usage", {}) if isinstance(data, dict) else {}
            if not isinstance(usage, dict):
                usage = {}

            entries: list[UsageEntry] = []
            for window, label in WINDOW_LABELS.items():
                meter = usage.get(window)
                if not isinstance(meter, dict):
                    continue
                percent = _percent(meter.get("percent"))
                if percent is None:
                    continue
                entries.append(
                    UsageEntry(
                        platform=platform_key,
                        label=label,
                        used=0.0,
                        limit=None,
                        remaining=None,
                        percent=percent,
                        reset_at=meter.get("resetsAt"),
                        unit="%",
                        # status "rate-limited" still reports the same percent;
                        # the dashboard's 100% row is enough to signal it.
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
