"""Compatibility and lossless credential migration to one accounts list per provider."""

from __future__ import annotations

import tomllib
from copy import deepcopy
from typing import Any

import pytest
import tomli_w

from llm_usage.config_schema import ConfigFormatError, to_accounts_config, to_runtime_config
from llm_usage.platform_keys import PlatformKeyConflict


def _config(section: dict[str, Any], provider: str = "kimi") -> dict[str, Any]:
    return {"platforms": {provider: section}}


@pytest.mark.parametrize(("provider", "credentials"), [
    ("kimi", {"api_key": "env:KIMI_KEY"}),
    ("volcengine-coding", {"access_key": "AK-example", "secret_key": "env:ARK_SECRET"}),
    ("volcengine-agent", {"access_key": "env:ARK_ACCESS", "secret_key": "secret-example"}),
    ("ollama", {"api_key": "ollama-example"}),
    ("opencode-go", {"api_key": "env:OPENCODE_KEY"}),
    ("clinepass", {"api_key": "cline-example"}),
    ("commandcode", {"api_key": "command-example"}),
    ("opendesign", {"control_key": "env:CONTROL_KEY", "api_key": "sk-amr-example"}),
])
def test_single_credentials_use_the_same_accounts_shape(provider, credentials) -> None:
    runtime = _config({
        "enabled": False,
        "display_name": "我的账号",
        "base_url": "https://example.test/api",
        "custom_option": {"keep": [1, 2]},
        **credentials,
    }, provider)
    original = deepcopy(runtime)

    encoded = to_accounts_config(runtime)
    section = encoded["platforms"][provider]

    assert section["accounts"] == [credentials]
    assert not section.keys() & set(credentials)
    assert section["enabled"] is False
    assert section["custom_option"] == {"keep": [1, 2]}
    assert to_runtime_config(encoded) == original
    assert runtime == original
    section["custom_option"]["keep"].append(3)
    assert runtime == original


def test_multiple_slots_preserve_backup_keys_options_and_unknown_slot_fields() -> None:
    runtime = _config({
        "enabled": False,
        "tier": "pro",
        "limits": {"weekly": 1200},
        "max_balance": 100,
        "workspace_id": "workspace-example",
        "api_key": "sk-amr-backup-model-key",
        "control_key": "env:INACTIVE_CONTROL",
        "credentials": [
            {"name": "账号一", "control_key": "env:CONTROL_A", "custom": {"x": 1}},
            {"name": "账号二", "control_key": "vela_ctrl_example", "api_key": "runtime-key"},
        ],
        "groups": [],
    }, "opendesign")

    encoded = to_accounts_config(runtime)
    section = encoded["platforms"]["opendesign"]

    assert section["accounts"] == runtime["platforms"]["opendesign"]["credentials"]
    assert section["legacy"] == {
        "api_key": "sk-amr-backup-model-key",
        "control_key": "env:INACTIVE_CONTROL",
        "groups": [],
    }
    assert to_runtime_config(encoded) == runtime
    assert to_accounts_config(encoded) == encoded


def test_one_unnamed_legacy_slot_gets_a_default_name_without_activating_backup_keys() -> None:
    runtime = _config({
        "api_key": "env:DORMANT_BACKUP",
        "credentials": [{"api_key": "env:ACTIVE_KEY", "custom_option": {"keep": True}}],
    })
    original = deepcopy(runtime)

    encoded = to_accounts_config(runtime)
    section = encoded["platforms"]["kimi"]

    assert section["accounts"] == [{
        "name": "套餐1", "api_key": "env:ACTIVE_KEY", "custom_option": {"keep": True},
    }]
    assert section["legacy"] == {"api_key": "env:DORMANT_BACKUP"}
    decoded = to_runtime_config(encoded)
    assert decoded["platforms"]["kimi"]["credentials"] == section["accounts"]
    assert decoded["platforms"]["kimi"]["api_key"] == "env:DORMANT_BACKUP"
    assert to_accounts_config(encoded) == encoded
    assert runtime == original


@pytest.mark.parametrize("name", ["自定义名字", ""])
def test_existing_single_slot_names_are_never_replaced(name) -> None:
    runtime = _config({"credentials": [{"name": name, "api_key": "env:KEY"}]})
    assert to_runtime_config(to_accounts_config(runtime)) == runtime


