"""Configuration load/save for llm-usage.

Config lives at ``./config.toml`` in the current working directory (TOML).
Reads use stdlib ``tomllib``; writes use ``tomli_w``.  API keys may use an
``env:VARNAME`` prefix to reference environment variables instead of storing
plaintext on disk.  Override the path with the ``LLM_USAGE_CONFIG`` env var.
"""

from __future__ import annotations

import os
import tempfile
import threading
import tomllib
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, TypeVar

import tomli_w

from llm_usage import i18n
from llm_usage.config_schema import ConfigFormatError, to_accounts_config, to_runtime_config
from llm_usage.platform_keys import (
    PlatformKeyConflict,
    canonical_platform_key,
)

CONFIG_PATH = Path.cwd() / "config.toml"

# 进程内互斥:llm-usage web 单 Uvicorn 进程,并发 FastAPI worker 线程共享此锁,
# 保证所有 config.toml 读-改-写在同一临界区内串行(避免丢失更新)。
_CONFIG_LOCK = threading.Lock()

# 版式:内联数组(含账号行内的 api_keys)超过该宽度时改为每项一行。
# 内联表本身不允许换行,所以超长账号行仍是单行,只是它的数组字段会展开。
LINE_LIMIT = 100
# 平台表内的字段优先级:常用设置在前,legacy / accounts 固定在末尾。
_PLATFORM_HEAD = ("enabled", "display_name", "base_url")
_PLATFORM_TAIL = ("legacy", "accounts")

# 区分「配置里没有 platforms 键」与「platforms 为空表」:前者不渲染任何表头。
_ABSENT: Any = object()

_T = TypeVar("_T")


