"""DevPass usage provider for the LLM Gateway ``GET /v1/key`` endpoint.

Official API documentation: https://docs.llmgateway.io/developers/devpass-usage
The regular gateway API key is sent as a Bearer credential. DevPass USD values
are decimal strings for the monthly plan, rolling daily and premium-model
weekly allowances; inactive rolling windows have a null reset time.
"""

from __future__ import annotations

import math
from typing import Any

import httpx

from llm_usage.models import (
    PlatformResult,
    UsageEntry,
    compute_percent,
    compute_remaining,
)

DEFAULT_BASE_URL = "https://api.llmgateway.io"
TIMEOUT = 10.0


def _usage_url(base_url: str) -> str:
    root = base_url.strip().rstrip("/")
    if root.endswith("/v1/key"):
        return root
    if root.endswith("/v1"):
        return f"{root}/key"
    return f"{root}/v1/key"


def _decimal(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _reset_at(value: Any) -> str | None:
    return value if isinstance(value, str) and value.strip() else None


def _parse_usage_payload(payload: dict[str, Any], platform: str) -> list[UsageEntry]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    windows = (
        ("每日", "devPlanDailyCreditsUsed", "devPlanDailyLimit", "devPlanDayResetsAt"),
        (
            "每周（高级模型）",
            "devPlanPremiumCreditsUsed",
            "devPlanPremiumWeeklyLimit",
            "devPlanPremiumWeekResetsAt",
        ),
        ("每月", "devPlanCreditsUsed", "devPlanCreditsLimit", None),
    )
    entries: list[UsageEntry] = []
    for label, used_field, limit_field, reset_field in windows:
        used = _decimal(data.get(used_field))
        limit = _decimal(data.get(limit_field))
        if used is None or limit is None:
            continue
        entries.append(
            UsageEntry(
                platform=platform,
                label=label,
                used=used,
                limit=limit,
                remaining=compute_remaining(used, limit),
                percent=compute_percent(used, limit),
                reset_at=_reset_at(data.get(reset_field)) if reset_field else None,
                unit="$",
            )
        )
    return entries


class DevPassProvider:
    name = "devpass"
    display_name = "DevPass"
    is_manual = False

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def fetch(self, config: dict[str, Any]) -> PlatformResult:
        display_name = config.get("display_name") or self.display_name
        platform_key = config.get("_platform_key", self.name)
        api_key = config.get("api_key")
        if not api_key:
            return PlatformResult(platform_key, display_name, error="未配置")
        base_url = config.get("base_url") or DEFAULT_BASE_URL
        if not isinstance(base_url, str) or not base_url.strip():
            return PlatformResult(platform_key, display_name, error="配置错误：base_url 无效")

        client = self._client or httpx.Client(timeout=TIMEOUT)
        own_client = self._client is None
        try:
            response = client.get(
                _usage_url(base_url),
                headers={"Authorization": f"Bearer {api_key}"},
            )
            if response.status_code in (401, 403):
                return PlatformResult(
                    platform_key,
                    display_name,
                    error=f"认证失败({response.status_code})",
                )
            if response.status_code != 200:
                return PlatformResult(
                    platform_key,
                    display_name,
                    error=f"请求失败(HTTP {response.status_code})",
                )
            payload = response.json()
            data = payload.get("data") if isinstance(payload, dict) else None
            if isinstance(data, dict) and data.get("devPlan") == "none":
                return PlatformResult(platform_key, display_name, error="无 DevPass 订阅")
            entries = _parse_usage_payload(payload, platform_key)
            if not entries:
                return PlatformResult(
                    platform_key, display_name, error="响应中未找到用量数据"
                )
            return PlatformResult(platform_key, display_name, entries=entries)
        except httpx.HTTPError:
            return PlatformResult(platform_key, display_name, error="网络错误")
        except ValueError:
            return PlatformResult(platform_key, display_name, error="响应不是有效 JSON")
        finally:
            if own_client:
                client.close()
