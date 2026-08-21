"""Provider registry + concurrent dispatch with error isolation.

A single platform failure never breaks the others: each provider's ``fetch``
is run in a thread and any exception is caught into ``PlatformResult.error``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from llm_usage.config import get_platform_order
from llm_usage.models import PlatformResult
from llm_usage.providers.base import Provider
from llm_usage.providers.kimi import KimiProvider
from llm_usage.providers.volcengine import VolcengineProvider
from llm_usage.providers.ollama import OllamaProvider
from llm_usage.providers.opencode_go import OpenCodeGoProvider


# Registry: platform key -> provider instance.
# Volcengine covers both coding & agent plans; we register two keys pointing
# at separate instances so config sections map cleanly.
PROVIDERS: dict[str, Provider] = {
    "kimi": KimiProvider(),
    "volcengine-coding": VolcengineProvider(),
    "volcengine-agent": VolcengineProvider(),
    "ollama": OllamaProvider(),
    "opencode-go": OpenCodeGoProvider(),
}

# Registry order as a lookup table; the fallback for platforms not listed in
# the config's ``platform_order``.
_REGISTRY_INDEX: dict[str, int] = {plat: i for i, plat in enumerate(PROVIDERS)}


def registry_index() -> dict[str, int]:
    """Return a copy of the registry-order lookup (platform key -> index)."""
    return dict(_REGISTRY_INDEX)

# Display names (override-able from config).
DISPLAY_NAMES: dict[str, str] = {
    "kimi": "Kimi Code",
    "volcengine-coding": "火山方舟 Coding Plan",
    "volcengine-agent": "火山方舟 Agent Plan",
    "ollama": "Ollama Cloud",
    "opencode-go": "OpenCode Go",
}


def _resolve_env_value(raw: str | None) -> str | None:
    """Expand an ``env:VARNAME`` reference; pass through literal values."""
    if not raw:
        return None
    import os

    if raw.startswith("env:"):
        return os.environ.get(raw[4:])
    return raw


# kept for backward compat / tests
_resolve_api_key = _resolve_env_value


def _prepare_config(platform: str, cfg: dict[str, Any]) -> dict[str, Any]:
    """Normalize a platform config section before handing to a provider."""
    out = dict(cfg)
    out["_platform_key"] = platform
    # display name resolution
    if "display_name" not in out:
        out["display_name"] = DISPLAY_NAMES.get(platform, platform)
    # expand env: prefixed credentials (api_key, access_key, secret_key)
    out["api_key"] = _resolve_env_value(cfg.get("api_key"))
    out["access_key"] = _resolve_env_value(cfg.get("access_key"))
    out["secret_key"] = _resolve_env_value(cfg.get("secret_key"))
    return out


def fetch_all(
    config: dict[str, Any],
    enabled_only: bool = True,
    max_workers: int = 5,
) -> list[PlatformResult]:
    """Fetch usage from every configured (enabled) platform concurrently.

    Each provider runs in its own thread.  Any exception is caught and turned
    into a ``PlatformResult`` with an ``error`` so partial results still render.
    """
    platforms_cfg: dict[str, Any] = config.get("platforms", {})
    tasks: list[tuple[str, Provider, dict[str, Any]]] = []
    for platform, pcfg in platforms_cfg.items():
        if not isinstance(pcfg, dict):
            continue
        if enabled_only and not pcfg.get("enabled", True):
            continue
        provider = PROVIDERS.get(platform)
        if provider is None:
            # unknown platform key — skip silently
            continue
        tasks.append((platform, provider, _prepare_config(platform, pcfg)))

    results: list[PlatformResult] = []
    if not tasks:
        return results

    def _run(p: Provider, cfg: dict[str, Any]) -> PlatformResult:
        try:
            res = p.fetch(cfg)
            if res.platform and res.platform != cfg.get("_platform_key"):
                # keep the canonical platform key
                res.platform = cfg["_platform_key"]
            return res
        except Exception as exc:  # noqa: BLE001 — isolate single-platform failure
            return PlatformResult(
                cfg["_platform_key"],
                cfg.get("display_name", cfg["_platform_key"]),
                error=f"内部错误：{exc}",
            )

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_run, prov, cfg): plat for plat, prov, cfg in tasks}
        for fut in as_completed(futures):
            results.append(fut.result())

    # stable order: config platform_order first, then registry order
    cfg_order = get_platform_order(config)
    order = {plat: i for i, plat in enumerate(cfg_order)}
    results.sort(
        key=lambda r: (order.get(r.platform, len(cfg_order)), _REGISTRY_INDEX.get(r.platform, 999))
    )
    return results