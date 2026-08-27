"""Web dashboard tests: FastAPI app, payload shape, TTL cache, auth, admin."""

from __future__ import annotations

import json
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



class TestPwa:
    def test_manifest_served_without_auth(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        """登录页也要能取 manifest 并注册 SW,故清单路由不设认证。"""
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        resp = client.get("/manifest.webmanifest")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("application/manifest+json")
        data = json.loads(resp.text)
        assert data["start_url"] == "/"
        assert data["display"] == "standalone"
        sizes = {icon["sizes"] for icon in data["icons"]}
        assert "192x192" in sizes and "512x512" in sizes

    def test_service_worker_served_without_auth(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        resp = client.get("/sw.js")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/javascript")
        assert '"llm-usage-v1"' in resp.text
        assert 'addEventListener("fetch"' in resp.text

    def test_icons_whitelist_and_traversal(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        """图标按白名单放行;路径穿越(线上原样形式 %2E%2E)与未知名一律 404。"""
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        resp = client.get("/icons/icon-192.png")
        assert resp.status_code == 200
        assert resp.content.startswith(b"\x89PNG")
        assert client.get("/icons/%2E%2E/sw.js").status_code == 404
        assert client.get("/icons/junk.png").status_code == 404

    def test_pages_declare_pwa_hooks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        for path in ("/", "/login"):
            html = client.get(path).text
            assert 'rel="manifest"' in html
            assert "serviceWorker" in html


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
        # 普通用户需要共享目标列表 → GET /api/users 放开到任意登录用户
        assert viewer.get("/api/users").status_code == 200
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

    def test_cannot_delete_self_and_single_admin_enforced(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        """单管理员模型:POST /api/users 的 is_admin 一律拒绝;不能删除自己。"""
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        # 尝试创建第二个管理员 → 400,用户未创建,count_admins 恒为 1
        resp = client.post(
            "/api/users",
            json={"username": "admin2", "password": "secret2", "is_admin": True},
        )
        assert resp.status_code == 400
        assert "管理员" in resp.json()["detail"]
        assert store.get_user("admin2") is None
        assert store.count_admins() == 1
        # 不带 is_admin 的新用户 → 普通用户
        resp = client.post(
            "/api/users", json={"username": "viewer", "password": "secret2"}
        )
        assert resp.status_code == 201
        assert store.get_user("viewer")["is_admin"] is False
        # 唯一管理员删自己 → 400(最后管理员保护的实际路径)
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
        assert group["api_keys"] == [
            {"name": None, "set": True, "env": None, "hint": "sk-g…45"}
        ]
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

    def test_put_gateway_named_keys_persist_and_view(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        # 保存:新 key 带名称;既有 key 留空保留 + 重命名
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "groups": [{
                "name": "组A", "daily_limit": None,
                "api_keys": [
                    {"value": "sk-new-123456", "name": "主 Key"},
                    {"index": 0, "value": "", "name": "改名 Key"},
                ],
            }],
        })
        assert resp.status_code == 400  # 尚无既有 key,index 0 无可保留
        assert "要保留" in resp.json()["detail"]
        # 先保存两个带名称的 key
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "groups": [{
                "name": "组A", "daily_limit": None,
                "api_keys": [
                    {"value": "sk-new-1-123456", "name": "主 Key"},
                    {"value": "sk-new-2-123456", "name": "备份 Key"},
                ],
            }],
        })
        assert resp.status_code == 200
        section = load_config()["platforms"]["llm-gateway"]
        assert section["groups"] == [{
            "name": "组A",
            "api_keys": [
                {"name": "主 Key", "value": "sk-new-1-123456"},
                {"name": "备份 Key", "value": "sk-new-2-123456"},
            ],
        }]
        # 视图回显名称(不返回明文,掩码来自 dict 条目内的 value)
        group = resp.json()["groups"][0]
        assert [k["name"] for k in group["api_keys"]] == ["主 Key", "备份 Key"]
        assert group["api_keys"][0]["set"] is True
        assert group["api_keys"][0]["hint"] == "sk-n…56"
        assert group["api_keys"][1]["hint"] == "sk-n…56"
        # 留空 key = 保留值;重命名第一个
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "groups": [{
                "name": "组A", "daily_limit": None,
                "api_keys": [
                    {"index": 0, "value": "", "name": "线上主 Key"},
                    {"index": 1, "value": "", "name": None},
                ],
            }],
        })
        assert resp.status_code == 200
        assert load_config()["platforms"]["llm-gateway"]["groups"] == [{
            "name": "组A",
            "api_keys": [
                {"name": "线上主 Key", "value": "sk-new-1-123456"},
                {"name": "备份 Key", "value": "sk-new-2-123456"},
            ],
        }]
        # 名称超长 → 400
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "groups": [{
                "name": "组A", "daily_limit": None,
                "api_keys": [{"value": "sk-x", "name": "长" * 101}],
            }],
        })
        assert resp.status_code == 400
        assert "1-100" in resp.json()["detail"]

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
            "api_keys": [{"name": None, "set": True, "env": None, "hint": "sk-l…23"}],
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
            "clinepass", "llm-gateway",
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
            "clinepass", "llm-gateway",
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
        assert (
            r.json()["registration_enabled"] is True
            and r.json()["allow_user_providers"] is True
        )
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


