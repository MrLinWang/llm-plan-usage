"""Unified TOML accounts survive imports, web edits and browser authorization."""

from __future__ import annotations

import copy
import tomllib
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

import test_opendesign_auth_web as auth_web  # noqa: E402

from llm_usage import config as config_mod  # noqa: E402
from llm_usage import store  # noqa: E402
from llm_usage.config import load_config  # noqa: E402

# Reuse the real device manager and its MockTransport, including fixture cleanup.
auth_client_factory = auth_web.auth_client_factory

ACCOUNTS_TOML = """\
platform_order = ["kimi", "volcengine-coding", "opendesign", "sub2api"]

[platforms.kimi]
enabled = true
[[platforms.kimi.accounts]]
name = "工作账户"
api_key = "sk-kimi-accounts-private"
[[platforms.kimi.accounts]]
name = "个人账户"
api_key = "env:KIMI_API_KEY"

[platforms.volcengine-coding]
enabled = false
tier = "pro"
[[platforms.volcengine-coding.accounts]]
name = "火山账户"
access_key = "AKLT-accounts-private"
secret_key = "volc-accounts-private"

[platforms.opendesign]
max_balance = 100
[[platforms.opendesign.accounts]]
name = "设计账户"
control_key = "vela_ctrl_accounts_private"

[platforms.sub2api]
base_url = "https://sub2api.test"
[[platforms.sub2api.accounts]]
name = "团队账户"
daily_limit = 25
api_keys = [
  { name = "主 Key", value = "sk-sub2api-accounts-private" },
  { name = "备用 Key", value = "env:SUB2API_API_KEY" },
]
"""

PRIVATE_VALUES = (
    "sk-kimi-accounts-private", "AKLT-accounts-private", "volc-accounts-private",
    "vela_ctrl_accounts_private", "sk-sub2api-accounts-private",
)


def _endpoint(admin: bool, suffix: str = "") -> str:
    return ("/api/config" if admin else "/api/my") + suffix


def _read_for(admin: bool) -> dict[str, Any]:
    return load_config() if admin else store.get_user_config("alice")


def _raw_accounts(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    # 落盘版式:每个供应商一个 [platforms.<key>] 表,accounts 在表内,无子表头
    headers = [line for line in text.splitlines() if line.lstrip().startswith("[")]
    assert headers, text
    for line in headers:
        assert not line.startswith("[[platforms."), line
        assert line.removeprefix("[platforms.").removesuffix("]").count(".") == 0, line
    raw = tomllib.loads(text)
    for section in raw.get("platforms", {}).values():
        assert "accounts" in section
        assert not {"credentials", "groups", "api_key", "control_key",
                    "access_key", "secret_key"}.intersection(section)
    return raw


@pytest.mark.parametrize("admin", [True, False])
def test_accounts_import_decodes_credentials_masks_views_and_isolates_users(
    auth_client_factory: Any, tmp_config_path: Path,
    monkeypatch: pytest.MonkeyPatch, admin: bool,
) -> None:
    client, _ = auth_client_factory({"platforms": {
        "ollama": {"enabled": False, "api_key": "sk-admin-sentinel-private"},
    }}, admin=admin)
    admin_before = tmp_config_path.read_bytes()
    fetched: list[dict[str, Any]] = []

    def fetch(config: dict[str, Any]) -> list[Any]:
        fetched.append(copy.deepcopy(config))
        return []

    monkeypatch.setattr("llm_usage.web.fetch_all", fetch)
    response = client.post(_endpoint(admin, "/import"), json={"content": ACCOUNTS_TOML})
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "platforms": 4, "enabled": 3}
    views_response = client.get("/api/config" if admin else "/api/my/platforms")
    assert views_response.status_code == 200
    for private in PRIVATE_VALUES:
        assert private not in views_response.text
    views = {item["key"]: item for item in views_response.json()["platforms"]}
    slots = views["kimi"]["credential_slots"]
    assert [item["name"] for item in slots] == ["工作账户", "个人账户"]
    assert slots[0]["credentials"]["api_key"]["set"] is True
    assert slots[1]["credentials"]["api_key"]["env"] == "env:KIMI_API_KEY"
    assert views["volcengine-coding"]["credential_slots"][0]["credentials"][
        "secret_key"
    ]["set"] is True
    assert views["opendesign"]["max_balance"] == 100
    assert views["opendesign"]["credential_slots"][0]["credentials"][
        "control_key"
    ]["set"] is True
    assert [key["name"] for key in views["sub2api"]["groups"][0]["api_keys"]] == [
        "主 Key", "备用 Key",
    ]

    assert client.get("/api/usage").status_code == 200
    assert len(fetched) == 1
    section = fetched[0]["platforms"]
    assert section["kimi"]["credentials"][0]["api_key"] == PRIVATE_VALUES[0]
    assert section["volcengine-coding"]["credentials"][0]["secret_key"] == PRIVATE_VALUES[2]
    assert section["volcengine-coding"]["tier"] == "pro"
    assert section["opendesign"]["credentials"][0]["control_key"] == PRIVATE_VALUES[3]
    assert section["sub2api"]["groups"][0]["api_keys"][0] == {
        "name": "主 Key", "value": PRIVATE_VALUES[4],
    }
    assert _read_for(admin) == fetched[0]
    if admin:
        assert set(_raw_accounts(tmp_config_path)["platforms"]) == set(section)
    else:
        assert tmp_config_path.read_bytes() == admin_before


