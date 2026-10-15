"""Device login lifecycle tests use only a mocked official HTTP service."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from llm_usage.opendesign_auth import (
    DEVICE_URL,
    Authorization,
    DeviceAuthorizationError,
    DeviceAuthorizationManager,
)

CONTROL_KEY = "vela_ctrl_account_secret"
RUNTIME_KEY = "sk-amr_runtime_secret"
DEVICE_SECRET = "private-device-secret"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0
        self.wall = 1_800_000_000.0

    def advance(self, seconds: float) -> None:
        self.now += seconds
        self.wall += seconds


def start_body(clock: Clock, **changes: Any) -> dict[str, Any]:
    return {
        "deviceId": "device-1", "deviceSecret": DEVICE_SECRET,
        "userCode": "ABCD-EFGH", "activationUrl": "https://www.open-design.ai/device",
        "pollIntervalSeconds": 5,
        "expiresAt": datetime.fromtimestamp(clock.wall + 600, UTC).isoformat(),
        **changes,
    }


class Service:
    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.calls: list[httpx.Request] = []
        self.start_response = httpx.Response(201, json=start_body(clock))
        self.poll_response = httpx.Response(200, json={
            "controlKey": CONTROL_KEY, "runtimeKey": RUNTIME_KEY,
        })

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.method == "POST"
        assert str(request.url).startswith(DEVICE_URL)
        assert "Authorization" not in request.headers
        if str(request.url) == DEVICE_URL:
            assert json.loads(request.content) == {
                "cliVersion": "0.1.4", "host": "llm-usage", "profile": "prod",
            }
            return self.start_response
        assert str(request.url) == f"{DEVICE_URL}/device-1/token"
        assert json.loads(request.content) == {"deviceSecret": DEVICE_SECRET}
        return self.poll_response


@pytest.fixture
def setup():
    clock = Clock()
    service = Service(clock)
    with httpx.Client(transport=httpx.MockTransport(service)) as client:
        manager = DeviceAuthorizationManager(
            client, clock=lambda: clock.now, wall_clock=lambda: clock.wall,
        )
        yield manager, service, clock, client


def saved(authorization: Authorization) -> dict[str, Any]:
    assert authorization.control_key == CONTROL_KEY
    return {"slot_index": 0, "platform": {"control_key": "vela…et"}}


class TestDeviceAuthorization:
    def test_start_authorize_replay_and_secret_cleanup(self, setup) -> None:
        manager, service, clock, client = setup
        pending = manager.start("alice", "opendesign#2", 1, "设计", "snapshot")
        assert pending["status"] == "pending"
        assert pending["activation_url"] == "https://www.open-design.ai/device"
        assert pending["user_code"] == "ABCD-EFGH"
        assert pending["poll_interval_seconds"] == 5
        assert pending["id"] != "device-1" and len(pending["id"]) > 32
        seen = []

        def save(auth):
            seen.append(auth)
            assert (auth.platform, auth.index, auth.name, auth.fingerprint) == (
                "opendesign#2", 1, "设计", "snapshot",
            )
            return saved(auth)

        result = manager.poll("alice", pending["id"], save)
        assert result["status"] == "authorized" and result["slot_index"] == 0
        replay = manager.poll("alice", pending["id"], save)
        assert replay == result and len(seen) == 1 and len(service.calls) == 2
        result["platform"]["control_key"] = "mutated"
        assert manager.poll("alice", pending["id"], save)["platform"]["control_key"] == "vela…et"
        public = json.dumps([pending, replay])
        for secret in (CONTROL_KEY, RUNTIME_KEY, DEVICE_SECRET, "device-1"):
            assert secret not in public and secret not in repr(manager._grants)
        grant = manager._grants[pending["id"]]
        assert grant.device_id == grant.device_secret == "" and grant.control_key is None
        assert CONTROL_KEY not in repr(seen)
        assert not client.is_closed

    def test_owner_isolation_cancel_and_replay(self, setup) -> None:
        manager, service, *_ = setup
        token = manager.start("alice", "opendesign")["id"]
        for operation in (
            lambda: manager.poll("bob", token, saved),
            lambda: manager.cancel("bob", token),
        ):
            with pytest.raises(DeviceAuthorizationError) as error:
                operation()
            assert error.value.status_code == 404
        assert len(service.calls) == 1
        assert manager.cancel("alice", token)
        with pytest.raises(DeviceAuthorizationError) as error:
            manager.poll("alice", token, saved)
        assert error.value.status_code == 404

    @pytest.mark.parametrize("body,status", [
        ({"status": "pending"}, 200), ({}, 202),
        ({"error": "authorization_pending"}, 400),
    ])
    def test_pending_obeys_server_interval(self, setup, body, status) -> None:
        manager, service, clock, _ = setup
        service.poll_response = httpx.Response(status, json=body)
        token = manager.start("alice", "opendesign")["id"]
        assert manager.poll("alice", token, saved)["status"] == "pending"
        assert manager.poll("alice", token, saved)["retry_after"] == 5
        assert len(service.calls) == 2
        clock.advance(4)
        assert manager.poll("alice", token, saved)["retry_after"] == 1
        assert len(service.calls) == 2
        service.poll_response = httpx.Response(200, json={"controlKey": CONTROL_KEY})
        clock.advance(1)
        assert manager.poll("alice", token, saved)["status"] == "authorized"
        assert len(service.calls) == 3

    def test_pending_empty_body_and_slow_down(self, setup) -> None:
        manager, service, clock, _ = setup
        token = manager.start("alice", "opendesign")["id"]
        service.poll_response = httpx.Response(202)
        assert manager.poll("alice", token, saved)["status"] == "pending"
        clock.advance(5)
        service.poll_response = httpx.Response(400, json={"error": {"code": "slow_down"}})
        pending = manager.poll("alice", token, saved)
        assert pending["poll_interval_seconds"] == pending["retry_after"] == 10

    @pytest.mark.parametrize("status", [429, 500, 503])
    def test_transient_http_errors_keep_grant_and_sanitize(self, setup, status, caplog) -> None:
        manager, service, clock, _ = setup
        token = manager.start("alice", "opendesign")["id"]
        service.poll_response = httpx.Response(status, text=CONTROL_KEY)
        with pytest.raises(DeviceAuthorizationError) as error:
            manager.poll("alice", token, saved)
        assert error.value.status_code == 502
        assert CONTROL_KEY not in str(error.value) + caplog.text
        assert manager.poll("alice", token, saved)["status"] == "pending"
        clock.advance(5)
        service.poll_response = httpx.Response(200, json={"controlKey": CONTROL_KEY})
        assert manager.poll("alice", token, saved)["status"] == "authorized"

    def test_network_error_does_not_leak_secrets(self, setup, caplog) -> None:
        manager, service, clock, client = setup
        token = manager.start("alice", "opendesign")["id"]

        def fail(request):
            raise httpx.ReadTimeout(DEVICE_SECRET + CONTROL_KEY, request=request)

        manager._client = httpx.Client(transport=httpx.MockTransport(fail))
        with manager._client:
            with pytest.raises(DeviceAuthorizationError) as error:
                manager.poll("alice", token, saved)
            for secret in (DEVICE_SECRET, CONTROL_KEY):
                assert secret not in str(error.value) + caplog.text
        manager._client = client
        clock.advance(5)
        assert manager.poll("alice", token, saved)["status"] == "authorized"

    @pytest.mark.parametrize("status,body,expected", [
        (401, {}, "expired"), (404, {}, "expired"), (410, {}, "expired"),
        (403, {}, "denied"), (400, {"error": "access_denied"}, "denied"),
        (400, {"error": "expired_token"}, "expired"),
        (200, {"status": "cancelled"}, "cancelled"),
    ])
    def test_upstream_terminal_states_clear_grant(self, setup, status, body, expected) -> None:
        manager, service, *_ = setup
        token = manager.start("alice", "opendesign")["id"]
        grant = manager._grants[token]
        service.poll_response = httpx.Response(status, json=body)
        assert manager.poll("alice", token, saved)["status"] == expected
        assert token not in manager._grants
        assert grant.control_key is None and grant.device_secret == ""

    def test_expiry_uses_upstream_deadline_and_local_cap(self, setup) -> None:
        manager, service, clock, _ = setup
        service.start_response = httpx.Response(201, json=start_body(clock, expiresAt=(
            datetime.fromtimestamp(clock.wall + 10, UTC).isoformat()
        )))
        token = manager.start("alice", "opendesign")["id"]
        clock.advance(10)
        assert manager.poll("alice", token, saved)["status"] == "expired"
        assert len(service.calls) == 1
        service.start_response = httpx.Response(201, json=start_body(clock, expiresAt=(
            datetime.fromtimestamp(clock.wall + 86400, UTC).isoformat()
        )))
        pending = manager.start("alice", "opendesign")
        public_expiry = datetime.fromisoformat(pending["expires_at"].replace("Z", "+00:00"))
        assert public_expiry.timestamp() == clock.wall + 900
        clock.advance(900)
        assert manager.poll("alice", pending["id"], saved)["status"] == "expired"

    @pytest.mark.parametrize("change", [
        {"activationUrl": "https://open-design.ai.evil.example/device"},
        {"activationUrl": "https://evilopen-design.ai/device"},
        {"activationUrl": "https://open-design.ai@evil.example/device"},
        {"activationUrl": "https://user:pass@open-design.ai/device"},
        {"activationUrl": "https://open-design.ai:443/device"},
        {"activationUrl": "http://open-design.ai/device"},
        {"activationUrl": "https://foo .open-design.ai/device"},
        {"activationUrl": "https://open-design.ai\\@evil.example/device"},
        {"deviceId": "../token"}, {"deviceSecret": ""}, {"userCode": None},
        {"pollIntervalSeconds": 0}, {"pollIntervalSeconds": True},
        {"pollIntervalSeconds": 500}, {"expiresAt": "not a date"},
        {"expiresAt": "2027-01-01T00:00:00"},
    ])
    def test_invalid_start_data_is_not_retained(self, setup, change) -> None:
        manager, service, clock, _ = setup
        service.start_response = httpx.Response(201, json=start_body(clock, **change))
        with pytest.raises(DeviceAuthorizationError) as error:
            manager.start("alice", "opendesign")
        assert error.value.status_code == 502 and not manager._grants
        assert not manager._starting

    @pytest.mark.parametrize("response", [
        httpx.Response(200, text=DEVICE_SECRET),
        httpx.Response(200, json=[]),
        httpx.Response(503, text=DEVICE_SECRET),
        httpx.Response(307, headers={"location": "https://evil.example"}),
    ])
    def test_start_failures_are_sanitized_and_release_capacity(self, setup, response) -> None:
        manager, service, *_ = setup
        service.start_response = response
        with pytest.raises(DeviceAuthorizationError) as error:
            manager.start("alice", "opendesign")
        assert DEVICE_SECRET not in str(error.value)
        assert len(service.calls) == 1 and not manager._starting and not manager._grants

    @pytest.mark.parametrize("body", [
        {"runtimeKey": RUNTIME_KEY}, {"controlKey": RUNTIME_KEY},
        {"controlKey": "vela_ctrl_"}, {"controlKey": "vela_ctrl_bad\nkey"},
        {"controlKey": "vela_ctrl_bad key"}, {"controlKey": 1},
    ])
    def test_missing_invalid_and_runtime_keys_never_reach_save(self, setup, body) -> None:
        manager, service, *_ = setup
        token = manager.start("alice", "opendesign")["id"]
        service.poll_response = httpx.Response(200, json=body)
        with pytest.raises(DeviceAuthorizationError) as error:
            manager.poll("alice", token, lambda auth: pytest.fail("must not save invalid key"))
        assert error.value.status_code == 502 and token not in manager._grants
        assert RUNTIME_KEY not in str(error.value)

    def test_control_key_under_user_is_supported(self, setup) -> None:
        manager, service, *_ = setup
        token = manager.start("alice", "opendesign")["id"]
        service.poll_response = httpx.Response(200, json={"user": {"controlKey": CONTROL_KEY}})
        assert manager.poll("alice", token, saved)["status"] == "authorized"

    def test_save_failure_retries_without_redeeming_token_twice(self, setup) -> None:
        manager, service, *_ = setup
        token = manager.start("alice", "opendesign")["id"]

        def fail(auth):
            raise ValueError("config changed")

        with pytest.raises(ValueError, match="config changed"):
            manager.poll("alice", token, fail)
        grant = manager._grants[token]
        assert grant.device_secret == "" and grant.control_key == CONTROL_KEY
        assert manager.poll("alice", token, saved)["status"] == "authorized"
        assert len(service.calls) == 2

    def test_simultaneous_polls_save_once(self, setup) -> None:
        manager, service, *_ = setup
        token = manager.start("alice", "opendesign")["id"]
        barrier = threading.Barrier(8)
        writes = []

        def poll():
            barrier.wait(timeout=5)

            def save(auth):
                writes.append(auth.control_key)
                return saved(auth)

            return manager.poll("alice", token, save)

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(lambda _: poll(), range(8)))
        assert len(writes) == 1 and len(service.calls) == 2
        assert all(result["status"] == "authorized" for result in results)

    def test_user_capacity_cancel_and_completed_eviction(self, setup) -> None:
        manager, service, *_ = setup
        tokens = [manager.start("alice", "opendesign")["id"] for _ in range(4)]
        with pytest.raises(DeviceAuthorizationError) as error:
            manager.start("alice", "opendesign")
        assert error.value.status_code == 429 and len(service.calls) == 4
        manager.poll("alice", tokens[0], saved)
        # The completed replay is retained while capacity allows it.
        bob = manager.start("bob", "opendesign")["id"]
        assert manager.poll("alice", tokens[0], saved)["status"] == "authorized"
        manager.start("alice", "opendesign")
        assert tokens[0] not in manager._grants
        manager.cancel("alice", tokens[1])
        manager.start("alice", "opendesign")
        assert bob in manager._grants

    def test_global_capacity_and_expiry_release(self, setup) -> None:
        manager, service, clock, _ = setup
        for user in range(8):
            for _ in range(4):
                manager.start(f"user{user}", "opendesign")
        with pytest.raises(DeviceAuthorizationError) as error:
            manager.start("another", "opendesign")
        assert error.value.status_code == 429 and len(service.calls) == 32
        clock.advance(600)
        service.start_response = httpx.Response(201, json=start_body(clock))
        manager.start("another", "opendesign")
        assert len(manager._grants) == 1

    def test_simultaneous_starts_reserve_capacity_before_network(self, setup) -> None:
        manager, service, clock, client = setup
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        request_count = 0

        def handler(request):
            nonlocal request_count
            with lock:
                request_count += 1
            barrier.wait(timeout=5)
            return httpx.Response(201, json=start_body(clock))

        manager._client = httpx.Client(transport=httpx.MockTransport(handler))
        with manager._client:
            with ThreadPoolExecutor(max_workers=8) as pool:
                def start(_):
                    try:
                        return manager.start("alice", "opendesign")["status"]
                    except DeviceAuthorizationError as exc:
                        return exc.status_code

                results = list(pool.map(start, range(8)))
        assert results.count("pending") == 4 and results.count(429) == 4
        assert request_count == 4 and len(manager._grants) == 4
        manager._client = client
