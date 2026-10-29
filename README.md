# llm-usage

**English** | [简体中文](README.zh-CN.md)

[![docker](https://github.com/MrLinWang/llm-plan-usage/actions/workflows/docker.yml/badge.svg)](https://github.com/MrLinWang/llm-plan-usage/actions/workflows/docker.yml)

A unified CLI for monitoring usage across multiple LLM coding plans and token plans.

## Installation

```bash
pip install -e .
```

For the Web dashboard, install the `web` extra:

```bash
pip install -e ".[web]"
```

Or use the one-shot setup script (requires [uv](https://docs.astral.sh/uv/)):

```bash
./setup.sh                    # create .venv and install dependencies (including test/web extras)
source .venv/bin/activate     # activate the virtual environment
```

## Quick start

```bash
llm-usage config --init     # generate an example ./config.toml
llm-usage config --format   # normalize an existing config to the accounts format
llm-usage show              # fetch every platform and render a rich table
llm-usage show --keys       # overview + per-key usage for multi-key groups
llm-usage show --json       # JSON output for scripting
llm-usage tui               # interactive terminal dashboard (q quit, r refresh, +/- interval)
llm-usage web               # browser dashboard (default http://127.0.0.1:8765)
llm-usage history --days 7  # view historical snapshots
```

## Language

- The Web UI follows the browser language (`zh*` → 简体中文, otherwise English). Use `⋯` → Language on any page, then choose **English** or **简体中文** in the submenu; the choice is stored in a cookie and `localStorage`.
- CLI: `--lang en|zh-CN`, or `LLM_USAGE_LANG` / `LANG`. `show --json` keeps canonical labels/errors for scripting; human output is localized.
- Provider errors and usage labels are kept canonical internally and translated at display time.

## Docker

Images are built and published to GHCR by GitHub Actions (`main` → `latest`, `v*` tag → version tag, multi-arch amd64/arm64):

```bash
docker run -d --name llm-usage \
  -p 8765:8765 \
  -v llm-usage-data:/data \
  ghcr.io/mrlinwang/llm-plan-usage:latest
```

Open http://127.0.0.1:8765/ and create the admin account on first visit. Then fill in each platform's credentials on the "Provider configuration" page (written back to `/data/config.toml`), or inject environment variables directly:

```bash
docker run -d --name llm-usage -p 8765:8765 -v llm-usage-data:/data \
  -e KIMI_API_KEY=sk-... -e TZ=Asia/Shanghai \
  ghcr.io/mrlinwang/llm-plan-usage:latest
```

- The `/data` volume persists configuration and the history database (including users/sessions); a named volume is recommended. When using a bind mount, the host directory must be writable by uid 1000.
- To change the port, use `-e PORT=9000 -p 9000:9000` (the healthcheck follows `$PORT`).
- Local build: `docker build -t llm-usage:dev .`

## Supported platforms

| Platform | Mode | Notes |
|----------|------|-------|
| Kimi Code (Moonshot Coding Plan) | Automatic API | `GET /coding/v1/usages`, Bearer; absolute `limits/usage` plus the newer `usages` ratio fallback |
| Volcengine Ark Coding Plan | Automatic API | Volcengine OpenAPI `GetCodingPlanUsage`, AK/SK + V4 signing |
| Volcengine Ark Agent Plan | Automatic API | Volcengine OpenAPI `GetAFPUsage`, AK/SK + V4 signing |
| Ollama Cloud | Automatic API | `GET /api/balance`, Bearer; legacy plans use 5-hour/weekly percent windows, credits plans use a monthly `$` allowance pool |
| OpenCode Go | Automatic API | `GET /zen/go/v1/usage`, Bearer Console key; 5-hour/weekly/monthly percent windows (Go/Plus/Teams allowances are model-dependent) |
| ClinePass | Automatic API | `GET /api/v1/users/me/plan/usage-limits`, Bearer; 5-hour/weekly/monthly percent windows; a missing subscription returns a clear message |
| Command Code | Automatic API | `GET /alpha/billing/credits` + `/subscriptions`, Bearer; 5-hour/weekly `$` windows + monthly allowance + optional Cloud minutes |
| OpenDesign | Automatic API | `GET /api/v1/workspaces/current` + `/api/v1/workspaces/{id}/billing/preflight`, Bearer control key; credit quota windows + USD balance |
| Sub2API | Automatic API | `GET /v1/usage`, Bearer; reads `subscription` into daily/weekly/monthly rows (older payloads fall back to today's `actual_cost`) |

The nine built-in platform keys above are all available. Each type can also have multiple **independent instances** (extra account cards, with keys such as `kimi#2`); see "Same-type provider instances" below.

## Configuration

The configuration file lives at `./config.toml` (current directory). API keys support an `env:VARNAME` prefix so secrets can be read from environment variables instead of being stored in plaintext. Run `llm-usage config --init` to generate the template (the repository also ships a read-only reference `config.toml.example` with identical content; you can run `cp config.toml.example config.toml`).

Every provider uses "one table per provider": platform settings and the `accounts` list live in the **same** `[platforms.<provider>]` table, with no separate `[[platforms.<provider>.accounts]]` child table. All nine provider types (including Sub2API's shared-key groups and instances such as `kimi#2`) share the same layout:

```toml
[platforms.kimi]
enabled = true
base_url = "https://api.kimi.com/coding/v1"
accounts = [
    { name = "Plan A", api_key = "env:KIMI_API_KEY_A" },
    { name = "Plan B", api_key = "sk-kimi-xxx" },
]

[platforms.volcengine-coding]
enabled = true
plan_type = "coding"
tier = "pro"
accounts = [
    { access_key = "env:VOLCENGINE_ACCESS_KEY", secret_key = "env:VOLCENGINE_SECRET_KEY" },
]

[platforms.sub2api]
enabled = true
base_url = "http://127.0.0.1:18080"
accounts = [
    { name = "Group 1", api_keys = ["env:SUB2API_API_KEY"] },
]
```

- `[platforms.<provider>]` holds platform settings such as `enabled`, `display_name`, `base_url`, `tier`, `limits`, `max_balance`, plus the `accounts` array.
- Each `accounts` entry is one account, **one per line**. `name` is optional. Credentials use `api_key` (kimi / ollama / opencode-go / clinepass / commandcode), `control_key` (OpenDesign), or `access_key` + `secret_key` (Volcengine). For Sub2API, one account is one shared-key group using `name`, an optional `daily_limit`, and `api_keys`; each `api_keys` entry may be a key string or a named `{ name = "Primary", value = "sk-..." }` object.
- Unused legacy credentials or groups are retained in the platform's `legacy` table (for example `legacy = { api_key = "sk-amr-..." }`) so migrations never silently drop them; they are not mixed into the active account list.

Legacy top-level credentials, `credentials`, Sub2API `groups` / `use_groups`, and the `[[platforms.<provider>.accounts]]` child-table format are still readable; saves are normalized to the in-table `accounts` array shown above. API keys and `env:` references are preserved verbatim, and environment variables are never expanded during formatting. You do not need to maintain both formats.

Normalize an existing file immediately:

```bash
llm-usage config --format
```

The command atomically rewrites the current config file and never prints secrets; conversion or write failures preserve the original file. The layout is deterministic: every provider has exactly one table header, and `accounts` is a multi-line array with one account per line. Nested `api_keys` only break to one item per line when that account line exceeds 100 columns, so the layout never flips between inline objects and multi-line tables based on secret length. Web saves use the same formatter.

### Automatic retries

A failed platform fetch automatically retries **transient failures**: network errors/timeouts, HTTP 408/429/5xx, response parse failures, and provider-internal exceptions. Retries are per platform (and per credential for multi-account platforms), with exponential backoff 0.5s → 1s → 2s (capped at 5s per sleep), without blocking other platforms' concurrent fetches. **Permanent errors are not retried** because the result would be identical: authentication failure (401), not configured, 404, configuration error, no subscription, and similar.

The retry budget is the top-level `max_retries` key. The default `3` means **three extra attempts** after the first failure (up to 4 requests and 3 backoff sleeps); `0` disables retries. Negative or non-integer values fall back to the default; values above 10 are capped at 10. Partial successes with a warning are not retried: when some keys in a gateway group succeed, retrying would only duplicate requests.

```toml
max_retries = 3
```

`show` / `tui` / `web` share the same retry logic (inside the Web TTL cache).

### OpenDesign

OpenDesign model-inference keys (`sk-amr-`) and account control keys (`vela_ctrl_`) serve different purposes. In the Web "Provider configuration" page, click "Web login authorization" in the OpenDesign credential slot, then "Open OpenDesign login and authorize" to visit the official page. Sign in to the OpenDesign account and confirm; enter the authorization code shown on the configuration page if requested. When you return, the authorization status updates automatically; a successful query credential is saved without requiring you to find or copy a `control_key`. Passwords are only entered on OpenDesign's official page. Existing credential slots can be re-authorized, and new slots can connect other accounts; standalone OpenDesign provider instances support the same flow.

Admin credentials are saved to `config.toml`; regular users' credentials are saved only to their own account configuration. Web login authorization preserves the existing model `api_key`, other plans, and the enabled flag; other edits such as display name and the enabled switch still require clicking "Save". If the provider is disabled, you must still enable it and save before usage appears. Expired or cancelled authorizations can be restarted.

If you already have a control key, you can enter it directly in the credential slot or configure it in `config.toml`. After logging in with the official CLI via `vela login`, the production control key is stored in `~/.amr/config.json` under `profiles.prod.controlKey` (or under `AMR_HOME` if set). Put that value into `control_key`, or set the `OPENDESIGN_CONTROL_KEY` environment variable and reference it from `config.toml`:

```toml
[platforms.opendesign]
enabled = true
base_url = "https://amr-link.open-design.ai/v1"
accounts = [
    { control_key = "env:OPENDESIGN_CONTROL_KEY" },  # vela_ctrl_ account control key
]
```

`base_url` is the model service address; usage is read through OpenDesign's official `amr-api` control endpoint. Multiple credential plans and standalone provider instances are supported. Saving a control key preserves the existing model-inference `api_key`.

The query first reads `GET https://amr-api.open-design.ai/api/v1/workspaces/current` to determine the active workspace, then `/api/v1/workspaces/{id}/billing/preflight`. You can set `workspace_id = "workspace ID"` manually in the platform section to query a specific workspace directly. Returned Coding Plan windows show used, limit, remaining, and reset time in their native `credits` unit; the wallet balance is shown separately as a "Balance usage" row in `$`. Set "Maximum balance ($)" in the Web provider editor to show a usage bar computed as `(max balance - current balance) / max balance`; a full balance shows an empty bar, and lower balances show more progress. You can also set `max_balance = 100` (USD) in `[platforms.opendesign]`. The limit is per provider instance and applies to each of its accounts; leave it empty or set it to `0` to disable the bar. The maximum balance is only a display scale: spend still shows as `-`; progress is 0% at or above the limit and 100% at zero or negative balance, while the real balance is always preserved.

With only an `sk-amr-` key, use the Web login authorization flow above to connect the account. Authentication and quota fields follow the [official login implementation](https://github.com/nexu-io/open-design/blob/main/apps/daemon/src/integrations/vela.ts) and [official billing preflight parser](https://github.com/nexu-io/open-design/blob/main/apps/daemon/src/integrations/vela-billing.ts).

### Sub2API

Sub2API is configured entirely in `config.toml` like every other platform; `base_url` is required. The config key is `sub2api`, with independent instances named `sub2api#N`; the default display name is `Sub2API`, customizable via `display_name`. Existing `llm-gateway` sections and instances are converted automatically on read and saved under the new key; history, user configs, and shares are migrated automatically, while existing credentials and environment references remain valid. If old and new keys both exist with different contents, an explicit conflict is reported instead of overwriting either account. The provider reads the gateway's `/v1/usage` `subscription` block: **each key in each group** reports daily/weekly/monthly quotas and usage. Successful keys are summed per window, and each group shows three rows: Daily / Weekly / Monthly. Limits default to the panel's `daily_limit_usd` / `weekly_limit_usd` / `monthly_limit_usd`; a group-level `daily_limit` **overrides** the daily quota (a multi-key group's limit is the sum of successful keys; if any key reports no quota, the limit stays empty and no progress bar is drawn). `subscription.expires_at` drives the Monthly row's countdown with **expiry** semantics: an expired plan shows "Expired" rather than "Reset". Older gateways without `subscription` fall back to `usage.today.actual_cost` (`cost` is an even older fallback) and produce a single Daily row. API keys are configured per **group**; each group gets its own display and history. The template generates one group (`Group 1`, `env:SUB2API_API_KEY`):

```toml
[platforms.sub2api]
enabled = true
base_url = "http://127.0.0.1:18080"
accounts = [
    # Optional daily_limit = 100 inside the account (overrides the panel daily quota; weekly/monthly are unaffected)
    { name = "Group 1", api_keys = ["sk-team-a-key-1", "sk-team-a-key-2"] },
]
```

Keys can be written directly in `config.toml` or referenced via `env:VARNAME`. Each key can have a **display name**, shown in `show --keys` and the Web dashboard popover, and included in per-key error messages:

```toml
[platforms.sub2api]
enabled = true
base_url = "http://127.0.0.1:18080"
accounts = [
    { name = "Group 1", api_keys = [
        { name = "Primary", value = "env:SUB2API_TEAM_A_1" },
        { value = "sk-team-a-key-2" },  # unnamed → displayed as key#2
    ] },
]
```

If some keys in a group fail, successful keys are still aggregated but the result carries a warning; partial aggregates are not written to history snapshots, so incomplete data is never mistaken for a complete usage record. The same key must not be assigned to multiple groups. Use the `LLM_USAGE_CONFIG` environment variable to override the config path (and `LLM_USAGE_DB` for the history database).

Legacy top-level single-key forms (`api_key` + optional `daily_limit` + `use_groups`) and the `groups` list remain readable; the Web UI presents the effective configuration as account groups and migrates it to `accounts` on save. New configs do not need `use_groups`.

### Multiple billing plans (kimi / volcengine ×2 / ollama / opencode-go / clinepass / commandcode / opendesign)

Except for Sub2API, every platform supports **multiple credentials**: different credentials under the same platform are **different billing plans** for that provider. Each credential is fully independent — separate fetch, separate display, no shared quota, no value aggregation; the dashboard partitions usage rows by plan for the same platform (`show` / `tui` / `web` all behave the same). This differs from Sub2API's shared keys inside one account (shared quota, aggregated sums). A single credential shows no plan header — a single slot (including `name = "套餐1"` written by the Web editor) is indistinguishable from the legacy single-key form; only two or more credentials produce plan sections.

The Web "Provider configuration" page starts with one credential slot per platform (`api_key` for kimi/ollama/opencode-go/clinepass/commandcode; `control_key` for OpenDesign; `access_key` + `secret_key` for the two Volcengine platforms). Click "Add credential" (or "Add AK/SK" for Volcengine) to add a plan; leave the plan name empty and the server names it `套餐1`, `套餐2`, ... A blank credential value means "keep the saved value"; saves write to the platform's `accounts` array (legacy single-key and `credentials` formats migrate automatically). Manual configuration works too:

```toml
[platforms.kimi]
enabled = true
# base_url = "https://api.kimi.com/coding/v1"
accounts = [
    { name = "Plan A", api_key = "env:KIMI_API_KEY_A" },
    { name = "Plan B", api_key = "sk-kimi-xxx" },
]
```

Each credential is an independent billing plan: `show`/`tui`/`web` display each plan's rows inside the same platform card, with a plan-name header; history snapshots record the plan; JSON output includes a `plan` field on each entry. If some credentials fail, the platform still shows the successful ones with a warning (and does not write a snapshot); only when all fail does it show an error. Legacy `credentials` arrays and top-level single credentials keep their original fetch behavior and are converted to the unified `accounts` format on save.

## Logging

Every command (`show` / `tui` / `web` / `config` / `history`) writes runtime logs to a `log/` directory next to the config file (i.e. `./log/llm-usage.log`, already `.gitignore`d; rotation: 1 MB per file, 3 backups `llm-usage.log.1`–`.3`) while still writing to stderr, so `show --json` stdout stays clean. Logs include provider failures and retries, Web startup/access/errors, login throttling, and other server details (user-facing errors remain sanitized as described in "Security"). Per-request INFO lines from httpx are suppressed to WARNING to avoid noise and filling the file quickly. Override the log path with `LLM_USAGE_LOG`:

```bash
LLM_USAGE_LOG=/var/log/llm-usage.log llm-usage web
```

In the Docker image the config file is `/data/config.toml`, so logs default to `/data/log/llm-usage.log` (persisted with the `/data` volume and surviving container recreation) with no extra environment variable.

## Security

- **File permissions**: `config.toml` and `history.db` are created with `0600` (they contain plaintext API keys, password hashes, and session tokens). Do not loosen permissions and do not commit them to git.
- **Login throttling**: login / registration / setup endpoints are throttled per source IP — 5 failures in 60 seconds lock the source for 5 minutes (HTTP 429). Counters are in-process and reset on restart.
- **Sanitized errors**: network/internal failures return generic messages (e.g. "Network error", "Internal error", `HTTP {status}`); details go only to server logs and are never echoed to the browser.
- **Session tokens**: session tokens are stored in plaintext in `history.db` — a database leak means sessions can be hijacked, so protect the file.
- **HTTPS reverse proxy**: if serving over HTTPS through a reverse proxy, set `LLM_USAGE_SECURE_COOKIE=1` to add the `Secure` flag to the session cookie.
- **Single-process deployment**: usage caches and login throttling are in-process; do not run multiple workers (the default single-worker `llm-usage web` needs no configuration).
- **WAL backups**: SQLite uses WAL mode; back up `history.db-wal` / `history.db-shm` too, or checkpoint first.

## Web dashboard

`llm-usage web` starts a FastAPI + Uvicorn server on `127.0.0.1:8765` by default; open `http://127.0.0.1:8765/` to view the usage overview. The dashboard is equivalent to `llm-usage show`, with one card per platform: each card shows the platform name and status, plus window / used / limit / remaining / reset time / reset countdown columns and a progress bar.

The backend caches provider results for `interval` seconds (default 60, min 5, max 3600); automatic polling reuses cached results until expiry and does not call external APIs repeatedly. The frontend polls at the `interval` returned by the server. This is a live dashboard; it does not write historical snapshots.

The "Refresh" button in the top-right immediately issues a request, bypassing the cache for your own config and the configs of visible shared sources, without clearing other configs' caches. While waiting, old cards remain and "Refreshing..." is shown; on success the cards update and the timer restarts. Late responses from older polls cannot overwrite a newer request's result or waiting state. On failure, existing data stays visible, an error is shown, and the page retries after 10 seconds; manual refresh remains available. New data still requires waiting for provider responses and any retries, but does not require waiting for the next automatic refresh.

After updating the server code, restart the Web service and reload the page to use the new refresh logic.

Use the top-right control to switch light/dark themes; the choice persists in browser `localStorage`. Without a manual choice it follows the system `prefers-color-scheme` (dark by default).

The dashboard uses a responsive layout: when the viewport cannot fit every platform card, the frontend tries three densities — normal → compact → ultra — compressing whitespace and secondary columns only, never the body text, and re-tries on resize. Each card is also its own container-query context; narrow cards hide the secondary "limit/reset time" columns and abbreviate numbers as k/M/B (full values on hover). Platforms with many entries collapse by default: "+N" expands all, and clicking again collapses. Periodic-window platforms keep core windows such as 5-hour/weekly when collapsed; non-periodic platforms with the same unit rotate every 5 seconds (paused on hover, focus, or hidden tab). Collapsed Sub2API multi-account cards also rotate the shortest window per account (usually daily): normal shows two accounts, compact/ultra shows one, and the shortest-window total stays fixed. Collapsed cards that still hide rows get a switch button in the card header (e.g. "1–2/3 ›"): click to show the next group, with the same number of rows per group, wrapping to the first group after the last, without expanding the whole card. Periodic-window cards (e.g. 5-hour/weekly → monthly in compact mode) page manually and never rotate automatically; in ultra mode, cards showing only "Total" switch between the total and each entry; at normal density, Sub2API cards with exactly two accounts can page through each account's weekly/monthly rows; cards with rotation can be switched manually by clicking the rotation indicator. The position is not persisted: data refreshes, window resizes, or any expand/collapse return to the default view. Non-periodic total rows sum only used amounts (no invented total progress when limits are incomplete). Totals on multi-account cards (Sub2API groups, multi-credential plans) mirror the account structure: a "Total" header plus one row per window (daily/weekly/monthly), summing only entries with the same window, unit, and addable values; when collapsed only the shortest window (daily for gateways) is shown, and compact/ultra expansion keeps only each account's shortest window, re-running the density pass immediately after expand/collapse (no resize needed). Expanded cards reflow their content into multiple columns based on the outer card grid; each platform's expanded state is kept in `sessionStorage` (current tab session only, surviving data/page refreshes and cleared on logout).

### PWA (install to desktop / offline viewing)

The dashboard is a Progressive Web App: install it from the browser (Chrome/Edge install icon) or "Add to Home Screen" in iOS Safari, and it runs in a standalone window. All four pages register the Service Worker (`/sw.js`, root scope) inline:

- **Offline fallback and privacy**: page navigation is network-first with a page-cache fallback; `/api/*` (including `/api/usage`) always goes directly to the network and is never written to or served from the Service Worker cache, while the server also returns `Cache-Control: no-store`. Offline usage requests fail; open pages keep their existing cards but never restore usage data from cache; reopening an offline page only loads the page shell. Polling resumes when the network returns.
- **Unauthenticated assets**: `/manifest.webmanifest` (app name/icons/standalone), `/icons/icon-192.png`, `/icons/icon-512.png`, and `/icons/apple-touch-icon.png` are available without login (so the login page can be installed too); the theme color is always dark `#16181d`.
- **Cache versioning**: after editing `static/sw.js`, bump the `CACHE` version at the top of the file so clients evict old caches.

```bash
llm-usage web --host 127.0.0.1 --port 8765 --interval 60
```

Without the web extra, `llm-usage web` prints a hint to install `pip install llm-usage[web]`.

### Login and user management

Every Web page and API requires login. On first launch (no users), visiting any page starts setup: create the first **admin** account and sign in automatically; afterwards use `/login`. Session cookies last 7 days (`httponly`, `samesite=lax`) and survive service restarts.

- **Admin**: the dashboard header's "User management" lists/adds/deletes users and resets others' passwords (you cannot delete yourself; the system supports **only one admin** — "Add user" always creates a regular user, and the admin can only be created by initial setup). See "Provider configuration" below.
- **Regular user**: the dashboard shows only their own enabled platforms (see "Multi-user usage isolation" below); the "Provider configuration" page mirrors the admin UI (credential slots + gateway base_url/groups + visibility) and writes to their own `user_configs` (in `history.db`), with no "User management" entry. The "Provider configuration" entry in the dashboard ⋯ menu is also shown to regular users (hidden when the site toggle is off).
- **User-config toggle**: the admin can disable **allowing regular users to add/edit their own providers** on the "User management" page (site setting, enabled by default; persisted in the `settings` table in `history.db`). When disabled, regular users see a notice on "Provider configuration", the ⋯ menu entry is hidden, and `/api/my/*` returns 403; read-only usage views are unaffected.
- **Open registration**: after the admin enables "Allow visitors to register on the login page", the login page shows "No account? Register"; self-registered users are always **regular users** and are signed in immediately. The setting is off by default and persisted in the `settings` table (survives restarts); when disabled the entry disappears and the registration API returns 403.
- **Username rules**: registration/creation usernames are 1–64 characters from letters, digits, underscore, dot, or hyphen (`[A-Za-z0-9_.-]`).
- **About**: the dashboard ⋯ menu's "About" opens a panel with the current version and a **release-notes log** (newest first, with a "Current version" badge; scrollable when long). The version is resolved by `src/llm_usage/version.py`: it prefers `llm_usage.__version__` (kept in sync with `pyproject.toml`'s `version`), falls back to installed package metadata only if missing/empty, and shows `Unknown` if neither exists — it never raises, so `llm-usage --version` and the panel always agree and the frontend never hardcodes it. The changelog data lives in `src/llm_usage/changelog.py` and ships with the package, so it is available inside the Docker image too. Close the panel with "Close", Esc, or by clicking the backdrop. If the API is unavailable, the version shows `Unknown`, the log area shows "Failed to load release notes", and the panel still opens.

  To release a new version, change three places: `pyproject.toml`'s `version`, `src/llm_usage/__init__.py`'s `__version__` (they must match), and prepend a record to `CHANGELOG` in `changelog.py` (`version` / `date` / `entries`, with each entry's `kind` being `feature` / `fix` / `breaking`); `version.py` only parses and needs no change. Tests assert that the first log entry matches the package version, so a missed update fails immediately.
- Changing or resetting a password invalidates that user's other sessions immediately.

Users and sessions live in `./history.db` (`users` / `sessions` tables). Passwords are stored as PBKDF2-SHA256 hashes (600k iterations, random salt), never plaintext; site-level toggles (such as open registration) live in the same database's `settings` table. If all admin passwords are lost, delete rows from the `users` table in `history.db` to re-run setup.

### Provider configuration

Admins can edit each platform's enabled flag and credentials on "Provider configuration" (`api_key` for kimi/ollama/opencode-go/clinepass/commandcode; `control_key` for OpenDesign; `access_key` + `secret_key` for the two Volcengine platforms; Sub2API has no top-level credential fields and uses group configuration). kimi/volcengine×2/ollama/opencode-go/clinepass/commandcode/opendesign are edited as **credential slots**: each slot is an independent billing plan (optional "Plan name"; empty becomes `套餐N`), with one credential input (two for Volcengine AK+SK); "Add credential" (or "Add AK/SK") appends a plan slot. Sub2API is edited as **groups**: one "Group 1" by default, each with an API-key input and a daily-limit input (empty = use the gateway's daily quota; a value overrides it without affecting weekly/monthly quotas); "Add shared key" appends a key sharing the group's quota, and "Add group" adds another group. Each key row also has an optional "Key name" shown in the dashboard's hover details and `show --keys` (empty = keep the existing name); keys in the same group have their daily usage summed. Credentials are never shown in plaintext: saved values appear masked (e.g. `Set (sk-k…ey)`) and `env:VARNAME` references pass through unchanged; an empty credential means "keep the saved value", and removing a whole slot is the only way to delete a credential. Saving writes immediately to `./config.toml` and invalidates the server usage cache (effective on the next refresh): every provider is written as an `accounts` array inside its `[platforms.<provider>]` table, with each Sub2API group saved as one account; legacy credential formats are migrated. **Unchecking "Enabled" never deletes credentials**: submitting only `enabled` updates just the flag; clicking "Save" on the Web page submits credential slots/groups too, but empty slots keep existing values, so credentials, plan slots, and Sub2API groups all survive (legacy formats may migrate to `accounts`, with values unchanged). Re-enable later without re-entering anything. A disabled platform does not appear on the dashboard (fetches skip it) but remains visible and editable on "Provider configuration". Sub2API also supports **one-click config.toml import**: the "Import config.toml" button next to the add bar uploads a TOML file (`POST /api/config/import`) and **replaces** the current configuration — admins replace `./config.toml`, regular users replace their `user_configs` in `history.db` (syncing local config to the online service). This is useful when migrating machines/deployments; a confirmation dialog appears, and you should back up existing config first. Importing identical content does not trigger a refetch (the cache is keyed by config hash).

### Same-type provider instances (multi-account cards)

The same provider type can have multiple **independent instances** (for example a second Kimi account): choose a type in the add bar at the top of "Provider configuration" and click "Add provider" to create a new card (instance keys look like `kimi#2`; the first instance keeps the bare base key). Each instance has its own credentials, display name, visibility, and dashboard card; new instances are **disabled** by default — configure credentials and enable them. Admin additions write to `./config.toml` (`POST /api/config/providers`); regular users write to their `user_configs` (`POST /api/my/providers`). Delete an instance with the card's "Delete this provider" button (with confirmation); its shares are cascaded away. The nine built-in base platforms cannot be deleted. Instance numbers increase monotonically and are never reused (deleting `kimi#2` then adding again yields `kimi#3`), avoiding ambiguity in historical snapshots. Terminal usage needs no configuration: instances are fetched and displayed automatically with their platform's config.

### Multi-user usage isolation and visibility

Usage is **private** by default: every user (including the admin) sees only their own platform configuration on their dashboard. The system uses a **single-admin model** — the admin account (created by the first setup) keeps platform config in `./config.toml`; regular users (including self-registered ones) keep platform config in the `user_configs` table (JSON) in `./history.db`, completely isolated from `config.toml`. The granularity is **per platform key**.

- **Regular users can configure all provider details, just like admins**: the "Provider configuration" page is structurally identical — regular users also get credential-slot editors (`api_key` for kimi/ollama/opencode-go, AK/SK for Volcengine, one billing plan per slot) and the Sub2API `base_url` + group editor (groups, daily limit, shared keys, key names), with field-level validation identical to admin `/api/config`, saving the unified `accounts` format. The only difference: regular-user saves go to their own `user_configs` (`/api/my/platforms`, in `history.db`), while admin saves go to `config.toml` (`/api/config`); credentials are always masked (`env:` / not-set states are preserved).
- **Three visibility states** (the dropdown on each platform card):
  - **Private** (default): visible only to yourself;
  - **Public**: visible to every signed-in user;
  - **Shared with selected users**: visible only to the selected usernames (you cannot share with yourself; targets must exist; duplicates are removed automatically).
- **Source labels**: shared platforms get a `(username)` suffix on the card title (e.g. `Kimi Code(alice)`); admin-public platforms likewise show `Sub2API(admin)` (sharing always labels the source, without exception).
- **Custom display names**: every platform card has a "Display name" input (all platforms; empty = keep current, max 64 characters). A renamed platform uses the new name immediately on your dashboard (and in terminal `show`/`tui`); share targets see `custom name(username)` (e.g. `MyK(alice)`).
- **Name collisions (multiple sources coexist)**: your own platform keeps the bare name (your own source, not part of collisions); shared platforms with the same name are all kept — `Kimi Code(admin)` and `Kimi Code(alice)` are two different cards, never overwritten or dropped.
- **Admin dashboard** = all platforms in `config.toml` + platforms other users made public/shared with admin (sharing with admin is allowed but has no additional effect); **regular-user dashboard** = their own DB platforms + admin public/shared platforms + other users' public/shared platforms.
- **Visibility storage**: rows in the `user_shares` table (`owner/platform/target`) in `history.db`; deleting a user cascades their config and all shares (as owner or target); deleting a provider instance cascades all of its share rows.
- The backend keeps a separate usage-cache entry per **config hash** (up to 32 entries, oldest evicted); users with identical configs share one cache entry and do not call external APIs repeatedly.

## Development

```bash
pip install -e ".[test,dev]"
python -m pytest tests/ -q          # full test suite (no network)
ruff check src tests && mypy src    # lint + type check
```

Note: the `show --plain` flag is currently not implemented (reserved; behaves like the default rendering).

## Time display

`Reset time` (quota window reset), the overview title, and historical timestamps are displayed in the machine's **local timezone**. Platforms return UTC timestamps; stored values (`reset_at`, snapshot `ts`) and `--json` output keep the original UTC ISO 8601 format. Changing the machine timezone changes the display.

The `Reset countdown` column shows the time until the next reset (e.g. `2d 3h`, `5h 20m`, `32m`), computed from the **current system clock** on every render — refreshed every second in `llm-usage tui`. An elapsed reset shows `Reset`; no reset time shows `-`. Timestamps are semantically distinguished: a plan expiry (Sub2API's Monthly row) shows `Expired` once passed, and the Web hover tooltip says "expiry time". The column is right-aligned with the progress-bar percentage label.

The Web dashboard behaves the same way: the server API returns UTC ISO timestamps, the browser converts them to local time, and the frontend recomputes the reset countdown every second.

## License

MIT — see [LICENSE](LICENSE).