def _set_sensitive_permissions(path: Path) -> None:
    """Best-effort 0600(凭证明文,收敛文件权限;幂等,失败不阻断)。"""
    try:
        path.chmod(0o600)
    except OSError:
        pass


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """原子写:同目录临时文件 + os.replace,读者永远看到完整旧文件或完整新文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            delete=False,
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as fh:
            tmp_path = Path(fh.name)
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        _set_sensitive_permissions(tmp_path)
        os.replace(tmp_path, path)
        tmp_path = None
        _set_sensitive_permissions(path)
    finally:
        if tmp_path is not None:
            try:
                tmp_path.unlink()
            except OSError:
                pass


def _write_config_unlocked(config: dict[str, Any], path: Path) -> None:
    """先完整序列化再原子落盘;序列化失败不会触碰目标文件。"""
    _atomic_write_bytes(path, format_config(config).encode("utf-8"))


def _is_table_array(value: Any) -> bool:
    """Whether ``value`` is a non-empty list of tables (a TOML array of tables)."""
    return isinstance(value, list) and bool(value) and all(
        isinstance(item, dict) for item in value
    )


def _toml_key(key: str) -> str:
    """Render a key as a TOML key (quoted only when the bare form is illegal)."""
    return tomli_w.dumps({key: 0}).removesuffix(" = 0\n")


def _toml_scalar(value: Any) -> str:
    """Render one scalar through ``tomli_w`` so quoting and escapes stay correct."""
    return tomli_w.dumps({"_": value}).removesuffix("\n").removeprefix("_ = ")


def _field_order(table: dict[str, Any]) -> list[str]:
    """Stable field order: ``name`` first, then plain values, then nested ones."""
    rest = [key for key in table if key != "name"]
    plain = sorted(key for key in rest if not isinstance(table[key], (dict, list)))
    nested = sorted(key for key in rest if isinstance(table[key], (dict, list)))
    return (["name"] if "name" in table else []) + plain + nested


def _inline_value(value: Any, indent: int) -> str:
    """Render a value as TOML text that fits on one line when it can.

    A table must stay on one line (TOML inline tables cannot span lines), while
    an array may break into one item per line once it outgrows the line budget.
    """
    if isinstance(value, dict):
        if not value:
            return "{}"
        pairs = [f"{_toml_key(key)} = {_inline_value(value[key], indent)}"
                 for key in _field_order(value)]
        return "{ " + ", ".join(pairs) + " }"
    if isinstance(value, list):
        if not value:
            return "[]"
        items = [_inline_value(item, indent + 4) for item in value]
        single = "[" + ", ".join(items) + "]"
        if all("\n" not in item for item in items) and indent + len(single) <= LINE_LIMIT:
            return single
        pad, close = " " * (indent + 4), " " * indent
        return "[\n" + "".join(f"{pad}{item},\n" for item in items) + close + "]"
    return _toml_scalar(value)


def _accounts_field(accounts: Any) -> str:
    """Render ``accounts`` as one line per account, never as child table headers."""
    if not isinstance(accounts, list) or not accounts:
        return f"accounts = {_inline_value(accounts, 0)}"
    lines = "".join(f"    {_inline_value(account, 4)},\n" for account in accounts)
    return "accounts = [\n" + lines + "]"


def _platform_block(key: str, section: dict[str, Any]) -> str:
    """Render one provider as a single ``[platforms.<key>]`` table."""
    header = ".".join(_toml_key(part) for part in ("platforms", key))
    lines = [f"[{header}]"]
    rest = sorted(
        field for field in section if field not in (*_PLATFORM_HEAD, *_PLATFORM_TAIL)
    )
    fields = [field for field in _PLATFORM_HEAD if field in section]
    fields += rest
    fields += [field for field in _PLATFORM_TAIL if field in section]
    for field in fields:
        value = section[field]
        if field == "accounts":
            lines.append(_accounts_field(value))
        else:
            lines.append(f"{_toml_key(field)} = {_inline_value(value, 0)}")
    return "\n".join(lines)


def _table_block(key: str, table: dict[str, Any]) -> str:
    """Render a top-level table (``[instance_counters]``) after the platforms."""
    header = f"[{_toml_key(key)}]"
    fields = [f"{_toml_key(field)} = {_inline_value(table[field], 0)}" for field in table]
    return "\n".join([header, *fields])


def _platforms_block(platforms: Any) -> list[str]:
    """Render the ``platforms`` value as its single parent table plus sections.

    Malformed-but-loadable shapes are preserved rather than dropped, so a
    rewrite can never silently empty a file: an array of tables becomes
    ``[[platforms]]`` rows and non-table entries stay as literal keys of
    ``[platforms]``.  Only an empty mapping keeps the bare ``[platforms]``.
    """
    if _is_table_array(platforms):
        return [
            "[[platforms]]\n" + "\n".join(
                f"{_toml_key(field)} = {_inline_value(item[field], 0)}"
                for field in _field_order(item)
            )
            for item in platforms
        ]
    if not isinstance(platforms, dict):
        return []
    if not platforms:
        return ["[platforms]"]
    literals: dict[str, Any] = {}
    blocks: list[str] = []
    for key, section in platforms.items():
        if isinstance(section, dict):
            blocks.append(_platform_block(key, section))
        else:
            literals[key] = section
    if literals:
        # Parent literals must precede the child tables they belong to.
        body = "\n".join(
            f"{_toml_key(key)} = {_inline_value(literals[key], 0)}"
            for key in literals
        )
        blocks.insert(0, f"[platforms]\n{body}")
    return blocks


def format_config(config: dict[str, Any]) -> str:
    """Render the canonical config with one single table per provider.

    Every provider keeps *all* of its settings — platform options, ``legacy``
    and ``accounts`` (plus Sub2API's nested ``api_keys``) — inside one
    ``[platforms.<key>]`` table, so no provider ever needs a child table
    header.  ``accounts`` is a multiline array holding one inline table per
    account (one account per line); an inline table cannot span lines, so a
    nested ``api_keys`` array breaks into one item per line once that account
    line outgrows :data:`LINE_LIMIT`.

    The layout is decided by field order and structure, never by which
    credentials happen to be set, so repeated rewrites stay byte-stable.  An
    unparseable shape raises :class:`ConfigFormatError` upstream; anything that
    loads is rendered back, so saving can never truncate a config.
    """
    canonical = to_accounts_config(config)
    blocks: list[str] = []
    platforms: Any = _ABSENT
    trailing: list[tuple[str, Any]] = []
    for key, value in canonical.items():
        if key == "platforms":
            platforms = value
        elif isinstance(value, dict) or _is_table_array(value):
            trailing.append((key, value))
        else:
            # Top-level literals (platform_order, max_retries, …) must precede
            # the first table header.
            blocks.append(tomli_w.dumps({key: value}).rstrip("\n"))

    if platforms is not _ABSENT:
        blocks.extend(_platforms_block(platforms))

    for key, value in trailing:
        if isinstance(value, dict):
            blocks.append(_table_block(key, value))
        else:
            for item in value:
                blocks.append(f"[[{_toml_key(key)}]]\n" + "\n".join(
                    f"{_toml_key(field)} = {_inline_value(item[field], 0)}"
                    for field in _field_order(item)
                ))
    return "\n\n".join(blocks) + ("\n" if blocks else "")

# The default template generated by ``llm-usage config --init``.
EXAMPLE_CONFIG = """\
# llm-usage configuration
# Each provider uses exactly one [platforms.<type>] table: platform settings and
# the accounts list live in the same table (no [[platforms.<type>.accounts]] child table).
# accounts is an array with one account per line. name is optional.
# Credentials may use env:VARNAME or literal secrets. Legacy top-level credentials,
# credentials, groups and child-table forms are still readable and are normalized on save.

# Optional platform display order:
# platform_order = [
#     "kimi", "volcengine-coding", "volcengine-agent", "ollama",
#     "opencode-go", "clinepass", "commandcode", "opendesign", "sub2api",
# ]
# Automatic retry: extra attempts after the first failure, default 3; 0 disables, max 10.
# Only transient failures are retried with exponential backoff 0.5s → 1s → 2s.
# max_retries = 3

[platforms.kimi]
enabled = true
base_url = "https://api.kimi.com/coding/v1"
accounts = [
    { api_key = "env:KIMI_API_KEY" },
]

[platforms.volcengine-coding]
enabled = true
plan_type = "coding"
tier = "pro"  # lite | pro
# limits = { session = 6000, weekly = 45000, monthly = 90000 }
accounts = [
    # Volcengine uses AccessKey/SecretKey + V4 signing, not a Bearer API key.
    { access_key = "env:VOLCENGINE_ACCESS_KEY", secret_key = "env:VOLCENGINE_SECRET_KEY" },
]

[platforms.volcengine-agent]
enabled = true
plan_type = "agent"
tier = "medium"  # small | medium | large | max
# limits = { "5h" = 6000, weekly = 45000, monthly = 90000 }
accounts = [
    { access_key = "env:VOLCENGINE_ACCESS_KEY", secret_key = "env:VOLCENGINE_SECRET_KEY" },
]

[platforms.ollama]
enabled = true
accounts = [
    { api_key = "env:OLLAMA_API_KEY" },
]

[platforms.opencode-go]
enabled = true
accounts = [
    { api_key = "env:OPENCODE_GO_API_KEY" },
]

[platforms.clinepass]
enabled = true
accounts = [
    { api_key = "env:CLINEPASS_API_KEY" },
]

[platforms.commandcode]
enabled = true
accounts = [
    { api_key = "env:COMMANDCODE_API_KEY" },
]

[platforms.opendesign]
enabled = true
base_url = "https://amr-link.open-design.ai/v1"
# The model service URL is mapped to OpenDesign's control-plane usage API.
# workspace_id = "workspace ID"  # optional; defaults to the current workspace
# max_balance = 100  # optional display scale for the USD balance; 0/unset hides the bar
accounts = [
    # Web "Provider configuration" can also save this through browser login authorization.
    { control_key = "env:OPENDESIGN_CONTROL_KEY" },  # vela_ctrl_, not an sk-amr- model key
]

[platforms.sub2api]
enabled = true
base_url = "http://127.0.0.1:18080"
accounts = [
    # One account is one shared-quota key group; adding keys does not add an account.
    # daily_limit = 100 overrides the panel daily quota (weekly/monthly are unaffected).
    # api_keys entries may be named: { name = "Primary", value = "env:SUB2API_API_KEY" }.
    { name = "Group 1", api_keys = ["env:SUB2API_API_KEY"] },
]
"""

# Chinese template used by ``llm-usage config --init --lang zh-CN``.
EXAMPLE_CONFIG_ZH = """\
# llm-usage configuration
# 每个供应商只写一个 [platforms.<type>] 表:平台设置与 accounts 账号列表都在表内,
# 不再使用 [[platforms.<type>.accounts]] 子表。accounts 写成数组,一个账号一行。
# 账号 name 可选;凭证使用 env:VARNAME 引用环境变量,也可直接填写密钥。
# 旧顶层凭证、credentials、groups 与子表格式仍可读取,保存时统一为本格式。

# 平台显示顺序(可选):
# platform_order = [
#     "kimi", "volcengine-coding", "volcengine-agent", "ollama",
#     "opencode-go", "clinepass", "commandcode", "opendesign", "sub2api",
# ]
# 自动重试:首次失败后的额外尝试次数,默认 3;0 关闭,最多 10。
# 仅重试网络错误、HTTP 408/429/5xx 等瞬时故障,指数退避 0.5s→1s→2s。
# max_retries = 3

[platforms.kimi]
enabled = true
base_url = "https://api.kimi.com/coding/v1"
accounts = [
    { api_key = "env:KIMI_API_KEY" },
]

[platforms.volcengine-coding]
enabled = true
plan_type = "coding"
tier = "pro"  # lite | pro
# limits = { session = 6000, weekly = 45000, monthly = 90000 }
accounts = [
    # 火山使用 AccessKey/SecretKey + V4 签名,不是 Bearer API key。
    { access_key = "env:VOLCENGINE_ACCESS_KEY", secret_key = "env:VOLCENGINE_SECRET_KEY" },
]

[platforms.volcengine-agent]
enabled = true
plan_type = "agent"
tier = "medium"  # small | medium | large | max
# limits = { "5h" = 6000, weekly = 45000, monthly = 90000 }
accounts = [
    { access_key = "env:VOLCENGINE_ACCESS_KEY", secret_key = "env:VOLCENGINE_SECRET_KEY" },
]

[platforms.ollama]
enabled = true
accounts = [
    { api_key = "env:OLLAMA_API_KEY" },
]

[platforms.opencode-go]
enabled = true
accounts = [
    { api_key = "env:OPENCODE_GO_API_KEY" },
]

[platforms.clinepass]
enabled = true
accounts = [
    { api_key = "env:CLINEPASS_API_KEY" },
]

[platforms.commandcode]
enabled = true
accounts = [
    { api_key = "env:COMMANDCODE_API_KEY" },
]

[platforms.opendesign]
enabled = true
base_url = "https://amr-link.open-design.ai/v1"
# 模型服务地址会自动映射到 OpenDesign 官方用量接口。
# workspace_id = "工作区 ID"  # 可选;默认当前工作区
# max_balance = 100  # 可选;美元余额用量的显示上限,0/未设置时不显示进度条
accounts = [
    # 也可通过 Web「供应商配置」中的网页登录授权自动保存。
    { control_key = "env:OPENDESIGN_CONTROL_KEY" },  # vela_ctrl_,非 sk-amr- 模型密钥
]

[platforms.sub2api]
enabled = true
base_url = "http://127.0.0.1:18080"
accounts = [
    # 一个账号对应一组共享额度的 Key;增加 Key 不增加账号。
    # 账号内可加 daily_limit = 100,覆盖面板日报额度(不影响周/月额度)。
    # api_keys 每项可写名字:{ name = "主 Key", value = "env:SUB2API_API_KEY" }。
    { name = "组1", api_keys = ["env:SUB2API_API_KEY"] },
]
"""


def config_path() -> Path:
    """Return the resolved config file path."""
    env = os.environ.get("LLM_USAGE_CONFIG")
    return Path(env) if env else CONFIG_PATH


def load_config(path: Path | None = None) -> dict[str, Any]:
    """Load config from disk; return ``{}`` if missing (never crash)."""
    p = path or config_path()
    if not p.exists():
        return {}
    try:
        with p.open("rb") as fh:
            return to_runtime_config(tomllib.load(fh))
    except (tomllib.TOMLDecodeError, OSError, PlatformKeyConflict, ConfigFormatError) as exc:
        raise RuntimeError(f"无法读取配置 {p}: {exc}") from exc


def save_config(config: dict[str, Any], path: Path | None = None) -> Path:
    """Write config to disk as TOML, creating the directory if needed."""
    p = path or config_path()
    with _CONFIG_LOCK:
        _write_config_unlocked(config, p)
    return p


def init_config(path: Path | None = None, overwrite: bool = False) -> Path:
    """Generate the example config template. Raises if it exists and not overwrite."""
    p = path or config_path()
    with _CONFIG_LOCK:
        if p.exists() and not overwrite:
            raise FileExistsError(f"配置已存在：{p}（使用 --force 覆盖）")
        template = EXAMPLE_CONFIG if i18n.current_locale() != "zh-CN" else EXAMPLE_CONFIG_ZH
        _atomic_write_bytes(p, template.encode("utf-8"))
    return p


def get_platform_config(config: dict[str, Any], platform: str) -> dict[str, Any]:
    """Return a platform section or ``{}`` if absent."""
    normalized = to_runtime_config(config)
    return normalized.get("platforms", {}).get(canonical_platform_key(platform), {}) or {}


def get_platform_order(config: dict[str, Any]) -> list[str]:
    """Return configured ordering; callers append any unlisted platforms."""
    order = config.get("platform_order")
    if not isinstance(order, list):
        return []
    return list(dict.fromkeys(canonical_platform_key(k) for k in order if isinstance(k, str)))


def mutate_config(
    mutation: Callable[[dict[str, Any]], _T],
    path: Path | None = None,
) -> tuple[dict[str, Any], _T]:
    """串行化读-改-写事务:锁内加载最新配置 → 回调修改 → 原子落盘。

    返回 ``(cfg, mutation_result)``。回调或写盘抛异常时目标文件保持不变。
    回调只能修改传入的字典,不得在锁内调用其它 config 写函数(锁不可重入)。
    """
    p = path or config_path()
    with _CONFIG_LOCK:
        cfg = load_config(p)
        result = mutation(cfg)
        _write_config_unlocked(cfg, p)
    return cfg, result


def set_platform_order(order: list[str], path: Path | None = None) -> dict[str, Any]:
    """Persist platform_order to TOML; return the full updated config."""
    cfg, _ = mutate_config(lambda c: c.__setitem__("platform_order", order), path)
    return cfg


def update_platform_config(
    platform: str,
    updates: dict[str, Any],
    path: Path | None = None,
    *,
    remove_fields: Iterable[str] = (),
) -> dict[str, Any]:
    """Merge ``updates`` into [platforms.<platform>], then pop ``remove_fields``.

    Single atomic save; returns the full updated config.
    """
    platform = canonical_platform_key(platform)

    def _apply(cfg: dict[str, Any]) -> None:
        section = cfg.setdefault("platforms", {}).setdefault(platform, {})
        section.update(updates)
        for field in remove_fields:
            section.pop(field, None)

    cfg, _ = mutate_config(_apply, path)
    return cfg


def remove_platform_config(
    platform: str, path: Path | None = None
) -> dict[str, Any]:
    """Remove [platforms.<platform>] entirely, save TOML, return the full updated config."""
    platform = canonical_platform_key(platform)

    def _remove(cfg: dict[str, Any]) -> None:
        cfg.get("platforms", {}).pop(platform, None)

    cfg, _ = mutate_config(_remove, path)
    return cfg