@pytest.mark.parametrize("section", [
    {},
    {"enabled": False},
    {"credentials": []},
    {"api_key": "", "credentials": []},
    {"api_key": "env:ACTIVE_KEY", "credentials": [], "groups": []},
])
def test_empty_legacy_arrays_survive_without_injecting_runtime_fields(section) -> None:
    runtime = _config(section)
    encoded = to_accounts_config(runtime)

    assert to_runtime_config(encoded) == runtime
    assert to_accounts_config(encoded) == encoded
    if "credentials" in section:
        assert encoded["platforms"]["kimi"]["legacy"]["credentials"] == []


def test_explicit_empty_accounts_do_not_invent_credentials() -> None:
    encoded = _config({"enabled": False, "accounts": []})
    assert to_runtime_config(encoded) == _config({"enabled": False})
    assert to_accounts_config(encoded) == encoded


@pytest.mark.parametrize(("section", "account_kind"), [
    ({"api_key": "env:SINGLE"}, "single"),
    ({"api_key": "env:SINGLE", "groups": []}, "single"),
    ({"use_groups": False, "api_key": "env:SINGLE", "groups": [
        {"name": "休眠组", "api_keys": ["env:DORMANT"]},
    ]}, "single"),
    ({"use_groups": True, "api_key": "env:DORMANT", "groups": [
        {"name": "使用中的组", "api_keys": ["env:ACTIVE"]},
    ]}, "groups"),
    ({"api_key": "env:DORMANT", "groups": [
        {"name": "优先使用的组", "api_keys": ["env:ACTIVE"]},
    ]}, "groups"),
    ({"use_groups": True, "api_key": "env:DORMANT", "groups": []}, "empty"),
    ({"use_groups": True, "api_key": "env:DORMANT"}, "empty"),
    ({"use_groups": False, "groups": [{"api_keys": ["env:DORMANT"]}]}, "empty"),
    ({"groups": []}, "empty"),
    ({"credentials": []}, "empty"),
])
def test_sub2api_preserves_active_mode_and_dormant_credentials(section, account_kind) -> None:
    runtime = _config({
        "enabled": False,
        "base_url": "http://localhost:18080",
        "usage_path": "/usage/custom",
        "daily_limit": 90,
        **section,
    }, "sub2api")
    original = deepcopy(runtime)

    encoded = to_accounts_config(runtime)
    result = encoded["platforms"]["sub2api"]
    accounts = result["accounts"]

    if account_kind == "single":
        assert accounts == [{"api_key": section["api_key"]}]
    elif account_kind == "groups":
        assert accounts == section["groups"]
    else:
        assert accounts == []
    assert result.get("use_groups") == section.get("use_groups")
    assert result["daily_limit"] == 90
    assert to_runtime_config(encoded) == runtime
    assert to_accounts_config(encoded) == encoded
    assert runtime == original


def test_sub2api_key_names_aliases_and_environment_references_are_not_rewritten() -> None:
    runtime = _config({
        "use_groups": True,
        "control_key": "env:UNUSED_CONTROL",
        "credentials": [{"name": "休眠槽", "api_key": "env:UNUSED_SLOT"}],
        "groups": [
            {
                "name": "第一组",
                "api_keys": [
                    {"name": "主 Key", "value": "env:SUB_KEY_A", "custom": "keep"},
                    "literal-example",
                ],
                "key_names": ["别名优先级较低", "备用 Key"],
                "daily_limit": 0,
                "custom_option": [1, 2],
            },
            {"label": "旧名称", "keys": "env:SUB_KEY_B", "api_key_names": ["Key B"]},
            {"api_key_envs": ["SUB_KEY_C", "SUB_KEY_D"], "key_names": ["C", "D"]},
        ],
    }, "sub2api#2")

    encoded = to_accounts_config(runtime)

    assert encoded["platforms"]["sub2api#2"]["accounts"] == (
        runtime["platforms"]["sub2api#2"]["groups"]
    )
    assert to_runtime_config(encoded) == runtime
    assert tomllib.loads(tomli_w.dumps(encoded)) == encoded


