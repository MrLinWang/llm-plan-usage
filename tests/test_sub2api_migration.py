"""Renaming a provider must preserve accounts, history, visibility and instance IDs."""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
import tomli_w

from llm_usage import store
from llm_usage.config import (
    load_config,
    remove_platform_config,
    save_config,
    update_platform_config,
)
from llm_usage.models import PlatformResult
from llm_usage.platform_keys import (
    PlatformKeyConflict,
    canonical_platform_key,
    normalize_platform_config,
    platform_key_aliases,
)
from llm_usage.providers import PROVIDERS, fetch_all, next_instance_key, resolve_provider_key
from llm_usage.providers.sub2api import Sub2ApiProvider


def _section() -> dict:
    return {
        "enabled": False, "display_name": "团队网关", "base_url": "https://sub2api.test",
        "use_groups": True, "groups": [
            {"name": "主账户", "api_keys": [{"name": "主 Key", "value": "env:OLD_KEY"}]},
        ],
    }


@pytest.mark.parametrize(("old", "new"), [
    ("llm-gateway", "sub2api"), ("llm-gateway#2", "sub2api#2"),
    ("llm-gateway#12", "sub2api#12"), ("sub2api#2", "sub2api#2"),
    ("llm-gateway#other", "llm-gateway#other"), ("kimi", "kimi"),
])
def test_key_aliases_only_rename_valid_provider_keys(old, new) -> None:
    assert canonical_platform_key(old) == new
    assert platform_key_aliases(old)[0] == new
    if new.startswith("sub2api"):
        assert resolve_provider_key(old) == "sub2api"


def test_config_normalization_preserves_data_and_instance_high_water() -> None:
    config = {
        "platforms": {"llm-gateway": _section(), "llm-gateway#2": _section(), "kimi": {}},
        "platform_order": ["llm-gateway", "kimi", "llm-gateway#2", "sub2api"],
        "instance_counters": {"llm-gateway": 12, "sub2api": 4, "kimi": 7},
        "max_retries": 1,
    }
    original = copy.deepcopy(config)
    normalized = normalize_platform_config(config)
    assert config == original
    assert list(normalized["platforms"]) == ["sub2api", "sub2api#2", "kimi"]
    assert normalized["platforms"]["sub2api"] == original["platforms"]["llm-gateway"]
    assert normalized["platform_order"] == ["sub2api", "kimi", "sub2api#2"]
    assert normalized["instance_counters"] == {"sub2api": 12, "kimi": 7}
    assert next_instance_key(config["platforms"], "llm-gateway", config["instance_counters"]) == (
        "sub2api#13"
    )
    assert normalize_platform_config(normalized) == normalized


def test_identical_alias_sections_collapse_without_duplicate_fetches() -> None:
    normalized = normalize_platform_config({"platforms": {
        "sub2api": _section(), "llm-gateway": _section(),
    }})
    assert list(normalized["platforms"]) == ["sub2api"]


def test_conflicting_accounts_are_not_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    existing = {"platforms": {"kimi": {"enabled": False}}}
    save_config(existing, path)
    before = path.read_bytes()
    config = {"platforms": {
        "sub2api": {"api_key": "different-private-account"},
        "llm-gateway": {"api_key": "original-private-account"},
    }}
    with pytest.raises(PlatformKeyConflict) as error:
        save_config(config, path)
    assert path.read_bytes() == before
    assert "private-account" not in str(error.value)
    path.write_text(tomli_w.dumps(config))
    with pytest.raises(RuntimeError, match="冲突"):
        load_config(path)


def test_legacy_toml_is_read_then_saved_with_canonical_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.toml"
    config = {
        "platforms": {"llm-gateway": _section(), "llm-gateway#2": _section()},
        "platform_order": ["llm-gateway"], "instance_counters": {"llm-gateway": 9},
    }
    path.write_text(tomli_w.dumps(config))
    assert list(load_config(path)["platforms"]) == ["sub2api", "sub2api#2"]
    updated = update_platform_config("llm-gateway", {"enabled": True}, path)
    assert updated["platforms"]["sub2api"]["groups"] == _section()["groups"]
    assert "llm-gateway" not in path.read_text()
    assert load_config(path)["instance_counters"]["sub2api"] == 9
    remove_platform_config("llm-gateway#2", path)
    assert set(load_config(path)["platforms"]) == {"sub2api"}


