"""Provider tests: Kimi and Volcengine parsing via httpx.MockTransport.

Covers the verification requirements from the plan:
  - ``used = limit - remaining`` conversion (Kimi)
  - millisecond resetTime parsing (Kimi)
  - 404 fallback from /usages to /usage (Kimi)
  - volcengine OpenAPI V4 signing: GetCodingPlanUsage (percent-only) + GetAFPUsage (used/total)
  - manual providers read from config
  - error isolation (single platform failure doesn't crash)
"""

from __future__ import annotations

import json

import httpx
import pytest

from llm_usage.models import PlatformResult, UsageEntry
from llm_usage.providers import (
    PROVIDERS,
    _resolve_api_key,
    fetch_all,
)
from llm_usage.providers.kimi import (
    KimiProvider,
    _parse_usage_payload,
    _window_label,
)
from llm_usage.providers.ollama import OllamaProvider
from llm_usage.providers.opencode_go import OpenCodeGoProvider
from llm_usage.providers.volcengine import (
    VOLCENGINE_LIMITS,
    VolcengineProvider,
    _parse_coding_plan,
    _parse_agent_plan,
    _resolve_coding_limit,
)


# ---------------------------------------------------------------------------
# Kimi parsing
# ---------------------------------------------------------------------------

def _kimi_payload_5h_and_week() -> dict:
    """Kimi response with a 5h limit window and a weekly usage window."""
    return {
        "limits": [
            {
                "name": "5小时",
                "limit": 120,
                "remaining": 40,
                "resetTime": 1723968000000,  # epoch millis
            }
        ],
        "usage": {
            "limit": 9000,
            "remaining": 4500,
            "resetTime": "2026-08-25T00:00:00Z",
        },
    }


class TestKimiParsing:
    def test_parse_5h_window_used_is_limit_minus_remaining(self) -> None:
        entries = _parse_usage_payload(_kimi_payload_5h_and_week(), "kimi")
        assert len(entries) == 2
        e5h = entries[0]
        assert e5h.label == "5小时"
        assert e5h.used == 80.0  # 120 - 40
        assert e5h.limit == 120.0
        assert e5h.remaining == 40.0
        assert e5h.percent == round(80 / 120 * 100, 1)

    def test_parse_week_window(self) -> None:
        entries = _parse_usage_payload(_kimi_payload_5h_and_week(), "kimi")
        wk = entries[1]
        assert wk.label == "每周"
        assert wk.used == 4500.0  # 9000 - 4500
        assert wk.limit == 9000.0
        assert wk.percent == 50.0

    def test_millisecond_resetime_parsed_to_iso(self) -> None:
        entries = _parse_usage_payload(_kimi_payload_5h_and_week(), "kimi")
        e5h = entries[0]
        assert e5h.reset_at is not None
        assert "T" in e5h.reset_at  # ISO 8601 from epoch millis
        # weekly resetTime is a string passthrough
        assert entries[1].reset_at == "2026-08-25T00:00:00Z"

    def test_empty_payload_returns_no_entries(self) -> None:
        assert _parse_usage_payload({}, "kimi") == []

    def test_nested_detail_shape_parsed(self) -> None:
        """Live API shape: limits[] items nest numbers under ``detail``."""
        payload = {
            "usage": {
                "limit": "100",
                "used": "27",
                "remaining": "73",
                "resetTime": "2026-08-19T16:48:27.482983Z",
            },
            "limits": [
                {
                    "window": {"duration": 300, "timeUnit": "TIME_UNIT_MINUTE"},
                    "detail": {
                        "limit": "100",
                        "used": "68",
                        "remaining": "32",
                        "resetTime": "2026-08-19T10:48:27.482983Z",
                    },
                }
            ],
        }
        entries = _parse_usage_payload(payload, "kimi")
        assert len(entries) == 2
        e5h = entries[0]
        assert e5h.label == "5小时"  # derived from window duration
        assert e5h.used == 68.0  # 100 - 32, string numbers accepted
        assert e5h.limit == 100.0
        assert e5h.remaining == 32.0
        assert e5h.reset_at == "2026-08-19T10:48:27.482983Z"

    @pytest.mark.parametrize(
        ("duration", "unit", "label"),
        [
            (300, "TIME_UNIT_MINUTE", "5小时"),
            (45, "TIME_UNIT_MINUTE", "45分钟"),
            (12, "TIME_UNIT_HOUR", "12小时"),
            (1, "TIME_UNIT_DAY", "1天"),
            (7, "TIME_UNIT_DAY", "7天"),
        ],
    )
    def test_window_label_derivation(
        self, duration: int, unit: str, label: str
    ) -> None:
        assert _window_label({"duration": duration, "timeUnit": unit}) == label

    def test_window_label_unknown_unit_returns_none(self) -> None:
        assert _window_label({"duration": 5, "timeUnit": "TIME_UNIT_SECOND"}) is None
        assert _window_label(None) is None
        assert _window_label({}) is None


