"""LLM Gateway usage provider, including shared daily limits across API-key groups.

Configuration lives entirely in ``config.toml``, same as every other
provider — no side files.  The simple single-key form::

    [platforms.llm-gateway]
    enabled = true
    base_url = "http://127.0.0.1:18080"
    api_key = "env:LLM_GATEWAY_API_KEY"

For multiple keys that share one daily quota, configure groups instead::

    [platforms.llm-gateway]
    enabled = true
    base_url = "http://127.0.0.1:18080"
    use_groups = true

    [[platforms.llm-gateway.groups]]
    name = "team-a"
    daily_limit = 100
    api_keys = ["env:LLM_GATEWAY_TEAM_A_1", "env:LLM_GATEWAY_TEAM_A_2"]

Each key may carry a display name — used in the per-key usage breakdown
(``UsageEntry.key_breakdown``) rendered by ``show --keys`` and the web
dashboard popover, and in per-key error messages::

    api_keys = [
        { name = "主 Key", value = "env:LLM_GATEWAY_TEAM_A_1" },
        { name = "备用", value = "env:LLM_GATEWAY_TEAM_A_2" },
    ]

The ``key_names`` / ``api_key_names`` list alias is also accepted (entry
``name`` wins when both are present); a plain string entry has no name and
falls back to ``key#N``.

``use_groups`` switches explicitly between the single-key and the groups
form (``true`` = groups only, ``false`` = single key only); when absent,
groups win if any are configured.  When ``use_groups`` is false, any
stored groups are ignored.

Each group is queried once per key.  Its ``used`` value is the sum of all
successful keys' ``usage.today.actual_cost`` values.  ``cost`` is only a
compatibility fallback for older gateways.  As with every other platform,
``env:VARNAME`` reads the secret from an environment variable instead of
storing it as plaintext in ``config.toml``.
"""

from __future__ import annotations

import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from typing import Any

import httpx

from llm_usage.models import (
    PlatformResult,
    UsageEntry,
    compute_percent,
    compute_remaining,
)

DEFAULT_USAGE_PATH = "/v1/usage"
TIMEOUT = 10.0
MAX_KEY_WORKERS = 8


class _GatewayConfigError(ValueError):
    """A user-facing gateway configuration error, not a response parsing error."""


@dataclass(frozen=True)
class _KeyUsage:
    """The successful daily cost returned by one API key."""

    actual_cost: float


def _as_float(value: Any) -> float | None:
    """Convert a JSON numeric value to float without raising."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _resolve_secret(value: Any) -> str | None:
    """Resolve an ``env:NAME`` reference without exposing its value."""
    if not isinstance(value, str) or not value:
        return None
    if value.startswith("env:"):
        return os.environ.get(value[4:])
    return value


def _daily_usage(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Return today's aggregate, with a defensive daily-list fallback."""
    usage = payload.get("usage")
    if isinstance(usage, dict):
        today = usage.get("today")
        if isinstance(today, dict):
            return today
        daily_usage = usage.get("daily_usage")
    else:
        daily_usage = None
    if not isinstance(daily_usage, list):
        daily_usage = payload.get("daily_usage")
    if isinstance(daily_usage, list):
        for item in reversed(daily_usage):
            if isinstance(item, dict):
                return item
    return None


def _daily_cost(payload: dict[str, Any]) -> float | None:
    """Extract actual daily cost, falling back to list cost for old payloads."""
    today = _daily_usage(payload)
    if today is None:
        return None
    actual_cost = _as_float(today.get("actual_cost"))
    if actual_cost is None:
        actual_cost = _as_float(today.get("cost"))
    return actual_cost


