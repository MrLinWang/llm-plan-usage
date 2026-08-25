"""Web dashboard: FastAPI app serving the usage overview with a TTL cache.

会话认证:未登录访问页面重定向到 /login,API 返回 401。首次启动(无用户)
通过 /api/auth/setup 创建首个管理员。用户/会话存储在 history.db(store.py)。
开放注册:管理员经 PUT /api/settings 打开开关后,访客可经 POST
/api/auth/register 自助注册(永远是普通用户,注册即登录);开关默认关闭,
存 history.db settings 表。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
from collections import OrderedDict
from dataclasses import replace
from datetime import datetime, timezone
from importlib.resources import files
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from llm_usage import store
from llm_usage.config import (
    get_platform_config,
    get_platform_order,
    load_config,
    set_platform_order,
    update_platform_config,
)
from llm_usage.display import results_to_dict
from llm_usage.models import PlatformResult
from llm_usage.providers import DISPLAY_NAMES, PROVIDERS, fetch_all, registry_index

logger = logging.getLogger(__name__)

MIN_INTERVAL = 5.0    # 与 tui.py 一致
MAX_INTERVAL = 3600.0

SESSION_COOKIE = "llm_usage_session"

# 各平台可在 Web 端编辑的凭证字段(与 providers._prepare_config 的字段一致)
CREDENTIAL_FIELDS: dict[str, list[str]] = {
    "kimi": ["api_key"],
    "volcengine-coding": ["access_key", "secret_key"],
    "volcengine-agent": ["access_key", "secret_key"],
    "ollama": ["api_key"],
    "opencode-go": ["api_key"],
    "llm-gateway": [],  # 纯组式:凭证以 groups[].api_keys 配置,无顶层凭证字段
}

_CONFIG_FIELDS = {
    "enabled", "display_name", "base_url", "api_key", "access_key", "secret_key",
    "groups",
}
_CREDENTIAL_NAMES = {"api_key", "access_key", "secret_key"}
_GATEWAY_GROUP_FIELDS = {"index", "name", "daily_limit", "api_keys"}
_GATEWAY_KEY_FIELDS = {"index", "value", "name"}

# PUT /api/settings 允许的字段(站点策略,存 history.db settings 表)
_SETTINGS_KEYS = {"registration_enabled"}

class _LoginLimiter:
    """Per-client sliding-window login throttle.

    同 IP 在 _window 秒内失败满 _max 次 → 锁 _lockout 秒（429）。
    进程内存态：单 worker 部署下有效，重启清零。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._fails: OrderedDict[str, list[float]] = OrderedDict()
        self._blocked: dict[str, float] = {}
        self._max = 5
        self._window = 60.0
        self._lockout = 300.0

    def check(self, key: str) -> float | None:
        """None = 放行；否则返回还需等待的秒数（>0）。"""
        now = time.monotonic()
        with self._lock:
            until = self._blocked.get(key)
            if until is not None:
                if now < until:
                    return until - now
                del self._blocked[key]
            stamps = self._fails.setdefault(key, [])
            stamps[:] = [t for t in stamps if now - t < self._window]
            if len(stamps) >= self._max:
                self._blocked[key] = now + self._lockout
                self._fails.pop(key, None)
                logger.warning("rate limit engaged for %s", key)
                return self._lockout
            stamps.append(now)
            return None

    def clear(self, key: str) -> None:
        with self._lock:
            self._fails.pop(key, None)
            self._blocked.pop(key, None)

def _client_key(request: Request) -> str:
    return request.client.host if request.client else "unknown"

class UsageCache:
    """TTL cache around fetch_all; refreshes at most once per interval."""

    def __init__(self, config: dict[str, Any], interval: float) -> None:
        self._config = config
        self._interval = max(min(interval, MAX_INTERVAL), MIN_INTERVAL)
        self._lock = threading.Lock()
        self._results: list[PlatformResult] | None = None
        self._fetched_at: float = 0.0  # time.monotonic()
        self._fetched_at_iso: str = ""

    @property
    def interval(self) -> float:
        return self._interval

    def set_config(self, config: dict[str, Any]) -> None:
        """Replace config and force the next get() to refetch."""
        with self._lock:
            self._config = config
            self._fetched_at = 0.0

    def invalidate(self) -> None:
        """Force the next get() to refetch (used by POST /api/refresh)."""
        with self._lock:
            self._fetched_at = 0.0

    def set_interval(self, interval: float) -> float:
        """Update the TTL interval (clamped to [MIN, MAX]); return the clamped value."""
        with self._lock:
            self._interval = max(min(interval, MAX_INTERVAL), MIN_INTERVAL)
            return self._interval

    def get(self) -> tuple[list[PlatformResult], str]:
        """Return (results, fetched_at_utc_iso), refreshing when stale.

        Holds the lock across the fetch so concurrent requests wait for the
        same fresh result instead of thundering the provider APIs.
        Double-check staleness after acquiring so only one thread fetches.
        """
        with self._lock:
            stale = (
                self._results is None
                or time.monotonic() - self._fetched_at >= self._interval
            )
            if stale:
                results = fetch_all(self._config)
                self._results = results
                self._fetched_at = time.monotonic()
                self._fetched_at_iso = datetime.now(timezone.utc).isoformat()
            return self._results, self._fetched_at_iso


