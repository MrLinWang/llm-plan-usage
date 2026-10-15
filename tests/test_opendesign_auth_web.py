"""Browser authorization routes persist only the caller's OpenDesign account."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from llm_usage import store  # noqa: E402
from llm_usage.config import load_config, save_config, update_platform_config  # noqa: E402
from llm_usage.opendesign_auth import DEVICE_URL, DeviceAuthorizationManager  # noqa: E402
from llm_usage.web import create_app  # noqa: E402

CONTROL_KEY = "vela_ctrl_authorized_private"
DEVICE_SECRET = "mock-device-secret-private"
RUNTIME_KEY = "sk-amr-runtime-private"
AUTH_URL = "/api/opendesign/auth"


class _Upstream:
    """The actual manager talks to this HTTP transport, never the real service."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.requests: list[httpx.Request] = []
        self.status = "authorized"
        self.poll_barrier: threading.Barrier | None = None
        self.on_token: Callable[[], None] | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        assert request.url.host == "amr-api.open-design.ai"
        assert request.method == "POST"
        assert "authorization" not in request.headers
        assert RUNTIME_KEY.encode() not in request.content
        assert CONTROL_KEY.encode() not in request.content
        if str(request.url) == DEVICE_URL:
            assert json.loads(request.content) == {
                "cliVersion": "0.1.4", "host": "llm-usage", "profile": "prod",
            }
            return httpx.Response(201, json={
                "deviceId": "device-1", "deviceSecret": DEVICE_SECRET,
                "userCode": "ABCD-EFGH",
                "activationUrl": "https://www.open-design.ai/device",
                "pollIntervalSeconds": 5,
                "expiresAt": datetime.fromtimestamp(self.now + 600, UTC).isoformat(),
            })
        assert str(request.url) == f"{DEVICE_URL}/device-1/token"
        assert json.loads(request.content) == {"deviceSecret": DEVICE_SECRET}
        if self.on_token is not None:
            self.on_token()
        if self.poll_barrier is not None:
            self.poll_barrier.wait(timeout=5)
        if self.status == "pending":
            return httpx.Response(202, json={})
        if self.status == "denied":
            return httpx.Response(403, json={"error": "access_denied"})
        return httpx.Response(200, json={
            "controlKey": CONTROL_KEY, "runtimeKey": "sk-amr-new-not-for-saving",
        })