# ---------------------------------------------------------------------------
# Kimi HTTP via MockTransport
# ---------------------------------------------------------------------------

def _mock_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


class TestKimiHttp:
    def test_fetch_success(self) -> None:
        payload = _kimi_payload_5h_and_week()

        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/coding/v1/usages"
            assert req.headers["Authorization"] == "Bearer sk-test"
            return httpx.Response(200, json=payload)

        provider = KimiProvider(client=_mock_client(handler))
        res = provider.fetch({"api_key": "sk-test", "display_name": "Kimi Code"})
        assert res.ok
        assert len(res.entries) == 2
        assert res.entries[0].used == 80.0

    def test_fetch_404_fallback_to_usage(self) -> None:
        payload = _kimi_payload_5h_and_week()
        calls = {"count": 0}

        def handler(req: httpx.Request) -> httpx.Response:
            calls["count"] += 1
            if req.url.path == "/coding/v1/usages":
                return httpx.Response(404)
            assert req.url.path == "/coding/v1/usage"
            return httpx.Response(200, json=payload)

        provider = KimiProvider(client=_mock_client(handler))
        res = provider.fetch({"api_key": "sk-test"})
        assert res.ok
        assert calls["count"] == 2
        assert len(res.entries) == 2

    def test_fetch_401_gives_hint_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401)

        provider = KimiProvider(client=_mock_client(handler))
        res = provider.fetch({"api_key": "sk-test"})
        assert not res.ok
        assert "401" in res.error
        assert "sk-kimi" in res.error

    def test_fetch_no_key_returns_unconfigured(self) -> None:
        res = KimiProvider().fetch({})
        assert not res.ok
        assert res.error == "未配置"


# ---------------------------------------------------------------------------
# Volcengine parsing
# ---------------------------------------------------------------------------

def _coding_plan_payload() -> dict:
    """GetCodingPlanUsage response: percent-only, no used/total."""
    return {
        "Status": "Running",
        "QuotaUsage": [
            {"Level": "session", "Percent": 25.0, "ResetTimestamp": 1723968000},
            {"Level": "weekly", "Percent": 50.0, "ResetTimestamp": 1723968000},
            {"Level": "monthly", "Percent": 10.0, "ResetTimestamp": 1723968000},
        ],
    }


def _agent_plan_payload() -> dict:
    """GetAFPUsage response: AFPFiveHour/AFPWeekly/AFPMonthly with Quota/Used/ResetTime."""
    return {
        "Result": {
            "PlanType": "medium",
            "AFPFiveHour": {"Quota": 10000, "Used": 250.0, "ResetTime": 1723968000000},
            "AFPWeekly": {"Quota": 35000, "Used": 8750.0, "ResetTime": 1723968000000},
            "AFPMonthly": {"Quota": 100000, "Used": 25000.0, "ResetTime": 1723968000000},
            "AFPDaily": {"Quota": 50000, "Used": 0, "ResetTime": 1723968000000},
        },
    }