class TestUserProviderPolicy:
    """allow_user_providers 站点开关:默认开;关闭后 /api/my/* 全部 403,
    只读仪表盘接口不受影响;管理员永远不受影响。"""

    def _bob_client(self, client: TestClient) -> TestClient:
        """已登录普通用户 bob 的独立 client(共享同一 app/DB)。"""
        bob = TestClient(client.app)
        r = bob.post("/api/auth/login", json={"username": "bob", "password": "secret1"})
        assert r.status_code == 200
        return bob

    def test_user_providers_allowed_by_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        store.create_user("bob", "secret1", is_admin=False)
        state = self._bob_client(client).get("/api/auth/state").json()
        assert state["allow_user_providers"] is True
        bob = self._bob_client(client)
        assert bob.get("/api/my/platforms").status_code == 200
        resp = bob.post("/api/my/providers", json={"type": "kimi"})
        assert resp.status_code == 200
        assert resp.json()["key"] == "kimi#2"

    def test_disable_blocks_my_endpoints_but_usage_reads(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        store.create_user("bob", "secret1", is_admin=False)
        bob = self._bob_client(client)
        # 开关开启时先给 bob 配一个实例,供 PUT/DELETE 门控验证(路径中 # 编码为 %23)
        resp = bob.post("/api/my/providers", json={"type": "kimi"})
        assert resp.status_code == 200
        r = client.put("/api/settings", json={"allow_user_providers": False})
        assert r.status_code == 200
        assert client.get("/api/settings").json()["allow_user_providers"] is False
        assert bob.get("/api/my/platforms").status_code == 403
        assert bob.put(
            "/api/my/platforms/kimi%232", json={"enabled": True}
        ).status_code == 403
        assert bob.post("/api/my/providers", json={"type": "kimi"}).status_code == 403
        assert bob.delete("/api/my/platforms/kimi%232").status_code == 403
        # 只读接口不受影响:普通用户仪表盘可用,管理员配置页 API 可用
        assert bob.get("/api/usage").status_code == 200
        assert client.get("/api/config").status_code == 200

    def test_enable_restores_access(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        store.create_user("bob", "secret1", is_admin=False)
        bob = self._bob_client(client)
        assert client.put(
            "/api/settings", json={"allow_user_providers": False}
        ).status_code == 200
        assert bob.get("/api/my/platforms").status_code == 403
        assert client.put(
            "/api/settings", json={"allow_user_providers": True}
        ).status_code == 200
        assert bob.get("/api/my/platforms").status_code == 200

    def test_settings_put_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        r = client.put("/api/settings", json={"allow_user_providers": "yes"})
        assert r.status_code == 400
        assert r.json()["detail"] == "allow_user_providers 需为布尔值"
        assert client.put("/api/settings", json={}).status_code == 400
        # 单独 PUT registration_enabled → 200 且只改注册开关(逐字段循环回归)
        r = client.put("/api/settings", json={"registration_enabled": True})
        assert r.status_code == 200
        body = client.get("/api/settings").json()
        assert body["registration_enabled"] is True
        assert body["allow_user_providers"] is True

    def test_users_page_has_provider_toggle_hook(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        """页面 JS 依赖的静态挂载点:开关卡片与普通用户模式门控分支。"""
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        users_html = client.get("/users").text
        assert "user-providers-enabled" in users_html
        assert "用户配置" in users_html
        config_html = client.get("/config").text
        assert "allow_user_providers" in config_html


class TestUserStore:
    """user_configs / user_shares 存储层:读写、覆盖、级联删除。"""

    def test_user_config_roundtrip(self, tmp_db_path: Path) -> None:
        assert store.get_user_config("alice") == {}
        cfg = {"platforms": {"kimi": {"enabled": True}}}
        store.set_user_config("alice", cfg)
        assert store.get_user_config("alice") == cfg
        # 覆盖写
        store.set_user_config("alice", {"platforms": {"ollama": {"enabled": False}}})
        assert store.get_user_config("alice") == {"platforms": {"ollama": {"enabled": False}}}
        store.delete_user_config("alice")
        assert store.get_user_config("alice") == {}

    def test_user_config_corrupt_json(self, tmp_db_path: Path) -> None:
        store.create_user("alice", "secret1")
        store.set_user_config("alice", {"platforms": {}})
        import sqlite3

        conn = sqlite3.connect(tmp_db_path)
        conn.execute(
            "UPDATE user_configs SET config = 'not-json' WHERE username = 'alice'"
        )
        conn.commit()
        conn.close()
        assert store.get_user_config("alice") == {}

    def test_visibility_crud(self, tmp_db_path: Path) -> None:
        store.create_user("alice", "secret1")
        store.create_user("bob", "secret2")
        store.create_user("admin", "secret3", is_admin=True)
        store.set_platform_visibility("alice", "kimi", "*")
        store.set_platform_visibility("alice", "ollama", "bob")
        # bob 可见:公开 + 指定共享;admin 只可见公开的
        assert store.list_shared_platforms("bob") == [
            {"owner": "alice", "platform": "kimi"},
            {"owner": "alice", "platform": "ollama"},
        ]
        assert store.list_shared_platforms("admin") == [
            {"owner": "alice", "platform": "kimi"}
        ]
        assert store.list_my_visibility("alice") == [
            {"platform": "kimi", "target": "*"},
            {"platform": "ollama", "target": "bob"},
        ]
        # 空 target = 仅清除;替换旧行
        store.set_platform_visibility("alice", "ollama", "")
        store.set_platform_visibility("alice", "kimi", "bob")
        assert store.list_shared_platforms("bob") == [
            {"owner": "alice", "platform": "kimi"}
        ]
        assert store.list_shared_platforms("admin") == []

    def test_delete_user_cascades_config_and_shares(self, tmp_db_path: Path) -> None:
        store.create_user("alice", "secret1")
        store.create_user("bob", "secret2")
        store.set_user_config("alice", {"platforms": {"kimi": {"enabled": True}}})
        store.set_platform_visibility("alice", "kimi", "bob")  # owner 行
        store.set_platform_visibility("bob", "ollama", "alice")  # target 行
        assert store.delete_user("alice")
        assert store.get_user_config("alice") == {}
        assert store.list_my_visibility("alice") == []
        # bob 不再被 alice 共享,也不再共享给 alice
        assert store.list_shared_platforms("bob") == []
        assert store.list_shared_platforms("admin") == []
        assert store.delete_user("alice") is False


class TestUserIsolation:
    """多用户用量隔离:默认私有、共享可见性、普通用户 DB 配置。"""

    def _config_client(
        self, monkeypatch: pytest.MonkeyPatch, cfg: dict,
    ) -> tuple[TestClient, list[int]]:
        """App + 已登录 admin;fetch 假实现按传入配置返回各启用平台(真实 fetch 语义)。"""
        from llm_usage.providers import DISPLAY_NAMES, PROVIDERS

        calls = [0]

        def fake(cfg_in):
            calls[0] += 1
            platforms = (cfg_in or {}).get("platforms", {}) or {}
            out = []
            for key in PROVIDERS:
                section = platforms.get(key)
                if section is None or section.get("enabled", True) is False:
                    continue
                entry = UsageEntry(key, "一次", 60, 120, 60, 50.0, None, "%", False)
                out.append(PlatformResult(
                    key, section.get("display_name") or DISPLAY_NAMES[key],
                    entries=[entry],
                ))
            return out

        monkeypatch.setattr("llm_usage.web.fetch_all", fake)
        client = TestClient(create_app(cfg, interval=60))
        store.create_user("admin", "secret1", is_admin=True)
        r = client.post("/api/auth/login", json={"username": "admin", "password": "secret1"})
        assert r.status_code == 200
        return client, calls

    def _register(self, client: TestClient, username: str) -> TestClient:
        """注册 alice/bob 等普通用户并返回其已登录 client。"""
        client.put("/api/settings", json={"registration_enabled": True})
        anon = TestClient(client.app)
        r = anon.post(
            "/api/auth/register", json={"username": username, "password": "secret1"}
        )
        assert r.status_code == 200
        return anon

    def test_default_private(self, monkeypatch: pytest.MonkeyPatch,
                             tmp_db_path: Path, tmp_config_path: Path) -> None:
        """默认私有:普通用户 usage 为空;admin 仍见 config.toml 全部平台。"""
        init_config(tmp_config_path)
        client, calls = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        assert alice.get("/api/usage").json()["platforms"] == []
        admin_names = [p["name"] for p in client.get("/api/usage").json()["platforms"]]
        assert set(admin_names) == {
            "kimi", "volcengine-coding", "volcengine-agent", "ollama",
            "opencode-go", "clinepass", "llm-gateway",
        }
        assert calls[0] == 2  # alice 空配置 + admin 配置,各自缓存条目

    def test_admin_share_to_user(self, monkeypatch: pytest.MonkeyPatch,
                                 tmp_db_path: Path, tmp_config_path: Path) -> None:
        """admin 共享给 A → 仅 A 可见;公开 → 所有普通用户可见并带 (admin) 后缀。"""
        init_config(tmp_config_path)
        client, calls = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        bob = self._register(client, "bob")
        # 先共享给 alice
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "visibility": {"type": "shared", "targets": ["alice"]},
        })
        assert resp.status_code == 200
        assert resp.json()["visibility"] == {"type": "shared", "targets": ["alice"]}
        alice_names = [p["name"] for p in alice.get("/api/usage").json()["platforms"]]
        bob_names = [p["name"] for p in bob.get("/api/usage").json()["platforms"]]
        assert "llm-gateway" in alice_names
        assert alice_names == ["llm-gateway"]  # 自己的配置为空,仅共享平台
        assert "llm-gateway" not in bob_names
        # 公开 → 所有人都可见,带 (admin) 后缀
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "visibility": {"type": "public", "targets": []},
        })
        assert resp.status_code == 200
        assert resp.json()["visibility"] == {"type": "public", "targets": []}
        for viewer in (alice, bob):
            gateway = [p for p in viewer.get("/api/usage").json()["platforms"]
                       if p["name"] == "llm-gateway"][0]
            assert gateway["display_name"] == "LLM Gateway(admin)"
        # 私有 → 无人可见
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "visibility": {"type": "private", "targets": []},
        })
        assert resp.status_code == 200
        for viewer in (alice, bob):
            names = [p["name"] for p in viewer.get("/api/usage").json()["platforms"]]
            assert "llm-gateway" not in names

    def test_my_platform_enable_and_visibility(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """普通用户启用自己的平台 → 仅自己可见;公开后他人可见且带 (用户名) 后缀。"""
        init_config(tmp_config_path)
        client, calls = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        bob = self._register(client, "bob")
        # alice 启用 kimi
        resp = alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "visibility": {"type": "private", "targets": []},
        })
        assert resp.status_code == 200
        assert resp.json() == {
            "key": "kimi",
            "type": "kimi",
            "display_name": "Kimi Code",
            "enabled": True,
            "visibility": {"type": "private", "targets": []},
            "credential_slots": [],
        }
        # 自己的配置存 DB,与 config.toml 隔离
        assert store.get_user_config("alice") == {
            "platforms": {"kimi": {"enabled": True}}
        }
        alice_names = [p["name"] for p in alice.get("/api/usage").json()["platforms"]]
        assert alice_names == ["kimi"]
        assert [p["name"] for p in bob.get("/api/usage").json()["platforms"]] == []
        # 公开 kimi → bob 可见且带 (alice) 后缀
        resp = alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "visibility": {"type": "public", "targets": []},
        })
        assert resp.status_code == 200
        bob_kimi = [p for p in bob.get("/api/usage").json()["platforms"]
                    if p["name"] == "kimi"]
        assert bob_kimi and bob_kimi[0]["display_name"] == "Kimi Code(alice)"
        # admin 仪表盘仍是 config.toml 平台,不带 (alice) 后缀
        admin_kimi = [p for p in client.get("/api/usage").json()["platforms"]
                      if p["name"] == "kimi"][0]
        assert admin_kimi["display_name"] == "Kimi Code"
        assert calls[0] == 3  # alice + bob + admin 各一次

    def test_my_platform_credentials_persist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """普通用户凭证槽保存:写 credentials 数组、清除顶层凭证、留空保留、视图脱敏。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        # 新槽写入
        resp = alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "credential_slots": [
                {"index": None, "name": None, "api_key": "sk-alice-key"},
            ],
        })
        assert resp.status_code == 200
        assert store.get_user_config("alice") == {
            "platforms": {
                "kimi": {
                    "enabled": True,
                    "credentials": [{"name": "套餐1", "api_key": "sk-alice-key"}],
                }
            }
        }
        # 视图脱敏
        slot = resp.json()["credential_slots"][0]
        assert slot["credentials"]["api_key"] == {
            "set": True, "env": None, "hint": "sk-a…ey",
        }
        # 留空 = 保留既有值;名称留空 = 保留既有名称
        resp = alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "credential_slots": [
                {"index": 0, "name": None, "api_key": ""},
            ],
        })
        assert resp.status_code == 200
        assert store.get_user_config("alice")["platforms"]["kimi"]["credentials"] == [
            {"name": "套餐1", "api_key": "sk-alice-key"}
        ]
        # 新槽(无 index)+ 既有槽并存
        resp = alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "credential_slots": [
                {"index": 0, "name": None, "api_key": ""},
                {"index": None, "name": "套餐B", "api_key": "sk-b-key"},
            ],
        })
        assert resp.status_code == 200
        creds = store.get_user_config("alice")["platforms"]["kimi"]["credentials"]
        assert creds == [
            {"name": "套餐1", "api_key": "sk-alice-key"},
            {"name": "套餐B", "api_key": "sk-b-key"},
        ]
        # 明文不回显
        raw = alice.get("/api/my/platforms").text
        assert "sk-alice-key" not in raw and "sk-b-key" not in raw

    def test_my_platform_gateway_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """普通用户 gateway:base_url + groups 写入、use_groups=true、留空 base_url 不修改。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        resp = alice.put("/api/my/platforms/llm-gateway", json={
            "enabled": True,
            "base_url": "http://127.0.0.1:18080",
            "groups": [
                {"index": None, "name": "组1", "daily_limit": None,
                 "api_keys": ["g-alice-key"]},
            ],
        })
        assert resp.status_code == 200
        section = store.get_user_config("alice")["platforms"]["llm-gateway"]
        assert section["base_url"] == "http://127.0.0.1:18080"
        assert section["use_groups"] is True
        assert section["groups"] == [
            {"name": "组1", "api_keys": ["g-alice-key"]}
        ]
        # 视图:key 脱敏;base_url 原文可见
        group = resp.json()["groups"][0]
        assert group["api_keys"][0] == {
            "name": None, "set": True, "env": None, "hint": "g-al…ey",
        }
        # 留空 base_url 不修改;groups 留空 key = 保留
        resp = alice.put("/api/my/platforms/llm-gateway", json={
            "enabled": True,
            "base_url": "",
            "groups": [
                {"index": 0, "name": "组1", "daily_limit": None,
                 "api_keys": [{"index": 0, "value": None}]},
            ],
        })
        assert resp.status_code == 200
        section = store.get_user_config("alice")["platforms"]["llm-gateway"]
        assert section["base_url"] == "http://127.0.0.1:18080"
        assert section["groups"] == [
            {"name": "组1", "api_keys": ["g-alice-key"]}
        ]

    def test_my_platform_put_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """普通用户保存校验:空槽/缺 secret_key/gateway 槽/顶层凭证字段/未知平台 → 400。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        for body in (
            {"credential_slots": []},                                    # 空槽数组
            {"credential_slots": [{"index": None, "api_key": ""}]},      # 缺 api_key
            {"credential_slots": [{"index": None, "access_key": "AK",
                                   "secret_key": ""}]},                  # 缺 secret_key
            {"api_key": "sk-top"},                                       # 顶层凭证字段
            {"groups": [{"name": "组1", "api_keys": ["k"]}]},            # kimi 不支持 groups
            {"base_url": "http://x"},                                    # kimi 不支持 base_url
        ):
            resp = alice.put("/api/my/platforms/kimi", json=body)
            assert resp.status_code == 400, body
        # gateway 不接受 credential_slots
        resp = alice.put("/api/my/platforms/llm-gateway", json={
            "credential_slots": [{"index": None, "api_key": "k"}],
        })
        assert resp.status_code == 400
        # 未知平台
        assert alice.put("/api/my/platforms/ghost", json={
            "enabled": True,
        }).status_code == 404
        # 校验失败不落库
        assert store.get_user_config("alice") == {}

    def test_own_platform_and_shared_coexist(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """同名冲突全部保留:自己的裸名 + admin 来源 + alice 来源三张卡并存。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        bob = self._register(client, "bob")
        # admin 在 config.toml 已启用 kimi,公开给所有人
        resp = client.put("/api/config/platforms/kimi", json={
            "visibility": {"type": "public", "targets": []},
        })
        assert resp.status_code == 200
        # alice 公开自己的 kimi
        alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "visibility": {"type": "public", "targets": []},
        })
        bob_kimi = [(p["name"], p["display_name"])
                    for p in bob.get("/api/usage").json()["platforms"]
                    if p["name"] == "kimi"]
        # admin 来源也标注 (admin) 后缀;两来源并存,按 owner 排序
        assert bob_kimi == [
            ("kimi", "Kimi Code(admin)"),
            ("kimi", "Kimi Code(alice)"),
        ]
        # bob 自己启用 kimi → 第三个裸名卡,三来源互不覆盖
        bob.put("/api/my/platforms/kimi", json={"enabled": True})
        bob_kimi = [(p["name"], p["display_name"])
                    for p in bob.get("/api/usage").json()["platforms"]
                    if p["name"] == "kimi"]
        assert bob_kimi == [
            ("kimi", "Kimi Code"),
            ("kimi", "Kimi Code(admin)"),
            ("kimi", "Kimi Code(alice)"),
        ]

    def test_my_platform_gateway_display_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """普通用户自定义仪表盘名称(全平台):持久化、留空不修改、共享带后缀。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        bob = self._register(client, "bob")
        # 自定义显示名称 + 启用 + 公开
        resp = alice.put("/api/my/platforms/llm-gateway", json={
            "enabled": True,
            "display_name": "我的渠道",
            "visibility": {"type": "public", "targets": []},
        })
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "我的渠道"
        assert store.get_user_config("alice")["platforms"]["llm-gateway"] == {
            "enabled": True,
            "display_name": "我的渠道",
        }
        # 自己的仪表盘显示自定义名(无后缀)
        alice_gw = [p for p in alice.get("/api/usage").json()["platforms"]
                    if p["name"] == "llm-gateway"][0]
        assert alice_gw["display_name"] == "我的渠道"
        # 共享给他人 → 自定义名 + (owner) 后缀
        bob_gw = [p for p in bob.get("/api/usage").json()["platforms"]
                  if p["name"] == "llm-gateway"][0]
        assert bob_gw["display_name"] == "我的渠道(alice)"
        # 留空 = 不修改
        resp = alice.put("/api/my/platforms/llm-gateway", json={
            "enabled": True,
            "display_name": "",
        })
        assert resp.status_code == 200
        assert store.get_user_config("alice")["platforms"]["llm-gateway"][
            "display_name"] == "我的渠道"
        # 非 gateway 平台同样支持自定义名称(全平台开放)
        resp = alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "display_name": "K",
            "visibility": {"type": "public", "targets": []},
        })
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "K"
        my_view = {
            p["key"]: p
            for p in alice.get("/api/my/platforms").json()["platforms"]
        }
        assert my_view["kimi"]["display_name"] == "K"
        # 自定义名传播:bob 的合并视图显示 自定义名(owner)
        bob_kimi = [p["display_name"]
                    for p in bob.get("/api/usage").json()["platforms"]
                    if p["name"] == "kimi"]
        assert "K(alice)" in bob_kimi

    def test_my_platform_display_name_too_long(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """display_name 超 64 字符 → 400(两个端点共用同一校验);64 字符合法。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        resp = alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "display_name": "长" * 65,
        })
        assert resp.status_code == 400
        assert resp.json()["detail"] == "显示名称最长 64 字符"
        resp = alice.put("/api/my/platforms/kimi", json={"display_name": "长" * 64})
        assert resp.status_code == 200

    def test_admin_gateway_display_name(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """admin gateway 自定义名称写 config.toml,仪表盘与共享视图生效。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "enabled": True,
            "display_name": "内网网关",
            "visibility": {"type": "public", "targets": []},
        })
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "内网网关"
        admin_gw = [p for p in client.get("/api/usage").json()["platforms"]
                    if p["name"] == "llm-gateway"][0]
        assert admin_gw["display_name"] == "内网网关"
        alice_gw = [p for p in alice.get("/api/usage").json()["platforms"]
                    if p["name"] == "llm-gateway"][0]
        assert alice_gw["display_name"] == "内网网关(admin)"
        # 留空 = 不修改
        resp = client.put("/api/config/platforms/llm-gateway", json={
            "enabled": True,
            "display_name": "",
        })
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "内网网关"

    def test_my_platforms_list_matches_admin_shape(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """普通用户平台列表与 admin 视图同构:凭证槽/base_url/groups,无明文。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        data = alice.get("/api/my/platforms").json()
        keys = [p["key"] for p in data["platforms"]]
        assert keys == [
            "kimi", "volcengine-coding", "volcengine-agent", "ollama",
            "opencode-go", "clinepass", "llm-gateway",
        ]
        for p in data["platforms"]:
            assert p["visibility"] == {"type": "private", "targets": []}
            if p["key"] == "llm-gateway":
                assert set(p) == {
                    "key", "type", "display_name", "enabled", "visibility",
                    "base_url", "groups",
                }
                assert p["base_url"] is None
                assert p["groups"] == []  # 无凭证/无 legacy 单 key → 空组列表
            elif p["key"] in ("kimi", "volcengine-coding", "volcengine-agent",
                              "ollama", "opencode-go", "clinepass"):
                assert set(p) == {
                    "key", "type", "display_name", "enabled", "visibility",
                    "credential_slots",
                }
                assert p["credential_slots"] == []
        # 存一条凭证后视图脱敏,不回明文
        alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "credential_slots": [
                {"index": None, "name": None, "api_key": "sk-alice-secret"},
            ],
        })
        data = alice.get("/api/my/platforms").json()
        kimi = next(p for p in data["platforms"] if p["key"] == "kimi")
        slot = kimi["credential_slots"][0]
        assert slot["name"] == "套餐1"
        assert slot["credentials"]["api_key"] == {
            "set": True, "env": None, "hint": "sk-a…et",
        }
        raw = alice.get("/api/my/platforms").text
        assert "sk-alice-secret" not in raw

    def test_admin_cannot_use_my_api(self, monkeypatch: pytest.MonkeyPatch,
                                     tmp_db_path: Path,
                                     tmp_config_path: Path) -> None:
        """admin 调 /api/my/* → 400;普通用户调 /api/config → 403;GET /api/users 放开。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        assert client.get("/api/my/platforms").status_code == 400
        assert client.put("/api/my/platforms/kimi", json={
            "enabled": True,
        }).status_code == 400
        alice = self._register(client, "alice")
        assert alice.get("/api/config").status_code == 403
        assert alice.put(
            "/api/config/platforms/kimi", json={"enabled": True}
        ).status_code == 403
        assert alice.get("/api/users").status_code == 200  # 共享目标列表
        names = [u["username"] for u in alice.get("/api/users").json()]
        assert "alice" in names and "admin" in names

    def test_visibility_validation(self, monkeypatch: pytest.MonkeyPatch,
                                   tmp_db_path: Path, tmp_config_path: Path) -> None:
        """无效 type/target → 400;共享给自己 → 400;重复 target 去重。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        for bad in (
            {"type": "everyone", "targets": []},
            {"type": "shared", "targets": "alice"},
            {"type": "shared", "targets": []},
            {"type": "shared", "targets": ["ghost"]},
            {"type": "shared", "targets": ["alice"]},   # 不能共享给自己
            {"type": "public", "targets": ["alice"]},
            {"type": "shared", "targets": ["admin", "bob"]},  # bob 尚不存在
        ):
            resp = alice.put("/api/my/platforms/kimi", json={
                "enabled": True, "visibility": bad,
            })
            assert resp.status_code == 400, bad
        # 注册 bob 后共享生效;重复 target 去重
        bob = self._register(client, "bob")
        resp = alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "visibility": {"type": "shared", "targets": ["bob", "bob"]},
        })
        assert resp.status_code == 200
        assert resp.json()["visibility"] == {"type": "shared", "targets": ["bob"]}
        bob_kimi = [p for p in bob.get("/api/usage").json()["platforms"]
                    if p["name"] == "kimi"]
        assert bob_kimi and bob_kimi[0]["display_name"] == "Kimi Code(alice)"
        # admin 侧同样校验
        resp = client.put("/api/config/platforms/kimi", json={
            "visibility": {"type": "shared", "targets": ["ghost"]},
        })
        assert resp.status_code == 400
        resp = client.put("/api/config/platforms/kimi", json={
            "visibility": {"type": "shared", "targets": ["admin"]},
        })
        assert resp.status_code == 400

    def test_share_to_admin_and_duplicate_owners(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """共享给 admin 合法(其仪表盘全量,无实际效果);多 owner 共享同平台全部保留。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        bob = self._register(client, "bob")
        resp = alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "visibility": {"type": "shared", "targets": ["admin"]},
        })
        assert resp.status_code == 200  # 允许共享给 admin
        # 两个 owner 公开同一平台 → 观众同时看到两个来源,后缀区分,不丢弃
        alice.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "visibility": {"type": "public", "targets": []},
        })
        bob.put("/api/my/platforms/kimi", json={
            "enabled": True,
            "visibility": {"type": "public", "targets": []},
        })
        carol = self._register(client, "carol")
        kimi = carol.get("/api/usage").json()["platforms"]
        names = [(p["name"], p["display_name"]) for p in kimi]
        assert names == [("kimi", "Kimi Code(alice)"), ("kimi", "Kimi Code(bob)")]
        # admin 仪表盘:自己的 config.toml kimi 裸名,无共享后缀
        viewer = TestClient(client.app)
        viewer.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        kimi = [p for p in viewer.get("/api/usage").json()["platforms"]
                if p["name"] == "kimi"]
        assert kimi and kimi[0]["display_name"] == "Kimi Code"

    def test_shared_config_deduped_by_config_hash(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """同配置多用户共享同一缓存条目(配置哈希驱动,不重复 fetch)。"""
        init_config(tmp_config_path)
        client, calls = self._config_client(monkeypatch, load_config())
        alice = self._register(client, "alice")
        bob = self._register(client, "bob")
        alice.put("/api/my/platforms/kimi", json={
            "enabled": True, "visibility": {"type": "public", "targets": []},
        })
        bob.put("/api/my/platforms/kimi", json={
            "enabled": True, "visibility": {"type": "public", "targets": []},
        })
        alice.get("/api/usage")
        bob.get("/api/usage")
        # alice/bob 配置哈希相同 → 两人共享同一条缓存,总共只 fetch 一次(去重的证据)
        assert calls[0] == 1

    def test_config_page_dual_mode_hooks(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """配置页对普通用户开放(JS 双模式),页面含普通用户端点与可见性控件。"""
        init_config(tmp_config_path)
        client, _ = self._config_client(monkeypatch, load_config())
        html = client.get("/config").text
        assert "/api/my/platforms" in html       # 普通用户模式端点
        assert "buildVisibilityRow" in html      # 可见性控件
        assert "dragstart" in html               # admin 拖拽排序保留
        alice = self._register(client, "alice")
        alice_html = alice.get("/config").text
        assert "/api/my/platforms" in alice_html


class TestSecurityHardening:
    """安全加固:登录限流 / 安全响应头 / Secure Cookie 开关 / 用户名字符集。"""

    def test_login_rate_limited(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        _patch_fetch(monkeypatch)
        store.create_user("admin", "secret1", is_admin=True)
        client = TestClient(create_app({"platforms": {}}))
        for _ in range(5):
            resp = client.post(
                "/api/auth/login", json={"username": "admin", "password": "wrong"}
            )
            assert resp.status_code == 401
        # 第 6 次失败 → 429
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "wrong"}
        )
        assert resp.status_code == 429
        # 锁定期内正确密码也 429;logout 不受限流但会话仍在锁内
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        assert resp.status_code == 429
        assert client.post("/api/auth/logout").status_code == 200
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        assert resp.status_code == 429
        # 新 app 实例(新 limiter)→ 正常登录
        other = TestClient(create_app({"platforms": {}}))
        resp = other.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        assert resp.status_code == 200

    def test_limiter_clear_on_success(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        """成功登录清空失败计数:之后再错 5 次不触发 429。"""
        _patch_fetch(monkeypatch)
        store.create_user("admin", "secret1", is_admin=True)
        client = TestClient(create_app({"platforms": {}}))
        for _ in range(4):
            client.post(
                "/api/auth/login", json={"username": "admin", "password": "wrong"}
            )
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        assert resp.status_code == 200
        for _ in range(5):
            resp = client.post(
                "/api/auth/login", json={"username": "admin", "password": "wrong"}
            )
            assert resp.status_code == 401

    def test_security_headers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        _patch_fetch(monkeypatch)
        client = TestClient(create_app({"platforms": {}}))
        for path in ("/login", "/api/usage"):
            resp = client.get(path)
            assert resp.headers["X-Frame-Options"] == "DENY"
            assert resp.headers["X-Content-Type-Options"] == "nosniff"
            assert resp.headers["Referrer-Policy"] == "no-referrer"

    def test_secure_cookie_env_flag(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        monkeypatch.setenv("LLM_USAGE_SECURE_COOKIE", "1")
        _patch_fetch(monkeypatch)
        store.create_user("admin", "secret1", is_admin=True)
        client = TestClient(create_app({"platforms": {}}))
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        assert resp.status_code == 200
        cookie = resp.headers["set-cookie"]
        assert "secure" in cookie.lower()

    def test_cookie_not_secure_by_default(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        monkeypatch.delenv("LLM_USAGE_SECURE_COOKIE", raising=False)
        _patch_fetch(monkeypatch)
        store.create_user("admin", "secret1", is_admin=True)
        client = TestClient(create_app({"platforms": {}}))
        resp = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        assert resp.status_code == 200
        assert "secure" not in resp.headers["set-cookie"].lower()

    def test_username_charset_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        for name in ["a/b", "<img>", "x y", "a\n", "x" * 65, "  ", "中文名"]:
            resp = client.post(
                "/api/users", json={"username": name, "password": "secret1"}
            )
            assert resp.status_code == 400, repr(name)


class TestRefreshIsolation:
    def test_refresh_only_invalidates_own_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path
    ) -> None:
        """POST /api/refresh 只失效自己的缓存条目,他人配置不重拉。"""
        calls = {"alice": 0, "bob": 0}

        def fake(cfg):
            calls[cfg["platforms"]["kimi"]["api_key"]] += 1
            return _fake_results()

        monkeypatch.setattr("llm_usage.web.fetch_all", fake)
        store.create_user("alice", "secret1", is_admin=False)
        store.create_user("bob", "secret1", is_admin=False)
        store.set_user_config(
            "alice", {"platforms": {"kimi": {"enabled": True, "api_key": "alice"}}}
        )
        store.set_user_config(
            "bob", {"platforms": {"kimi": {"enabled": True, "api_key": "bob"}}}
        )
        app = create_app({"platforms": {}})
        alice = TestClient(app)
        bob = TestClient(app)
        assert alice.post(
            "/api/auth/login", json={"username": "alice", "password": "secret1"}
        ).status_code == 200
        assert bob.post(
            "/api/auth/login", json={"username": "bob", "password": "secret1"}
        ).status_code == 200
        # 各自首次拉取
        alice.get("/api/usage")
        bob.get("/api/usage")
        assert calls == {"alice": 1, "bob": 1}
        # alice 刷新 → 只重拉 alice 的配置
        assert alice.post("/api/refresh").status_code == 200
        assert calls == {"alice": 2, "bob": 1}
        # bob 缓存仍新鲜 → 不变
        bob.get("/api/usage")
        assert calls == {"alice": 2, "bob": 1}


class TestProviderInstances:
    """同类型供应商实例(多账号卡片):添加/删除/列表排序/共享传播。"""

    def _client(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_path: Path,
    ) -> TestClient:
        """App + 已登录 admin;fetch 假实现支持实例键(经 resolve_provider_key 判型)。"""
        from llm_usage.providers import DISPLAY_NAMES, resolve_provider_key

        def fake(cfg_in):
            platforms = (cfg_in or {}).get("platforms", {}) or {}
            out = []
            for key, section in platforms.items():
                ptype = resolve_provider_key(key)
                if ptype is None or not isinstance(section, dict):
                    continue
                if section.get("enabled", True) is False:
                    continue
                entry = UsageEntry(key, "一次", 60, 120, 60, 50.0, None, "%", False)
                out.append(PlatformResult(
                    key, section.get("display_name") or DISPLAY_NAMES[ptype],
                    entries=[entry],
                ))
            return out

        monkeypatch.setattr("llm_usage.web.fetch_all", fake)
        client = TestClient(create_app(load_config(), interval=60))
        store.create_user("admin", "secret1", is_admin=True)
        r = client.post(
            "/api/auth/login", json={"username": "admin", "password": "secret1"}
        )
        assert r.status_code == 200
        return client

    def _register(self, client: TestClient, username: str) -> TestClient:
        client.put("/api/settings", json={"registration_enabled": True})
        anon = TestClient(client.app)
        r = anon.post(
            "/api/auth/register", json={"username": username, "password": "secret1"}
        )
        assert r.status_code == 200
        return anon

    def test_provider_types_requires_login(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client = self._client(monkeypatch, tmp_config_path)
        anon = TestClient(client.app)
        assert anon.get("/api/provider-types").status_code == 401

    def test_admin_add_list_and_full_chain(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client = self._client(monkeypatch, tmp_config_path)
        alice = self._register(client, "alice")
        types = client.get("/api/provider-types").json()["types"]
        assert {t["key"]: t["label"] for t in types}["kimi"] == "Kimi Code"
        # 添加 → kimi#2,默认禁用 + 默认显示名 + 私有可见性
        resp = client.post("/api/config/providers", json={"type": "kimi"})
        assert resp.status_code == 200
        view = resp.json()
        assert view["key"] == "kimi#2"
        assert view["type"] == "kimi"
        assert view["display_name"] == "Kimi Code #2"
        assert view["enabled"] is False
        assert view["visibility"] == {"type": "private", "targets": []}
        # 落盘 config.toml
        section = load_config()["platforms"]["kimi#2"]
        assert section["enabled"] is False
        assert section["display_name"] == "Kimi Code #2"
        # 列表紧随基础键
        keys = [p["key"] for p in client.get("/api/config").json()["platforms"]]
        assert keys.index("kimi#2") == keys.index("kimi") + 1
        # 连加两次单调递增 → kimi#3
        resp = client.post("/api/config/providers", json={"type": "kimi"})
        assert resp.json()["key"] == "kimi#3"
        # 全链路:启用 + 显示名 + 凭证槽 + 可见性
        resp = client.put("/api/config/platforms/kimi%232", json={
            "enabled": True,
            "display_name": "工作号K",
            "credential_slots": [
                {"index": None, "name": "工作套餐", "api_key": "sk-second-account"},
            ],
            "visibility": {"type": "shared", "targets": ["alice"]},
        })
        assert resp.status_code == 200
        view = resp.json()
        assert view["display_name"] == "工作号K"
        assert view["enabled"] is True
        assert view["credential_slots"][0]["credentials"]["api_key"]["hint"] == "sk-s…nt"
        assert view["visibility"] == {"type": "shared", "targets": ["alice"]}
        cfg = load_config()["platforms"]["kimi#2"]
        assert cfg["credentials"] == [{"name": "工作套餐", "api_key": "sk-second-account"}]
        assert "api_key" not in cfg  # 槽式保存后清除顶层凭证
        # 共享传播:alice 见 工作号K(admin)
        names = [p["display_name"] for p in alice.get("/api/usage").json()["platforms"]]
        assert names == ["工作号K(admin)"]

    def test_admin_delete_cascade_and_guards(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client = self._client(monkeypatch, tmp_config_path)
        self._register(client, "alice")
        client.post("/api/config/providers", json={"type": "kimi"})
        client.put("/api/config/platforms/kimi%232", json={
            "visibility": {"type": "shared", "targets": ["alice"]},
        })
        assert ("kimi#2", "alice") in {
            (r["platform"], r["target"])
            for r in store.list_my_visibility("admin")
        }
        # 裸键不可删
        resp = client.delete("/api/config/platforms/kimi")
        assert resp.status_code == 400
        assert "内置平台" in resp.json()["detail"]
        # 未配置的实例 404
        assert client.delete("/api/config/platforms/kimi%239").status_code == 404
        # 未知类型 404
        assert client.delete("/api/config/platforms/foo%239").status_code == 404
        # 正常删除 → 配置与共享行级联清理
        assert client.delete("/api/config/platforms/kimi%232").json() == {"ok": True}
        keys = [p["key"] for p in client.get("/api/config").json()["platforms"]]
        assert "kimi#2" not in keys
        assert "kimi#2" not in load_config().get("platforms", {})
        assert store.list_my_visibility("admin") == []

    def test_add_provider_validation(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client = self._client(monkeypatch, tmp_config_path)
        resp = client.post("/api/config/providers", json={"type": "foo"})
        assert resp.status_code == 400
        assert "未知供应商类型" in resp.json()["detail"]
        resp = client.post(
            "/api/config/providers", json={"type": "kimi", "extra": 1}
        )
        assert resp.status_code == 400
        assert "未知字段" in resp.json()["detail"]

    def test_my_add_delete_propagation_and_admin_rejected(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client = self._client(monkeypatch, tmp_config_path)
        alice = self._register(client, "alice")
        bob = self._register(client, "bob")
        # admin 不能用普通用户端点
        assert client.post("/api/my/providers", json={"type": "kimi"}).status_code == 400
        # alice 添加 ollama 实例 → 写入自己的 user_configs
        resp = alice.post("/api/my/providers", json={"type": "ollama"})
        assert resp.status_code == 200
        view = resp.json()
        assert view["key"] == "ollama#2"
        assert view["type"] == "ollama"
        assert view["enabled"] is False
        assert store.get_user_config("alice")["platforms"]["ollama#2"] == {
            "enabled": False,
            "display_name": "Ollama Cloud #2",
        }
        # 列表中紧随 ollama
        keys = [p["key"] for p in alice.get("/api/my/platforms").json()["platforms"]]
        assert keys.index("ollama#2") == keys.index("ollama") + 1
        # 启用并公开 → bob 见 Ollama Cloud #2(alice);alice 自见裸名
        resp = alice.put("/api/my/platforms/ollama%232", json={
            "enabled": True,
            "visibility": {"type": "public", "targets": []},
        })
        assert resp.status_code == 200
        bob_names = {
            p["name"]: p["display_name"]
            for p in bob.get("/api/usage").json()["platforms"]
        }
        assert bob_names == {"ollama#2": "Ollama Cloud #2(alice)"}
        alice_names = [
            p["display_name"] for p in alice.get("/api/usage").json()["platforms"]
        ]
        assert alice_names == ["Ollama Cloud #2"]
        # 删除后传播消失
        assert alice.delete("/api/my/platforms/ollama%232").json() == {"ok": True}
        assert store.get_user_config("alice").get("platforms", {}) == {}
        assert bob.get("/api/usage").json()["platforms"] == []
        # 守卫:裸键 400 / 未配置 404
        assert alice.delete("/api/my/platforms/kimi").status_code == 400
        assert alice.delete("/api/my/platforms/ollama%232").status_code == 404

    def test_order_endpoint_keeps_instances(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client = self._client(monkeypatch, tmp_config_path)
        client.post("/api/config/providers", json={"type": "kimi"})
        order = ["kimi#2", "kimi", "volcengine-coding", "volcengine-agent",
                 "ollama", "opencode-go", "clinepass", "llm-gateway"]
        resp = client.put("/api/config/order", json={"order": order})
        assert resp.status_code == 200
        assert resp.json()["order"] == order  # 实例键不被过滤
        assert load_config()["platform_order"] == order
        # 列表排序仍按类型分组:kimi 在 kimi#2 前
        keys = [p["key"] for p in client.get("/api/config").json()["platforms"]]
        assert keys.index("kimi") < keys.index("kimi#2")

    def test_list_endpoints_tolerate_unknown_configured_sections(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """手改配置里的未知平台段不炸列表端点:被过滤忽略(实例键解析失败的容错)。"""
        init_config(tmp_config_path)
        update_platform_config("legacy-thing", {"enabled": True})
        client = self._client(monkeypatch, tmp_config_path)
        resp = client.get("/api/config")
        assert resp.status_code == 200
        from llm_usage.providers import PROVIDERS

        keys = [p["key"] for p in resp.json()["platforms"]]
        assert "legacy-thing" not in keys
        assert set(keys) == set(PROVIDERS)
        # 普通用户侧同样容错
        alice = self._register(client, "alice")
        store.set_user_config("alice", {"platforms": {"junk-key": {"enabled": True}}})
        resp = alice.get("/api/my/platforms")
        assert resp.status_code == 200
        keys = [p["key"] for p in resp.json()["platforms"]]
        assert "junk-key" not in keys

    def test_delete_then_readd_never_reuses_numbers(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """删除唯一/最高实例后再添加 → 编号继续递增(admin 与普通用户双路径)。"""
        init_config(tmp_config_path)
        client = self._client(monkeypatch, tmp_config_path)
        alice = self._register(client, "alice")
        # admin: 添加 kimi#2 → 删除 → 再添加得 kimi#3
        assert client.post("/api/config/providers", json={"type": "kimi"}).json()["key"] == "kimi#2"
        assert client.delete("/api/config/platforms/kimi%232").json() == {"ok": True}
        resp = client.post("/api/config/providers", json={"type": "kimi"})
        assert resp.json()["key"] == "kimi#3"
        counters = load_config().get("instance_counters")
        assert counters["kimi"] >= 3
        # user: alice 添加 ollama#2 → 删除 → 再添加得 ollama#3(计数器存自己的 user_configs)
        assert alice.post("/api/my/providers", json={"type": "ollama"}).json()["key"] == "ollama#2"
        assert alice.delete("/api/my/platforms/ollama%232").json() == {"ok": True}
        resp = alice.post("/api/my/providers", json={"type": "ollama"})
        assert resp.json()["key"] == "ollama#3"
        cfg = store.get_user_config("alice")
        assert cfg["instance_counters"]["ollama"] >= 3
        # 计数器不参与平台列表
        keys = [p["key"] for p in alice.get("/api/my/platforms").json()["platforms"]]
        assert set(keys) & {"instance_counters"} == set()

    def test_malformed_counter_tolerated_and_self_healed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        """手改/损坏的 instance_counters 值不炸添加端点:按缺省分配并自愈为整数。"""
        init_config(tmp_config_path)
        client = self._client(monkeypatch, tmp_config_path)
        cfg = load_config()
        cfg["instance_counters"] = {"kimi": "9"}
        from llm_usage.config import save_config
        save_config(cfg)
        resp = client.post("/api/config/providers", json={"type": "kimi"})
        assert resp.status_code == 200
        assert resp.json()["key"] == "kimi#2"
        counters = load_config()["instance_counters"]
        assert counters["kimi"] == 2  # 损坏值被整数覆写