@pytest.fixture
def auth_client_factory(
    tmp_config_path: Path, tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setattr("llm_usage.web.fetch_all", lambda _: [])
    clients: list[TestClient] = []
    transports: list[httpx.Client] = []

    def make(
        config: dict[str, Any] | None = None, *, admin: bool = True,
    ) -> tuple[TestClient, _Upstream]:
        config = config if config is not None else {"platforms": {}}
        save_config(config, tmp_config_path)
        app = create_app(config)
        upstream = _Upstream()
        transport = httpx.Client(transport=httpx.MockTransport(upstream))
        transports.append(transport)
        app.state.opendesign_auth = DeviceAuthorizationManager(
            transport, clock=lambda: upstream.now, wall_clock=lambda: upstream.now,
        )
        username = "admin" if admin else "alice"
        store.create_user(username, "secret1", is_admin=admin)
        client = TestClient(app)
        clients.append(client)
        assert client.post("/api/auth/login", json={
            "username": username, "password": "secret1",
        }).status_code == 200
        if not admin:
            store.set_user_config(username, config)
        return client, upstream

    yield make
    for client in clients:
        client.close()
    for transport in transports:
        transport.close()


def _start(client: TestClient, **target: Any) -> str:
    response = client.post(AUTH_URL, json={"platform": "opendesign", **target})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["status"] == "pending"
    assert result["activation_url"] == "https://www.open-design.ai/device"
    assert result["user_code"] == "ABCD-EFGH"
    assert result["poll_interval_seconds"] == 5
    _assert_private(response)
    return str(result["id"])


def _assert_private(response: httpx.Response) -> None:
    for secret in (CONTROL_KEY, DEVICE_SECRET, RUNTIME_KEY, "sk-amr-new-not-for-saving"):
        assert secret not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("admin", [True, False])
def test_authorization_saves_account_without_overwriting_runtime_or_other_fields(
    auth_client_factory: Callable[..., Any], admin: bool,
) -> None:
    config = {"interval": 123, "platforms": {
        "opendesign": {
            "enabled": False, "display_name": "设计账户", "api_key": RUNTIME_KEY,
            "base_url": "https://amr-link.open-design.ai/v1", "workspace_id": "ws-1",
            "credentials": [{"name": "原套餐", "control_key": "vela_ctrl_old_private"}],
        },
        "kimi": {"api_key": "unrelated-secret", "enabled": False},
    }}
    client, upstream = auth_client_factory(config, admin=admin)
    read_config = load_config if admin else lambda: store.get_user_config("alice")
    initial = read_config()
    token = _start(client, index=0, name="网页登录账户")
    assert read_config() == initial
    response = client.post(f"{AUTH_URL}/{token}", json={})
    assert response.status_code == 200, response.text
    _assert_private(response)
    result = response.json()
    assert result["status"] == "authorized"
    assert result["slot_index"] == 0
    assert result["platform"]["credential_slots"][0]["credentials"]["control_key"]["set"]
    saved = read_config()
    expected = initial["platforms"]["opendesign"].copy()
    expected["credentials"] = [{"name": "网页登录账户", "control_key": CONTROL_KEY}]
    assert saved["platforms"]["opendesign"] == expected
    assert saved["platforms"]["kimi"] == config["platforms"]["kimi"]
    assert saved["interval"] == 123
    if not admin:
        assert load_config() == config
    request_count = len(upstream.requests)
    replay = client.post(f"{AUTH_URL}/{token}", json={})
    assert replay.status_code == 200
    assert replay.json() == result
    assert len(upstream.requests) == request_count
    assert read_config() == saved


@pytest.mark.parametrize("legacy", [True, False])
def test_append_preserves_all_existing_accounts(
    auth_client_factory: Callable[..., Any], legacy: bool,
) -> None:
    old_slot = {"name": "套餐1", "control_key": "vela_ctrl_old_private"}
    section: dict[str, Any] = {"api_key": RUNTIME_KEY}
    if legacy:
        section["control_key"] = old_slot["control_key"]
    else:
        section["credentials"] = [old_slot]
    client, _ = auth_client_factory({"platforms": {"opendesign": section}})
    token = _start(client, name="另一个账户")
    response = client.post(f"{AUTH_URL}/{token}", json={})
    assert response.status_code == 200, response.text
    assert response.json()["slot_index"] == 1
    saved = load_config()["platforms"]["opendesign"]
    assert saved["credentials"] == [
        old_slot, {"name": "另一个账户", "control_key": CONTROL_KEY},
    ]
    assert "control_key" not in saved
    assert saved["api_key"] == RUNTIME_KEY


def test_unconfigured_builtin_can_be_connected(auth_client_factory: Callable[..., Any]) -> None:
    client, _ = auth_client_factory()
    token = _start(client)
    response = client.post(f"{AUTH_URL}/{token}", json={})
    assert response.status_code == 200, response.text
    assert response.json()["slot_index"] == 0
    section = load_config()["platforms"]["opendesign"]
    assert section.get("enabled", True) is True
    assert section["credentials"][0]["control_key"] == CONTROL_KEY


def test_instance_connection_changes_only_that_instance(
    auth_client_factory: Callable[..., Any],
) -> None:
    initial = {"platforms": {
        "opendesign": {"control_key": "vela_ctrl_other_account"},
        "opendesign#2": {"enabled": False, "display_name": "第二个设计账户"},
    }}
    client, _ = auth_client_factory(initial)
    token = _start(client, platform="opendesign#2")
    response = client.post(f"{AUTH_URL}/{token}", json={})
    assert response.status_code == 200
    assert response.json()["platform"]["key"] == "opendesign#2"
    saved = load_config()["platforms"]
    assert saved["opendesign"] == initial["platforms"]["opendesign"]
    assert saved["opendesign#2"]["enabled"] is False


def test_pending_cancel_and_expiry_do_not_save(auth_client_factory: Callable[..., Any]) -> None:
    client, upstream = auth_client_factory()
    upstream.status = "pending"
    initial = load_config()
    token = _start(client)
    assert client.post(f"{AUTH_URL}/{token}", json={}).json()["status"] == "pending"
    cancelled = client.request("DELETE", f"{AUTH_URL}/{token}", json={})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert client.post(f"{AUTH_URL}/{token}", json={}).status_code == 404
    token = _start(client)
    upstream.now += 601
    response = client.post(f"{AUTH_URL}/{token}", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "expired"
    assert load_config() == initial


def test_denied_authorization_never_saves(auth_client_factory: Callable[..., Any]) -> None:
    client, upstream = auth_client_factory()
    upstream.status = "denied"
    token = _start(client)
    response = client.post(f"{AUTH_URL}/{token}", json={})
    assert response.status_code == 200
    assert response.json()["status"] == "denied"
    assert load_config() == {"platforms": {}}


def test_other_user_cannot_poll_or_cancel_grant(auth_client_factory: Callable[..., Any]) -> None:
    client, upstream = auth_client_factory()
    token = _start(client)
    store.create_user("alice", "secret1", is_admin=False)
    other = TestClient(client.app)
    try:
        assert other.post("/api/auth/login", json={
            "username": "alice", "password": "secret1",
        }).status_code == 200
        count = len(upstream.requests)
        for method in ("POST", "DELETE"):
            response = other.request(method, f"{AUTH_URL}/{token}", json={})
            assert response.status_code == 404
            _assert_private(response)
        assert len(upstream.requests) == count
        assert client.post(f"{AUTH_URL}/{token}", json={}).json()["status"] == "authorized"
    finally:
        other.close()


def test_routes_require_session_and_json(auth_client_factory: Callable[..., Any]) -> None:
    client, upstream = auth_client_factory()
    public = TestClient(client.app)
    try:
        for method, path in (("POST", AUTH_URL), ("POST", f"{AUTH_URL}/missing"),
                             ("DELETE", f"{AUTH_URL}/missing")):
            assert public.request(method, path, json={}).status_code == 401
        count = len(upstream.requests)
        assert client.post(AUTH_URL, data={"platform": "opendesign"}).status_code == 422
        assert len(upstream.requests) == count
    finally:
        public.close()


@pytest.mark.parametrize(("body", "status"), [
    ({}, 400), ({"platform": "kimi"}, 400), ({"platform": "unknown"}, 404),
    ({"platform": "opendesign#2"}, 404), ({"platform": "opendesign", "index": True}, 400),
    ({"platform": "opendesign", "index": -1}, 400),
    ({"platform": "opendesign", "index": 0}, 400),
    ({"platform": "opendesign", "name": 3}, 400),
    ({"platform": "opendesign", "name": "x" * 101}, 400),
    ({"platform": "opendesign", "control_key": CONTROL_KEY}, 400),
])
def test_invalid_target_rejected_before_network(
    auth_client_factory: Callable[..., Any], body: dict[str, Any], status: int,
) -> None:
    client, upstream = auth_client_factory()
    response = client.post(AUTH_URL, json=body)
    assert response.status_code == status, response.text
    assert not upstream.requests
    _assert_private(response)


def test_credentials_changed_during_login_are_not_overwritten(
    auth_client_factory: Callable[..., Any],
) -> None:
    client, _ = auth_client_factory({"platforms": {"opendesign": {
        "control_key": "vela_ctrl_old_private",
    }}})
    token = _start(client, index=0)
    update_platform_config("opendesign", {"control_key": "vela_ctrl_manual_change"})
    expected = load_config()
    response = client.post(f"{AUTH_URL}/{token}", json={})
    assert response.status_code == 409, response.text
    assert load_config() == expected
    _assert_private(response)


def test_unrelated_changes_during_login_are_preserved(
    auth_client_factory: Callable[..., Any],
) -> None:
    client, _ = auth_client_factory({"platforms": {"opendesign": {
        "control_key": "vela_ctrl_old_private",
    }}})
    token = _start(client, index=0)
    update_platform_config("opendesign", {"display_name": "新名称", "enabled": False})
    update_platform_config("kimi", {"enabled": False, "api_key": "unrelated"})
    response = client.post(f"{AUTH_URL}/{token}", json={})
    assert response.status_code == 200, response.text
    saved = load_config()["platforms"]
    assert saved["opendesign"]["display_name"] == "新名称"
    assert saved["opendesign"]["enabled"] is False
    assert saved["kimi"] == {"enabled": False, "api_key": "unrelated"}


def test_regular_user_policy_rechecked_before_authorization_save(
    auth_client_factory: Callable[..., Any],
) -> None:
    client, upstream = auth_client_factory(admin=False)
    token = _start(client)
    initial = store.get_user_config("alice")
    count = len(upstream.requests)
    store.set_setting("allow_user_providers", "0")
    for method, path, body in (
        ("POST", AUTH_URL, {"platform": "opendesign"}),
        ("POST", f"{AUTH_URL}/{token}", {}),
        ("DELETE", f"{AUTH_URL}/{token}", {}),
    ):
        assert client.request(method, path, json=body).status_code == 403
    assert len(upstream.requests) == count
    assert store.get_user_config("alice") == initial


def test_unknown_poll_fields_cannot_trigger_authorization_save(
    auth_client_factory: Callable[..., Any],
) -> None:
    client, upstream = auth_client_factory()
    token = _start(client)
    count = len(upstream.requests)
    response = client.post(f"{AUTH_URL}/{token}", json={"platform": "opendesign#2"})
    assert response.status_code == 400
    assert len(upstream.requests) == count
    assert load_config() == {"platforms": {}}


def test_concurrent_regular_user_authorizations_preserve_both_instances(
    auth_client_factory: Callable[..., Any],
) -> None:
    initial = {"platforms": {"opendesign": {}, "opendesign#2": {"enabled": False}}}
    client, upstream = auth_client_factory(initial, admin=False)
    first = _start(client)
    second = _start(client, platform="opendesign#2")
    other_session = TestClient(client.app)
    try:
        assert other_session.post("/api/auth/login", json={
            "username": "alice", "password": "secret1",
        }).status_code == 200
        upstream.poll_barrier = threading.Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(client.post, f"{AUTH_URL}/{first}", json={}),
                pool.submit(other_session.post, f"{AUTH_URL}/{second}", json={}),
            ]
            responses = [future.result(timeout=10) for future in futures]
        for response in responses:
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "authorized"
        saved = store.get_user_config("alice")["platforms"]
        assert saved["opendesign"]["credentials"][0]["control_key"] == CONTROL_KEY
        assert saved["opendesign#2"]["credentials"][0]["control_key"] == CONTROL_KEY
        assert saved["opendesign#2"]["enabled"] is False
        assert load_config() == initial
    finally:
        other_session.close()


def test_recreated_username_cannot_complete_deleted_users_authorization(
    auth_client_factory: Callable[..., Any],
) -> None:
    client, upstream = auth_client_factory(admin=False)
    token = _start(client)
    store.delete_user("alice")
    store.create_user("alice", "different-pass", is_admin=False)
    replacement = TestClient(client.app)
    try:
        assert replacement.post("/api/auth/login", json={
            "username": "alice", "password": "different-pass",
        }).status_code == 200
        count = len(upstream.requests)
        response = replacement.post(f"{AUTH_URL}/{token}", json={})
        assert response.status_code == 404
        assert len(upstream.requests) == count
        assert store.get_user_config("alice") == {}
    finally:
        replacement.close()


@pytest.mark.parametrize(("change", "status"), [("policy", 403), ("delete", 401)])
def test_authorization_rechecks_user_and_policy_after_upstream_request(
    auth_client_factory: Callable[..., Any], change: str, status: int,
) -> None:
    client, upstream = auth_client_factory(admin=False)
    token = _start(client)
    initial = store.get_user_config("alice")
    if change == "policy":
        upstream.on_token = lambda: store.set_setting("allow_user_providers", "0")
    else:
        upstream.on_token = lambda: store.delete_user("alice")
    response = client.post(f"{AUTH_URL}/{token}", json={})
    assert response.status_code == status, response.text
    _assert_private(response)
    assert len(upstream.requests) == 2
    assert store.get_user_config("alice") == (initial if change == "policy" else {})
    assert load_config() == {"platforms": {}}
