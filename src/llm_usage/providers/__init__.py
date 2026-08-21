"""Provider registry + concurrent dispatch with error isolation.

A single platform failure never breaks the others: each provider's ``fetch``
is run in a thread and any exception is caught into ``PlatformResult.error``.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

from llm_usage.config import get_platform_order
from llm_usage.models import PlatformResult, UsageEntry
from llm_usage.providers.base import Provider
from llm_usage.providers.kimi import KimiProvider
from llm_usage.providers.volcengine import VolcengineProvider
from llm_usage.providers.ollama import OllamaProvider
from llm_usage.providers.opencode_go import OpenCodeGoProvider
from llm_usage.providers.llm_gateway import LlmGatewayProvider


# Registry: platform key -> provider instance.
# Volcengine covers both coding & agent plans; we register two keys pointing
# at separate instances so config sections map cleanly.
PROVIDERS: dict[str, Provider] = {
    "kimi": KimiProvider(),
    "volcengine-coding": VolcengineProvider(),
    "volcengine-agent": VolcengineProvider(),
    "ollama": OllamaProvider(),
    "opencode-go": OpenCodeGoProvider(),
    "llm-gateway": LlmGatewayProvider(),
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
    "llm-gateway": "LLM Gateway",
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


def _prepare_config(platform: str, pcfg: dict[str, Any]) -> dict[str, Any]:
    """Normalize a platform config section before handing to a provider."""
    out = dict(pcfg)
    out["_platform_key"] = platform
    # display name resolution
    if "display_name" not in out:
        out["display_name"] = DISPLAY_NAMES.get(platform, platform)
    # expand env: prefixed credentials (api_key, access_key, secret_key)
    out["api_key"] = _resolve_env_value(pcfg.get("api_key"))
    out["access_key"] = _resolve_env_value(pcfg.get("access_key"))
    out["secret_key"] = _resolve_env_value(pcfg.get("secret_key"))
    return out


def _credential_specs(prepared: dict[str, Any]) -> list[tuple[dict[str, Any], str | None]]:
    """Expand a prepared section into per-credential fetch specs.

    Returns a list of ``(sub_config, plan_name)``.  When the section has a
    non-empty ``credentials`` list (multi-credential billing plans), one spec
    per credential is produced — each is an independent fetch with its own
    plan name.  Otherwise a single spec for the legacy top-level credential
    is returned with ``plan_name=None``.  LLM Gateway keeps its own groups
    handling and is never expanded here.
    """
    platform = prepared["_platform_key"]
    slots = prepared.get("credentials")
    if platform == "llm-gateway" or not isinstance(slots, list) or not slots:
        return [(prepared, None)]
    specs: list[tuple[dict[str, Any], str | None]] = []
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict):
            continue
        plan_name = slot.get("name")
        if not isinstance(plan_name, str) or not plan_name.strip():
            plan_name = f"套餐{index + 1}"
        sub = dict(prepared)
        sub["api_key"] = _resolve_env_value(slot.get("api_key"))
        sub["access_key"] = _resolve_env_value(slot.get("access_key"))
        sub["secret_key"] = _resolve_env_value(slot.get("secret_key"))
        sub.pop("credentials", None)
        specs.append((sub, plan_name))
    if not specs:
        return [(prepared, None)]
    return specs


def fetch_all(
    config: dict[str, Any],
    enabled_only: bool = True,
    max_workers: int = 5,
) -> list[PlatformResult]:
    """Fetch usage from every configured (enabled) platform concurrently.

    Each provider runs in its own thread.  Any exception is caught and turned
    into a ``PlatformResult`` with an ``error`` so partial results still render.
    Platforms configured with a ``credentials`` list fan out into one
    independent fetch per credential (each = one billing plan); results are
    merged back into a single ``PlatformResult`` per platform with every entry
    tagged by its plan name.
    """
    platforms_cfg: dict[str, Any] = config.get("platforms", {})
    tasks: list[tuple[str, Provider, dict[str, Any], str | None]] = []
    for platform, pcfg in platforms_cfg.items():
        if not isinstance(pcfg, dict):
            continue
        if enabled_only and not pcfg.get("enabled", True):
            continue
        provider = PROVIDERS.get(platform)
        if provider is None:
            # unknown platform key — skip silently
            continue
        for sub, plan_name in _credential_specs(_prepare_config(platform, pcfg)):
            tasks.append((platform, provider, sub, plan_name))

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

    # 提交顺序 = 配置顺序(platform_order 之前);合并时按提交顺序累积,
    # 与现有“结果按注册表/平台顺序重排”的展示语义一致。
    by_platform: dict[str, list[tuple[str | None, PlatformResult]]] = {
        plat: [] for plat, _, _, _ in tasks
    }
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        pending: list[tuple[str, str | None, Any]] = []
        for plat, prov, cfg, plan in tasks:
            pending.append((plat, plan, pool.submit(_run, prov, cfg)))
        # 任务已全部并发提交;这里按提交顺序收集,保证每平台的 plan 顺序稳定
        for plat, plan, fut in pending:
            by_platform[plat].append((plan, fut.result()))

    for plat, pairs in by_platform.items():
        display_name = next(
            (r.display_name for _, r in pairs if r.ok),
            next((r.display_name for _, r in pairs), None),
        )
        if display_name is None:
            display_name = plat
        entries: list[UsageEntry] = []
        failures: list[str] = []
        for plan, res in pairs:
            if res.error is None:
                for entry in res.entries:
                    entry.plan = plan
                    entries.append(entry)
            else:
                failures.append(f"{plan}：{res.error}" if plan else res.error)
        if entries:
            results.append(PlatformResult(
                plat, display_name, entries=entries,
                warning="；".join(failures) if failures else None,
            ))
        elif failures:
            results.append(PlatformResult(
                plat, display_name, error="；".join(failures),
            ))
        else:
            results.append(PlatformResult(plat, display_name, entries=[]))

    # stable order: config platform_order first, then registry order
    cfg_order = get_platform_order(config)
    order = {plat: i for i, plat in enumerate(cfg_order)}
    results.sort(
        key=lambda r: (order.get(r.platform, len(cfg_order)), _REGISTRY_INDEX.get(r.platform, 999))
    )
    return results