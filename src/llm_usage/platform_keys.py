"""Canonical provider keys and non-destructive legacy configuration migration."""

from __future__ import annotations

from typing import Any


class PlatformKeyConflict(ValueError):
    """Legacy and canonical sections cannot be merged without losing an account."""


def canonical_platform_key(key: str) -> str:
    if key == "llm-gateway":
        return "sub2api"
    prefix = "llm-gateway#"
    if key.startswith(prefix) and key[len(prefix):].isdigit():
        return "sub2api#" + key[len(prefix):]
    return key


def platform_key_aliases(key: str) -> tuple[str, ...]:
    """Keys that identify the same stored platform, canonical spelling first."""
    canonical = canonical_platform_key(key)
    if canonical == "sub2api":
        return canonical, "llm-gateway"
    if canonical.startswith("sub2api#") and canonical[8:].isdigit():
        return canonical, "llm-gateway#" + canonical[8:]
    return (canonical,)


def normalize_platform_config(config: dict[str, Any]) -> dict[str, Any]:
    """Rename sections, display order and allocation counters, keeping secrets intact.

    If two different sections would acquire the same key, fail explicitly;
    silently choosing either section could discard an account or its quota.
    Equal duplicate sections are safe to collapse. Input objects are not edited.
    """
    result = dict(config)
    platforms = config.get("platforms")
    if isinstance(platforms, dict):
        renamed: dict[str, Any] = {}
        for key, section in platforms.items():
            canonical = canonical_platform_key(key)
            if canonical in renamed and renamed[canonical] != section:
                raise PlatformKeyConflict(
                    f"旧配置键与 {canonical} 冲突，请合并配置或使用不同实例键"
                )
            renamed[canonical] = section
        result["platforms"] = renamed
    order = config.get("platform_order")
    if isinstance(order, list):
        seen: set[str] = set()
        renamed_order: list[Any] = []
        for key in order:
            if isinstance(key, str):
                key = canonical_platform_key(key)
                if key in seen:
                    continue
                seen.add(key)
            renamed_order.append(key)
        result["platform_order"] = renamed_order
    counters = config.get("instance_counters")
    if isinstance(counters, dict):
        renamed_counters: dict[str, Any] = {}
        for key, value in counters.items():
            canonical = canonical_platform_key(key)
            previous = renamed_counters.get(canonical)
            if isinstance(value, int) and not isinstance(value, bool):
                if isinstance(previous, int) and not isinstance(previous, bool):
                    value = max(value, previous)
            elif canonical in renamed_counters:
                continue
            renamed_counters[canonical] = value
        result["instance_counters"] = renamed_counters
    return result
