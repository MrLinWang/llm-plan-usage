"""Web dashboard: FastAPI app serving the usage overview with a TTL cache.

会话认证:未登录访问页面重定向到 /login,API 返回 401。首次启动(无用户)
通过 /api/auth/setup 创建首个管理员。用户/会话存储在 history.db(store.py)。
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from importlib.resources import files
from typing import Any

from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from llm_usage import store
from llm_usage.config import get_platform_config, load_config, update_platform_config
from llm_usage.display import results_to_dict
from llm_usage.models import PlatformResult
from llm_usage.providers import DISPLAY_NAMES, PROVIDERS, fetch_all

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
}

# PUT /api/config/platforms/{key} 允许的字段超集
_CONFIG_FIELDS = {"enabled", "display_name", "api_key", "access_key", "secret_key"}
_CREDENTIAL_NAMES = {"api_key", "access_key", "secret_key"}


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


# ---- 认证辅助 ----

def _current_user(request: Request) -> dict[str, Any] | None:
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return store.get_session_user(token)


def _set_session_cookie(response: JSONResponse, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE, token,
        max_age=store.SESSION_TTL_DAYS * 86400,
        httponly=True, samesite="lax", path="/",
    )


def _valid_username(value: Any) -> bool:
    return isinstance(value, str) and 1 <= len(value.strip()) <= 64


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


def _platform_view(key: str) -> dict[str, Any]:
    """从磁盘读最新配置,返回单个平台的可编辑视图。"""
    section = get_platform_config(load_config(), key)
    return {
        "key": key,
        "display_name": section.get("display_name") or DISPLAY_NAMES[key],
        "enabled": section.get("enabled", True),
        "credentials": {
            field: _credential_view(section.get(field))
            for field in CREDENTIAL_FIELDS[key]
        },
    }


def create_app(config: dict[str, Any], interval: float = 60.0) -> FastAPI:
    cache = UsageCache(config, interval)
    app = FastAPI(title="llm-usage")
    pages = {
        name: (files("llm_usage") / "static" / name).read_text(encoding="utf-8")
        for name in ("index.html", "login.html", "users.html", "config.html")
    }

    # ---- 页面 ----

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
        user = _current_user(request)
        if user is None:
            return RedirectResponse("/login", status_code=302)
        if not user["is_admin"]:
            return RedirectResponse("/", status_code=302)
        return HTMLResponse(pages["config.html"])

    # ---- 认证 API ----

    @app.get("/api/auth/state")
    def auth_state(request: Request) -> dict[str, Any]:
        user = _current_user(request)
        return {
            "authenticated": user is not None,
            "needs_setup": store.count_users() == 0,
            "user": (
                {"username": user["username"], "is_admin": user["is_admin"]}
                if user else None
            ),
        }

    @app.post("/api/auth/setup")
    def auth_setup(body: dict[str, Any] = Body(...)) -> JSONResponse:
        if store.count_users() > 0:
            raise HTTPException(status_code=409, detail="已完成初始化")
        username = body.get("username")
        if not _valid_username(username):
            raise HTTPException(status_code=400, detail="用户名需为 1-64 个字符")
        password = body.get("password")
        if not _valid_password(password):
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        username = username.strip()
        try:
            store.create_user(username, password, is_admin=True)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        token = store.create_session(username)
        resp = JSONResponse({"username": username, "is_admin": True})
        _set_session_cookie(resp, token)
        return resp

    @app.post("/api/auth/login")
    def auth_login(body: dict[str, Any] = Body(...)) -> JSONResponse:
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
        try:
            results, fetched_at = cache.get()
        except Exception as exc:  # noqa: BLE001 — fetch_all 内部已隔离单平台失败,这里兜底
            return JSONResponse({"error": f"获取用量失败:{exc}"}, status_code=500)
        return JSONResponse({
            "fetched_at": fetched_at,
            "interval": cache.interval,
            "platforms": results_to_dict(results)["platforms"],
        })

    # ---- 用户管理 API(管理员) ----

    @app.get("/api/users")
    def users_list(admin: dict[str, Any] = Depends(api_admin)) -> list[dict[str, Any]]:
        return store.list_users()

    @app.post("/api/users", status_code=201)
    def users_create(
        body: dict[str, Any] = Body(...),
        admin: dict[str, Any] = Depends(api_admin),
    ) -> dict[str, Any]:
        username = body.get("username")
        if not _valid_username(username):
            raise HTTPException(status_code=400, detail="用户名需为 1-64 个字符")
        password = body.get("password")
        if not _valid_password(password):
            raise HTTPException(status_code=400, detail="密码至少 6 位")
        username = username.strip()
        try:
            store.create_user(username, password, is_admin=bool(body.get("is_admin")))
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

    # ---- 供应商配置 API(管理员) ----

    @app.get("/api/config")
    def config_get(admin: dict[str, Any] = Depends(api_admin)) -> dict[str, Any]:
        return {"platforms": [_platform_view(key) for key in PROVIDERS]}

    @app.put("/api/config/platforms/{key}")
    def config_put(
        key: str,
        body: dict[str, Any] = Body(...),
        admin: dict[str, Any] = Depends(api_admin),
    ) -> dict[str, Any]:
        if key not in PROVIDERS:
            raise HTTPException(status_code=404, detail="未知平台")
        extra = sorted(set(body) - _CONFIG_FIELDS)
        if extra:
            raise HTTPException(
                status_code=400, detail="未知字段: " + ", ".join(extra)
            )
        for field in _CREDENTIAL_NAMES & set(body):
            if field not in CREDENTIAL_FIELDS[key]:
                raise HTTPException(
                    status_code=400, detail=f"平台 {key} 不支持字段: {field}"
                )
        updates: dict[str, Any] = {}
        if isinstance(body.get("enabled"), bool):
            updates["enabled"] = body["enabled"]
        display_name = body.get("display_name")
        if isinstance(display_name, str) and display_name.strip():
            updates["display_name"] = display_name.strip()
        for field in CREDENTIAL_FIELDS[key]:
            value = body.get(field)
            # 留空 = 不修改;仅非空字符串写回
            if isinstance(value, str) and value.strip():
                updates[field] = value.strip()
        if updates:
            new_config = update_platform_config(key, updates)
            cache.set_config(new_config)
        return _platform_view(key)

    return app
