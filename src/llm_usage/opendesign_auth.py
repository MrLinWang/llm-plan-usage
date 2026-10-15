"""Short-lived, user-owned OpenDesign browser login authorizations.

Only the official control host receives device secrets. Completion saves the
control key through a caller-supplied callback before exposing a public result;
the key itself never becomes part of a response or a logged exception.
"""

from __future__ import annotations

import math
import re
import secrets
import threading
import time
from collections.abc import Callable
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

DEVICE_URL = "https://amr-api.open-design.ai/api/v1/cli/device-authorizations"
MAX_AUTHORIZATIONS = 32
MAX_USER_AUTHORIZATIONS = 4
MAX_AUTHORIZATION_TTL = 900
REQUEST_TIMEOUT = 15.0


class DeviceAuthorizationError(Exception):
    """An intentionally sanitized error suitable for a JSON API response."""

    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True)
class Authorization:
    """Private callback input; never serialize this object to a client."""

    platform: str
    index: int | None
    name: str | None
    fingerprint: str
    control_key: str = field(repr=False)


@dataclass(repr=False)
class _Grant:
    owner: str
    platform: str
    index: int | None
    name: str | None
    fingerprint: str
    device_id: str
    device_secret: str
    interval: int
    expires: float
    expires_at: str
    next_poll: float = 0
    control_key: str | None = None
    completed: dict[str, Any] | None = None
    lock: Any = field(default_factory=threading.Lock)

    def clear_secrets(self) -> None:
        self.device_id = ""
        self.device_secret = ""
        self.control_key = None


def _text(value: Any, *, max_length: int = 4096) -> str | None:
    if not isinstance(value, str) or not value or len(value) > max_length:
        return None
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        return None
    return value


def _activation_url(value: Any) -> str | None:
    if _text(value) is None:
        return None
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port is not None
            or "\\" in value
            or re.fullmatch(
                r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*open-design\.ai", host,
            ) is None
        ):
            return None
    except ValueError:
        return None
    return str(value)


def _expires_at(value: Any) -> float | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return None
        result = parsed.timestamp()
        return result if math.isfinite(result) else None
    except (ValueError, OverflowError, OSError):
        return None


def _status(body: dict[str, Any]) -> str:
    """Read machine states only; upstream prose may contain secrets."""
    for key in ("status", "code", "error"):
        value = body.get(key)
        if isinstance(value, dict):
            value = value.get("code")
        if isinstance(value, str):
            normalized = value.lower().replace("-", "_")
            if normalized in {
                "pending", "authorization_pending", "slow_down",
                "expired", "expired_token", "authorization_expired",
                "denied", "access_denied", "authorization_denied",
                "cancelled", "canceled", "authorization_cancelled",
            }:
                return normalized
    return ""