def test_web_empty_credentials_disable_and_reenable_keep_accounts_on_disk(
    auth_client_factory: Any, tmp_config_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    client, _ = auth_client_factory()
    imported = client.post("/api/config/import", json={"content": ACCOUNTS_TOML})
    assert imported.status_code == 200, imported.text
    original = _raw_accounts(tmp_config_path)["platforms"]
    writes: list[Path] = []
    real_write = config_mod._atomic_write_bytes

    def write(path: Path, payload: bytes) -> None:
        writes.append(path)
        real_write(path, payload)

    monkeypatch.setattr(config_mod, "_atomic_write_bytes", write)
    fields = {
        "kimi": ("api_key",),
        "volcengine-coding": ("access_key", "secret_key"),
        "opendesign": ("control_key",),
    }
    for key, credential_fields in fields.items():
        view = next(item for item in client.get("/api/config").json()["platforms"]
                    if item["key"] == key)
        slots = [{"index": slot["index"], "name": None,
                  **dict.fromkeys(credential_fields, "")}
                 for slot in view["credential_slots"]]
        for enabled in (False, True):
            count_before = len(writes)
            response = client.put("/api/config/platforms/" + key, json={
                "enabled": enabled, "credential_slots": slots,
            })
            assert response.status_code == 200, response.text
            assert len(writes) == count_before + 1
            current = _raw_accounts(tmp_config_path)["platforms"]
            assert current[key]["enabled"] is enabled
            assert current[key]["accounts"] == original[key]["accounts"]
            assert current["sub2api"] == original["sub2api"]
    assert load_config()["platforms"]["volcengine-coding"]["tier"] == "pro"
    assert load_config()["platforms"]["opendesign"]["max_balance"] == 100


@pytest.mark.parametrize("admin", [True, False])
def test_accounts_sub2api_shared_keys_keep_names_and_allow_group_edits(
    auth_client_factory: Any, tmp_config_path: Path, admin: bool,
) -> None:
    client, _ = auth_client_factory(admin=admin)
    admin_before = tmp_config_path.read_bytes()
    imported = client.post(_endpoint(admin, "/import"), json={"content": ACCOUNTS_TOML})
    assert imported.status_code == 200, imported.text
    original = copy.deepcopy(_read_for(admin)["platforms"]["sub2api"]["groups"])
    endpoint = _endpoint(admin, "/platforms/sub2api")
    for enabled in (False, True):
        response = client.put(endpoint, json={
            "enabled": enabled, "base_url": "", "groups": [{
                "name": "团队账户", "daily_limit": 25,
                "api_keys": [{"index": index, "value": "", "name": None}
                             for index in range(2)],
            }],
        })
        assert response.status_code == 200, response.text
        section = _read_for(admin)["platforms"]["sub2api"]
        assert section["enabled"] is enabled
        assert section["base_url"] == "https://sub2api.test"
        assert section["groups"] == original
        for private in PRIVATE_VALUES:
            assert private not in response.text
    response = client.put(endpoint, json={"groups": [
        {"name": "团队账户", "daily_limit": 25, "api_keys": [
            {"index": 0, "value": "", "name": None},
            {"index": 1, "value": "sk-replacement-private", "name": "新备用 Key"},
        ]},
        {"name": "另一个账户", "api_keys": ["env:SUB2API_OTHER_KEY"]},
    ]})
    assert response.status_code == 200, response.text
    assert "sk-replacement-private" not in response.text
    groups = _read_for(admin)["platforms"]["sub2api"]["groups"]
    assert groups[0]["api_keys"][0] == original[0]["api_keys"][0]
    assert groups[0]["api_keys"][1] == {
        "name": "新备用 Key", "value": "sk-replacement-private",
    }
    assert groups[1]["api_keys"] == ["env:SUB2API_OTHER_KEY"]
    if admin:
        assert _raw_accounts(tmp_config_path)["platforms"]["sub2api"]["accounts"] == groups
    else:
        assert tmp_config_path.read_bytes() == admin_before


@pytest.mark.parametrize("admin", [True, False])
@pytest.mark.parametrize("append", [True, False])
def test_browser_authorization_keeps_disk_accounts_and_runtime_key(
    auth_client_factory: Any, tmp_config_path: Path, admin: bool, append: bool,
) -> None:
    old_account = {"name": "原账户", "control_key": "vela_ctrl_existing_private"}
    initial = {"platforms": {"opendesign": {
        "enabled": False, "api_key": auth_web.RUNTIME_KEY, "max_balance": 100,
        "credentials": [old_account],
    }}}
    client, upstream = auth_client_factory(initial, admin=admin)
    before = tmp_config_path.read_bytes()
    assert _raw_accounts(tmp_config_path)["platforms"]["opendesign"]["accounts"] == [
        old_account,
    ]
    target: dict[str, Any] = {"platform": "opendesign", "name": "授权账户"}
    if not append:
        target["index"] = 0
    started = client.post(auth_web.AUTH_URL, json=target)
    assert started.status_code == 200, started.text
    response = client.post(f"{auth_web.AUTH_URL}/{started.json()['id']}", json={})
    assert response.status_code == 200, response.text
    auth_web._assert_private(response)
    assert response.json()["status"] == "authorized"
    assert response.json()["slot_index"] == int(append)
    assert len(upstream.requests) == 2
    new_account = {"name": "授权账户", "control_key": auth_web.CONTROL_KEY}
    expected = [old_account, new_account] if append else [new_account]
    section = _read_for(admin)["platforms"]["opendesign"]
    assert section["credentials"] == expected
    assert section["api_key"] == auth_web.RUNTIME_KEY
    assert section["enabled"] is False
    assert section["max_balance"] == 100
    if admin:
        assert _raw_accounts(tmp_config_path)["platforms"]["opendesign"]["accounts"] == expected
    else:
        assert tmp_config_path.read_bytes() == before


@pytest.mark.parametrize("admin", [True, False])
@pytest.mark.parametrize("invalid_content", [
    '[platforms.kimi]\naccounts = "sk-malformed-private"\n',
    '[platforms.kimi]\napi_key = 123\n',
    '[platforms.kimi]\ncredentials = ["sk-malformed-private"]\n',
    '[platforms.sub2api]\ngroups = "sk-malformed-private"\n',
])
def test_invalid_accounts_import_does_not_replace_saved_config(
    auth_client_factory: Any, tmp_config_path: Path, admin: bool, invalid_content: str,
) -> None:
    client, _ = auth_client_factory({"platforms": {
        "kimi": {"api_key": "sk-original-private"},
    }}, admin=admin)
    saved = copy.deepcopy(_read_for(admin))
    before = tmp_config_path.read_bytes()
    response = client.post(_endpoint(admin, "/import"), json={
        "content": invalid_content,
    })
    assert response.status_code == 400, response.text
    assert "sk-malformed-private" not in response.text
    assert _read_for(admin) == saved
    assert tmp_config_path.read_bytes() == before