class UserCaches:
    """Per-config TTL cache pool: one UsageCache per distinct config hash.

    Each user's own config (and every distinct shared config) gets its own
    cache entry, keyed by the config's content hash so edits invalidate by
    hash mismatch on the next get() — no explicit invalidation needed.
    """

    MAX_CACHES = 32

    def __init__(self, interval: float) -> None:
        self._lock = threading.Lock()
        self._caches: OrderedDict[str, UsageCache] = OrderedDict()
        self._interval = max(min(interval, MAX_INTERVAL), MIN_INTERVAL)

    @property
    def interval(self) -> float:
        return self._interval

    def _key(self, config: dict[str, Any]) -> str:
        return hashlib.sha1(
            json.dumps(config, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()

    def get(self, config: dict[str, Any]) -> tuple[list[PlatformResult], str]:
        """Return (results, fetched_at) for the given config's cache entry."""
        key = self._key(config)
        with self._lock:
            cache = self._caches.get(key)
            if cache is None:
                cache = UsageCache(config, self._interval)
                self._caches[key] = cache
                if len(self._caches) > self.MAX_CACHES:
                    self._caches.popitem(last=False)  # 淘汰最旧条目
        return cache.get()

    def invalidate_all(self) -> None:
        """Force every cached config to refetch on its next get()."""
        with self._lock:
            for cache in self._caches.values():
                cache.invalidate()

    def invalidate_for(self, config: dict[str, Any]) -> None:
        """仅失效指定配置的缓存条目(下一次 get() 重新拉取)。"""
        key = self._key(config)
        with self._lock:
            cache = self._caches.get(key)
            if cache is not None:
                cache.invalidate()

    def set_interval(self, interval: float) -> float:
        """Update the TTL for the pool (clamped to [MIN, MAX]); return it."""
        with self._lock:
            self._interval = max(min(interval, MAX_INTERVAL), MIN_INTERVAL)
            for cache in self._caches.values():
                cache.set_interval(self._interval)
            return self._interval


# ---- 认证辅助 ----

def _current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return store.get_session_user(token)


def _secure_cookie_enabled() -> bool:
    """HTTPS 反代场景设 LLM_USAGE_SECURE_COOKIE=1 给会话 Cookie 加 Secure。"""
    return os.environ.get("LLM_USAGE_SECURE_COOKIE", "").lower() in {"1", "true", "yes"}

def _set_session_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=store.SESSION_TTL_DAYS * 86400,
        httponly=True, samesite="lax", path="/",
        secure=_secure_cookie_enabled(),
    )


_USERNAME_RE = re.compile(r"[A-Za-z0-9_.-]{1,64}")

def _valid_username(value: Any) -> bool:
    return isinstance(value, str) and bool(_USERNAME_RE.fullmatch(value))


def _valid_password(value: Any) -> bool:
    return isinstance(value, str) and len(value) >= 6


def api_user(request: Request) -> dict[str, Any]:
    """FastAPI 依赖:要求已登录用户,否则 401。"""
    user = _current_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="未登录")
    return user


def api_admin(user: dict[str, Any] = Depends(api_user)) -> dict[str, Any]:
    """FastAPI 依赖:要求管理员,否则 403(未登录由 api_user 抛 401)。"""
    if not user["is_admin"]:
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return user


# ---- 供应商配置视图 ----

def _credential_view(raw: Any) -> dict[str, Any]:
    """脱敏凭证视图:不返回明文,仅标记是否设置/env 引用/前缀提示。"""
    if not raw or not isinstance(raw, str):
        return {"set": False, "env": None, "hint": None}
    if raw.startswith("env:"):
        return {"set": True, "env": raw, "hint": None}
    hint = raw[:4] + "…" + raw[-2:] if len(raw) >= 8 else "••••"
    return {"set": True, "env": None, "hint": hint}


def _gateway_group_raw_entries(group: dict[str, Any]) -> list[Any]:
    """Raw group key entries with dicts preserved (``{"name", "value"}``)."""
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


def _gateway_key_name(value: Any, index: int, group: dict[str, Any]) -> str | None:
    """Per-key display name: ``{"name": …}`` entry wins, then the legacy name array."""
    if isinstance(value, dict):
        raw_name = value.get("name")
        if isinstance(raw_name, str) and raw_name.strip():
            return raw_name.strip()
    aliases = group.get("key_names")
    if aliases is None:
        aliases = group.get("api_key_names")
    if isinstance(aliases, list) and index < len(aliases):
        alias = aliases[index]
        if isinstance(alias, str) and alias.strip():
            return alias.strip()
    return None


def _gateway_groups_view(section: dict[str, Any]) -> list[dict[str, Any]]:
    groups = section.get("groups")
    if not isinstance(groups, list):
        groups = []
    if not groups and isinstance(section.get("api_key"), str) and section["api_key"].strip():
        # 兼容旧单 Key 配置:合成一组,Web 保存时即完成迁移
        return [{
            "index": 0,
            "name": "组1",
            "daily_limit": section.get("daily_limit"),
            "api_keys": [{"name": None, **_credential_view(section["api_key"])}],
        }]
    views: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        if not isinstance(group, dict):
            continue
        name = group.get("name") or group.get("label") or f"组{index + 1}"
        raw_entries = _gateway_group_raw_entries(group)
        views.append({
            "index": index,
            "name": str(name),
            "daily_limit": group.get("daily_limit"),
            "api_keys": [
                {
                    "name": _gateway_key_name(value, key_index, group),
                    **_credential_view(
                        value.get("value") if isinstance(value, dict) else value
                    ),
                }
                for key_index, value in enumerate(raw_entries)
            ],
        })
    return views


def _gateway_existing_group(
    existing_groups: list[Any], index: Any, position: int,
) -> dict[str, Any]:
    source_index = index if isinstance(index, int) and not isinstance(index, bool) else position
    if source_index < 0 or source_index >= len(existing_groups):
        return {}
    group = existing_groups[source_index]
    return group if isinstance(group, dict) else {}


