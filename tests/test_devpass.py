"""DevPass usage API parsing, request and error handling."""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from llm_usage.models import PlatformResult
from llm_usage.providers.devpass import DevPassProvider, _parse_usage_payload, _usage_url


def _payload() -> dict:
    return {
        "data": {
            "label": "My coding tool",
            "usage": "31.42",
            "limit": None,
            "devPlan": "pro",
            "devPlanCreditsUsed": "25",
            "devPlanCreditsLimit": "158",
            "devPlanCreditsRemaining": "133.00",
            "devPlanDailyLimit": "14.22",
            "devPlanDailyCreditsUsed": "3.10",
            "devPlanDayResetsAt": "2026-08-22T09:30:00.000Z",
            "devPlanPremiumWeeklyLimit": "18.96",
            "devPlanPremiumCreditsUsed": "5.00",
            "devPlanPremiumWeekResetsAt": "2026-08-28T12:00:00.000Z",
        }
    }


def _mock_client(
    handler: Callable[[httpx.Request], httpx.Response],
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def _fetch(client: httpx.Client, **config: object) -> PlatformResult:
    return DevPassProvider(client=client).fetch({
        "api_key": "llmgtwy_test",
        "_platform_key": "devpass",
        **config,
    })


def test_parse_monthly_daily_and_premium_weekly_windows() -> None:
    entries = _parse_usage_payload(_payload(), "devpass")
    assert [(entry.label, entry.used, entry.limit, entry.percent) for entry in entries] == [
        ("每日", 3.1, 14.22, 21.8),
        ("每周（高级模型）", 5.0, 18.96, 26.4),
        ("每月", 25.0, 158.0, 15.8),
    ]
    assert [entry.remaining for entry in entries] == pytest.approx([11.12, 13.96, 133.0])
    assert [entry.reset_at for entry in entries] == [
        "2026-08-22T09:30:00.000Z",
        "2026-08-28T12:00:00.000Z",
        None,
    ]
    assert all(entry.platform == "devpass" and entry.unit == "$" for entry in entries)


def test_parse_inactive_windows_and_zero_allowance() -> None:
    payload = _payload()
    payload["data"].update({
        "devPlanDailyCreditsUsed": "0.00",
        "devPlanDayResetsAt": None,
        "devPlanPremiumCreditsUsed": "0.00",
        "devPlanPremiumWeekResetsAt": None,
        "devPlanPremiumWeeklyLimit": "0",
    })
    entries = _parse_usage_payload(payload, "devpass")
    assert entries[0].percent == 0.0
    assert entries[1].percent is None
    assert [entry.reset_at for entry in entries] == [None, None, None]


@pytest.mark.parametrize("value", [None, True, "", "invalid", "NaN", "Infinity", -1])
def test_parse_rejects_invalid_decimal_values(value: object) -> None:
    payload = _payload()
    payload["data"]["devPlanDailyCreditsUsed"] = value
    entries = _parse_usage_payload(payload, "devpass")
    assert [entry.label for entry in entries] == ["每周（高级模型）", "每月"]
    assert _parse_usage_payload({}, "devpass") == []


@pytest.mark.parametrize(("base_url", "expected"), [
    ("https://api.llmgateway.io", "https://api.llmgateway.io/v1/key"),
    ("https://api.llmgateway.io/", "https://api.llmgateway.io/v1/key"),
    ("https://api.llmgateway.io/v1", "https://api.llmgateway.io/v1/key"),
    ("https://api.llmgateway.io/v1/key", "https://api.llmgateway.io/v1/key"),
])
def test_usage_url_normalization(base_url: str, expected: str) -> None:
    assert _usage_url(base_url) == expected


def test_fetch_uses_regular_gateway_bearer_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == "https://api.llmgateway.io/v1/key"
        assert request.headers["Authorization"] == "Bearer llmgtwy_test"
        return httpx.Response(200, json=_payload())

    result = _fetch(_mock_client(handler), display_name="My DevPass")
    assert result.ok and result.display_name == "My DevPass"
    assert len(result.entries) == 3


@pytest.mark.parametrize(("status", "message"), [
    (401, "认证失败(401)"),
    (403, "认证失败(403)"),
    (429, "请求失败(HTTP 429)"),
    (500, "请求失败(HTTP 500)"),
])
def test_fetch_http_errors(status: int, message: str) -> None:
    result = _fetch(_mock_client(lambda _request: httpx.Response(status)))
    assert result.error == message


def test_fetch_rejects_pay_as_you_go_key() -> None:
    payload = _payload()
    payload["data"]["devPlan"] = "none"
    result = _fetch(_mock_client(lambda _request: httpx.Response(200, json=payload)))
    assert result.error == "无 DevPass 订阅"


def test_fetch_network_json_and_missing_usage_errors() -> None:
    def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    assert _fetch(_mock_client(timeout)).error == "网络错误"
    invalid_json = _mock_client(lambda _request: httpx.Response(200, content=b"not-json"))
    assert _fetch(invalid_json).error == "响应不是有效 JSON"
    missing = _mock_client(lambda _request: httpx.Response(200, json={"data": {}}))
    assert _fetch(missing).error == "响应中未找到用量数据"


def test_fetch_requires_api_key() -> None:
    assert DevPassProvider().fetch({}).error == "未配置"
