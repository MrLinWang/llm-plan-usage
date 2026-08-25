"""火山方舟 (Volcengine Ark) usage provider — covers Coding Plan and Agent Plan.

Uses the Volcengine OpenAPI with AK/SK + V4 request signing (not the Bearer
``/api/v3/endpoints/{id}/usage`` endpoint — that endpoint returns no usage data
for Coding/Agent Plans, verified against the live API).

  Coding Plan: ``GetCodingPlanUsage`` → ``QuotaUsage[]`` with ``Percent`` + ``ResetTimestamp``
               (backend returns percent only — no used/total; we derive used/limit
               from the tier table + percent)
  Agent Plan:  ``GetAFPUsage`` → periods with ``Used`` / ``Total`` + ``Percent`` + ``ResetTimestamp``

OpenAPI endpoint: ``GET https://open.cn-beijing.volcengineapi.com/?Action=<RPC>&Version=2024-01-01``
Auth: Volcengine AK/SK V4 HMAC-SHA256 signing (Service=ark, Region=cn-beijing).
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
from typing import Any
from urllib.parse import quote

import httpx

from llm_usage.models import (
    PlatformResult,
    UsageEntry,
    compute_percent,
    compute_remaining,
)

OPENAPI_HOST = "open.volcengineapi.com"
OPENAPI_URL = f"https://{OPENAPI_HOST}/"
OPENAPI_SERVICE = "ark"
OPENAPI_REGION = "cn-beijing"
OPENAPI_VERSION = "2024-01-01"
TIMEOUT = 10.0

# Coding Plan window labels (session / weekly / monthly)
CODING_WINDOW_LABELS = {"session": "5小时", "weekly": "每周", "monthly": "每月"}
# Agent Plan window labels (5h / weekly / monthly)
AGENT_WINDOW_LABELS = {"5h": "5小时", "weekly": "每周", "monthly": "每月"}

# RPC action per plan type
PLAN_ACTIONS = {
    "coding": "GetCodingPlanUsage",
    "agent": "GetAFPUsage",
}

# Embedded default limits per tier for deriving used/limit from percent-only
# responses (Coding Plan).  lite/pro verified; large/max unverified → None.
VOLCENGINE_LIMITS: dict[str, dict[str, float | None]] = {
    "lite": {"session": 1200, "weekly": 9000, "monthly": 18000},
    "pro": {"session": 6000, "weekly": 45000, "monthly": 90000},
}
# Agent plan tiers (AFP credits) — used as fallback when Total is absent
AGENT_LIMITS: dict[str, dict[str, float | None]] = {
    "small": {"5h": 1200, "weekly": 9000, "monthly": 18000},
    "medium": {"5h": 6000, "weekly": 45000, "monthly": 90000},
    "large": {"5h": None, "weekly": None, "monthly": None},
    "max": {"5h": None, "weekly": None, "monthly": None},
}


# ---------------------------------------------------------------------------
# Volcengine V4 signing (self-contained, no SDK dependency)
# ---------------------------------------------------------------------------

def _hmac_sha256(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _get_signing_key(sk: str, date: str, region: str, service: str) -> bytes:
    k_date = _hmac_sha256(sk.encode("utf-8"), date)
    k_region = _hmac_sha256(k_date, region)
    k_service = _hmac_sha256(k_region, service)
    return _hmac_sha256(k_service, "request")


def _canonical_query(query: dict[str, str]) -> str:
    res = []
    for key in query:
        value = str(query[key])
        res.append((quote(key, safe="-_.~"), quote(value, safe="-_.~")))
    return "&".join(f"{k}={v}" for k, v in sorted(res))


def _sign_v4(
    method: str,
    query: dict[str, str],
    ak: str,
    sk: str,
) -> dict[str, str]:
    """Build the signed headers for a Volcengine OpenAPI GET request.

    GET requests must NOT include Content-Type in the signed headers —
    the server ignores it for GET and its absence changes the canonical
    request, causing SignatureDoesNotMatch if it was signed.
    """
    headers: dict[str, str] = {"Host": OPENAPI_HOST}
    format_date = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    headers["X-Date"] = format_date
    body_hash = hashlib.sha256(b"").hexdigest()  # empty body for GET
    headers["X-Content-Sha256"] = body_hash

    path = "/"
    signed_headers: dict[str, str] = {}
    for key in headers:
        if key in ("Content-Type", "Content-Md5", "Host") or key.startswith("X-"):
            signed_headers[key.lower()] = headers[key]

    signed_str = "".join(f"{k}:{signed_headers[k]}\n" for k in sorted(signed_headers))
    signed_headers_string = ";".join(sorted(signed_headers.keys()))

    canonical_request = "\n".join([
        method, path, _canonical_query(query),
        signed_str, signed_headers_string, body_hash,
    ])
    credential_scope = f"{format_date[:8]}/{OPENAPI_REGION}/{OPENAPI_SERVICE}/request"
    signing_str = "\n".join([
        "HMAC-SHA256", format_date, credential_scope,
        hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
    ])
    signing_key = _get_signing_key(sk, format_date[:8], OPENAPI_REGION, OPENAPI_SERVICE)
    signature = hmac.new(signing_key, signing_str.encode("utf-8"), hashlib.sha256).hexdigest()

    headers["Authorization"] = (
        f"HMAC-SHA256 Credential={ak}/{credential_scope}, "
        f"SignedHeaders={signed_headers_string}, Signature={signature}"
    )
    return headers
# ---------------------------------------------------------------------------

def _parse_reset_timestamp(ts: Any) -> str | None:
    """Parse ResetTimestamp — epoch seconds (Coding Plan) or millis (Agent Plan)."""
    if ts is None or ts == 0:
        return None
    try:
        val = float(ts)
    except (TypeError, ValueError):
        return None
    # Agent Plan gives ms, Coding Plan gives seconds; detect by magnitude
    if val > 1e12:
        val /= 1000.0
    dt = datetime.datetime.fromtimestamp(val, tz=datetime.timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + str(int(dt.microsecond)).zfill(6) + "Z"


def _resolve_coding_limit(
    level: str, tier: str | None, override: dict[str, Any] | None
) -> float | None:
    """Derive the limit for a Coding Plan window from tier table or config override."""
    if override:
        return override.get(level)
    tier_table = VOLCENGINE_LIMITS.get(tier or "", {})
    return tier_table.get(level)


def _parse_coding_plan(
    data: dict[str, Any], platform: str, tier: str | None, override: dict[str, Any] | None
) -> list[UsageEntry]:
    """Parse GetCodingPlanUsage response → UsageEntry list.

    The backend returns only ``Percent`` — no ``used``/``total``.  We derive
    ``used`` and ``limit`` from the tier table + percent so the display shows
    absolute numbers alongside the percentage.
    """
    entries: list[UsageEntry] = []
    for quota in data.get("QuotaUsage", []) or []:
        level = quota.get("Level", "")
        label = CODING_WINDOW_LABELS.get(level, level)
        percent = quota.get("Percent")
        reset = _parse_reset_timestamp(quota.get("ResetTimestamp"))

        limit = _resolve_coding_limit(level, tier, override)
        percent_f = float(percent) if percent is not None else None
        used = None
        if limit is not None and percent_f is not None:
            used = round(percent_f / 100.0 * limit, 0)

        entries.append(
            UsageEntry(
                platform=platform,
                label=label,
                used=used if used is not None else 0.0,
                limit=limit,
                remaining=compute_remaining(used, limit) if used is not None else None,
                percent=round(percent_f, 1) if percent_f is not None else None,
                reset_at=reset,
                unit="次",
            )
        )
    return entries


def _parse_agent_plan(data: dict[str, Any], platform: str, tier: str | None) -> list[UsageEntry]:
    """Parse GetAFPUsage response → UsageEntry list.

    Response shape (verified live):
      ``Result.AFPFiveHour`` / ``AFPWeekly`` / ``AFPMonthly`` / ``AFPDaily``
      each with ``Quota`` (limit), ``Used``, ``ResetTime`` (epoch ms).
    We show the 5h / weekly / monthly windows (skip daily).
    """
    result = data.get("Result", data)
    if not isinstance(result, dict):
        return []

    # Map API field → (label, include?)
    windows = [
        ("AFPFiveHour", "5小时"),
        ("AFPWeekly", "每周"),
        ("AFPMonthly", "每月"),
    ]
    entries: list[UsageEntry] = []
    for field, label in windows:
        window = result.get(field)
        if not window or not isinstance(window, dict):
            continue
        quota = window.get("Quota")
        used = window.get("Used")
        reset = _parse_reset_timestamp(window.get("ResetTime"))

        used_f = float(used) if used is not None else 0.0
        limit_f = float(quota) if quota is not None else None

        entries.append(
            UsageEntry(
                platform=platform,
                label=label,
                used=round(used_f, 1),
                limit=limit_f,
                remaining=compute_remaining(used_f, limit_f),
                percent=compute_percent(used_f, limit_f),
                reset_at=reset,
                unit="AFP",
            )
        )
    return entries


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class VolcengineProvider:
    """Volcengine Ark provider (coding or agent, decided by config)."""

    name = "volcengine"
    display_name = "火山方舟"
    is_manual = False

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def fetch(self, config: dict[str, Any]) -> PlatformResult:
        platform_key = config.get("_platform_key", self.name)
        display_name = config.get("display_name") or self.display_name
        ak = config.get("access_key")
        sk = config.get("secret_key")
        if not ak or not sk:
            return PlatformResult(platform_key, display_name, error="未配置（需要 AK/SK）")

        plan_type = config.get("plan_type", "coding")  # coding | agent
        tier = config.get("tier")
        override_limits = config.get("limits")
        action = PLAN_ACTIONS.get(plan_type, "GetCodingPlanUsage")

        query: dict[str, str] = {"Action": action, "Version": OPENAPI_VERSION}
        headers = _sign_v4("GET", query, ak, sk)

        client = self._client or httpx.Client(timeout=TIMEOUT)
        own_client = self._client is None
        try:
            resp = client.get(OPENAPI_URL, params=query, headers=headers)
            if resp.status_code != 200:
                return PlatformResult(
                    platform_key, display_name,
                    error=f"HTTP {resp.status_code}",
                )
            data = resp.json()
            # Volcengine OpenAPI wraps results; error check
            resp_meta = data.get("ResponseMetadata", {})
            if "Error" in resp_meta:
                msg = resp_meta["Error"].get("Code", "未知错误")
                return PlatformResult(platform_key, display_name, error=msg)

            result = data.get("Result", data)
            if plan_type == "coding":
                entries = _parse_coding_plan(result, platform_key, tier, override_limits)
            else:
                entries = _parse_agent_plan(result, platform_key, tier)
            if not entries:
                return PlatformResult(
                    platform_key, display_name,
                    error="无订阅或未找到用量数据",
                )
            return PlatformResult(platform_key, display_name, entries=entries)
        except httpx.HTTPError:
            return PlatformResult(
                platform_key, display_name, error="网络错误"
            )
        finally:
            if own_client:
                client.close()