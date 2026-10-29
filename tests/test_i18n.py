"""Locale resolution, catalog integrity and Web page injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_usage import i18n


def test_normalize_and_resolve_locale_precedence(monkeypatch) -> None:
    env = {"LLM_USAGE_LANG": "zh-CN", "LANG": "en_US.UTF-8"}
    assert i18n.resolve_locale("en", cookie="zh-CN", accept_language="en-US", env=env) == "en"
    assert i18n.resolve_locale(None, cookie="zh-CN", accept_language="en-US", env=env) == "zh-CN"
    assert i18n.resolve_locale(None, accept_language="fr-FR,zh;q=0.9", env=env) == "zh-CN"
    assert i18n.resolve_locale(None, accept_language="fr-FR", env=env) == "en"
    assert i18n.resolve_locale(None, env=env) == "zh-CN"
    assert i18n.resolve_locale(None, env={}) == "en"


def test_t_falls_back_to_english_then_key() -> None:
    token = i18n.set_locale("en")
    try:
        assert i18n.t("nav.dashboard") == "Dashboard"
        assert i18n.t("missing.key") == "missing.key"
        assert i18n.t("provider.error.http", status=503) == "Request failed (HTTP 503)"
    finally:
        i18n.reset_locale(token)


def test_label_unit_and_message_translation() -> None:
    token = i18n.set_locale("en")
    try:
        assert i18n.translate_label("5小时") == "5 hours"
        assert i18n.translate_label("45分钟") == "45m"
        assert i18n.translate_unit("次") == "requests"
        assert i18n.translate_message("网络错误") == "Network error"
        assert i18n.translate_message("认证失败(401)") == "Authentication failed (401)"
        assert i18n.translate_message("组A：网络错误") == "组A: Network error"
    finally:
        i18n.reset_locale(token)


def test_catalog_key_parity_and_non_empty_values() -> None:
    en = i18n.catalog("en")
    zh = i18n.catalog("zh-CN")
    for section in ("keys", "source", "labels", "units", "messages"):
        assert set(en[section]) == set(zh[section]), section
        assert all(str(v).strip() for v in en[section].values())
        assert all(str(v).strip() for v in zh[section].values())


def test_every_static_page_text_node_has_a_translation() -> None:
    import re
    from html.parser import HTMLParser

    source = i18n.catalog("en")["source"]

    class P(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.skip = 0
            self.values: list[str] = []

        def handle_starttag(self, tag, attrs):  # type: ignore[no-untyped-def]
            if tag in ("script", "style"):
                self.skip += 1

        def handle_endtag(self, tag):  # type: ignore[no-untyped-def]
            if tag in ("script", "style") and self.skip:
                self.skip -= 1

        def handle_data(self, data):  # type: ignore[no-untyped-def]
            if not self.skip and re.search(r"[\u4e00-\u9fff]", data) and data.strip():
                self.values.append(data.strip())

    missing: set[str] = set()
    for path in Path("src/llm_usage/static").glob("*.html"):
        parser = P()
        parser.feed(path.read_text(encoding="utf-8"))
        missing.update(value for value in parser.values if value not in source)
    assert not missing, missing


def test_page_injection_sets_locale_catalog_and_assets() -> None:
    from llm_usage.web import inject_i18n_page

    html = Path("src/llm_usage/static/login.html").read_text(encoding="utf-8")
    en = inject_i18n_page(html, "en")
    assert '<html lang="en" data-i18n-pending' in en
    assert "window.__LLM_USAGE_LOCALE__=\"en\"" in en
    assert "window.__LLM_USAGE_I18N__" in en
    assert '/i18n.js' in en and '/i18n.css' in en
    zh = inject_i18n_page(html, "zh-CN")
    assert '<html lang="zh-CN" data-i18n-pending' in zh


def test_config_template_is_localized(tmp_path: Path) -> None:
    from llm_usage import config

    token = i18n.set_locale("en")
    try:
        p = tmp_path / "en.toml"
        config.init_config(p)
        text = p.read_text(encoding="utf-8")
        assert "llm-usage configuration" in text
        assert "每个供应商" not in text
    finally:
        i18n.reset_locale(token)
    token = i18n.set_locale("zh-CN")
    try:
        p = tmp_path / "zh.toml"
        config.init_config(p)
        assert "每个供应商" in p.read_text(encoding="utf-8")
    finally:
        i18n.reset_locale(token)


def test_changelog_english_uses_text_en() -> None:
    from llm_usage.changelog import changelog

    token = i18n.set_locale("en")
    try:
        first = changelog()[0]["entries"][0]
        assert first["text_en"] in first["text"] or first["text"] == first["text_en"]
        assert first["text"] != "仪表盘 ⋯ 菜单新增「关于」，显示当前版本号"
    finally:
        i18n.reset_locale(token)


def test_service_worker_cache_version_contains_i18n_assets() -> None:
    sw = Path("src/llm_usage/static/sw.js").read_text(encoding="utf-8")
    assert "llm-usage-v3" in sw
    assert "/i18n.js" in sw and "/i18n.css" in sw

def test_login_route_renders_injected_page() -> None:
    from llm_usage.web import create_app

    app = create_app({"platforms": {}})
    route = next(r for r in app.routes if getattr(r, "path", None) == "/login")
    token = i18n.set_locale("en")
    try:
        response = route.endpoint()
    finally:
        i18n.reset_locale(token)
    body = response.body.decode("utf-8")
    assert response.status_code == 200
    assert '<html lang="en" data-i18n-pending' in body
    assert "window.__LLM_USAGE_I18N__" in body
    assert '/i18n.js' in body