def test_unsupported_group_api_key_is_never_activated_as_a_single_key() -> None:
    runtime = _config({"groups": [{"api_key": "env:IGNORED_GROUP_FIELD"}]}, "sub2api")
    encoded = to_accounts_config(runtime)
    assert encoded["platforms"]["sub2api"]["accounts"] == [{
        "name": "组1", "api_key": "env:IGNORED_GROUP_FIELD",
    }]
    decoded = to_runtime_config(encoded)
    assert "api_key" not in decoded["platforms"]["sub2api"]
    assert decoded["platforms"]["sub2api"]["groups"][0]["api_key"] == (
        "env:IGNORED_GROUP_FIELD"
    )


def test_legacy_platform_key_order_and_counters_are_normalized_before_accounts() -> None:
    old = {
        "platform_order": ["llm-gateway", "llm-gateway#2", "kimi"],
        "instance_counters": {"llm-gateway": 3, "sub2api": 2},
        "platforms": {"llm-gateway#2": {"api_key": "env:OLD_VARIABLE"}},
    }
    original = deepcopy(old)

    encoded = to_accounts_config(old)

    assert encoded["platforms"]["sub2api#2"]["accounts"] == [{"api_key": "env:OLD_VARIABLE"}]
    assert encoded["platform_order"] == ["sub2api", "sub2api#2", "kimi"]
    assert encoded["instance_counters"] == {"sub2api": 3}
    assert to_runtime_config(encoded)["platforms"]["sub2api#2"] == {
        "api_key": "env:OLD_VARIABLE",
    }
    assert old == original


def test_unknown_providers_and_unregistered_instance_names_are_untouched() -> None:
    config = {"platforms": {
        "future-provider": {"accounts": "unknown", "api_key": 123, "legacy": ["anything"]},
        "kimi#custom": {"accounts": "unknown", "credentials": "custom-format"},
    }}

    assert to_accounts_config(config) == config
    assert to_runtime_config(config) == config
    result = to_accounts_config(config)
    result["platforms"]["future-provider"]["legacy"].append("new")
    assert config["platforms"]["future-provider"]["legacy"] == ["anything"]


@pytest.mark.parametrize("section", [
    {"accounts": "secret-example-value"},
    {"accounts": ["secret-example-value"]},
    {"accounts": [{"api_key": 123}]},
    {"accounts": [{"access_key": True}]},
    {"credentials": {"api_key": "secret-example-value"}},
    {"credentials": ["secret-example-value"]},
    {"credentials": [{"control_key": ["secret-example-value"]}]},
    {"api_key": {"secret-example-value": True}},
    {"legacy": "secret-example-value"},
    {"legacy": {"api_key": 123}},
    {"legacy": {"credentials": [123]}},
    {"legacy": {"accounts": []}},
    {"legacy": {"legacy": {}}},
])
def test_bad_account_formats_fail_without_echoing_secrets(section) -> None:
    config = _config(section)
    original = deepcopy(config)

    for transform in (to_accounts_config, to_runtime_config):
        if transform is to_runtime_config and not section.keys() & {"accounts", "legacy"}:
            assert transform(config) == config
            continue
        with pytest.raises(ConfigFormatError) as exc:
            transform(config)
        assert "secret-example-value" not in str(exc.value)
        assert config == original


@pytest.mark.parametrize("field", ["accounts", "legacy", "credentials", "groups"])
@pytest.mark.parametrize("provider", ["kimi", "sub2api"])
@pytest.mark.parametrize("named", [False, True])
def test_nested_structural_fields_cannot_be_flattened_or_activate_other_accounts(
    field: str, provider: str, named: bool,
) -> None:
    account: dict[str, Any] = {"api_key": "env:KEY", field: "private-reserved-value"}
    if named:
        account["name"] = "账号一"
    config = _config({"accounts": [account]}, provider)
    original = deepcopy(config)

    for transform in (to_accounts_config, to_runtime_config):
        with pytest.raises(ConfigFormatError) as exc:
            transform(config)
        assert "private-reserved-value" not in str(exc.value)
        assert config == original