def test_legacy_instance_fetch_uses_groups_and_emits_canonical_ids(monkeypatch) -> None:
    calls = []

    def handler(request):
        calls.append(request)
        assert request.headers["authorization"] == "Bearer group-key"
        return httpx.Response(200, json={"subscription": {
            "daily_usage_usd": 1.5, "daily_limit_usd": 20,
            "weekly_usage_usd": 3, "weekly_limit_usd": 100,
        }})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        monkeypatch.setitem(PROVIDERS, "sub2api", Sub2ApiProvider(client))
        results = fetch_all({"platforms": {"llm-gateway#2": {
            "base_url": "https://sub2api.test", "groups": [
                {"name": "主账户", "api_keys": ["group-key"]},
            ],
            "credentials": [{"api_key": "not-a-gateway-credential"}],
        }}})
    assert len(calls) == 1 and len(results) == 1 and results[0].ok
    assert results[0].platform == "sub2api#2" and results[0].display_name == "Sub2API"
    assert all(entry.platform == "sub2api#2" for entry in results[0].entries)
    assert all(entry.plan == "主账户" for entry in results[0].entries)
    assert results[0].entries[0].used == 1.5


def _legacy_db(path: Path, *, conflicting: bool = False) -> None:
    now = datetime.now(UTC)
    config = {"platforms": {"llm-gateway#2": _section()},
              "platform_order": ["llm-gateway#2"], "instance_counters": {"llm-gateway": 9}}
    if conflicting:
        config["platforms"]["sub2api#2"] = {"api_key": "other-account"}
    with sqlite3.connect(path) as conn:
        conn.executescript(store._SCHEMA)
        conn.execute("INSERT INTO users VALUES (?, ?, 0, ?)", ("alice", "unused", now.isoformat()))
        conn.execute("INSERT INTO users VALUES (?, ?, 0, ?)", ("bob", "unused", now.isoformat()))
        conn.execute("INSERT INTO sessions VALUES (?, ?, ?)",
                     ("existing-session", "alice", (now + timedelta(days=1)).isoformat()))
        conn.execute("INSERT INTO user_configs VALUES (?, ?, ?)",
                     ("alice", json.dumps(config), now.isoformat()))
        for platform in ("llm-gateway", "llm-gateway#2", "sub2api#2"):
            conn.execute(
                'INSERT INTO snapshots (ts,platform,label,used,"limit",remaining,unit) '
                "VALUES (?,?, '每日', 1, 10, 9, '$')", (now.isoformat(), platform),
            )
        for platform in ("llm-gateway#2", "sub2api#2"):
            conn.execute("INSERT INTO user_shares VALUES (?, ?, ?)", ("alice", platform, "bob"))
        conn.execute("INSERT INTO settings VALUES ('registration_enabled','1')")


def test_database_migration_preserves_history_shares_sessions_and_user_config(tmp_db_path) -> None:
    _legacy_db(tmp_db_path)
    rows = store.query_history(path=tmp_db_path)
    assert len(rows) == 3
    assert [row["platform"] for row in rows] == ["sub2api", "sub2api#2", "sub2api#2"]
    assert len(store.query_history(platform="llm-gateway#2", path=tmp_db_path)) == 2
    config = store.get_user_config("alice", tmp_db_path)
    assert config["platforms"]["sub2api#2"] == _section()
    assert config["instance_counters"] == {"sub2api": 9}
    assert store.list_shared_platforms("bob", tmp_db_path) == [
        {"owner": "alice", "platform": "sub2api#2"},
    ]
    assert store.get_session_user("existing-session", tmp_db_path)["username"] == "alice"
    assert store.get_setting("registration_enabled", tmp_db_path) == "1"
    with sqlite3.connect(tmp_db_path) as conn:
        saved = conn.execute("SELECT config FROM user_configs").fetchone()[0]
        assert "llm-gateway" not in saved
        assert conn.execute("SELECT COUNT(*) FROM user_shares").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0] == 3
    store._init_done.discard(str(tmp_db_path))
    assert len(store.query_history(path=tmp_db_path)) == 3  # restart migration is idempotent
    store.replace_platform_visibility("alice", "llm-gateway#2", [], tmp_db_path)
    assert store.list_shared_platforms("bob", tmp_db_path) == []