def _parse_daily_usage(
    payload: dict[str, Any],
    platform: str,
    *,
    label: str = "今日",
    daily_limit: float | None = None,
) -> list[UsageEntry]:
    """Map one payload to a USD entry, optionally applying a group limit."""
    actual_cost = _daily_cost(payload)
    if actual_cost is None:
        return []
    return [
        UsageEntry(
            platform=platform,
            label=label,
            used=round(actual_cost, 8),
            limit=daily_limit,
            remaining=compute_remaining(actual_cost, daily_limit),
            percent=compute_percent(actual_cost, daily_limit),
            reset_at=None,
            unit="$",
        )
    ]


def _error_from_response(response: httpx.Response) -> str:
    """Build a concise Chinese error from a gateway error response."""
    if response.status_code == 401:
        return "认证失败(401)：请检查 LLM_GATEWAY_API_KEY"
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict):
        message = body.get("message") or body.get("error") or body.get("code")
        if message:
            return f"请求失败(HTTP {response.status_code})：{message}"
    return f"请求失败(HTTP {response.status_code})"


def _group_name(group: dict[str, Any], index: int) -> str:
    value = group.get("name") or group.get("label")
    return str(value) if value else f"组{index + 1}"


def _group_limit(group: dict[str, Any], fallback: Any = None) -> float | None:
    value = group.get("daily_limit", fallback)
    if value is None:
        return None
    parsed = _as_float(value)
    if parsed is None or not math.isfinite(parsed) or parsed < 0:
        raise _GatewayConfigError("daily_limit 必须是非负数字或 null")
    return parsed


def _group_raw_key_entries(group: dict[str, Any]) -> list[Any]:
    """Raw key entries (dicts preserved): plain strings, ``env:`` refs, or ``{"name", "value"}``."""
    values = group.get("api_keys")
    if values is None:
        values = group.get("keys")
    if isinstance(values, str):
        return [values]
    if isinstance(values, list):
        return values
    envs = group.get("api_key_envs")
    if isinstance(envs, str):
        return [f"env:{envs}"]
    if isinstance(envs, list):
        return [f"env:{value}" for value in envs if isinstance(value, str)]
    return []


def _group_key_values(group: dict[str, Any]) -> list[Any]:
    """Resolved key strings: plain ``env:``/literal values, dicts unwrapped."""
    return [
        value.get("value") if isinstance(value, dict) else value
        for value in _group_raw_key_entries(group)
    ]


def _group_key_names(group: dict[str, Any], raw_keys: list[Any]) -> list[str | None]:
    """Per-key display names, aligned with ``raw_keys``.

    A ``{"name": ..., "value": ...}`` entry carries its own name; the
    ``key_names`` / ``api_key_names`` list is the legacy array alias used
    only for entries without one (entry name wins).  ``None`` = fall back
    to ``key#N``.
    """
    aliases = group.get("key_names")
    if aliases is None:
        aliases = group.get("api_key_names")
    if not isinstance(aliases, list):
        aliases = []
    names: list[str | None] = []
    for index, value in enumerate(raw_keys):
        name = None
        if isinstance(value, dict):
            raw_name = value.get("name")
            if isinstance(raw_name, str) and raw_name.strip():
                name = raw_name.strip()
        if name is None and index < len(aliases):
            alias = aliases[index]
            if isinstance(alias, str) and alias.strip():
                name = alias.strip()
        names.append(name)
    return names


def _configured_groups(config: dict[str, Any]) -> list[dict[str, Any]]:
    groups = config.get("groups")
    if groups is None:
        return []
    if not isinstance(groups, list):
        raise _GatewayConfigError("groups 必须是数组")
    if any(not isinstance(group, dict) for group in groups):
        raise _GatewayConfigError("groups 中每项必须是对象")
    return groups


def _resolved_group_keys(group: dict[str, Any]) -> list[str]:
    """Resolve and stably de-duplicate keys within one group."""
    keys: list[str] = []
    seen: set[str] = set()
    for value in _group_key_values(group):
        key = _resolve_secret(value)
        if key and key not in seen:
            seen.add(key)
            keys.append(key)
    return keys


