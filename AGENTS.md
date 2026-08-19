# Repository Guidelines

## Project Overview

`llm-usage` is a Python 3.11+ CLI that unifies usage monitoring for multiple LLM coding-plan / token-plan platforms (Kimi Code, Volcengine Ark Coding + Agent plans, Ollama Cloud, OpenCode Go). Platforms with public APIs are fetched live via `httpx`; platforms without APIs (Ollama, OpenCode Go) are manually entered and stored in config. Results render as a color-coded `rich` table, with SQLite history snapshots and JSON output for scripting.

## Architecture & Data Flow

Flat layered design with one-way data flow:

```
cli.py (click group)
  → config.load_config()                    # TOML at ./config.toml (cwd)
  → providers.fetch_all(cfg)                 # ThreadPoolExecutor, one thread per enabled platform
      → _prepare_config() per platform       # inject _platform_key, resolve display_name, expand env: api_key
      → Provider.fetch(config_section)       # live: httpx sync; manual: reads config entries
          → returns PlatformResult(entries=[UsageEntry...], error=None|str)
  → display.render_results() / results_to_json()   # rich Table+Panel or JSON
  → store.save_snapshot(results)            # SQLite, skips errored platforms
```

Key abstractions:
- **`Provider` Protocol** (`providers/base.py`): `runtime_checkable` protocol — `name`, `display_name`, `is_manual` attrs + `fetch(config) -> PlatformResult`. Providers return errors inside `PlatformResult.error`, never raise.
- **`UsageEntry` / `PlatformResult`** (`models.py`): the single contract every provider produces and display/store consume. `PlatformResult.ok` is `error is None`.
- **Registry** (`providers/__init__.py`): `PROVIDERS` dict maps 5 platform keys → provider instances. Volcengine registered twice (`volcengine-coding`, `volcengine-agent`) — same class, different config sections.
- **Error isolation**: `fetch_all` wraps each `fetch` in `_run` which catches all exceptions → `PlatformResult(error="内部错误：...")`. A single platform failure never breaks the batch. Results re-sorted to registry order after `as_completed`.
- **Concurrency**: `ThreadPoolExecutor` (sync `httpx.Client`), not asyncio — avoids asyncio dependency spread.
- **Dependency injection**: `httpx.Client` is constructor-injected into live providers (`client=None` → own client created/closed per `fetch`). Tests pass `httpx.MockTransport`-backed clients.

## Key Directories

| Path | Purpose |
|------|---------|
| `src/llm_usage/` | All application source (flat, no subpackages except `providers/`) |
| `src/llm_usage/providers/` | Provider implementations + registry/dispatch |
| `tests/` | pytest suite (no subdirectories) |
| `./config.toml` | Runtime config (cwd; overridable via `LLM_USAGE_CONFIG`) |
| `./history.db` | SQLite snapshots (cwd; overridable via `LLM_USAGE_DB`) |

## Development Commands

```bash
# Install (editable)
pip install -e .

# Run
llm-usage show                  # fetch all, rich table
llm-usage show --json           # JSON for scripting
llm-usage show --plain --no-save
llm-usage add ollama --label "5小时" --used 60 --limit 100 --unit "次"
llm-usage config --init         # generate template config
llm-usage config --force        # overwrite existing
llm-usage history --days 7 --platform kimi

python -m llm_usage show        # equivalent to console script

# Test
python -m pytest tests/ -v       # 44 tests, ~0.1s

# No lint/type-check/CI config exists. No Makefile, no lockfile.
```

## Code Conventions & Common Patterns

