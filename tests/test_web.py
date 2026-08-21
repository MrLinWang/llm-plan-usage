"""Web dashboard tests: FastAPI app, payload shape, TTL cache, auth, admin."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from llm_usage import store  # noqa: E402
from llm_usage.config import init_config, load_config, update_platform_config  # noqa: E402
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


def _auth_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_db_path: Path,
    cfg: dict | None = None,
    results=None,
    interval: float = 60,
    username: str = "admin",
    password: str = "secret1",
    admin: bool = True,
) -> tuple[TestClient, list[int]]:
    """App + 已登录用户:返回 (带会话 cookie 的 client, fetch 计数)。"""
    calls = _patch_fetch(monkeypatch, results)
    client = TestClient(
        create_app(cfg if cfg is not None else {"platforms": {}}, interval=interval)
    )
    store.create_user(username, password, is_admin=admin)
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200
    return client, calls


class TestWeb:
    def test_index_returns_html(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        resp = client.get("/")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        assert "重置倒计时" in resp.text

    def test_index_has_theme_toggle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        html = client.get("/").text
        assert 'id="theme-toggle"' in html
        assert 'html[data-theme="light"]' in html
        assert "llm-usage-theme" in html  # localStorage 持久化键

    def test_index_has_card_layout_hooks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        """页面 JS 依赖的静态挂载点:缺失时任一平台卡片都不会渲染。"""
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        html = client.get("/").text
        assert 'id="platforms"' in html    # 平台卡片网格容器
        assert 'id="entry-table"' in html  # 卡片内 entry 表格的 <template>

    def test_usage_payload_shape(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
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
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
    ) -> None:
        results = [PlatformResult("kimi", "Kimi Code", error="未配置")]
        client, _ = _auth_client(monkeypatch, tmp_db_path, results=results)
        data = client.get("/api/usage").json()
        platform = data["platforms"][0]
        assert platform["error"] == "未配置"
        assert platform["entries"] == []

    def test_cache_within_interval(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
    ) -> None:
        client, calls = _auth_client(monkeypatch, tmp_db_path)
        client.get("/api/usage")
        client.get("/api/usage")
        assert calls[0] == 1

    def test_cache_expires(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        calls = _patch_fetch(monkeypatch)
        cache = UsageCache({}, 5)
        cache.get()
        cache._fetched_at -= 10  # 让缓存过期(超过 interval=5)
        cache.get()
        assert calls[0] == 2

    def test_create_app_clamps_interval(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path, interval=1)
        data = client.get("/api/usage").json()
        assert data["interval"] == 5.0

    def test_index_has_refresh_controls(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        html = client.get("/").text
        assert 'id="refresh-now"' in html      # 马上刷新按钮
        assert 'id="interval-select"' in html  # 刷新间隔下拉
        assert 'id="more-menu-btn"' in html    # ⋯ 菜单按钮
        assert 'id="more-dropdown"' in html    # 折叠菜单容器

    def test_refresh_forces_refetch(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, calls = _auth_client(monkeypatch, tmp_db_path)
        client.get("/api/usage")
        client.get("/api/usage")
        assert calls[0] == 1  # TTL 内命中缓存
        resp = client.post("/api/refresh")
        assert resp.status_code == 200
        assert calls[0] == 2  # 强制重新拉取
        data = resp.json()
        assert {"fetched_at", "interval", "platforms"} <= data.keys()
        client.get("/api/usage")
        assert calls[0] == 2  # 刷新后 TTL 重新计时

    def test_interval_update_and_clamp(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        assert client.post("/api/interval", json={"interval": 120}).json() == {"interval": 120.0}
        assert client.get("/api/usage").json()["interval"] == 120.0
        assert client.post("/api/interval", json={"interval": 1}).json() == {"interval": 5.0}
        assert client.post("/api/interval", json={"interval": 99999}).json() == {"interval": 3600.0}

    def test_interval_invalid_body(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        for bad in ({"interval": "60"}, {}, {"interval": True}, {"interval": None}):
            assert client.post("/api/interval", json=bad).status_code == 400

    def test_refresh_and_interval_require_auth(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        assert client.post("/api/refresh").status_code == 401
        assert client.post("/api/interval", json={"interval": 30}).status_code == 401

    def test_favicon_no_404(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        """页面声明内联 data URI 图标;残留的 /favicon.ico 请求兜底返回 204 而非 404。"""
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        assert client.get("/favicon.ico").status_code == 204
        html = client.get("/").text
        assert 'rel="icon"' in html
        assert "data:image/svg+xml" in html


class TestWebAuth:
    def test_unauthenticated_blocked(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/login"
        assert client.get("/api/usage").status_code == 401
        assert client.get("/login").status_code == 200

    def test_setup_creates_first_admin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        assert client.get("/api/auth/state").json()["needs_setup"] is True
        resp = client.post(
            "/api/auth/setup", json={"username": "admin", "password": "secret1"}
        )
        assert resp.status_code == 200
        state = client.get("/api/auth/state").json()
        assert state["authenticated"] is True
        assert state["user"]["is_admin"] is True
        # 已初始化后再次 setup → 409
        resp = client.post(
            "/api/auth/setup", json={"username": "other", "password": "secret1"}
        )
        assert resp.status_code == 409

    def test_login_logout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        _patch_fetch(monkeypatch)
        store.create_user("admin", "secret1", is_admin=True)
        client = TestClient(create_app({"platforms": {}}))
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong"}
        )
        assert resp.status_code == 401
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        assert resp.status_code == 200
        assert client.get("/api/auth/state").json()["authenticated"] is True
        client.post("/api/auth/logout")
        assert client.get("/api/auth/state").json()["authenticated"] is False

    def test_setup_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        resp = client.post(
            "/api/auth/setup", json={"username": "  ", "password": "secret1"}
        )
        assert resp.status_code == 400
        resp = client.post(
            "/api/auth/setup", json={"username": "admin", "password": "12345"}
        )
        assert resp.status_code == 400

    def test_create_user_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        resp = client.post("/api/users", json={"username": "", "password": "secret1"})
        assert resp.status_code == 400
        resp = client.post("/api/users", json={"username": "v", "password": "12345"})
        assert resp.status_code == 400
        resp = client.post("/api/users", json={"username": "admin", "password": "secret1"})
        assert resp.status_code == 409

    def test_viewer_permissions(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        resp = client.post(
            "/api/users", json={"username": "viewer", "password": "secret2"}
        )
        assert resp.status_code == 201
        viewer = TestClient(client.app)
        resp = viewer.post(
            "/api/auth/login", json={"username": "viewer", "password": "secret2"}
        )
        assert resp.status_code == 200
        assert viewer.get("/").status_code == 200
        assert viewer.get("/api/users").status_code == 403
        assert viewer.get("/api/config").status_code == 403
        resp = viewer.get("/users", follow_redirects=False)
        assert resp.status_code == 302
        assert resp.headers["location"] == "/"

    def test_user_management(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        resp = client.post(
            "/api/users", json={"username": "viewer", "password": "secret2"}
        )
        assert resp.status_code == 201
        names = [u["username"] for u in client.get("/api/users").json()]
        assert "viewer" in names
        assert client.delete("/api/users/viewer").status_code == 200
        names = [u["username"] for u in client.get("/api/users").json()]
        assert "viewer" not in names
        assert client.delete("/api/users/viewer").status_code == 404

    def test_cannot_delete_self_or_last_admin(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        client.post(
            "/api/users",
            json={"username": "admin2", "password": "secret2", "is_admin": True},
        )
        # 两个管理员时可删另一个
        assert client.delete("/api/users/admin2").status_code == 200
        # 只剩自己:删自己 → 400(最后管理员保护的实际路径)
        resp = client.delete("/api/users/admin")
        assert resp.status_code == 400

    def test_reset_password_invalidates_sessions(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        client.post("/api/users", json={"username": "viewer", "password": "secret2"})
        viewer = TestClient(client.app)
        viewer.post(
            "/api/auth/login", json={"username": "viewer", "password": "secret2"}
        )
        assert viewer.get("/api/usage").status_code == 200
        resp = client.post("/api/users/viewer/password", json={"password": "newpass1"})
        assert resp.status_code == 200
        assert viewer.get("/api/usage").status_code == 401  # 旧会话已失效
        resp = viewer.post(
            "/api/auth/login", json={"username": "viewer", "password": "newpass1"}
        )
        assert resp.status_code == 200

    def test_change_own_password(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        other = TestClient(client.app)
        other.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        resp = client.post(
            "/api/auth/password",
            json={"old_password": "wrong", "new_password": "newpass1"},
        )
        assert resp.status_code == 401
        resp = client.post(
            "/api/auth/password",
            json={"old_password": "secret1", "new_password": "newpass1"},
        )
        assert resp.status_code == 200
        assert other.get("/api/usage").status_code == 401   # 其他会话失效
        assert client.get("/api/usage").status_code == 200  # 当前会话保留


class TestWebConfig:
    def test_get_config_credential_slots(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        platforms = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}
        # 模板中的 env: 引用原样出现在槽 0 的 env 字段
        kimi_slots = platforms["kimi"]["credential_slots"]
        assert kimi_slots == [{
            "index": 0,
            "name": None,
            "credentials": {"api_key": {"set": True, "env": "env:KIMI_API_KEY",
                                        "hint": None}},
        }]
        volc = platforms["volcengine-coding"]["credential_slots"][0]["credentials"]
        assert volc["access_key"]["env"] == "env:VOLCENGINE_ACCESS_KEY"
        assert volc["secret_key"]["env"] == "env:VOLCENGINE_SECRET_KEY"
        # 字面量密钥 → 脱敏 hint,env 为 null
        update_platform_config("kimi", {"api_key": "sk-kimi-secret-key"})
        kimi = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}[
            "kimi"
        ]["credential_slots"][0]["credentials"]["api_key"]
        assert kimi == {"set": True, "env": None, "hint": "sk-k…ey"}
        # 短密钥无法取头尾 → 固定掩码
        update_platform_config("kimi", {"api_key": "short"})
        kimi = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}[
            "kimi"
        ]["credential_slots"][0]["credentials"]["api_key"]
        assert kimi["hint"] == "••••"
        # 未设置字段 → 无凭证槽(页面渲染默认空槽,placeholder「未设置」)
        cfg = load_config()
        del cfg["platforms"]["ollama"]["api_key"]
        from llm_usage.config import save_config

        save_config(cfg)
        ollama_slots = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}[
            "ollama"
        ]["credential_slots"]
        assert ollama_slots == []
        # 平台无任何凭证 → 空槽列表(页面渲染默认空槽)
        cfg = load_config()
        del cfg["platforms"]["kimi"]["api_key"]
        save_config(cfg)
        kimi_slots = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}[
            "kimi"
        ]["credential_slots"]
        assert kimi_slots == []

    def test_put_enabled_invalidates_cache(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, calls = _auth_client(monkeypatch, tmp_db_path)
        assert client.get("/api/usage").status_code == 200
        assert calls[0] == 1
        resp = client.put("/api/config/platforms/kimi", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        assert load_config()["platforms"]["kimi"]["enabled"] is False
        # 缓存在 interval 内仍触发新的 fetch → 配置保存后缓存已失效
        client.get("/api/usage")
        assert calls[0] == 2

    def test_put_gateway_groups_persist_and_view(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "enabled": True,
            "base_url": "http://gw.internal:9090",
            "groups": [{"name": "组1", "daily_limit": 50, "api_keys": ["sk-g1-12345"]}],
        })
        assert resp.status_code == 200
        assert resp.json()["base_url"] == "http://gw.internal:9090"
        group = resp.json()["groups"][0]
        assert group["name"] == "组1"
        assert group["daily_limit"] == 50
        assert group["api_keys"] == [{"set": True, "env": None, "hint": "sk-g…45"}]
        # 磁盘持久化:groups 写入且 use_groups 被置为 true
        section = load_config()["platforms"]["llm-gateway"]
        assert section["base_url"] == "http://gw.internal:9090"
        assert section["groups"] == [
            {"name": "组1", "daily_limit": 50, "api_keys": ["sk-g1-12345"]}
        ]
        assert section["use_groups"] is True
        # 重复分组名 → 400
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "groups": [
                {"name": "组1", "api_keys": ["sk-a"]},
                {"name": "组1", "api_keys": ["sk-b"]},
            ],
        })
        assert resp.status_code == 400
        # 留空 key = 保留已保存的值 → 200(未改动)
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "groups": [{"name": "组1", "daily_limit": None, "api_keys": [{"index": 0, "value": ""}]}],
        })
        assert resp.status_code == 200
        # 空 daily_limit = 不设限;key 留空 = 保留已保存的值
        assert load_config()["platforms"]["llm-gateway"]["groups"] == [
            {"name": "组1", "api_keys": ["sk-g1-12345"]}
        ]

    def test_put_gateway_legacy_single_key_migrates(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        # 去掉模板默认组,模拟纯旧单 Key 配置
        cfg = load_config()
        del cfg["platforms"]["llm-gateway"]["groups"]
        from llm_usage.config import save_config

        save_config(cfg)
        update_platform_config("llm-gateway", {"api_key": "sk-legacy-123"})
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        # 视图把旧单 Key 合成一组展示
        gateway = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}[
            "llm-gateway"
        ]
        assert gateway["groups"] == [{
            "index": 0,
            "name": "组1",
            "daily_limit": None,
            "api_keys": [{"set": True, "env": None, "hint": "sk-l…23"}],
        }]
        # 保存时留空 key = 保留旧值 → 迁移为 groups
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "groups": [{
                "index": 0, "name": "组1", "daily_limit": None,
                "api_keys": [{"index": 0, "value": ""}],
            }],
        })
        assert resp.status_code == 200
        section = load_config()["platforms"]["llm-gateway"]
        assert section["groups"] == [{"name": "组1", "api_keys": ["sk-legacy-123"]}]
        assert section["use_groups"] is True

    def test_put_gateway_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        # 空 groups → 400(纯组式至少一组)
        resp = client.put("/api/config/platforms/llm-gateway", json={"groups": []})
        assert resp.status_code == 400
        assert "至少需要一个分组" in resp.json()["detail"]
        # 顶层 daily_limit / use_groups 已从编辑面移除 → 未知字段 400
        resp = client.put("/api/config/platforms/llm-gateway", json={"daily_limit": 50})
        assert resp.status_code == 400
        resp = client.put(
            "/api/config/platforms/llm-gateway", json={"use_groups": True}
        )
        assert resp.status_code == 400
        # groups 提交到其他平台 → 400
        resp = client.put("/api/config/platforms/kimi", json={
            "groups": [{"name": "组1", "api_keys": ["sk-kimi"]}],
        })
        assert resp.status_code == 400
        # 无任何既有 key 时空 key 串 → 400(留空 = 保留,但无值可保留)
        cfg = load_config()
        del cfg["platforms"]["llm-gateway"]["groups"]
        cfg["platforms"]["llm-gateway"].pop("api_key", None)
        from llm_usage.config import save_config

        save_config(cfg)
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "groups": [{"name": "组1", "api_keys": [""]}],
        })
        assert resp.status_code == 400
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "groups": [{"name": "组1", "api_keys": [{"index": 0, "value": ""}]}],
        })
        assert resp.status_code == 400

    def test_put_credential_slots_persist_and_migrate(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """基于模板 env 槽 0 保留 + 新增槽 → 写 credentials 并清除顶层凭证。"""
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        resp = client.put("/api/config/platforms/kimi", json={
            "credential_slots": [
                {"index": 0, "name": None, "api_key": ""},     # 保留 env:KIMI_API_KEY
                {"index": None, "name": "套餐B", "api_key": "sk-b-12345"},
            ],
        })
        assert resp.status_code == 200
        section = load_config()["platforms"]["kimi"]
        assert section["credentials"] == [
            {"name": "套餐1", "api_key": "env:KIMI_API_KEY"},
            {"name": "套餐B", "api_key": "sk-b-12345"},
        ]
        assert "api_key" not in section  # 顶层凭证已删除
        slots = resp.json()["credential_slots"]
        assert len(slots) == 2
        assert slots[0]["name"] == "套餐1"
        assert slots[0]["credentials"]["api_key"]["env"] == "env:KIMI_API_KEY"
        assert slots[1]["name"] == "套餐B"
        assert slots[1]["credentials"]["api_key"]["hint"] == "sk-b…45"

    def test_put_credential_slots_keep_and_replace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        resp = client.put("/api/config/platforms/kimi", json={
            "credential_slots": [{"index": None, "name": "套餐A", "api_key": "sk-a-123"}],
        })
        assert resp.status_code == 200
        # 留空 name/key → 保留既有值
        resp = client.put("/api/config/platforms/kimi", json={
            "credential_slots": [{"index": 0, "name": "", "api_key": ""}],
        })
        assert resp.status_code == 200
        assert load_config()["platforms"]["kimi"]["credentials"] == [
            {"name": "套餐A", "api_key": "sk-a-123"}
        ]
        # 填新值 → 替换
        resp = client.put("/api/config/platforms/kimi", json={
            "credential_slots": [{"index": 0, "name": "套餐A2", "api_key": "sk-a2-456"}],
        })
        assert resp.status_code == 200
        assert load_config()["platforms"]["kimi"]["credentials"] == [
            {"name": "套餐A2", "api_key": "sk-a2-456"}
        ]

    def test_put_credential_slots_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        # 空数组 → 400
        resp = client.put("/api/config/platforms/kimi", json={"credential_slots": []})
        assert resp.status_code == 400
        assert "至少需要一个凭证" in resp.json()["detail"]
        # 重复 name → 400
        resp = client.put("/api/config/platforms/kimi", json={
            "credential_slots": [
                {"name": "套餐A", "api_key": "sk-a-123"},
                {"name": "套餐A", "api_key": "sk-b-456"},
            ],
        })
        assert resp.status_code == 400
        assert "凭证名称重复" in resp.json()["detail"]
        # 新槽 api_key 留空(无既有值)→ 400
        cfg = load_config()
        del cfg["platforms"]["kimi"]["api_key"]
        from llm_usage.config import save_config

        save_config(cfg)
        resp = client.put("/api/config/platforms/kimi", json={
            "credential_slots": [{"index": None, "api_key": ""}],
        })
        assert resp.status_code == 400
        assert "缺少 api_key" in resp.json()["detail"]
        # volcengine 槽缺 secret_key(且无既有值)→ 400
        cfg = load_config()
        del cfg["platforms"]["volcengine-coding"]["access_key"]
        del cfg["platforms"]["volcengine-coding"]["secret_key"]
        from llm_usage.config import save_config

        save_config(cfg)
        resp = client.put("/api/config/platforms/volcengine-coding", json={
            "credential_slots": [{"name": "套餐A", "access_key": "AKLT-x"}],
        })
        assert resp.status_code == 400
        assert "缺少 secret_key" in resp.json()["detail"]
        # gateway 提交 credential_slots → 400
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "credential_slots": [{"name": "套餐A", "api_key": "sk-x"}],
        })
        assert resp.status_code == 400
        assert "不支持字段" in resp.json()["detail"]
        # 旧字段名 credentials → 未知字段 400
        resp = client.put("/api/config/platforms/kimi", json={
            "credentials": [{"name": "套餐A", "api_key": "sk-a-123"}],
        })
        assert resp.status_code == 400
        assert "未知字段" in resp.json()["detail"]
        # 非 list → 400;含未知键 → 400;index 非法 → 400
        resp = client.put("/api/config/platforms/kimi", json={"credential_slots": "x"})
        assert resp.status_code == 400
        resp = client.put("/api/config/platforms/kimi", json={
            "credential_slots": [{"name": "套餐A", "api_key": "sk-a", "daily_limit": 5}],
        })
        assert resp.status_code == 400
        resp = client.put("/api/config/platforms/kimi", json={
            "credential_slots": [{"index": True, "api_key": "sk-a"}],
        })
        assert resp.status_code == 400

    def test_put_credential_slots_volcengine_roundtrip(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """volcengine 槽保存 AK/SK,视图脱敏;凭证数组可被 fetch 消费。"""
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        resp = client.put("/api/config/platforms/volcengine-agent", json={
            "credential_slots": [{"index": None, "name": "套餐X",
                                  "access_key": "AKLT-new", "secret_key": "sk-new-1"}],
        })
        assert resp.status_code == 200
        section = load_config()["platforms"]["volcengine-agent"]
        assert section["credentials"] == [
            {"name": "套餐X", "access_key": "AKLT-new", "secret_key": "sk-new-1"}
        ]
        assert "access_key" not in section and "secret_key" not in section
        slot = resp.json()["credential_slots"][0]
        assert slot["credentials"]["access_key"]["hint"] == "AKLT…ew"
        assert slot["credentials"]["secret_key"]["hint"] == "sk-n…-1"

    def test_get_config_gateway_base_url_view(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """gateway 视图返回 base_url(模板值);其他平台无该字段。"""
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        platforms = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}
        assert platforms["llm-gateway"]["base_url"] == "http://127.0.0.1:18080"
        assert "base_url" not in platforms["kimi"]

    def test_put_gateway_base_url_keep_and_replace(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """base_url 留空 = 保留;非空 = 替换;其他平台提交 → 400。"""
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        # 留空 = 不修改
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "base_url": "", "groups": [{"name": "组1", "api_keys": ["sk-a-123"]}],
        })
        assert resp.status_code == 200
        assert load_config()["platforms"]["llm-gateway"]["base_url"] == "http://127.0.0.1:18080"
        # 非空 → 写回
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "base_url": "http://gw.new:8080",
            "groups": [{"name": "组1", "api_keys": ["sk-a-123"]}],
        })
        assert resp.status_code == 200
        assert resp.json()["base_url"] == "http://gw.new:8080"
        assert load_config()["platforms"]["llm-gateway"]["base_url"] == "http://gw.new:8080"
        # 其他平台提交 base_url → 400
        resp = client.put("/api/config/platforms/kimi", json={"base_url": "http://x"})
        assert resp.status_code == 400
        assert "base_url" in resp.json()["detail"]

    def test_put_credentials(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        before = load_config()["platforms"]["kimi"]["api_key"]
        # 留空 = 不修改
        resp = client.put("/api/config/platforms/kimi", json={"api_key": ""})
        assert resp.status_code == 200
        assert load_config()["platforms"]["kimi"]["api_key"] == before
        # 非空 → 写回磁盘
        resp = client.put(
            "/api/config/platforms/kimi", json={"api_key": "sk-new-key-123"}
        )
        assert resp.status_code == 200
        assert load_config()["platforms"]["kimi"]["api_key"] == "sk-new-key-123"
        assert resp.json()["credential_slots"][0]["credentials"]["api_key"]["hint"] == "sk-n…23"

    def test_put_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        resp = client.put("/api/config/platforms/kimi", json={"nope": 1})
        assert resp.status_code == 400
        resp = client.put("/api/config/platforms/ghost", json={"enabled": True})
        assert resp.status_code == 404
        # kimi 没有 access_key 字段
        resp = client.put("/api/config/platforms/kimi", json={"access_key": "x"})
        assert resp.status_code == 400

    def test_put_order_persists_and_reorders_get(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, calls = _auth_client(monkeypatch, tmp_db_path)
        assert client.get("/api/usage").status_code == 200
        assert calls[0] == 1
        new_order = [
            "opencode-go", "ollama", "volcengine-agent", "volcengine-coding", "kimi",
            "llm-gateway",
        ]
        resp = client.put("/api/config/order", json={"order": new_order})
        assert resp.status_code == 200
        assert resp.json()["order"] == new_order
        assert load_config()["platform_order"] == new_order
        keys = [p["key"] for p in client.get("/api/config").json()["platforms"]]
        assert keys == new_order
        # 缓存在 interval 内仍触发新的 fetch → 顺序保存后缓存已失效
        client.get("/api/usage")
        assert calls[0] == 2

    def test_put_order_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        resp = client.put("/api/config/order", json={"order": "kimi"})
        assert resp.status_code == 400
        resp = client.put("/api/config/order", json={"order": ["kimi", 1]})
        assert resp.status_code == 400
        # 未知 key 被丢弃,未列出的平台按注册表顺序补全
        resp = client.put("/api/config/order", json={"order": ["ghost", "ollama"]})
        assert resp.status_code == 200
        assert resp.json()["order"] == [
            "ollama", "kimi", "volcengine-coding", "volcengine-agent", "opencode-go",
            "llm-gateway",
        ]

    def test_config_page_has_drag_handle(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
    ) -> None:
        """页面 JS 依赖的拖放挂载点:缺失时卡片无法拖动排序。"""
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        html = client.get("/config").text
        assert "drag-handle" in html
        assert "dragstart" in html


class TestRegistration:
    """开放注册 + 管理员开关:settings 表持久化,注册即登录(普通用户)。"""

    def test_register_disabled_by_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        anon = TestClient(client.app)
        r = anon.post(
            "/api/auth/register", json={"username": "alice", "password": "secret1"}
        )
        assert r.status_code == 403
        assert r.json()["detail"] == "注册已关闭"
        assert store.get_user("alice") is None
        assert anon.get("/api/auth/state").json()["registration_enabled"] is False

    def test_register_when_enabled(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        r = client.put("/api/settings", json={"registration_enabled": True})
        assert r.status_code == 200
        assert r.json() == {"registration_enabled": True}
        anon = TestClient(client.app)
        r = anon.post(
            "/api/auth/register", json={"username": "alice", "password": "secret1"}
        )
        assert r.status_code == 200
        assert r.json() == {"username": "alice", "is_admin": False}
        state = anon.get("/api/auth/state").json()  # 注册即登录:cookie 生效
        assert state["authenticated"] is True
        assert state["registration_enabled"] is True
        assert store.get_user("alice")["is_admin"] is False

    def test_register_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        client.put("/api/settings", json={"registration_enabled": True})
        anon = TestClient(client.app)
        r = anon.post("/api/auth/register", json={"username": "", "password": "secret1"})
        assert r.status_code == 400
        r = anon.post(
            "/api/auth/register", json={"username": "x" * 65, "password": "secret1"}
        )
        assert r.status_code == 400
        r = anon.post("/api/auth/register", json={"username": "alice", "password": "12345"})
        assert r.status_code == 400
        anon.post("/api/auth/register", json={"username": "alice", "password": "secret1"})
        r = anon.post(
            "/api/auth/register", json={"username": "alice", "password": "secret1"}
        )
        assert r.status_code == 409
        assert r.json()["detail"] == "用户名已存在"
        client.put("/api/settings", json={"registration_enabled": False})
        r = anon.post("/api/auth/register", json={"username": "bob", "password": "secret1"})
        assert r.status_code == 403

    def test_settings_api_auth_and_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        anon = TestClient(client.app)
        assert anon.get("/api/settings").status_code == 401
        assert anon.put("/api/settings", json={"registration_enabled": True}).status_code == 401
        viewer, _ = _auth_client(
            monkeypatch, tmp_db_path, username="bob", admin=False
        )
        assert viewer.get("/api/settings").status_code == 403
        assert viewer.put(
            "/api/settings", json={"registration_enabled": True}
        ).status_code == 403
        assert (
            client.put("/api/settings", json={"registration_enabled": "yes"}).status_code
            == 400
        )
        assert client.put("/api/settings", json={}).status_code == 400
        r = client.put("/api/settings", json={"nope": 1})
        assert r.status_code == 400
        assert "未知字段" in r.json()["detail"]
        # 持久化:新 app 实例(模拟重启)读到同一 history.db 中的开关
        client.put("/api/settings", json={"registration_enabled": True})
        client2 = TestClient(create_app({"platforms": {}}))
        r = client2.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        assert r.status_code == 200
        assert client2.get("/api/settings").json()["registration_enabled"] is True

    def test_login_and_users_page_hooks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        """页面 JS 依赖的静态挂载点:缺失时注册入口/开关不渲染。"""
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        anon = TestClient(client.app)
        login_html = anon.get("/login").text
        assert "switch-mode" in login_html
        assert "没有账号?注册" in login_html
        users_html = client.get("/users").text
        assert "reg-enabled" in users_html
        assert "注册设置" in users_html
