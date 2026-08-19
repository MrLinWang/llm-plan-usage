# llm-usage

Unified CLI tool to monitor LLM coding plan / token usage across multiple platforms.

## Install

```bash
pip install -e .
```

Or use the one-click setup script (requires [uv](https://docs.astral.sh/uv/)):

```bash
./setup.sh                    # creates .venv, installs deps including test extra
source .venv/bin/activate     # activate the environment

## Quick start

```bash
llm-usage config --init     # generate example config at ./config.toml
llm-usage show              # fetch all platforms, render a rich table
llm-usage show --json        # JSON output for scripting
llm-usage history --days 7
```

## Supported platforms

| Platform | Method | Notes |
|----------|--------|-------|
| Kimi Code (Moonshot Coding Plan) | Auto API | `GET /coding/v1/usages`, Bearer |
| 火山方舟 Coding Plan | Auto API | Volcengine OpenAPI `GetCodingPlanUsage`, AK/SK + V4 signing |
| 火山方舟 Agent Plan | Auto API | Volcengine OpenAPI `GetAFPUsage`, AK/SK + V4 signing |
| Ollama Cloud | Auto API | `GET /api/usage`, Bearer |
| OpenCode Go | Auto API | `GET /zen/go/v1/usage`, Bearer |

## Config

Located at `./config.toml` (current directory). API keys support an `env:VARNAME` prefix to
read from environment variables instead of storing plaintext on disk. Run
`llm-usage config --init` to generate a template. Override the path with the
`LLM_USAGE_CONFIG` env var (history DB with `LLM_USAGE_DB`).

## Time display

`重置时间` (window reset), the overview title, and history timestamps are
shown in the machine's **local timezone**. Providers return UTC timestamps;
stored values (`reset_at`, snapshot `ts`) and `--json` output keep them in
raw UTC ISO 8601. Moving the machine to another timezone shifts the display
accordingly.