class TestVolcengineCodingParsing:
    def test_parse_coding_plan_percent_only(self) -> None:
        entries = _parse_coding_plan(_coding_plan_payload(), "volcengine-coding", "pro", None)
        assert len(entries) == 3
        labels = [e.label for e in entries]
        assert labels == ["5小时", "每周", "每月"]
        # session window: 25% of pro limit (6000) = 1500
        e = entries[0]
        assert e.percent == 25.0
        assert e.limit == 6000.0
        assert e.used == 1500.0  # derived: 25% * 6000
        assert e.remaining == 4500.0

    def test_parse_coding_plan_no_tier_shows_percent_only(self) -> None:
        entries = _parse_coding_plan(_coding_plan_payload(), "volcengine-coding", None, None)
        e = entries[0]
        assert e.percent == 25.0
        assert e.limit is None
        assert e.used == 0.0
        assert e.remaining is None

    def test_coding_limit_override_from_config(self) -> None:
        override = {"session": 999, "weekly": 9999, "monthly": 99999}
        entries = _parse_coding_plan(_coding_plan_payload(), "volcengine-coding", "lite", override)
        assert entries[0].limit == 999.0
        assert entries[0].used == round(25.0 / 100 * 999, 0)

    def test_coding_resettimestamp_seconds_to_iso(self) -> None:
        entries = _parse_coding_plan(_coding_plan_payload(), "volcengine-coding", "pro", None)
        assert entries[0].reset_at is not None
        assert "T" in entries[0].reset_at

    def test_resolve_coding_limit_pro_values(self) -> None:
        assert _resolve_coding_limit("session", "pro", None) == 6000
        assert _resolve_coding_limit("weekly", "pro", None) == 45000
        assert _resolve_coding_limit("monthly", "pro", None) == 90000
        assert _resolve_coding_limit("session", "lite", None) == 1200


class TestVolcengineAgentParsing:
    def test_parse_agent_plan_used_quota(self) -> None:
        entries = _parse_agent_plan(_agent_plan_payload(), "volcengine-agent", "medium")
        assert len(entries) == 3  # 5h + weekly + monthly (daily skipped)
        e5h = entries[0]
        assert e5h.label == "5小时"
        assert e5h.used == 250.0
        assert e5h.limit == 10000.0  # Quota
        assert e5h.remaining == 9750.0
        assert e5h.percent == 2.5  # 250/10000*100

    def test_agent_daily_skipped(self) -> None:
        entries = _parse_agent_plan(_agent_plan_payload(), "volcengine-agent", "medium")
        labels = [e.label for e in entries]
        assert "每日" not in labels

    def test_agent_resettimestamp_ms_to_iso(self) -> None:
        entries = _parse_agent_plan(_agent_plan_payload(), "volcengine-agent", "medium")
        assert entries[0].reset_at is not None
        assert "T" in entries[0].reset_at