@pytest.mark.parametrize("section", [
    {"accounts": [{"api_keys": [123]}]},
    {"accounts": [{"api_keys": [{"value": 123}]}]},
    {"accounts": [{"api_keys": [{"value": "secret-example-value", "name": 123}]}]},
    {"accounts": [{"api_key_envs": [123]}]},
    {"groups": [{"api_keys": [False]}]},
    {"groups": [{"keys": {"value": "secret-example-value"}}]},
    {"groups": [{"key_names": [123]}]},
    {"use_groups": True, "accounts": [{"api_key": "secret-example-value"}]},
    {"use_groups": False, "accounts": [{"api_keys": ["secret-example-value"]}]},
])
def test_sub2api_rejects_invalid_keys_or_conflicting_active_modes(section) -> None:
    for transform in (to_accounts_config, to_runtime_config):
        if transform is to_runtime_config and "accounts" not in section:
            assert transform(_config(section, "sub2api")) == _config(section, "sub2api")
            continue
        with pytest.raises(ConfigFormatError) as exc:
            transform(_config(section, "sub2api"))
        assert "secret-example-value" not in str(exc.value)


@pytest.mark.parametrize("old", [
    {"api_key": "secret-example-value"},
    {"control_key": "secret-example-value"},
    {"credentials": []},
    {"groups": []},
])
def test_accounts_and_old_root_credentials_cannot_compete(old) -> None:
    for transform in (to_accounts_config, to_runtime_config):
        with pytest.raises(ConfigFormatError) as exc:
            transform(_config({"accounts": [], **old}))
        assert "secret-example-value" not in str(exc.value)


@pytest.mark.parametrize("section", [
    {"api_key": "secret-example-value", "legacy": {"api_key": "other-example"}},
    {"enabled": False, "legacy": {"enabled": True}},
    {"accounts": [{"api_key": "secret-example-value"}], "legacy": {"api_key": "backup"}},
    {"accounts": [{"name": "A", "api_key": "secret-example-value"}],
     "legacy": {"credentials": []}},
    {"enabled": False, "accounts": [{"api_key": "secret-example-value", "enabled": True}]},
])
def test_legacy_and_account_option_collisions_fail_instead_of_overwriting(section) -> None:
    original = deepcopy(section)
    for transform in (to_accounts_config, to_runtime_config):
        with pytest.raises(ConfigFormatError) as exc:
            transform(_config(section))
        assert "secret-example-value" not in str(exc.value)
    assert section == original


def test_equal_legacy_root_fields_still_cannot_overwrite_one_another() -> None:
    with pytest.raises(ConfigFormatError):
        to_runtime_config(_config({
            "api_key": "same-example", "legacy": {"api_key": "same-example"},
        }))


def test_single_unnamed_account_can_preserve_extra_runtime_options() -> None:
    encoded = _config({"accounts": [{
        "api_key": "env:KIMI_KEY", "custom_option": {"preserve": [1, 2]},
    }]})
    runtime = to_runtime_config(encoded)
    assert runtime["platforms"]["kimi"] == {
        "api_key": "env:KIMI_KEY", "custom_option": {"preserve": [1, 2]},
    }
    assert to_runtime_config(to_accounts_config(runtime)) == runtime


def test_conflicting_platform_aliases_keep_the_existing_error_type() -> None:
    with pytest.raises(PlatformKeyConflict) as exc:
        to_accounts_config({"platforms": {
            "llm-gateway": {"api_key": "secret-example-value"},
            "sub2api": {"api_key": "different-example"},
        }})
    assert "secret-example-value" not in str(exc.value)


def test_existing_legacy_extension_tables_are_merged_without_losing_unknown_fields() -> None:
    encoded = _config({
        "accounts": [{"api_key": "env:KEY"}],
        "legacy": {"credentials": [], "future_option": {"keep": [1, 2]}},
    })
    runtime = to_runtime_config(encoded)
    assert runtime == _config({
        "api_key": "env:KEY", "credentials": [], "future_option": {"keep": [1, 2]},
    })
    assert to_runtime_config(to_accounts_config(runtime)) == runtime


def test_invalid_legacy_credentials_stay_isolated_until_a_write_is_requested() -> None:
    config = {"platforms": {
        "kimi": {"api_key": False},
        "sub2api": {"groups": "invalid-legacy-groups"},
        "ollama": {"api_key": "env:VALID_KEY"},
    }}
    assert to_runtime_config(config) == config
    with pytest.raises(ConfigFormatError):
        to_accounts_config(config)
