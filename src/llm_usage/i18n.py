"""Small stdlib-only i18n runtime shared by the CLI and Web UI.

English is the default locale; Simplified Chinese is selected by browser /
system locale or an explicit switch.  The catalogs are flat JSON files shipped
inside the package.  Canonical provider labels and error strings stay Chinese
internally/storage; :func:`translate_text` localizes them for presentation.
"""

from __future__ import annotations

import json
import os
import re
from contextvars import ContextVar, Token
from functools import lru_cache
from importlib.resources import files
from typing import Any

Locale = str
SUPPORTED_LOCALES: tuple[Locale, ...] = ("en", "zh-CN")
DEFAULT_LOCALE: Locale = "en"

_current_locale: ContextVar[Locale] = ContextVar("llm_usage_locale", default=DEFAULT_LOCALE)


def normalize_locale(value: Any) -> Locale | None:
    """Map common locale spellings to one of the supported locales."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().lower().replace("_", "-")
    if text.startswith("zh"):
        return "zh-CN"
    if text.startswith("en"):
        return "en"
    return None


def _accept_language(header: str | None) -> Locale | None:
    if not header:
        return None
    for part in header.split(","):
        tag = part.split(";", 1)[0].strip()
        locale = normalize_locale(tag)
        if locale is not None:
            return locale
    return None


def resolve_locale(
    explicit: str | None = None,
    *,
    cookie: str | None = None,
    accept_language: str | None = None,
    env: dict[str, str] | None = None,
) -> Locale:
    """Resolve locale with the documented precedence.

    ``explicit`` (CLI ``--lang`` / ``?lang=``) → cookie → Accept-Language →
    ``LLM_USAGE_LANG`` → ``LC_ALL``/``LANG`` → English.
    """
    for candidate in (explicit, cookie):
        locale = normalize_locale(candidate)
        if locale is not None:
            return locale
    if accept_language:
        # A real browser sent a language header: use it, or fall back to English
        # when none of its tags are supported.
        return _accept_language(accept_language) or DEFAULT_LOCALE
    env = dict(os.environ) if env is None else env
    for key in ("LLM_USAGE_LANG", "LC_ALL", "LANG"):
        locale = normalize_locale(env.get(key))
        if locale is not None:
            return locale
    return DEFAULT_LOCALE


def current_locale() -> Locale:
    return _current_locale.get()


def set_locale(locale: str | None) -> Token[Locale]:
    return _current_locale.set(normalize_locale(locale) or DEFAULT_LOCALE)


def reset_locale(token: Token[Locale]) -> None:
    _current_locale.reset(token)


@lru_cache(maxsize=8)
def catalog(locale: str | None = None) -> dict[str, Any]:
    loc = normalize_locale(locale or current_locale()) or DEFAULT_LOCALE
    path = files("llm_usage").joinpath("locales", f"{loc}.json")
    return json.loads(path.read_text(encoding="utf-8"))


@lru_cache(maxsize=8)
def _catalogs() -> dict[str, dict[str, Any]]:
    return {loc: catalog(loc) for loc in SUPPORTED_LOCALES}


def t(key: str, **params: Any) -> str:
    """Translate a stable key, falling back en → zh → the key itself."""
    for loc in (current_locale(), DEFAULT_LOCALE, "zh-CN"):
        value = catalog(loc).get("keys", {}).get(key)
        if isinstance(value, str):
            return value.format(**params) if params else value
    return key


def _lookup(section: str, value: str) -> str | None:
    for loc in (current_locale(), DEFAULT_LOCALE, "zh-CN"):
        hit = catalog(loc).get(section, {}).get(value)
        if isinstance(hit, str):
            return hit
    return None


def _translate_value(section: str, value: str, *, label: bool = False) -> str:
    if current_locale() == "zh-CN":
        return value
    stripped = value.strip()
    if stripped != value:
        translated = _translate_value(section, stripped, label=label)
        if translated != stripped:
            lead = value[: len(value) - len(value.lstrip())]
            tail = value[len(value.rstrip()):]
            return lead + translated + tail
    hit = _lookup(section, stripped)
    if hit is not None:
        return hit
    if label:
        match = re.fullmatch(r"(\d+)\s*(天|小时|分钟|分)", stripped)
        if match:
            suffix = {"天": "d", "小时": "h", "分钟": "m", "分": "m"}[match.group(2)]
            return f"{match.group(1)}{suffix}"
    return value


def translate_label(label: str) -> str:
    return _translate_value("labels", label, label=True)


def translate_unit(unit: str) -> str:
    return _translate_value("units", unit)


def translate_text(text: str) -> str:
    """Translate a canonical Chinese UI/provider message for presentation."""
    if not text or current_locale() == "zh-CN":
        return text
    exact = _lookup("source", text) or _lookup("messages", text)
    if exact is not None:
        return exact
    stripped = text.strip()
    if stripped != text:
        inner = translate_text(stripped)
        if inner != stripped:
            return text[: len(text) - len(text.lstrip())] + inner + text[len(text.rstrip()):]
    # Common dynamic provider messages.
    m = re.fullmatch(r"认证失败\((\d+)\)", stripped)
    if m:
        return t("provider.error.auth", status=m.group(1))
    m = re.fullmatch(r"认证失败\((\d+)\)：(.*)", stripped)
    if m:
        return t("provider.error.auth_detail", status=m.group(1), detail=translate_text(m.group(2)))
    m = re.fullmatch(r"请求失败\(HTTP (\d+)\)", stripped)
    if m:
        return t("provider.error.http", status=m.group(1))
    m = re.fullmatch(r"HTTP (\d+)", stripped)
    if m:
        return t("provider.error.http", status=m.group(1))
    m = re.fullmatch(r"(\d+)天(\d+)小时", stripped)
    if m:
        return f"{m.group(1)}d {m.group(2)}h"
    m = re.fullmatch(r"(\d+)小时(\d+)分", stripped)
    if m:
        return f"{m.group(1)}h {m.group(2)}m"
    m = re.fullmatch(r"(\d+)分", stripped)
    if m:
        return f"{m.group(1)}m"
    # Joined warnings: split by the canonical Chinese separator and rejoin.
    if "；" in text:
        return "; ".join(translate_text(part) for part in text.split("；"))
    # "组A：..." / "组A 备用：..."
    if "：" in text:
        prefix, suffix = text.split("：", 1)
        return f"{prefix}: {translate_text(suffix)}"
    return text


translate_message = translate_text