def _gateway_preserved_entry(old_keys: list[Any], index: Any) -> Any:
    """The existing raw entry (string, env ref, or named dict) to keep."""
    if not isinstance(index, int) or isinstance(index, bool):
        raise ValueError("api_keys 的 index 必须是非负整数")
    if index < 0 or index >= len(old_keys):
        raise ValueError("要保留的 API key 不存在")
    entry = old_keys[index]
    value = entry.get("value") if isinstance(entry, dict) else entry
    if not isinstance(value, str) or not value.strip():
        raise ValueError("要保留的 API key 不存在")
    return entry


def _gateway_key_value(value: Any, old_keys: list[Any], position: int) -> Any:
    """Canonicalize one key payload entry to a plain string or a named dict.

    ``None`` / empty string / ``{"index": n, "value": null}`` mean "keep the
    saved key" — resolved through ``old_keys`` (string or dict, name intact).
    """
    if value is None:
        return _gateway_preserved_entry(old_keys, position)
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return _gateway_preserved_entry(old_keys, position)
        if value == "env:" or value.startswith("env:") and not value[4:].strip():
            raise ValueError("API key 的 env 引用不能为空")
        return value
    if not isinstance(value, dict):
        raise ValueError("api_keys 中每项必须是字符串或对象")
    extra = sorted(set(value) - _GATEWAY_KEY_FIELDS)
    if extra:
        raise ValueError("api_keys 未知字段: " + ", ".join(extra))
    key_index = value.get("index", position)
    raw_value = value.get("value")
    if raw_value is None:
        key = _gateway_preserved_entry(old_keys, key_index)
    else:
        key = _gateway_key_value(raw_value, old_keys, position)
    raw_name = value.get("name")
    if raw_name is None:
        return key
    if not isinstance(raw_name, str) or not raw_name.strip():
        # 名称留空 = 清除名称(否则 UI 无法移除名称);保留的 key 本身不变
        if isinstance(key, dict):
            key = dict(key)
            key.pop("name", None)
        return key
    if len(raw_name.strip()) > 100:
        raise ValueError("API key 名称需为 1-100 个字符")
    name = raw_name.strip()
    if isinstance(key, dict):
        return {**key, "name": name}
    return {"name": name, "value": key}


def _validate_gateway_groups(
    raw_groups: Any, current_section: dict[str, Any],
) -> list[dict[str, Any]]:
    """Validate and canonicalize the Web groups payload without exposing keys."""
    if not isinstance(raw_groups, list):
        raise ValueError("groups 必须是数组")
    if not raw_groups:
        raise ValueError("至少需要一个分组")
    existing = current_section.get("groups")
    existing_groups = existing if isinstance(existing, list) else []
    if not existing_groups and (
        isinstance(current_section.get("api_key"), str)
        and current_section["api_key"].strip()
    ):
        # 旧单 Key 配置:合成一组作为 keep/留空 的解析来源,首次保存即迁移
        existing_groups = [{
            "name": "组1",
            "daily_limit": current_section.get("daily_limit"),
            "api_keys": [current_section["api_key"]],
        }]
    names: set[str] = set()
    seen_keys: set[str] = set()
    groups: list[dict[str, Any]] = []
    for position, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, dict):
            raise ValueError("groups 中每项必须是对象")
        extra = sorted(set(raw_group) - _GATEWAY_GROUP_FIELDS)
        if extra:
            raise ValueError("group 未知字段: " + ", ".join(extra))
        group_index = raw_group.get("index")
        if group_index is not None and (
            isinstance(group_index, bool) or not isinstance(group_index, int)
            or group_index < 0
        ):
            raise ValueError("group 的 index 必须是非负整数")
        name = raw_group.get("name")
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 100:
            raise ValueError("分组名称需为 1-100 个字符")
        name = name.strip()
        if name in names:
            raise ValueError(f"分组名称重复：{name}")
        names.add(name)
        old_group = _gateway_existing_group(existing_groups, raw_group.get("index"), position)
        old_raw = _gateway_group_raw_entries(old_group)

        canonical: dict[str, Any] = {"name": name}
        if "daily_limit" in raw_group:
            limit = raw_group["daily_limit"]
            if limit is not None and (
                isinstance(limit, bool) or not isinstance(limit, (int, float))
                or not math.isfinite(float(limit)) or not float(limit) >= 0
            ):
                raise ValueError("daily_limit 必须是非负数字或 null")
            if limit is not None:
                canonical["daily_limit"] = limit
        elif "daily_limit" in old_group and old_group["daily_limit"] is not None:
            canonical["daily_limit"] = old_group["daily_limit"]

        if "api_keys" not in raw_group:
            if not old_raw:
                raise ValueError(f"分组 {name} 至少需要一个 API key")
            keys = list(old_raw)
        else:
            raw_keys = raw_group["api_keys"]
            if not isinstance(raw_keys, list) or not raw_keys:
                raise ValueError(f"分组 {name} 至少需要一个 API key")
            keys = [_gateway_key_value(value, old_raw, key_position)
                    for key_position, value in enumerate(raw_keys)]
        for key in keys:
            identity = key.get("value") if isinstance(key, dict) else key
            if not isinstance(identity, str) or not identity.strip():
                raise ValueError(f"分组 {name} 包含无效 API key")
            if identity in seen_keys:
                raise ValueError("同一个 API key 不能属于多个分组或重复配置")
            seen_keys.add(identity)
        canonical["api_keys"] = keys
        groups.append(canonical)
    return groups


