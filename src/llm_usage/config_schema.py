"""Lossless adapters between account tables and the existing provider configuration.

The persisted format uses ``accounts`` for every known provider.  The runtime
projection deliberately keeps the older credential/group shapes, so providers
and credential editors do not need to understand two storage formats.  Dormant
credentials are kept in a ``legacy`` table rather than silently activated or
discarded during conversion.  Secret values, including ``env:`` references,
are never resolved here.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from llm_usage.platform_keys import normalize_platform_config

_PROVIDER_KEYS = frozenset({
    "kimi", "volcengine-coding", "volcengine-agent", "ollama", "opencode-go",
    "clinepass", "commandcode", "opendesign", "sub2api",
})
_CREDENTIAL_FIELDS = ("api_key", "control_key", "access_key", "secret_key")
_OLD_FIELDS = frozenset((*_CREDENTIAL_FIELDS, "credentials", "groups"))
_GROUP_KEYS = ("api_keys", "keys", "api_key_envs")


class ConfigFormatError(ValueError):
    """A configuration cannot be converted safely without losing information."""


def _provider_type(key: str) -> str | None:
    base, separator, suffix = key.rpartition("#")
    if separator and base and suffix.isdigit():
        key = base
    return key if key in _PROVIDER_KEYS else None


def _copy_config(config: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ConfigFormatError("配置必须是表")
    return deepcopy(normalize_platform_config(config))


def _validate_credentials(item: dict[str, Any]) -> None:
    for field in _CREDENTIAL_FIELDS:
        if field in item and not isinstance(item[field], str):
            raise ConfigFormatError("账号凭证必须为字符串")


def _validate_name(item: dict[str, Any]) -> None:
    if "name" in item and not isinstance(item["name"], str):
        raise ConfigFormatError("账号名称必须为字符串")


def _validate_group(item: dict[str, Any]) -> None:
    _validate_credentials(item)
    _validate_name(item)
    for field in _GROUP_KEYS:
        if field not in item:
            continue
        value = item[field]
        if isinstance(value, str):
            continue
        if not isinstance(value, list):
            raise ConfigFormatError("账号的 API key 必须为字符串或数组")
        for entry in value:
            if isinstance(entry, str):
                continue
            if field != "api_key_envs" and isinstance(entry, dict):
                if not isinstance(entry.get("value"), str):
                    raise ConfigFormatError("账号的 API key 必须为字符串")
                if "name" in entry and not isinstance(entry["name"], str):
                    raise ConfigFormatError("API key 名称必须为字符串")
                continue
            raise ConfigFormatError("账号的 API key 必须为字符串")
    for field in ("key_names", "api_key_names"):
        if field in item and (
            not isinstance(item[field], list)
            or any(not isinstance(name, str) for name in item[field])
        ):
            raise ConfigFormatError("API key 名称列表必须为字符串数组")


def _validate_slots(value: Any, *, groups: bool = False) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ConfigFormatError("账号配置必须为数组")
    for item in value:
        if not isinstance(item, dict):
            raise ConfigFormatError("账号数组中的每项必须为表")
        if item.keys() & {"accounts", "legacy", "credentials", "groups"}:
            raise ConfigFormatError("账号内不能嵌套 accounts、legacy、credentials 或 groups")
        _validate_credentials(item)
        _validate_name(item)
        if groups:
            _validate_group(item)
    return value


def _validate_old_fields(section: dict[str, Any]) -> None:
    _validate_credentials(section)
    if "credentials" in section:
        _validate_slots(section["credentials"])
    if "groups" in section:
        _validate_slots(section["groups"], groups=True)


def _merge_without_overwrite(target: dict[str, Any], source: dict[str, Any]) -> None:
    if target.keys() & source.keys():
        raise ConfigFormatError("账号配置与平台或 legacy 中的字段冲突，请先合并配置")
    target.update(source)


def _runtime_section(section: dict[str, Any], provider: str) -> dict[str, Any]:
    if "accounts" not in section and "legacy" not in section:
        # Old provider-local mistakes must keep their existing error isolation:
        # loading one invalid legacy key must not abort every other provider.
        return section
    result = dict(section)
    legacy = result.pop("legacy", {})
    if not isinstance(legacy, dict):
        raise ConfigFormatError("legacy 必须为表")
    if "accounts" in legacy or "legacy" in legacy:
        raise ConfigFormatError("legacy 中不能包含账号配置或嵌套 legacy")

    if "accounts" in result:
        if result.keys() & _OLD_FIELDS:
            raise ConfigFormatError("accounts 不能与旧凭证、credentials 或 groups 同时配置")
        accounts = _validate_slots(result.pop("accounts"), groups=provider == "sub2api")
        if accounts:
            if provider == "sub2api":
                single = (
                    len(accounts) == 1
                    and "name" not in accounts[0]
                    and "api_key" in accounts[0]
                    and not any(field in accounts[0] for field in _GROUP_KEYS)
                )
                if single:
                    if result.get("use_groups") is True:
                        raise ConfigFormatError("单 Key 账号与 use_groups 模式冲突")
                    _merge_without_overwrite(result, accounts[0])
                else:
                    if result.get("use_groups") is False:
                        raise ConfigFormatError("分组账号与 use_groups 模式冲突")
                    result["groups"] = accounts
            elif len(accounts) == 1 and "name" not in accounts[0]:
                _merge_without_overwrite(result, accounts[0])
            else:
                result["credentials"] = accounts

    _merge_without_overwrite(result, legacy)
    _validate_old_fields(result)
    return result


def to_runtime_config(config: dict[str, Any]) -> dict[str, Any]:
    """Decode account tables without editing the input or expanding secrets.

    Legacy sections remain accepted.  Unknown provider sections are copied
    verbatim, so importing configuration for a future provider is safe.
    Conflicting representations fail instead of choosing one credential set.
    """
    result = _copy_config(config)
    platforms = result.get("platforms")
    if not isinstance(platforms, dict):
        return result
    for key, section in platforms.items():
        provider = _provider_type(key)
        if provider is not None and isinstance(section, dict):
            platforms[key] = _runtime_section(section, provider)
    return result


def _accounts_section(section: dict[str, Any], provider: str) -> dict[str, Any]:
    _validate_old_fields(section)
    result = {key: value for key, value in section.items() if key not in _OLD_FIELDS}
    legacy: dict[str, Any] = {}
    accounts: list[dict[str, Any]] = []

    if provider == "sub2api":
        groups = section.get("groups", [])
        grouped = section.get("use_groups") is True or (
            section.get("use_groups") is not False and bool(groups)
        )
        if grouped:
            accounts = groups
            if (
                len(accounts) == 1 and "name" not in accounts[0]
                and "api_key" in accounts[0]
                and not any(field in accounts[0] for field in _GROUP_KEYS)
            ):
                # An old group may contain an unsupported api_key field.
                # Preserve its group identity rather than accidentally making
                # that previously ignored value an active single-key account.
                accounts = [{**accounts[0], "name": "组1"}]
            if not groups and "groups" in section:
                legacy["groups"] = groups
            for field in _CREDENTIAL_FIELDS:
                if field in section:
                    legacy[field] = section[field]
        else:
            if "api_key" in section:
                accounts = [{"api_key": section["api_key"]}]
            if "groups" in section:
                legacy["groups"] = section["groups"]
            for field in _CREDENTIAL_FIELDS:
                if field != "api_key" and field in section:
                    legacy[field] = section[field]
        if "credentials" in section:
            legacy["credentials"] = section["credentials"]
    else:
        credentials = section.get("credentials", [])
        if credentials:
            accounts = credentials
            if len(accounts) == 1 and "name" not in accounts[0]:
                # A nameless singleton means the old top-level shape when
                # decoded. Keep an actual slot as a slot, including unknown
                # per-account options and any dormant top-level credentials.
                accounts = [{**accounts[0], "name": "套餐1"}]
            for field in _CREDENTIAL_FIELDS:
                if field in section:
                    legacy[field] = section[field]
        else:
            account = {field: section[field] for field in _CREDENTIAL_FIELDS if field in section}
            if account:
                accounts = [account]
            if "credentials" in section:
                legacy["credentials"] = section["credentials"]
        if "groups" in section:
            legacy["groups"] = section["groups"]

    if legacy:
        result["legacy"] = legacy
    result["accounts"] = accounts
    return result


def to_accounts_config(config: dict[str, Any]) -> dict[str, Any]:
    """Encode runtime credentials as uniform account tables, preserving backups."""
    result = to_runtime_config(config)
    platforms = result.get("platforms")
    if not isinstance(platforms, dict):
        return result
    for key, section in platforms.items():
        provider = _provider_type(key)
        if provider is not None and isinstance(section, dict):
            platforms[key] = _accounts_section(section, provider)
    return result