def test_conflicting_user_config_is_preserved_and_visibility_can_be_cleared(tmp_db_path) -> None:
    _legacy_db(tmp_db_path, conflicting=True)
    store.query_history(path=tmp_db_path)
    with pytest.raises(PlatformKeyConflict):
        store.get_user_config("alice", tmp_db_path)
    with sqlite3.connect(tmp_db_path) as conn:
        saved = json.loads(conn.execute("SELECT config FROM user_configs").fetchone()[0])
        assert set(saved["platforms"]) == {"llm-gateway#2", "sub2api#2"}
        assert conn.execute("SELECT COUNT(*) FROM user_shares").fetchone()[0] == 2
    store.set_user_config("alice", {"platforms": {"sub2api#2": _section()}}, tmp_db_path)
    store.replace_platform_visibility("alice", "sub2api#2", [], tmp_db_path)
    assert store.list_shared_platforms("bob", tmp_db_path) == []


def test_web_uses_canonical_keys_and_accepts_legacy_editor_and_import(
    tmp_config_path, tmp_db_path, monkeypatch,
) -> None:
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from llm_usage.web import create_app

    config = {"platforms": {"llm-gateway": _section(), "llm-gateway#2": _section()},
              "instance_counters": {"llm-gateway": 7}}
    tmp_config_path.write_text(tomli_w.dumps(config))
    monkeypatch.setattr("llm_usage.web.fetch_all", lambda cfg: [
        PlatformResult(key, section.get("display_name", "Sub2API"))
        for key, section in cfg.get("platforms", {}).items() if section.get("enabled", True)
    ])
    with TestClient(create_app(config)) as admin:
        assert admin.post("/api/auth/setup", json={
            "username": "admin", "password": "secret1",
        }).status_code == 200
        types = admin.get("/api/provider-types").json()["types"]
        assert {"key": "sub2api", "label": "Sub2API"} in types
        assert "llm-gateway" not in [entry["key"] for entry in types]
        views = admin.get("/api/config").json()["platforms"]
        assert {"sub2api", "sub2api#2"} <= {view["key"] for view in views}
        assert all("llm-gateway" not in view["key"] for view in views)
        created = admin.post("/api/config/providers", json={"type": "sub2api"})
        assert created.status_code == 200 and created.json()["key"] == "sub2api#8"
        assert "llm-gateway" not in tmp_config_path.read_text()
        updated = admin.put("/api/config/platforms/llm-gateway", json={"enabled": True})
        assert updated.status_code == 200 and updated.json()["key"] == "sub2api"
        assert updated.json()["groups"][0]["name"] == "主账户"
        ordered = admin.put("/api/config/order", json={
            "order": ["llm-gateway", "sub2api", "llm-gateway#2"],
        }).json()["order"]
        assert ordered[:2] == ["sub2api", "sub2api#2"]
        assert len(ordered) == len(set(ordered))
        legacy_instance = quote("llm-gateway#8", safe="")
        assert admin.delete("/api/config/platforms/" + legacy_instance).status_code == 200
        store.create_user("alice", "secret1")
        with TestClient(admin.app) as alice:
            assert alice.post("/api/auth/login", json={
                "username": "alice", "password": "secret1",
            }).status_code == 200
            imported = alice.post("/api/my/import", json={"content": tomli_w.dumps(config)})
            assert imported.status_code == 200
            assert set(store.get_user_config("alice")["platforms"]) == {"sub2api", "sub2api#2"}
            updated = alice.put("/api/my/platforms/llm-gateway", json={"enabled": True})
            assert updated.status_code == 200 and updated.json()["key"] == "sub2api"
            assert alice.get("/api/usage").json()["platforms"][0]["name"] == "sub2api"
            invalid = alice.post("/api/my/import", json={"content": tomli_w.dumps({
                "platforms": {"llm-gateway": {"api_key": "one"}, "sub2api": {"api_key": "two"}},
            })})
            assert invalid.status_code == 400