def _credential_slots_view(section: dict[str, Any], fields: list[str]) -> list[dict[str, Any]]:
    """凭证槽视图:每个槽 = 一个独立计费套餐的凭证,不返回明文。

    - ``credentials`` 非空列表 → 每项一槽(index = 数组下标)。
    - 否则顶层凭证字段任一非空 → 合成单槽(index 0, 无名称):
      legacy 单 Key 以槽 0 呈现,Web 首次保存即迁移为 credentials 数组。
    - 否则空列表(页面渲染默认一个空槽)。
    """
    slots = section.get("credentials")
    if isinstance(slots, list) and slots:
        views: list[dict[str, Any]] = []
        for index, slot in enumerate(slots):
            if not isinstance(slot, dict):
                continue
            views.append({
                "index": index,
                "name": slot.get("name") or None,
                "credentials": {f: _credential_view(slot.get(f)) for f in fields},
            })
        return views
    if any(
        isinstance(section.get(f), str) and section.get(f).strip() for f in fields
    ):
        return [{
            "index": 0,
            "name": None,
            "credentials": {f: _credential_view(section.get(f)) for f in fields},
        }]
    return []


def _clear_top_level_credentials(key: str) -> dict[str, Any]:
    """删除平台 section 的顶层凭证字段并保存,返回最终完整配置。

    在 credentials 数组已写盘之后调用(先写后删,避免中间态);
    cache.set_config 使用返回的最终配置。
    """
    from llm_usage.config import save_config

    cfg = load_config()
    section = cfg.get("platforms", {}).get(key)
    if isinstance(section, dict):
        for field in _CREDENTIAL_NAMES:
            section.pop(field, None)
        save_config(cfg)
    return cfg


def _existing_slot(
    current_section: dict[str, Any], index: Any, position: int,
) -> dict[str, Any]:
    """取某槽的既有值来源:credentials[index],或 legacy 顶层凭证(仅 index 0)。"""
    slots = current_section.get("credentials")
    if isinstance(slots, list) and slots:
        if not isinstance(index, int) or isinstance(index, bool):
            return {}
        if index < 0 or index >= len(slots) or not isinstance(slots[index], dict):
            return {}
        return slots[index]
    if index == 0 or (index is None and position == 0):
        return {
            "name": None,
            "api_key": current_section.get("api_key"),
            "access_key": current_section.get("access_key"),
            "secret_key": current_section.get("secret_key"),
        }
    return {}


def _validate_credential_slots(
    raw_slots: Any, current_section: dict[str, Any], fields: list[str],
) -> list[dict[str, Any]]:
    """Validate and canonicalize the Web credential slots payload.

    Each slot = one independent billing plan.  Empty field values mean
    "keep the existing value" (no value to keep → 400).  The canonical
    output has no ``index`` and no empty values.
    """
    if not isinstance(raw_slots, list):
        raise ValueError("credential_slots 必须是数组")
    if not raw_slots:
        raise ValueError("至少需要一个凭证")
    allowed = {"index", "name"} | set(fields)
    names: set[str] = set()
    canonical: list[dict[str, Any]] = []
    for position, raw_slot in enumerate(raw_slots):
        if not isinstance(raw_slot, dict):
            raise ValueError("credential_slots 中每项必须是对象")
        extra = sorted(set(raw_slot) - allowed)
        if extra:
            raise ValueError("凭证未知字段: " + ", ".join(extra))
        slot_index = raw_slot.get("index")
        if slot_index is not None and (
            isinstance(slot_index, bool) or not isinstance(slot_index, int)
            or slot_index < 0
        ):
            raise ValueError("凭证的 index 必须是非负整数")
        existing = _existing_slot(current_section, slot_index, position)

        name = raw_slot.get("name")
        if name is None or (isinstance(name, str) and not name.strip()):
            # 留空 = 保留既有名称;无既有 → 默认 套餐N
            name = existing.get("name")
            if not isinstance(name, str) or not name.strip():
                name = None
        else:
            if not isinstance(name, str):
                raise ValueError("凭证名称需为字符串")
            name = name.strip()
            if not 1 <= len(name) <= 100:
                raise ValueError("凭证名称需为 1-100 个字符")
        if not name:
            name = f"套餐{position + 1}"
        if name in names:
            raise ValueError(f"凭证名称重复：{name}")
        names.add(name)

        out_slot: dict[str, Any] = {"name": name}
        for field in fields:
            value = raw_slot.get(field)
            if value is None or (isinstance(value, str) and not value.strip()):
                # 留空 = 保留既有值
                keep = existing.get(field)
                if not isinstance(keep, str) or not keep.strip():
                    raise ValueError(f"凭证 {name} 缺少 {field}")
                out_slot[field] = keep
                continue
            if not isinstance(value, str):
                raise ValueError(f"{field} 需为字符串")
            value = value.strip()
            if value.startswith("env:") and not value[4:].strip():
                raise ValueError(f"{field} 的 env 引用不能为空")
            out_slot[field] = value
        # 完整性:每槽必须凭证齐全
        if "api_key" in fields and not (out_slot.get("api_key") or "").strip():
            raise ValueError(f"凭证 {name} 需要 API key")
        if "access_key" in fields and "secret_key" in fields:
            if not (out_slot.get("access_key") or "").strip() \
                    or not (out_slot.get("secret_key") or "").strip():
                raise ValueError(f"凭证 {name} 需要 AK/SK")
        canonical.append(out_slot)
    return canonical


def _platform_view(key: str) -> dict[str, Any]:
    """从磁盘读最新配置,返回单个平台的可编辑视图。"""
    section = get_platform_config(load_config(), key)
    view = {
        "key": key,
        "display_name": section.get("display_name") or DISPLAY_NAMES[key],
        "enabled": section.get("enabled", True),
    }
    if CREDENTIAL_FIELDS[key]:
        view["credential_slots"] = _credential_slots_view(
            section, CREDENTIAL_FIELDS[key]
        )
    if key == "llm-gateway":
        view["base_url"] = section.get("base_url")
        view["groups"] = _gateway_groups_view(section)
    return view