class DeviceAuthorizationManager:
    """App-local bounded grants, with serialized polling and exactly-once saving.

    ``on_authorized`` runs under a grant's lock and must return public, masked
    JSON data. A failed save retains the key in memory for a retry until expiry.
    A successful save clears upstream secrets and caches only that public data,
    so a lost response or a simultaneous poll cannot save the account twice.
    """

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._clock = clock
        self._wall_clock = wall_clock
        self._lock = threading.Lock()
        self._grants: dict[str, _Grant] = {}
        self._starting: dict[str, int] = {}

    def _request(self, url: str, body: dict[str, Any]) -> httpx.Response:
        try:
            if self._client is not None:
                return self._client.post(
                    url, json=body, timeout=REQUEST_TIMEOUT, follow_redirects=False,
                )
            with httpx.Client() as client:
                return client.post(
                    url, json=body, timeout=REQUEST_TIMEOUT, follow_redirects=False,
                )
        except httpx.HTTPError:
            raise DeviceAuthorizationError("OpenDesign 登录服务网络错误，请重试", 502) from None

    def _prune_locked(self) -> None:
        now = self._clock()
        for token, grant in list(self._grants.items()):
            # An active poll rechecks expiry after its HTTP request. Do not race
            # its save callback or let a concurrent start erase its replay data.
            if grant.expires <= now and not grant.lock.locked():
                grant.clear_secrets()
                del self._grants[token]

    def _reserve(self, owner: str) -> None:
        with self._lock:
            self._prune_locked()
            own = sum(grant.owner == owner for grant in self._grants.values())
            own += self._starting.get(owner, 0)
            total = len(self._grants) + sum(self._starting.values())
            for token, grant in list(self._grants.items()):
                if own < MAX_USER_AUTHORIZATIONS and total < MAX_AUTHORIZATIONS:
                    break
                if (
                    grant.completed is not None and not grant.lock.locked()
                    and (total >= MAX_AUTHORIZATIONS or grant.owner == owner)
                ):
                    del self._grants[token]
                    total -= 1
                    own -= grant.owner == owner
            if own >= MAX_USER_AUTHORIZATIONS or total >= MAX_AUTHORIZATIONS:
                raise DeviceAuthorizationError("待完成的 OpenDesign 登录过多，请先完成或取消", 429)
            self._starting[owner] = self._starting.get(owner, 0) + 1

    def start(
        self,
        owner: str,
        platform: str,
        index: int | None = None,
        name: str | None = None,
        fingerprint: str = "",
    ) -> dict[str, Any]:
        self._reserve(owner)
        try:
            response = self._request(DEVICE_URL, {
                "cliVersion": "0.1.4", "host": "llm-usage", "profile": "prod",
            })
            if response.status_code not in (200, 201):
                raise DeviceAuthorizationError("OpenDesign 登录请求失败，请重试", 502)
            body = self._json(response)
            device_id = _text(body.get("deviceId"), max_length=256)
            device_secret = _text(body.get("deviceSecret"))
            user_code = _text(body.get("userCode"), max_length=128)
            activation = _activation_url(body.get("activationUrl"))
            expiry = _expires_at(body.get("expiresAt"))
            interval = body.get("pollIntervalSeconds")
            if (
                device_id is None or re.fullmatch(r"[A-Za-z0-9_-]+", device_id) is None
                or device_secret is None or user_code is None or activation is None
                or expiry is None or not isinstance(interval, int) or isinstance(interval, bool)
                or not 1 <= interval <= 300
            ):
                raise DeviceAuthorizationError("OpenDesign 登录响应格式无效", 502)
            now_wall = self._wall_clock()
            ttl = min(expiry - now_wall, MAX_AUTHORIZATION_TTL)
            if ttl <= 0:
                raise DeviceAuthorizationError("OpenDesign 登录请求已过期，请重试", 410)
            expires_at = (
                datetime.fromtimestamp(now_wall + ttl, UTC).isoformat().replace("+00:00", "Z")
            )
            token = secrets.token_urlsafe(32)
            grant = _Grant(
                owner, platform, index, name, fingerprint, device_id, device_secret,
                interval, self._clock() + ttl, expires_at,
            )
            with self._lock:
                self._grants[token] = grant
            return {
                "id": token, "status": "pending", "user_code": user_code,
                "activation_url": activation, "poll_interval_seconds": interval,
                "expires_at": expires_at,
            }
        finally:
            with self._lock:
                remaining = self._starting[owner] - 1
                if remaining:
                    self._starting[owner] = remaining
                else:
                    del self._starting[owner]

    @staticmethod
    def _json(response: httpx.Response) -> dict[str, Any]:
        try:
            body = response.json()
        except ValueError:
            raise DeviceAuthorizationError("OpenDesign 登录响应不是有效 JSON", 502) from None
        if not isinstance(body, dict):
            raise DeviceAuthorizationError("OpenDesign 登录响应格式无效", 502)
        return body

    def _get(self, owner: str, token: str) -> _Grant:
        with self._lock:
            grant = self._grants.get(token)
            if grant is None or grant.owner != owner:
                raise DeviceAuthorizationError("OpenDesign 登录请求不存在或已失效", 404)
            return grant

    def _is_current(self, owner: str, token: str, grant: _Grant) -> bool:
        with self._lock:
            return grant.owner == owner and self._grants.get(token) is grant

    def _remove(self, token: str, grant: _Grant) -> None:
        grant.clear_secrets()
        with self._lock:
            if self._grants.get(token) is grant:
                del self._grants[token]

    def poll(
        self,
        owner: str,
        authorization_id: str,
        on_authorized: Callable[[Authorization], dict[str, Any]],
    ) -> dict[str, Any]:
        grant = self._get(owner, authorization_id)
        with grant.lock:
            if not self._is_current(owner, authorization_id, grant):
                raise DeviceAuthorizationError("OpenDesign 登录请求不存在或已失效", 404)
            if grant.expires <= self._clock():
                self._remove(authorization_id, grant)
                return {"id": authorization_id, "status": "expired"}
            if grant.completed is not None:
                return deepcopy(grant.completed)
            if grant.control_key is None:
                if self._clock() < grant.next_poll:
                    return self._pending(authorization_id, grant)
                # Even failed requests respect the upstream minimum interval.
                grant.next_poll = self._clock() + grant.interval
                response = self._request(
                    f"{DEVICE_URL}/{grant.device_id}/token",
                    {"deviceSecret": grant.device_secret},
                )
                if grant.expires <= self._clock():
                    self._remove(authorization_id, grant)
                    return {"id": authorization_id, "status": "expired"}
                if response.status_code in (401, 404, 410):
                    self._remove(authorization_id, grant)
                    return {"id": authorization_id, "status": "expired"}
                if response.status_code == 403:
                    self._remove(authorization_id, grant)
                    return {"id": authorization_id, "status": "denied"}
                if response.status_code == 429 or response.status_code >= 500:
                    raise DeviceAuthorizationError("OpenDesign 登录服务暂时不可用，请稍后重试", 502)
                try:
                    body = self._json(response)
                except DeviceAuthorizationError:
                    if response.status_code == 202:
                        return self._pending(authorization_id, grant)
                    raise
                state = _status(body)
                if state in {"expired", "expired_token", "authorization_expired"}:
                    self._remove(authorization_id, grant)
                    return {"id": authorization_id, "status": "expired"}
                if state in {"denied", "access_denied", "authorization_denied"}:
                    self._remove(authorization_id, grant)
                    return {"id": authorization_id, "status": "denied"}
                if state in {"cancelled", "canceled", "authorization_cancelled"}:
                    self._remove(authorization_id, grant)
                    return {"id": authorization_id, "status": "cancelled"}
                if response.status_code == 202 or state in {
                    "pending", "authorization_pending", "slow_down",
                }:
                    if state == "slow_down":
                        grant.interval = min(grant.interval + 5, 300)
                        grant.next_poll = self._clock() + grant.interval
                    return self._pending(authorization_id, grant)
                if response.status_code != 200:
                    self._remove(authorization_id, grant)
                    raise DeviceAuthorizationError("OpenDesign 登录验证失败，请重新登录", 502)
                key = body.get("controlKey")
                if key is None and isinstance(body.get("user"), dict):
                    key = body["user"].get("controlKey")
                key = _text(key)
                if (
                    key is None or not key.startswith("vela_ctrl_")
                    or len(key) <= len("vela_ctrl_") or any(char.isspace() for char in key)
                ):
                    self._remove(authorization_id, grant)
                    raise DeviceAuthorizationError("OpenDesign 登录未返回有效的用量凭证", 502)
                grant.control_key = key
                # Polling is complete; only the key is needed for a save retry.
                grant.device_id = ""
                grant.device_secret = ""
            assert grant.control_key is not None
            result = on_authorized(Authorization(
                grant.platform, grant.index, grant.name, grant.fingerprint, grant.control_key,
            ))
            grant.completed = {**deepcopy(result), "id": authorization_id, "status": "authorized"}
            grant.clear_secrets()
            return deepcopy(grant.completed)

    def _pending(self, token: str, grant: _Grant) -> dict[str, Any]:
        return {
            "id": token, "status": "pending", "poll_interval_seconds": grant.interval,
            "retry_after": max(1, math.ceil(grant.next_poll - self._clock())),
        }

    def cancel(self, owner: str, authorization_id: str) -> bool:
        grant = self._get(owner, authorization_id)
        with grant.lock:
            if not self._is_current(owner, authorization_id, grant):
                raise DeviceAuthorizationError("OpenDesign 登录请求不存在或已失效", 404)
            self._remove(authorization_id, grant)
            return True
