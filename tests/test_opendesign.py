"""OpenDesign control-plane parsing, authentication, scope and failure tests."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from llm_usage.display import results_to_dict
from llm_usage.providers import fetch_all
from llm_usage.providers.opendesign import (
    CONTROL_KEY_REQUIRED,
    OpenDesignProvider,
    _parse_usage_payload,
)

CONTROL_KEY = "vela_ctrl_test_secret"
WORKSPACE = "workspace-test"


def _payload() -> dict[str, Any]:
    # Native credit strings, UTC timestamps and null reset from the official
    # WorkspaceBillingPreflight contract; no invented credits/USD conversion.
    return {
        "workspaceId": WORKSPACE,
        "balanceUsd": "12.3456",
        "codingPlan": {
            "workspaceId": WORKSPACE,
            "eligible": True,
            "tier": "plus",
            "windows": [
                {"durationSeconds": 604800, "usedCredits": "250", "limitCredits": "1000",
                 "remainingCredits": "750", "resetsAt": None},
                {"durationSeconds": 18000, "usedCredits": "60", "limitCredits": "100",
                 "remainingCredits": "40", "resetsAt": "2026-10-16T08:00:00+08:00"},
            ],
        },
    }


def _client(payload: Any = None, *, fail_path: str = "", status: int = 200) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.headers["Authorization"] == f"Bearer {CONTROL_KEY}"
        assert request.url.host == "amr-api.open-design.ai"
        if request.url.path.endswith(fail_path) and status != 200:
            return httpx.Response(status, json={"error": f"secret {CONTROL_KEY}"})
        if request.url.path == "/api/v1/workspaces/current":
            return httpx.Response(200, json={"activeWorkspaceId": WORKSPACE})
        assert request.url.path == f"/api/v1/workspaces/{WORKSPACE}/billing/preflight"
        return httpx.Response(200, json=_payload() if payload is None else payload)
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestOpenDesign:
    @pytest.mark.parametrize("balance,maximum,percent", [
        (100, 100, 0), (25, 100, 75), (0, 100, 100), (-2, 100, 100),
        (150, 100, 0), (12.5, 50, 75), (25, 0, None), (25, None, None),
    ])
    def test_wallet_progress_uses_configured_scale_without_inventing_spend(
        self, balance, maximum, percent,
    ) -> None:
        payload = _payload()
        payload["balanceUsd"] = str(balance)
        with _client(payload) as client:
            result = OpenDesignProvider(client).fetch({
                "control_key": CONTROL_KEY, "max_balance": maximum,
            })
        assert result.ok
        wallet = result.entries[-1]
        assert (wallet.used, wallet.remaining) == (0, balance)
        assert wallet.limit == (maximum or None)
        assert wallet.percent == percent
        assert result.entries[0].percent == 60  # credit windows keep usage semantics
        assert results_to_dict([result])["platforms"][0]["entries"][-1]["percent"] == percent

    @pytest.mark.parametrize("maximum", [-1, True, "100", [], float("nan"), float("inf")])
    def test_invalid_wallet_scale_never_sends_request(self, maximum) -> None:
        def handler(request):
            pytest.fail("invalid display scales must be rejected before HTTP")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = OpenDesignProvider(client).fetch({
                "control_key": CONTROL_KEY, "max_balance": maximum,
            })
        assert result.error and result.error.startswith("配置错误")

    def test_live_shape_and_json(self) -> None:
        with _client() as client:
            result = OpenDesignProvider(client=client).fetch({
                "control_key": CONTROL_KEY,
                "api_key": "sk-amr-never-send-this",
                "base_url": "https://amr-link.open-design.ai/v1",
                "_platform_key": "opendesign#2", "display_name": "设计账户",
            })
            assert not client.is_closed  # injected client remains caller-owned
        assert result.ok and result.warning is None
        assert result.platform == "opendesign#2" and result.display_name == "设计账户"
        five_hour, weekly, wallet = result.entries
        assert [e.label for e in result.entries] == ["5小时", "每周", "余额用量"]
        assert {e.platform for e in result.entries} == {"opendesign#2"}
        assert (five_hour.used, five_hour.limit, five_hour.remaining, five_hour.percent) == (
            60, 100, 40, 60,
        )
        assert five_hour.reset_at == "2026-10-16T00:00:00Z"
        assert five_hour.unit == "credits"
        assert weekly.reset_at is None
        assert wallet.remaining == 12.3456 and wallet.unit == "$"
        assert wallet.used == 0 and wallet.limit is None and wallet.percent is None
        serialized = results_to_dict([result])
        assert serialized["platforms"][0]["entries"][2]["remaining"] == 12.3456

    @pytest.mark.parametrize("config,error", [
        ({}, "未配置 control_key"),
        ({"api_key": "sk-amr-runtime-secret"}, CONTROL_KEY_REQUIRED),
        ({"control_key": "sk-amr-runtime-secret"}, CONTROL_KEY_REQUIRED),
        ({"control_key": 42}, CONTROL_KEY_REQUIRED),
    ])
    def test_missing_or_runtime_key_never_sends_request(self, config, error) -> None:
        def handler(request):
            pytest.fail("runtime/missing keys must not be sent to the control plane")
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = OpenDesignProvider(client).fetch(config)
        assert result.error == error and not result.entries

    def test_control_key_legacy_api_key_field(self) -> None:
        with _client() as client:
            result = OpenDesignProvider(client).fetch({"api_key": CONTROL_KEY})
        assert result.ok

    @pytest.mark.parametrize("base_url", [
        "https://amr-api.open-design.ai", "https://amr-api.open-design.ai/api/",
        "https://amr-api.open-design.ai/api/v1/", "https://amr-api.open-design.ai/v1",
        "https://amr-link.open-design.ai/v1/",
    ])
    def test_base_url_forms(self, base_url) -> None:
        with _client() as client:
            assert OpenDesignProvider(client).fetch({
                "control_key": CONTROL_KEY, "base_url": base_url,
            }).ok

    def test_configured_workspace_skips_current(self) -> None:
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json=_payload())
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = OpenDesignProvider(client).fetch({
                "control_key": CONTROL_KEY, "workspace_id": WORKSPACE,
            })
        assert result.ok and len(requests) == 1
        assert requests[0].url.path.endswith(f"/{WORKSPACE}/billing/preflight")

    @pytest.mark.parametrize("stage", ["current", "preflight"])
    @pytest.mark.parametrize("status", [401, 403, 404, 429, 503])
    def test_http_errors_are_sanitized(self, stage, status) -> None:
        with _client(fail_path=stage, status=status) as client:
            result = OpenDesignProvider(client).fetch({"control_key": CONTROL_KEY})
        expected = f"认证失败({status})" if status in (401, 403) else f"请求失败(HTTP {status})"
        assert result.error == expected and not result.entries

    @pytest.mark.parametrize("stage", ["current", "preflight"])
    def test_network_error(self, stage, caplog) -> None:
        def handler(request):
            if request.url.path.endswith(stage):
                raise httpx.ReadTimeout(f"secret {CONTROL_KEY}", request=request)
            return httpx.Response(200, json={"activeWorkspaceId": WORKSPACE})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = OpenDesignProvider(client).fetch({"control_key": CONTROL_KEY})
        assert result.error == "网络错误"
        assert CONTROL_KEY not in caplog.text

    @pytest.mark.parametrize("stage", ["current", "preflight"])
    def test_invalid_json(self, stage) -> None:
        def handler(request):
            if request.url.path.endswith(stage):
                return httpx.Response(200, text=f"not json {CONTROL_KEY}")
            return httpx.Response(200, json={"activeWorkspaceId": WORKSPACE})
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            result = OpenDesignProvider(client).fetch({"control_key": CONTROL_KEY})
        assert result.error == "响应不是有效 JSON"

    @pytest.mark.parametrize("body", [[], {}, {"activeWorkspaceId": 5}])
    def test_invalid_current_workspace(self, body) -> None:
        with httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=body),
        )) as client:
            result = OpenDesignProvider(client).fetch({"control_key": CONTROL_KEY})
        assert result.error == "响应中未找到用量数据"

    @pytest.mark.parametrize("body", [[], {}, {"workspaceId": "other", "balanceUsd": "99"}])
    def test_invalid_or_cross_workspace_payload(self, body) -> None:
        with _client(body) as client:
            result = OpenDesignProvider(client).fetch({"control_key": CONTROL_KEY})
        assert result.error == "响应中未找到用量数据"

    @pytest.mark.parametrize("balance", ["0", "-1.25", "15.5"])
    def test_wallet_only_has_no_invented_usage_or_allowance(self, balance) -> None:
        body = _payload()
        body["codingPlan"].update(eligible=False, windows=[])
        body["balanceUsd"] = balance
        entries, warning = _parse_usage_payload(body, "opendesign", WORKSPACE)
        assert warning is None and len(entries) == 1
        assert entries[0].remaining == float(balance)
        assert entries[0].limit is None and entries[0].percent is None

    @pytest.mark.parametrize("field,value", [
        ("durationSeconds", True), ("durationSeconds", 0),
        ("usedCredits", "NaN"), ("usedCredits", "inf"), ("usedCredits", -1),
        ("limitCredits", 0), ("remainingCredits", True), ("remainingCredits", "1001"),
    ])
    def test_partial_windows_have_warning(self, field, value) -> None:
        body = _payload()
        body["codingPlan"]["windows"][0][field] = value
        entries, warning = _parse_usage_payload(body, "opendesign", WORKSPACE)
        assert [e.label for e in entries] == ["5小时", "余额用量"]
        assert warning

    def test_missing_balance_keeps_window_data_with_warning(self) -> None:
        body = _payload()
        del body["balanceUsd"]
        entries, warning = _parse_usage_payload(body, "opendesign", WORKSPACE)
        assert [e.label for e in entries] == ["5小时", "每周"] and warning

    def test_registry_env_slots_instance_and_retry(self, monkeypatch) -> None:
        from llm_usage.providers import PROVIDERS
        attempts = []
        def handler(request):
            attempts.append(request)
            if len(attempts) == 1:
                return httpx.Response(503)
            if request.url.path.endswith("/current"):
                return httpx.Response(200, json={"workspaceId": WORKSPACE})
            return httpx.Response(200, json=_payload())
        monkeypatch.setenv("DESIGN_CONTROL", CONTROL_KEY)
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            monkeypatch.setitem(PROVIDERS, "opendesign", OpenDesignProvider(client))
            results = fetch_all({"platforms": {"opendesign#2": {
                "enabled": True, "max_balance": 50, "credentials": [
                    {"name": "账户A", "control_key": "env:DESIGN_CONTROL"},
                    {"name": "账户B", "control_key": "env:DESIGN_CONTROL"},
                ],
            }}}, max_workers=1)
        assert len(results) == 1 and results[0].ok
        assert {e.plan for e in results[0].entries} == {"账户A", "账户B"}
        assert all(e.platform == "opendesign#2" for e in results[0].entries)
        wallets = [e for e in results[0].entries if e.label == "余额用量"]
        assert len(wallets) == 2
        assert all((e.limit, e.percent) == (50, 75.3) for e in wallets)
        assert all(r.headers["authorization"] == f"Bearer {CONTROL_KEY}" for r in attempts)
        assert len(attempts) == 5  # one transient failure plus two successful calls per slot