def _visibility_view(username: str) -> dict[str, Any]:
    """某用户所有平台的可见性聚合:``{platform: {type, targets}}``。"""
    rows = store.list_my_visibility(username)
    view: dict[str, dict[str, Any]] = {}
    for row in rows:
        entry = view.setdefault(row["platform"], {"type": "private", "targets": []})
        if row["target"] == "*":
            entry["type"] = "public"
            entry["targets"] = []
        else:
            entry["type"] = "shared"
            entry["targets"].append(row["target"])
    return view


def _validate_visibility(
    raw: Any, username: str,
) -> dict[str, Any]:
    """校验并规范化可见性:``{"type": "public"|"shared"|"private", "targets": [...]}``。

    返回 ``{"targets": [...]}`` —— public 写 ``["*"]``,private/shared 写目标列表。
    未知 type / 不存在的 target / target 是自己 → ValueError。
    """
    if not isinstance(raw, dict):
        raise ValueError("visibility 需为对象")
    extra = sorted(set(raw) - {"type", "targets"})
    if extra:
        raise ValueError("visibility 未知字段: " + ", ".join(extra))
    vtype = raw.get("type")
    if vtype not in ("public", "shared", "private"):
        raise ValueError("visibility.type 需为 public/shared/private")
    targets = raw.get("targets")
    if vtype == "public":
        if targets:
            raise ValueError("visibility.public 不接受 targets")
        return {"targets": ["*"]}
    if vtype == "private":
        if targets:
            raise ValueError("visibility.private 不接受 targets")
        return {"targets": []}
    if not isinstance(targets, list) or not targets:
        raise ValueError("visibility.shared 需要 targets 用户名列表")
    deduped: list[str] = []
    seen: set[str] = set()
    for target in targets:
        if not isinstance(target, str) or not target.strip():
            raise ValueError("visibility.targets 需为用户名列表")
        target = target.strip()
        if target == username:
            raise ValueError("不能共享给自己")
        if target in seen:
            continue
        if store.get_user(target) is None:
            raise ValueError(f"目标用户不存在：{target}")
        seen.add(target)
        deduped.append(target)
    return {"targets": deduped}


def _apply_visibility(
    username: str, platform: str, visibility: dict[str, Any],
) -> None:
    """把规范化后的可见性写入 user_shares(先清后插,原子替换)。"""
    store.replace_platform_visibility(username, platform, visibility["targets"])


def _validate_platform_fields(
    key: str, body: dict[str, Any], allow_top_level_creds: bool,
) -> None:
    """404 未知平台 + 400 未知字段/平台支持性校验(两个 PUT handler 共用)。"""
    if key not in PROVIDERS:
        raise HTTPException(status_code=404, detail="未知平台")
    allowed = {
        "enabled", "visibility", "credential_slots", "base_url", "groups",
        "display_name",
    }
    if allow_top_level_creds:
        allowed |= _CREDENTIAL_NAMES
    extra = sorted(set(body) - allowed)
    if extra:
        raise HTTPException(status_code=400, detail="未知字段: " + ", ".join(extra))
    if not allow_top_level_creds and "display_name" in body and key != "llm-gateway":
        raise HTTPException(status_code=400, detail=f"平台 {key} 不支持字段: display_name")
    if "groups" in body and key != "llm-gateway":
        raise HTTPException(status_code=400, detail=f"平台 {key} 不支持字段: groups")
    if "base_url" in body and key != "llm-gateway":
        raise HTTPException(status_code=400, detail=f"平台 {key} 不支持字段: base_url")
    if "credential_slots" in body and key == "llm-gateway":
        raise HTTPException(
            status_code=400, detail="平台 llm-gateway 不支持字段: credential_slots"
        )
    if "credential_slots" in body and not CREDENTIAL_FIELDS[key]:
        raise HTTPException(
            status_code=400, detail=f"平台 {key} 不支持字段: credential_slots"
        )
    if allow_top_level_creds:
        for field in _CREDENTIAL_NAMES & set(body):
            if field not in CREDENTIAL_FIELDS[key]:
                raise HTTPException(
                    status_code=400, detail=f"平台 {key} 不支持字段: {field}"
                )


def _gateway_updates(
    key: str, body: dict[str, Any], section: dict[str, Any],
) -> dict[str, Any]:
    """llm-gateway 专用:base_url 留空不改;groups 校验 + use_groups=True。

    display_name 由调用方按各自规则处理(admin 全平台 / my 仅 gateway)。
    """
    updates: dict[str, Any] = {}
    if key != "llm-gateway":
        return updates
    base_url = body.get("base_url")
    if isinstance(base_url, str) and base_url.strip():
        updates["base_url"] = base_url.strip()
    if "groups" in body:
        try:
            updates["groups"] = _validate_gateway_groups(body["groups"], section)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        # 覆盖历史遗留的 use_groups=false:保存后 fetch 一定走分组模式
        updates["use_groups"] = True
    return updates