class TestVolcengineHttp:
    def _openapi_handler(self, action: str, coding_data: dict, agent_data: dict):
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.params.get("Action") == action
            assert req.url.params.get("Version") == "2024-01-01"
            # V4 signing headers present
            assert "Authorization" in req.headers
            assert req.headers["Authorization"].startswith("HMAC-SHA256")
            assert "X-Date" in req.headers
            if "GetCodingPlanUsage" in action:
                return httpx.Response(200, json={"Result": coding_data})
            return httpx.Response(200, json=agent_data)
        return handler

    def test_coding_plan_fetch(self) -> None:
        handler = self._openapi_handler(
            "GetCodingPlanUsage", _coding_plan_payload(), _agent_plan_payload()
        )
        provider = VolcengineProvider(client=_mock_client(handler))
        res = provider.fetch({
            "access_key": "AKLT-test",
            "secret_key": "sk-test",
            "plan_type": "coding",
            "tier": "pro",
            "_platform_key": "volcengine-coding",
            "display_name": "火山方舟 Coding Plan",
        })
        assert res.ok
        assert len(res.entries) == 3
        assert res.entries[0].percent == 25.0
        assert res.entries[0].limit == 6000.0

    def test_agent_plan_fetch(self) -> None:
        handler = self._openapi_handler(
            "GetAFPUsage", _coding_plan_payload(), _agent_plan_payload()
        )
        provider = VolcengineProvider(client=_mock_client(handler))
        res = provider.fetch({
            "access_key": "AKLT-test",
            "secret_key": "sk-test",
            "plan_type": "agent",
            "tier": "medium",
            "_platform_key": "volcengine-agent",
            "display_name": "火山方舟 Agent Plan",
        })
        assert res.ok
        assert len(res.entries) == 3
        assert res.entries[0].used == 250.0
        assert res.entries[0].limit == 10000.0  # Quota from AFPFiveHour

    def test_no_aksk_returns_unconfigured(self) -> None:
        res = VolcengineProvider().fetch({})
        assert not res.ok
        assert "AK/SK" in res.error

    def test_openapi_error_response(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={
                "ResponseMetadata": {"Error": {"Code": "AccessDenied", "Message": "权限不足"}}
            })
        provider = VolcengineProvider(client=_mock_client(handler))
        res = provider.fetch({
            "access_key": "AKLT-test", "secret_key": "sk-test",
            "plan_type": "coding", "tier": "pro",
            "_platform_key": "volcengine-coding", "display_name": "火山方舟",
        })
        assert not res.ok
        assert "权限不足" in res.error

    def test_http_error(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(500)
        provider = VolcengineProvider(client=_mock_client(handler))
        res = provider.fetch({
            "access_key": "AKLT-test", "secret_key": "sk-test",
            "plan_type": "coding", "tier": "pro",
            "_platform_key": "volcengine-coding", "display_name": "火山方舟",
        })
        assert not res.ok
        assert "500" in res.error


# ---------------------------------------------------------------------------
# Ollama Cloud (live API)
# ---------------------------------------------------------------------------

def _ollama_usage_payload() -> dict:
    """Ollama /api/usage response: usage is 0-1 float."""
    return {
        "activity": {
            "cost": "0.00000",
            "period": {"type": "last_4_weeks",
                        "starting_at": "2026-07-27T00:00:00Z",
                        "ending_at": "2026-08-19T01:44:00Z"},
            "models": [],
        },
        "limits": {
            "session": {"usage": 0.333, "models": [{"name": "glm-5.2", "request_count": 133}]},
            "weekly": {"usage": 0.136, "models": [{"name": "glm-5.2", "request_count": 504}]},
        },
    }


class TestOllamaHttp:
    def test_fetch_success(self) -> None:
        payload = _ollama_usage_payload()
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/api/usage"
            assert req.headers["Authorization"] == "Bearer test-key"
            return httpx.Response(200, json=payload)
        provider = OllamaProvider(client=_mock_client(handler))
        res = provider.fetch({"api_key": "test-key", "display_name": "Ollama Cloud",
                              "_platform_key": "ollama"})
        assert res.ok
        assert len(res.entries) == 2
        assert res.entries[0].label == "5小时"
        assert res.entries[0].percent == 33.3  # 0.333 * 100
        assert res.entries[1].label == "每周"
        assert res.entries[1].percent == 13.6  # 0.136 * 100
        assert res.entries[0].is_manual is False

    def test_fetch_401(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401)
        provider = OllamaProvider(client=_mock_client(handler))
        res = provider.fetch({"api_key": "test-key", "display_name": "Ollama Cloud"})
        assert not res.ok
        assert "401" in res.error

    def test_no_key_returns_unconfigured(self) -> None:
        res = OllamaProvider().fetch({})
        assert not res.ok
        assert res.error == "未配置"


# ---------------------------------------------------------------------------
# OpenCode Go (live API)
# ---------------------------------------------------------------------------

def _opencode_go_usage_payload() -> dict:
    """opencode.ai/zen/go/v1/usage response: percent + resetsAt per window."""
    return {
        "usage": {
            "rolling": {"status": "ok", "percent": 3, "resetsAt": "2026-08-19T04:23:00.867Z"},
            "weekly": {"status": "ok", "percent": 25, "resetsAt": "2026-08-24T00:00:00.867Z"},
            "monthly": {"status": "ok", "percent": 12, "resetsAt": "2026-09-17T17:37:53.867Z"},
        }
    }


class TestOpenCodeGoHttp:
    def test_fetch_success(self) -> None:
        payload = _opencode_go_usage_payload()
        def handler(req: httpx.Request) -> httpx.Response:
            assert req.url.path == "/zen/go/v1/usage"
            assert req.headers["Authorization"] == "Bearer test-key"
            return httpx.Response(200, json=payload)
        provider = OpenCodeGoProvider(client=_mock_client(handler))
        res = provider.fetch({"api_key": "test-key", "display_name": "OpenCode Go",
                              "_platform_key": "opencode-go"})
        assert res.ok
        assert len(res.entries) == 3
        labels = [e.label for e in res.entries]
        assert labels == ["5小时", "每周", "每月"]
        # rolling: 3% of $12 = $0.36
        e = res.entries[0]
        assert e.percent == 3.0
        assert e.limit == 12.0
        assert e.used == 0.36
        assert e.unit == "$"
        assert e.is_manual is False
        assert e.reset_at == "2026-08-19T04:23:00.867Z"

    def test_fetch_401(self) -> None:
        def handler(req: httpx.Request) -> httpx.Response:
            return httpx.Response(401)
        provider = OpenCodeGoProvider(client=_mock_client(handler))
        res = provider.fetch({"api_key": "test-key", "display_name": "OpenCode Go"})
        assert not res.ok
        assert "401" in res.error

    def test_no_key_returns_unconfigured(self) -> None:
        res = OpenCodeGoProvider().fetch({})
        assert not res.ok
        assert res.error == "未配置"


# ---------------------------------------------------------------------------
# Registry / dispatch
# ---------------------------------------------------------------------------

class TestRegistry:
    def test_all_five_providers_registered(self) -> None:
        assert set(PROVIDERS.keys()) == {
            "kimi", "volcengine-coding", "volcengine-agent", "ollama", "opencode-go"
        }

    def test_env_prefix_resolution(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TEST_KEY_VAR", "secret123")
        assert _resolve_api_key("env:TEST_KEY_VAR") == "secret123"
        assert _resolve_api_key("sk-literal") == "sk-literal"
        assert _resolve_api_key(None) is None
        assert _resolve_api_key("env:NONEXISTENT_VAR") is None

    def test_fetch_all_error_isolation(self) -> None:
        """A provider missing credentials should not break the batch."""
        # kimi without api_key → returns error, not crash
        cfg = {
            "platforms": {
                "kimi": {"enabled": True},  # no api_key → "未配置"
            }
        }
        results = fetch_all(cfg)
        assert len(results) == 1
        assert not results[0].ok  # error expected, not crash

    def test_fetch_all_disabled_platforms_skipped(self) -> None:
        cfg = {
            "platforms": {
                "kimi": {"enabled": False, "api_key": "env:KIMI_API_KEY"},
                "ollama": {"enabled": True, "api_key": "env:OLLAMA_API_KEY"},
            }
        }
        results = fetch_all(cfg)
        platforms = [r.platform for r in results]
        assert "ollama" in platforms
        assert "kimi" not in platforms

    def test_fetch_all_empty_config(self) -> None:
        assert fetch_all({}) == []

    def test_fetch_all_respects_platform_order(self) -> None:
        cfg = {
            "platforms": {
                "kimi": {"enabled": True},
                "ollama": {"enabled": True},
                "opencode-go": {"enabled": True},
            },
            "platform_order": ["opencode-go", "ollama", "kimi"],
        }
        assert [r.platform for r in fetch_all(cfg)] == ["opencode-go", "ollama", "kimi"]

    def test_fetch_all_platform_order_partial(self) -> None:
        """platform_order 只列部分 key:列出的排最前,其余保持注册表顺序。"""
        cfg = {
            "platforms": {
                "kimi": {"enabled": True},
                "ollama": {"enabled": True},
                "opencode-go": {"enabled": True},
            },
            "platform_order": ["ollama"],
        }
        assert [r.platform for r in fetch_all(cfg)] == ["ollama", "kimi", "opencode-go"]

    def test_fetch_all_platform_order_unknown_ignored(self) -> None:
        cfg = {
            "platforms": {
                "kimi": {"enabled": True},
                "ollama": {"enabled": True},
                "opencode-go": {"enabled": True},
            },
            "platform_order": ["ghost", "ollama"],
        }
        assert [r.platform for r in fetch_all(cfg)] == ["ollama", "kimi", "opencode-go"]