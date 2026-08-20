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
    def test_get_config_credential_views(
        self, monkeypatch: pytest.MonkeyPatch, tmp_db_path: Path,
        tmp_config_path: Path,
    ) -> None:
        init_config(tmp_config_path)
        client, _ = _auth_client(monkeypatch, tmp_db_path)
        platforms = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}
        # 模板中的 env: 引用原样出现在 env 字段
        kimi = platforms["kimi"]["credentials"]["api_key"]
        assert kimi == {"set": True, "env": "env:KIMI_API_KEY", "hint": None}
        volc = platforms["volcengine-coding"]["credentials"]
        assert volc["access_key"]["env"] == "env:VOLCENGINE_ACCESS_KEY"
        assert volc["secret_key"]["env"] == "env:VOLCENGINE_SECRET_KEY"
        # 字面量密钥 → 脱敏 hint,env 为 null
        update_platform_config("kimi", {"api_key": "sk-kimi-secret-key"})
        kimi = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}[
            "kimi"
        ]["credentials"]["api_key"]
        assert kimi == {"set": True, "env": None, "hint": "sk-k…ey"}
        # 短密钥无法取头尾 → 固定掩码
        update_platform_config("kimi", {"api_key": "short"})
        kimi = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}[
            "kimi"
        ]["credentials"]["api_key"]
        assert kimi["hint"] == "••••"
        # 未设置字段 → set 为 False
        cfg = load_config()
        del cfg["platforms"]["ollama"]["api_key"]
        from llm_usage.config import save_config

        save_config(cfg)
        ollama = {p["key"]: p for p in client.get("/api/config").json()["platforms"]}[
            "ollama"
        ]["credentials"]["api_key"]
        assert ollama == {"set": False, "env": None, "hint": None}

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
        assert resp.json()["credentials"]["api_key"]["hint"] == "sk-n…23"

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