def _validate_visibility_body(
    body: dict[str, Any], username: str,
) -> dict[str, Any] | None:
    """visibility 校验 + 规范化;无 visibility 字段返回 None。"""
    if "visibility" not in body:
        return None
    try:
        return _validate_visibility(body["visibility"], username)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def create_app(config: dict[str, Any], interval: float = 60.0) -> FastAPI:
    cache = UserCaches(interval)
    limiter = _LoginLimiter()
    app = FastAPI(title="llm-usage")

    @app.middleware("http")
    async def _security_headers(request: Request, call_next: Any) -> Response:
        resp = await call_next(request)
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        return resp
    pages = {
        name: (files("llm_usage") / "static" / name).read_text(encoding="utf-8")
        for name in ("index.html", "login.html", "users.html", "config.html")
    }

    # ---- 配置来源与按用户合并 ----

    def _config_for(user: dict[str, Any]) -> dict[str, Any]:
        """单管理员模型:admin 永远用 config.toml;普通用户用自己存 DB 的配置。"""
        if user["is_admin"]:
            return load_config()
        return store.get_user_config(user["username"])

    def _usage_payload_for(user: dict[str, Any]) -> JSONResponse:
        try:
            own = _config_for(user)
            results, fetched_at = cache.get(own)
            # 自己的平台保留裸名;共享来的平台一律带 (owner) 后缀,
            # 同名冲突时每个来源都保留展示,不丢弃任何来源。
            merged: dict[str, PlatformResult] = {r.platform: r for r in results}
            extra: list[PlatformResult] = []
            for share in store.list_shared_platforms(user["username"]):
                owner = share["owner"]
                if owner == user["username"]:
                    continue  # 自身来源不参与共享合并(自己的平台是裸名)
                owner_user = store.get_user(owner)
                if owner_user is None:
                    continue
                owner_cfg = (
                    load_config()
                    if owner_user["is_admin"]
                    else store.get_user_config(owner)
                )
                shared_results, shared_at = cache.get(owner_cfg)
                if shared_at > fetched_at:
                    fetched_at = shared_at
                for r in shared_results:
                    if r.platform != share["platform"]:
                        continue
                    # 副本上标注来源,不改动缓存里的共享结果(避免二次合并叠加后缀)
                    tagged = replace(r)
                    tagged.display_name = f"{r.display_name}({owner})"
                    if r.platform not in merged:
                        merged[r.platform] = tagged  # 无冲突:收录
                    elif merged[r.platform].display_name != tagged.display_name:
                        extra.append(tagged)  # 同名冲突:来源区分,不丢弃
            platforms = list(merged.values()) + extra
        except Exception:  # noqa: BLE001 - fetch_all 内部已隔离单平台失败,这里兜底
            logger.error("usage payload failed", exc_info=True)
            return JSONResponse(
                {"error": "获取用量失败，请查看服务端日志"}, status_code=500
            )
        return JSONResponse({
            "fetched_at": fetched_at,
            "interval": cache.interval,
            "platforms": results_to_dict(platforms)["platforms"],
        })

    # ---- 页面 ----

    @app.get("/favicon.ico")
    def favicon() -> Response:
        """浏览器默认请求 /favicon.ico;页面已声明内联 data URI 图标,这里兜底返回 204 避免 404 日志。"""
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        if _current_user(request) is None:
            return RedirectResponse("/login", status_code=302)
        return HTMLResponse(pages["index.html"])

    @app.get("/login", response_class=HTMLResponse)
    def login_page() -> HTMLResponse:
        return HTMLResponse(pages["login.html"])

    @app.get("/users", response_class=HTMLResponse)
    def users_page(request: Request) -> Response:
        user = _current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        if not user["is_admin"]:
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(pages["users.html"])

    @app.get("/config", response_class=HTMLResponse)
    def config_page(request: Request) -> Response:
        """配置页对登录用户开放:页面 JS 按 /api/auth/state 分流 admin/普通用户模式。"""
        user = _current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        return HTMLResponse(pages["config.html"])

    # ---- 认证 API ----

    @app.get("/api/auth/state")
    def auth_state(request: Request) -> dict[str, Any]:
        user = _current_user(request)
        return {
            "authenticated": user is not None,
            "needs_setup": store.count_users() == 0,
            "registration_enabled": store.get_setting("registration_enabled") == "1",
            "user": (
                {"username": user["username"], "is_admin": user["is_admin"]}
                if user else None
            ),
        }

    @app.post("/api/auth/setup")
    def auth_setup(
        request: Request,
        body: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        wait = limiter.check(_client_key(request))
        if wait is not None:
            seconds = int(wait) + 1
            raise HTTPException(
                status_code=429, detail=f"尝试过于频繁，请 {seconds} 秒后再试"
            )
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not _valid_username(username):
            raise HTTPException(
                status_code=400,
                detail="用户名需为 1-64 位字母、数字、下划线、点或连字符",
            )
        if not isinstance(password, str) or not _valid_password(password):
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        username = username.strip()
        if not store.create_first_admin(username, password):
            raise HTTPException(status_code=409, detail="已完成初始化")
        token = store.create_session(username)
        resp = JSONResponse({"username": username, "is_admin": True})
        _set_session_cookie(resp, token)
        limiter.clear(_client_key(request))
        return resp

    @app.post("/api/auth/register")
    def auth_register(
        request: Request,
        body: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        wait = limiter.check(_client_key(request))
        if wait is not None:
            seconds = int(wait) + 1
            raise HTTPException(
                status_code=429, detail=f"尝试过于频繁，请 {seconds} 秒后再试"
            )
        if store.get_setting("registration_enabled") != "1":
            raise HTTPException(status_code=403, detail="注册已关闭")
        username = body.get("username")
        password = body.get("password")
        if not isinstance(username, str) or not _valid_username(username):
            raise HTTPException(
                status_code=400,
                detail="用户名需为 1-64 位字母、数字、下划线、点或连字符",
            )
        if not isinstance(password, str) or not _valid_password(password):
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        username = username.strip()
        try:
            store.create_user(username, password, is_admin=False)  # 自助注册永远是普通用户
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        token = store.create_session(username)
        resp = JSONResponse({"username": username, "is_admin": False})
        _set_session_cookie(resp, token)  # 注册即登录,与 auth_setup 一致
        limiter.clear(_client_key(request))
        return resp

    @app.post("/api/auth/login")
    def auth_login(
        request: Request,
        body: dict[str, Any] = Body(...),
    ) -> JSONResponse:
        wait = limiter.check(_client_key(request))
        if wait is not None:
            seconds = int(wait) + 1
            raise HTTPException(
                status_code=429, detail=f"尝试过于频繁，请 {seconds} 秒后再试"
            )
        username = body.get("username")
        password = body.get("password")
        user = (
            store.verify_user(username, password)
            if isinstance(username, str) and isinstance(password, str)
            else None
        )
        if user is None:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = store.create_session(user["username"])
        resp = JSONResponse(user)
        _set_session_cookie(resp, token)
        limiter.clear(_client_key(request))
        return resp

    @app.post("/api/auth/logout")
    def auth_logout(request: Request) -> JSONResponse:
        token = request.cookies.get(SESSION_COOKIE)
        if token:
            store.delete_session(token)
        resp = JSONResponse({"ok": True})
        resp.delete_cookie(SESSION_COOKIE, path="/")
        return resp

    @app.post("/api/auth/password")
    def auth_password(
        request: Request,
        body: dict[str, Any] = Body(...),
        user: dict[str, Any] = Depends(api_user),
    ) -> dict[str, bool]:
        old = body.get("old_password")
        if not isinstance(old, str) or store.verify_user(user["username"], old) is None:
            raise HTTPException(status_code=401, detail="原密码错误")
        new = body.get("new_password")
        if not _valid_password(new):
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        store.set_user_password(user["username"], new)
        store.delete_user_sessions(
            user["username"], keep_token=request.cookies.get(SESSION_COOKIE)
        )
        return {"ok": True}

    # ---- 用量 API ----

    @app.get("/api/usage")
    def usage(user: dict[str, Any] = Depends(api_user)) -> JSONResponse:
        return _usage_payload_for(user)

    @app.post("/api/refresh")
    def usage_refresh(user: dict[str, Any] = Depends(api_user)) -> JSONResponse:
        """立即刷新:仅失效当前用户配置的缓存条目后重新 fetch,响应同构。"""
        cache.invalidate_for(_config_for(user))
        return _usage_payload_for(user)

    @app.post("/api/interval")
    def usage_interval(
        body: dict[str, Any] = Body(...),
        user: dict[str, Any] = Depends(api_user),
    ) -> dict[str, Any]:
        """调整自动刷新间隔(运行期、进程级,不写入 config.toml)。"""
        value = body.get("interval")
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
        ):
            raise HTTPException(status_code=400, detail="间隔需为 5-3600 秒内的数字")
        return {"interval": cache.set_interval(float(value))}

    # ---- 用户管理 API ----
    # GET 放开到任意登录用户(普通用户配置页需要共享目标列表);写操作保持管理员。

    @app.get("/api/users")
    def users_list(user: dict[str, Any] = Depends(api_user)) -> list[dict[str, Any]]:
        return store.list_users()

    @app.post("/api/users", status_code=201)
    def users_create(
        body: dict[str, Any] = Body(...),
        admin: dict[str, Any] = Depends(api_admin),
    ) -> dict[str, Any]:
        """新建用户。单管理员模型:is_admin 一律拒绝,所有新用户都是普通用户。"""
        if body.get("is_admin") is True:
            raise HTTPException(
                status_code=400,
                detail="系统只存在一个管理员：不能创建新的管理员账号",
            )
        username = body.get("username")
        if not _valid_username(username):
            raise HTTPException(
                status_code=400,
                detail="用户名需为 1-64 位字母、数字、下划线、点或连字符",
            )
        password = body.get("password")
        if not _valid_password(password):
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        username = username.strip()
        try:
            store.create_user(username, password, is_admin=False)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return store.get_user(username)

    @app.delete("/api/users/{username}")
    def users_delete(
        username: str,
        admin: dict[str, Any] = Depends(api_admin),
    ) -> dict[str, bool]:
        target = store.get_user(username)
        if target is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        if username == admin["username"]:
            raise HTTPException(status_code=400, detail="不能删除当前登录用户")
        if target["is_admin"] and store.count_admins() == 1:
            raise HTTPException(status_code=400, detail="至少保留一个管理员")
        store.delete_user(username)
        return {"ok": True}

    @app.post("/api/users/{username}/password")
    def users_reset_password(
        username: str,
        body: dict[str, Any] = Body(...),
        admin: dict[str, Any] = Depends(api_admin),
    ) -> dict[str, bool]:
        if store.get_user(username) is None:
            raise HTTPException(status_code=404, detail="用户不存在")
        password = body.get("password")
        if not _valid_password(password):
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        store.set_user_password(username, password)
        store.delete_user_sessions(username)
        return {"ok": True}

    # ---- 站点设置 API(管理员) ----

    @app.get("/api/settings")
    def settings_get(admin: dict[str, Any] = Depends(api_admin)) -> dict[str, Any]:
        return {"registration_enabled": store.get_setting("registration_enabled") == "1"}

    @app.put("/api/settings")
    def settings_put(
        body: dict[str, Any] = Body(...),
        admin: dict[str, Any] = Depends(api_admin),
    ) -> dict[str, Any]:
        extra = sorted(set(body) - _SETTINGS_KEYS)
        if extra:
            raise HTTPException(status_code=400, detail="未知字段: " + ", ".join(extra))
        value = body.get("registration_enabled")
        if not isinstance(value, bool):
            raise HTTPException(status_code=400, detail="registration_enabled 需为布尔值")
        store.set_setting("registration_enabled", "1" if value else "0")
        return {"registration_enabled": value}

    # ---- 普通用户平台配置 API ----
    # 普通用户的平台配置存 history.db(user_configs),与 config.toml 完全隔离;
    # 与 admin /api/config 同构:凭证槽 / gateway base_url+组 / 可见性均可编辑。
    # 管理员调用 → 400。

    def _non_admin(user: dict[str, Any]) -> dict[str, Any]:
        if user["is_admin"]:
            raise HTTPException(status_code=400, detail="管理员请使用 /api/config")
        return user

    def _my_platform_view(user: dict[str, Any], key: str) -> dict[str, Any]:
        section = get_platform_config(
            store.get_user_config(user["username"]), key
        )
        visibility = _visibility_view(user["username"]).get(
            key, {"type": "private", "targets": []}
        )
        view: dict[str, Any] = {
            "key": key,
            "display_name": section.get("display_name") or DISPLAY_NAMES[key],
            "enabled": section.get("enabled", True),
            "visibility": visibility,
        }
        if CREDENTIAL_FIELDS[key]:
            view["credential_slots"] = _credential_slots_view(
                section, CREDENTIAL_FIELDS[key]
            )
        if key == "llm-gateway":
            view["base_url"] = section.get("base_url")
            view["groups"] = _gateway_groups_view(section)
        return view

    @app.get("/api/my/platforms")
    def my_platforms(
        user: dict[str, Any] = Depends(api_user),
    ) -> dict[str, Any]:
        user = _non_admin(user)
        cfg_order = get_platform_order(store.get_user_config(user["username"]))
        order = {k: i for i, k in enumerate(cfg_order)}
        reg = registry_index()
        keys = sorted(PROVIDERS, key=lambda k: (order.get(k, len(cfg_order)), reg[k]))
        return {"platforms": [_my_platform_view(user, key) for key in keys]}

    @app.put("/api/my/platforms/{key}")
    def my_platforms_put(
        key: str,
        body: dict[str, Any] = Body(...),
        user: dict[str, Any] = Depends(api_user),
    ) -> dict[str, Any]:
        user = _non_admin(user)
        _validate_platform_fields(key, body, allow_top_level_creds=False)
        visibility = _validate_visibility_body(body, user["username"])
        cfg = store.get_user_config(user["username"])
        platforms = cfg.setdefault("platforms", {})
        section = platforms.setdefault(key, {})
        if isinstance(body.get("enabled"), bool):
            section["enabled"] = body["enabled"]
        if "credential_slots" in body:
            # 校验来源是用户自己已存的 section;留空 = 保留既有值
            try:
                section["credentials"] = _validate_credential_slots(
                    body["credential_slots"], section, CREDENTIAL_FIELDS[key]
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            # 槽式保存后清除顶层凭证字段(单 JSON 原子写,无中间态问题)
            for field in _CREDENTIAL_NAMES:
                section.pop(field, None)
        if key == "llm-gateway":
            display_name = body.get("display_name")
            # 留空 = 不修改;仅非空字符串写回(与 admin 约定一致)
            if isinstance(display_name, str) and display_name.strip():
                section["display_name"] = display_name.strip()
            section.update(_gateway_updates(key, body, section))
        store.set_user_config(user["username"], cfg)
        if visibility is not None:
            _apply_visibility(user["username"], key, visibility)
        return _my_platform_view(user, key)

    # ---- 供应商配置 API(管理员) ----

    @app.get("/api/config")
    def config_get(admin: dict[str, Any] = Depends(api_admin)) -> dict[str, Any]:
        cfg_order = get_platform_order(load_config())
        order = {k: i for i, k in enumerate(cfg_order)}
        reg = registry_index()
        keys = sorted(PROVIDERS, key=lambda k: (order.get(k, len(cfg_order)), reg[k]))
        vis = _visibility_view(admin["username"])
        return {
            "platforms": [
                {**_platform_view(key), "visibility": vis.get(
                    key, {"type": "private", "targets": []}
                )}
                for key in keys
            ]
        }

    @app.put("/api/config/platforms/{key}")
    def config_put(
        key: str,
        body: dict[str, Any] = Body(...),
        admin: dict[str, Any] = Depends(api_admin),
    ) -> dict[str, Any]:
        _validate_platform_fields(key, body, allow_top_level_creds=True)
        visibility = _validate_visibility_body(body, admin["username"])
        current_section = (
            get_platform_config(load_config(), key)
            if "credential_slots" in body or "groups" in body
            else {}
        )
        updates: dict[str, Any] = {}
        if isinstance(body.get("enabled"), bool):
            updates["enabled"] = body["enabled"]
        display_name = body.get("display_name")
        # 全平台通用;留空 = 不修改;仅非空字符串写回
        if isinstance(display_name, str) and display_name.strip():
            updates["display_name"] = display_name.strip()
        for field in CREDENTIAL_FIELDS[key]:
            value = body.get(field)
            # 留空 = 不修改;仅非空字符串写回
            if isinstance(value, str) and value.strip():
                updates[field] = value.strip()
        if "credential_slots" in body:
            try:
                updates["credentials"] = _validate_credential_slots(
                    body["credential_slots"], current_section, CREDENTIAL_FIELDS[key]
                )
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        updates.update(_gateway_updates(key, body, current_section))
        if updates:
            update_platform_config(key, updates)
            if "credential_slots" in body:
                # 槽式保存后清除顶层凭证字段,避免双写不一致(先写后删,避免中间态)
                _clear_top_level_credentials(key)
        if visibility is not None:
            _apply_visibility(admin["username"], key, visibility)
        return {
            **_platform_view(key),
            "visibility": _visibility_view(admin["username"]).get(
                key, {"type": "private", "targets": []}
            ),
        }

    @app.put("/api/config/order")
    def config_put_order(
        body: dict[str, Any] = Body(...),
        admin: dict[str, Any] = Depends(api_admin),
    ) -> dict[str, Any]:
        order = body.get("order")
        if not isinstance(order, list) or not all(isinstance(k, str) for k in order):
            raise HTTPException(status_code=400, detail="order 需为字符串数组")
        known = [k for k in order if k in PROVIDERS]
        missing = [k for k in PROVIDERS if k not in known]
        full_order = known + missing
        set_platform_order(full_order)
        return {"order": full_order}

    return app
