"""Web dashboard tests: FastAPI app, payload shape, TTL cache."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from llm_usage.models import PlatformResult, UsageEntry  # noqa: E402
from llm_usage.web import UsageCache, create_app  # noqa: E402


def _fake_results() -> list[PlatformResult]:
    entry = UsageEntry(
        "kimi", "5小时", 80, 120, 40, 66.7, "2099-01-01T00:00:00Z", "%", False
    )
    return [PlatformResult("kimi", "Kimi Code", entries=[entry])]


def _patch_fetch(monkeypatch: pytest.MonkeyPatch, results=None) -> list[int]:
    """Patch llm_usage.web.fetch_all with a counting fake; return [calls]."""
    calls = [0]

    def fake(_cfg):
        calls[0] += 1
        return _fake_results() if results is None else results

    monkeypatch.setattr("llm_usage.web.fetch_all", fake)
    return calls


class TestWeb:
    def test_index_returns_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "重置倒计时" in resp.text

    def test_index_has_theme_toggle(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        html = client.get("/").text
        assert 'id="theme-toggle"' in html
        assert 'html[data-theme="light"]' in html
        assert "llm-usage-theme" in html  # localStorage 持久化键

    def test_index_has_card_layout_hooks(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """页面 JS 依赖的静态挂载点:缺失时任一平台卡片都不会渲染。"""
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        html = client.get("/").text
        assert 'id="platforms"' in html    # 平台卡片网格容器
        assert 'id="entry-table"' in html  # 卡片内 entry 表格的 <template>

    def test_usage_payload_shape(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        resp = client.get("/api/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert {"fetched_at", "interval", "platforms"} <= data.keys()
        platform = data["platforms"][0]
        assert platform["name"] == "kimi"
        assert platform["display_name"] == "Kimi Code"
        assert platform["error"] is None
        assert platform["entries"][0]["percent"] == 66.7

    def test_usage_error_platform_included(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _patch_fetch(monkeypatch, results=[
            PlatformResult("kimi", "Kimi Code", error="未配置"),
        ])
        client = TestClient(create_app({"platforms": {}}))
        data = client.get("/api/usage").json()
        platform = data["platforms"][0]
        assert platform["error"] == "未配置"
        assert platform["entries"] == []

    def test_cache_within_interval(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        calls = _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}, interval=60))
        client.get("/api/usage")
        client.get("/api/usage")
        assert calls[0] == 1

    def test_cache_expires(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = _patch_fetch(monkeypatch)
        cache = UsageCache({}, 5)
        cache.get()
        cache._fetched_at -= 10  # 让缓存过期(超过 interval=5)
        cache.get()
        assert calls[0] == 2

    def test_create_app_clamps_interval(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({}, interval=1))
        data = client.get("/api/usage").json()
        assert data["interval"] == 5.0
