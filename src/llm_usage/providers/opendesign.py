"""OpenDesign AMR account usage (control-plane API, not model inference).

Read the active workspace with ``GET /api/v1/workspaces/current``, then its
``GET /api/v1/workspaces/{id}/billing/preflight``.  The latter contains
``codingPlan.windows`` (integer-string credit amounts) and ``balanceUsd``.
The official response contract is implemented in OpenDesign's
``apps/daemon/src/integrations/vela-billing.ts::parseBillingPreflight``.

Billing requires a ``vela_ctrl_`` control key from ``vela login``; an
``sk-amr-`` runtime key only authorizes model execution.  Never send a known
runtime key to billing or invent usage/limits from a plan's advertised price.
"""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from llm_usage.models import PlatformResult, UsageEntry, compute_percent

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://amr-api.open-design.ai"
TIMEOUT = 10.0
CONTROL_KEY_REQUIRED = "配置错误：用量查询需要 control_key（vela_ctrl_），模型 API key 无法查询用量"
PARTIAL_WARNING = "部分用量数据缺失或无效"
_WINDOW_LABELS = {18000: "5小时", 86400: "每日", 604800: "每周", 2592000: "每月"}


def _api_root(base_url: str) -> str:
    """Accept either the public model URL or a control-plane API root."""
    parsed = urlsplit(base_url.strip().rstrip("/"))
    if parsed.hostname == "amr-link.open-design.ai":
        return f"{DEFAULT_BASE_URL}/api/v1"
    path = parsed.path.rstrip("/")
    for suffix in ("/api/v1", "/v1", "/api"):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    return urlunsplit((parsed.scheme, parsed.netloc, path + "/api/v1", "", ""))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = float(value)
    except (ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def _reset_time(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        timestamp = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _window_label(duration: int) -> str:
    if duration in _WINDOW_LABELS:
        return _WINDOW_LABELS[duration]
    for size, unit in ((86400, "天"), (3600, "小时"), (60, "分钟")):
        if duration % size == 0:
            return f"{duration // size}{unit}"
    return f"{duration}秒"


def _parse_usage_payload(
    payload: dict[str, Any], platform: str, workspace_id: str,
) -> tuple[list[UsageEntry], str | None]:
    """Keep workspace credit windows and its wallet in their native units."""
    if payload.get("workspaceId") != workspace_id:
        return [], None
    entries: list[UsageEntry] = []
    partial = False
    plan = payload.get("codingPlan")
    if isinstance(plan, dict) and plan.get("eligible") is True:
        windows = plan.get("windows")
        if plan.get("workspaceId") != workspace_id or not isinstance(windows, list):
            partial = True
        else:
            parsed: list[tuple[int, UsageEntry]] = []
            for window in windows:
                if not isinstance(window, dict):
                    partial = True
                    continue
                duration = window.get("durationSeconds")
                used = _number(window.get("usedCredits"))
                limit = _number(window.get("limitCredits"))
                remaining = _number(window.get("remainingCredits"))
                if (
                    isinstance(duration, bool) or not isinstance(duration, int)
                    or duration <= 0 or used is None or used < 0
                    or limit is None or limit <= 0 or remaining is None
                    or not 0 <= remaining <= limit
                ):
                    partial = True
                    continue
                parsed.append((duration, UsageEntry(
                    platform=platform,
                    label=_window_label(duration),
                    used=used,
                    limit=limit,
                    remaining=remaining,
                    percent=compute_percent(used, limit),
                    reset_at=_reset_time(window.get("resetsAt")),
                    unit="credits",
                )))
            entries.extend(entry for _, entry in sorted(parsed, key=lambda pair: pair[0]))
            if not parsed:
                partial = True
    elif not isinstance(plan, dict) or plan.get("eligible") is not False:
        partial = True

    balance = _number(payload.get("balanceUsd"))
    if balance is not None:
        # A balance proves neither spend nor a total allowance.  Show it only
        # in "remaining", leaving the limit and percentage unknown.
        entries.append(UsageEntry(
            platform=platform, label="余额", used=0.0, limit=None,
            remaining=balance, percent=None, reset_at=None, unit="$",
        ))
    else:
        partial = True
    return entries, PARTIAL_WARNING if partial and entries else None


class OpenDesignProvider:
    """Read OpenDesign workspace quotas using an explicitly configured key."""

    name = "opendesign"
    display_name = "OpenDesign"
    is_manual = False

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def fetch(self, config: dict[str, Any]) -> PlatformResult:
        display_name = config.get("display_name") or self.display_name
        platform = config.get("_platform_key", self.name)
        key = config.get("control_key")
        legacy_key = config.get("api_key")
        if not key and isinstance(legacy_key, str) and legacy_key.startswith("vela_ctrl_"):
            key = legacy_key
        if not key:
            error = CONTROL_KEY_REQUIRED if legacy_key else "未配置 control_key"
            return PlatformResult(platform, display_name, error=error)
        if not isinstance(key, str) or not key.startswith("vela_ctrl_"):
            return PlatformResult(platform, display_name, error=CONTROL_KEY_REQUIRED)

        root = _api_root(config.get("base_url") or DEFAULT_BASE_URL)
        client = self._client or httpx.Client(timeout=TIMEOUT)
        try:
            headers = {"Authorization": f"Bearer {key}"}
            workspace_id = config.get("workspace_id")
            if not isinstance(workspace_id, str) or not workspace_id.strip():
                response = client.get(f"{root}/workspaces/current", headers=headers)
                response.raise_for_status()
                current = response.json()
                workspace_id = (
                    current.get("activeWorkspaceId") or current.get("workspaceId")
                    if isinstance(current, dict) else None
                )
            if not isinstance(workspace_id, str) or not workspace_id.strip():
                return PlatformResult(platform, display_name, error="响应中未找到用量数据")
            workspace_id = workspace_id.strip()
            response = client.get(
                f"{root}/workspaces/{quote(workspace_id, safe='')}/billing/preflight",
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
            entries, warning = (
                _parse_usage_payload(payload, platform, workspace_id)
                if isinstance(payload, dict) else ([], None)
            )
            if not entries:
                return PlatformResult(platform, display_name, error="响应中未找到用量数据")
            return PlatformResult(platform, display_name, entries=entries, warning=warning)
        except httpx.HTTPStatusError as exc:
            status = exc.response.status_code
            error = f"认证失败({status})" if status in (401, 403) else f"请求失败(HTTP {status})"
            return PlatformResult(platform, display_name, error=error)
        except httpx.HTTPError:
            # Exception URLs/headers may contain credentials; never echo them.
            logger.warning("OpenDesign usage request failed")
            return PlatformResult(platform, display_name, error="网络错误")
        except ValueError:
            return PlatformResult(platform, display_name, error="响应不是有效 JSON")
        finally:
            if self._client is None:
                client.close()