- **Type hints**: PEP 604 union syntax (`float | None`, `dict[str, Any]`). Every module starts with `from __future__ import annotations`.
- **Error handling**: Providers catch their own errors and return `PlatformResult(error=...)` — the string is user-facing Chinese (e.g. `"未配置"`, `"认证失败(401)..."`). The registry's `_run` adds a final `except Exception` safety net. CLI exits: `0` success, `1` any platform errored, `2` config/user errors.
- **Config path resolution**: `config.config_path()` reads `LLM_USAGE_CONFIG` env var, falls back to `./config.toml` (cwd). `store.db_path()` reads `LLM_USAGE_DB`, falls back to `./history.db` (cwd).
- **`env:` prefix**: Credentials in config may be `"env:VARNAME"` — `_resolve_env_value()` in `providers/__init__.py` expands via `os.environ.get`. Applied to `api_key`, `access_key`, `secret_key`. Keeps secrets out of the config file.
- **Volcengine auth**: Uses **AK/SK (AccessKey/SecretKey) + V4 HMAC-SHA256 signing** — NOT Bearer API key. The Bearer `/api/v3/endpoints/{id}/usage` endpoint returns no usage for Coding/Agent Plans (verified against the live API). Self-contained `_sign_v4()` in `volcengine.py` signs OpenAPI calls to `GetCodingPlanUsage` / `GetAFPUsage` (Service=ark, Version=2024-01-01).
- **Volcengine Coding Plan**: Backend returns **percent-only** (`QuotaUsage[]` with `Level`/`Percent`/`ResetTimestamp`, no used/total). Tier table (`VOLCENGINE_LIMITS` for lite/pro) derives `used = percent/100 * limit`. Config `limits` override takes precedence.
- **Volcengine Agent Plan**: Backend returns **absolute values** (`Periods` with `Used`/`Total`/`Percent`/`ResetTimestamp`). Falls back to `AGENT_LIMITS` tier table when `Total` is absent (large/max unverified → `None`).
- **SQLite reserved word**: The `snapshots` table quotes `"limit"` as a column name (it's a SQLite keyword) in schema, INSERT, and SELECT.
- **Timezone convention**: `reset_at` (UsageEntry) and snapshot `ts` are stored as ISO 8601 **UTC** (`Z`/offset suffix; naive treated as UTC). All terminal display converts to the machine's local time via `display._to_local_time()` (panel title included); JSON output keeps the raw UTC values.
- **Manual providers**: `limit` of `0` or `None` → treated as unlimited (`limit=None`, `percent=None`). `is_manual=True` entries read from config, never hit the network.
- **Display color thresholds**: `RED_THRESHOLD=95.0`, `YELLOW_THRESHOLD=80.0` — percent cell colored red/yellow/green accordingly.
- **Naming**: Provider classes `XxxProvider`; test classes `TestXxx`; test methods `test_descriptive_snake_case`.
- **Statelessness**: Provider instances are singletons in the registry; no per-call state except the injected `httpx.Client`.

## Important Files

| File | Role |
|------|------|
| `src/llm_usage/cli.py` | `@click.group()` entry point — `show`/`add`/`config`/`history`; exit codes; `_load()` helper |
| `src/llm_usage/models.py` | `UsageEntry`, `PlatformResult` dataclasses + `compute_remaining`/`compute_percent` |
| `src/llm_usage/providers/__init__.py` | `PROVIDERS` registry, `fetch_all` concurrent dispatch, `_prepare_config`, `env:` key resolution |
| `src/llm_usage/providers/base.py` | `Provider` Protocol definition |
| `src/llm_usage/providers/kimi.py` | Kimi live provider — `/usages` with 404→`/usage` fallback, `limits[]`+`usage` parsing, ms/ISO resetTime, Chinese 401/404 hints |
| `src/llm_usage/providers/volcengine.py` | Volcengine live provider — AK/SK V4 signing → `GetCodingPlanUsage` (percent-only) + `GetAFPUsage` (used/total); self-contained V4 signer; tier table for deriving used from percent |
| `src/llm_usage/providers/manual.py` | `ManualProvider` base — reads config entries, zero/None limit → unlimited |
| `src/llm_usage/config.py` | TOML load/save (`tomllib` read / `tomli_w` write), `EXAMPLE_CONFIG` template, `set_manual_entry` |
| `src/llm_usage/store.py` | SQLite `snapshots` table, `save_snapshot` (skips errored), `query_history` (platform/days filters) |
| `src/llm_usage/display.py` | `render_results` (rich Panel+Table), `results_to_json`, `render_history` |
| `src/llm_usage/__main__.py` | `python -m llm_usage` shim → `cli.main()` |
| `pyproject.toml` | Sole build config — setuptools, deps, entry point, pytest config |

## Runtime/Tooling Preferences

- **Python ≥3.11** required — uses stdlib `tomllib` for TOML reads. Writes use `tomli_w` (stdlib has no TOML writer).
- **Runtime deps**: `httpx>=0.27`, `rich>=13.0`, `click>=8.1`, `tomli_w>=1.0` — pinned with `>=` floors only, no upper bounds.
- **Package manager**: `pip` (editable install). `uv`/`uvx` available on the system as alternatives. No lockfile committed.
- **Build backend**: `setuptools.build_meta` (`setuptools>=68.0`), src-layout (`packages.find where=["src"]`).
- **Entry points**: console script `llm-usage = "llm_usage.cli:main"` and `python -m llm_usage` — both call the same `cli.main()`.

## Testing & QA

- **Framework**: pytest ≥7.0 (optional dependency via `[project.optional-dependencies] test`). Configured in `pyproject.toml` with `testpaths = ["tests"]`.
- **Run**: `python -m pytest tests/ -v` — 44 tests, ~0.1s, no network.
- **No coverage config, no markers, no tox/CI.**

### Test isolation patterns

- **`tmp_config_path`** fixture (`conftest.py`): `monkeypatch.setenv("LLM_USAGE_CONFIG", tmp_path/"config.toml")` — redirects config I/O to temp.
- **`tmp_db_path`** fixture: same pattern with `LLM_USAGE_DB` — isolates SQLite per test.
- **`_clear_api_keys`** (autouse): `monkeypatch.delenv` for `KIMI_API_KEY`/`VOLCENGINE_API_KEY`/`VOLCENGINE_ACCESS_KEY`/`VOLCENGINE_SECRET_KEY` — prevents real keys leaking into provider tests.
- **HTTP mocking**: `httpx.MockTransport(handler)` injected via `client=` kwarg in provider constructors. Handler functions assert request URL/headers and return canned `httpx.Response`. No network, no external services.

### Coverage areas

- **`test_providers.py`**: Kimi parsing (`used=limit-remaining`, epoch-ms resetTime→ISO, 404→`/usage` fallback, 401 hint); Volcengine OpenAPI V4 signing (Coding Plan percent-only → derived used via tier table; Agent Plan used/total; config `limits` override; error response with `ResponseMetadata.Error`; HTTP error); manual providers (defaults, config reads, zero-limit→unlimited); registry (all 5 keys, `env:` prefix resolution for `api_key`/`access_key`/`secret_key`, error isolation, disabled-platform skip, empty config).
- **`test_config.py`**: init creates/refuses-overwrite/overwrites; load-missing→`{}`; roundtrip; `set_manual_entry` update/add/persist; `env:` prefix stored verbatim.
- **`test_display.py`**: render contains platform names / error rows / manual marker; JSON structure; history empty + with-rows; local-time conversion of `reset_at`/history `ts` (UTC input → local display); store save/query/filter/two-snapshots/manual-flag-persisted/failed-not-saved.