def _validate_group_keys(groups: list[dict[str, Any]]) -> None:
    """Reject a key assigned to multiple groups to prevent double counting."""
    seen: set[str] = set()
    names: set[str] = set()
    for index, group in enumerate(groups):
        name = _group_name(group, index)
        if name in names:
            raise _GatewayConfigError(f"分组名称重复：{name}")
        names.add(name)
        for key in _resolved_group_keys(group):
            if key in seen:
                raise _GatewayConfigError("同一个 API key 不能属于多个分组")
            seen.add(key)


class LlmGatewayProvider:
    """LLM Gateway live provider, configured entirely via ``config.toml``."""

    name = "llm-gateway"
    display_name = "LLM Gateway"
    is_manual = False

    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client

    def _request_key(
        self,
        client: httpx.Client,
        api_key: str,
        base_url: str,
        usage_path: str,
    ) -> _KeyUsage:
        headers = {"Authorization": f"Bearer {api_key}"}
        response = client.get(
            f"{base_url.rstrip('/')}{usage_path}",
            headers=headers,
        )
        if response.status_code != 200:
            raise RuntimeError(_error_from_response(response))
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("响应格式无效")
        actual_cost = _daily_cost(payload)
        if actual_cost is None:
            raise RuntimeError("响应中未找到今日 USD 用量")
        return _KeyUsage(actual_cost=actual_cost)

    def _fetch_group(
        self,
        group: dict[str, Any],
        index: int,
        client: httpx.Client,
        base_url: str,
        usage_path: str,
        fallback_limit: Any = None,
    ) -> tuple[UsageEntry | None, list[str], list[dict[str, Any]]]:
        name = _group_name(group, index)
        daily_limit = _group_limit(group, fallback_limit)
        raw_entries = _group_raw_key_entries(group)
        raw_keys = _group_key_values(group)
        key_names = _group_key_names(group, raw_entries)
        keys: list[tuple[int, str]] = []
        failures_by_number: dict[int, str] = {}
        seen: set[str] = set()
        for number, (entry, raw_key) in enumerate(
            zip(raw_entries, raw_keys, strict=False), 1
        ):
            label = key_names[number - 1] or f"key#{number}"
            if not isinstance(raw_key, str) or not raw_key.strip():
                failures_by_number[number] = f"{name} {label}：API key 配置无效"
            elif not (resolved := _resolve_secret(raw_key)):
                failures_by_number[number] = f"{name} {label}：未配置 API key"
            elif resolved in seen:
                failures_by_number[number] = f"{name} {label}：重复 API key（已忽略）"
            else:
                seen.add(resolved)
                keys.append((number, resolved))
        if not keys:
            failures = [failures_by_number[number] for number in sorted(failures_by_number)]
            breakdown = [
                {
                    "number": number,
                    "name": key_names[number - 1],
                    "used": None,
                    "ok": False,
                    "error": failures_by_number[number],
                }
                for number in sorted(failures_by_number)
            ]
            return None, failures or [f"{name}：未配置 API key"], breakdown

        successes_by_number: dict[int, float] = {}
        workers = min(MAX_KEY_WORKERS, len(keys))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {
                pool.submit(
                    self._request_key,
                    client,
                    key,
                    base_url,
                    usage_path,
                ): number
                for number, key in keys
            }
            for future in as_completed(futures):
                number = futures[future]
                label = key_names[number - 1] or f"key#{number}"
                try:
                    successes_by_number[number] = future.result().actual_cost
                except Exception as exc:  # noqa: BLE001 - isolate one key
                    failures_by_number[number] = f"{name} {label}：{exc}"

        failures = [failures_by_number[number] for number in sorted(failures_by_number)]
        breakdown = [
            (
                {
                    "number": number,
                    "name": key_names[number - 1],
                    "used": successes_by_number[number],
                    "ok": True,
                    "error": None,
                }
                if number in successes_by_number
                else {
                    "number": number,
                    "name": key_names[number - 1],
                    "used": None,
                    "ok": False,
                    "error": failures_by_number[number],
                }
            )
            for number in range(1, len(raw_keys) + 1)
        ]

        if not successes_by_number:
            return None, failures or [f"{name}：没有成功的 key"], breakdown
        used = round(sum(successes_by_number.values()), 8)
        entry = UsageEntry(
            platform="llm-gateway",
            label=name,
            used=used,
            limit=daily_limit,
            remaining=compute_remaining(used, daily_limit),
            percent=compute_percent(used, daily_limit),
            reset_at=None,
            unit="$",
            key_breakdown=breakdown,
        )
        return entry, failures, breakdown

    def fetch(self, config: dict[str, Any]) -> PlatformResult:
        platform_key = config.get("_platform_key", self.name)
        display_name = config.get("display_name") or self.display_name

        use_groups = config.get("use_groups")
        groups: list[dict[str, Any]]
        api_key: Any = None
        try:
            groups = _configured_groups(config)
            if use_groups is True:
                if not groups:
                    return PlatformResult(
                        platform_key, display_name, error="已启用共享额度分组模式，但未配置任何分组"
                    )
                _validate_group_keys(groups)
            elif use_groups is False:
                groups = []
                api_key = config.get("api_key")
                if not _resolve_secret(api_key):
                    return PlatformResult(
                        platform_key, display_name, error="未配置 API key"
                    )
            elif groups:
                _validate_group_keys(groups)
            else:
                api_key = config.get("api_key")
                if not _resolve_secret(api_key):
                    return PlatformResult(
                        platform_key, display_name, error="未配置 API key"
                    )
        except _GatewayConfigError as exc:
            return PlatformResult(platform_key, display_name, error=f"配置错误：{exc}")

        base_url = config.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            return PlatformResult(
                platform_key, display_name, error="未配置 base_url"
            )
        usage_path = config.get("usage_path") or DEFAULT_USAGE_PATH
        if not isinstance(usage_path, str) or not usage_path.startswith("/"):
            return PlatformResult(
                platform_key, display_name, error="usage_path 必须以 / 开头"
            )

        client = self._client or httpx.Client(timeout=TIMEOUT)
        own_client = self._client is None
        try:
            if not groups:
                group = {
                    "name": "今日",
                    "api_keys": [api_key],
                }
                entry, failures, _breakdown = self._fetch_group(
                    group,
                    0,
                    client,
                    base_url,
                    usage_path,
                    config.get("daily_limit"),
                )
                if entry is None:
                    return PlatformResult(
                        platform_key,
                        display_name,
                        error=failures[0] if failures else "未配置 API key",
                    )
                return PlatformResult(
                    platform_key,
                    display_name,
                    entries=[entry],
                    warning="；".join(failures) if failures else None,
                )

            entries: list[UsageEntry] = []
            failures: list[str] = []
            for index, group in enumerate(groups):
                entry, group_failures, _breakdown = self._fetch_group(
                    group,
                    index,
                    client,
                    base_url,
                    usage_path,
                    config.get("daily_limit"),
                )
                if entry is not None:
                    entry.platform = platform_key
                    entries.append(entry)
                failures.extend(group_failures)
            if not entries:
                return PlatformResult(
                    platform_key,
                    display_name,
                    error="；".join(failures) if failures else "未配置 API key",
                )
            return PlatformResult(
                platform_key,
                display_name,
                entries=entries,
                warning="；".join(failures) if failures else None,
            )
        except _GatewayConfigError as exc:
            return PlatformResult(platform_key, display_name, error=f"配置错误：{exc}")
        except httpx.HTTPError as exc:
            return PlatformResult(platform_key, display_name, error=f"网络错误：{exc}")
        except ValueError as exc:
            return PlatformResult(
                platform_key, display_name, error=f"响应不是有效 JSON：{exc}"
            )
        finally:
            if own_client:
                client.close()
