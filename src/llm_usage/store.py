"""SQLite storage: usage history snapshots + Web 端用户/会话.

Each successful ``llm-usage show`` writes one row per ``UsageEntry``.
``llm-usage history`` reads them back for trend display.  The ``users`` /
``sessions`` tables back the Web dashboard's login (pbkdf2 password hashes,
7-day session tokens); the ``settings`` KV table holds site policy toggles
(e.g. ``registration_enabled``); all live in the same ``history.db``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from llm_usage.models import PlatformResult, UsageEntry

DB_PATH = Path.cwd() / "history.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS snapshots (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  platform TEXT NOT NULL,
  label TEXT NOT NULL,
  used REAL,
  "limit" REAL,
  remaining REAL,
  percent REAL,
  reset_at TEXT,
  unit TEXT,
  is_manual INTEGER DEFAULT 0,
  plan TEXT
);
CREATE TABLE IF NOT EXISTS users (
  username TEXT PRIMARY KEY,
  password_hash TEXT NOT NULL,
  is_admin INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
  token TEXT PRIMARY KEY,
  username TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_configs (
  username TEXT PRIMARY KEY,
  config TEXT NOT NULL,          -- JSON: {"platforms": {key: section}}
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS user_shares (
  owner TEXT NOT NULL, platform TEXT NOT NULL, target TEXT NOT NULL,
  PRIMARY KEY (owner, platform, target)
);
"""


def db_path() -> Path:
    env = os.environ.get("LLM_USAGE_DB")
    return Path(env) if env else DB_PATH




def _connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(p))
    conn.executescript(_SCHEMA)
    # 平滑迁移既有库:snapshots 缺 plan 列则补上(SQLite 无 IF NOT EXISTS 的 ADD COLUMN)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(snapshots)")}
    if "plan" not in cols:
        conn.execute("ALTER TABLE snapshots ADD COLUMN plan TEXT")
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_snapshot(
    results: list[PlatformResult],
    path: Path | None = None,
) -> int:
    """Persist entries from complete successful platforms. Returns row count.

    A warning means a grouped provider returned only a partial aggregate, so it
    is deliberately not written as a normal historical snapshot.
    """
    conn = _connect(path)
    ts = _now_iso()
    rows = 0
    try:
        for res in results:
            if not res.ok or res.warning or not res.entries:
                continue
            for e in res.entries:
                conn.execute(
                    "INSERT INTO snapshots "
                    "(ts, platform, label, used, \"limit\", remaining, percent, "
                    "reset_at, unit, is_manual, plan) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        ts,
                        res.platform,
                        e.label,
                        e.used,
                        e.limit,
                        e.remaining,
                        e.percent,
                        e.reset_at,
                        e.unit,
                        1 if e.is_manual else 0,
                        e.plan,
                    ),
                )
                rows += 1
        conn.commit()
    finally:
        conn.close()
    return rows


def query_history(
    platform: str | None = None,
    days: int | None = None,
    path: Path | None = None,
) -> list[dict[str, Any]]:
    """Read snapshots, optionally filtered by platform and a lookback window.

    Rows are dicts keyed by the snapshot columns, newest first.
    """
    conn = _connect(path)
    try:
        sql = "SELECT ts, platform, label, used, \"limit\", remaining, percent, " \
              "reset_at, unit, is_manual, plan FROM snapshots"
        clauses: list[str] = []
        params: list[Any] = []
        if platform:
            clauses.append("platform = ?")
            params.append(platform)
        if days is not None:
            import datetime as _dt

            cutoff = (datetime.now(timezone.utc) - _dt.timedelta(days=days)).isoformat()
            clauses.append("ts >= ?")
            params.append(cutoff)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY ts ASC"
        cur = conn.execute(sql, params)
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]
    finally:
        conn.close()


# ---- 用户与会话(Web 端认证) ----

PBKDF2_ITERATIONS = 600_000
SESSION_TTL_DAYS = 7


def hash_password(password: str) -> str:
    """Hash a password as ``pbkdf2_sha256$iterations$salt_hex$dk_hex``."""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac(
        "sha256", password.encode(), salt, PBKDF2_ITERATIONS
    )
    return f"pbkdf2_sha256${PBKDF2_ITERATIONS}${salt.hex()}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Constant-time check of ``password`` against a stored hash."""
    try:
        scheme, iterations, salt_hex, dk_hex = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode(), bytes.fromhex(salt_hex), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), dk_hex)
    except (ValueError, TypeError):
        return False


def _user_dict(row: tuple[Any, ...]) -> dict[str, Any]:
    return {"username": row[0], "is_admin": bool(row[1]), "created_at": row[2]}


def count_users(path: Path | None = None) -> int:
    conn = _connect(path)
    try:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    finally:
        conn.close()


def count_admins(path: Path | None = None) -> int:
    conn = _connect(path)
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM users WHERE is_admin = 1"
        ).fetchone()[0]
    finally:
        conn.close()


def create_user(
    username: str,
    password: str,
    is_admin: bool = False,
    path: Path | None = None,
) -> None:
    """Create a user. Raises ``ValueError`` if the username already exists."""
    conn = _connect(path)
    try:
        try:
            conn.execute(
                "INSERT INTO users (username, password_hash, is_admin, created_at) "
                "VALUES (?,?,?,?)",
                (username, hash_password(password), 1 if is_admin else 0, _now_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            raise ValueError("用户名已存在") from None
    finally:
        conn.close()


def get_user(username: str, path: Path | None = None) -> dict[str, Any] | None:
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT username, is_admin, created_at FROM users WHERE username = ?",
            (username,),
        ).fetchone()
        return _user_dict(row) if row else None
    finally:
        conn.close()


def verify_user(
    username: str, password: str, path: Path | None = None
) -> dict[str, Any] | None:
    """Return the user dict on correct credentials, else None."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT username, is_admin, created_at, password_hash "
            "FROM users WHERE username = ?",
            (username,),
        ).fetchone()
    finally:
        conn.close()
    if not row or not verify_password(password, row[3]):
        return None
    return _user_dict(row)


def list_users(path: Path | None = None) -> list[dict[str, Any]]:
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT username, is_admin, created_at FROM users "
            "ORDER BY created_at, username"
        ).fetchall()
        return [_user_dict(r) for r in rows]
    finally:
        conn.close()


def set_user_password(
    username: str, password: str, path: Path | None = None
) -> bool:
    """Reset a user's password. Returns False if the user does not exist."""
    conn = _connect(path)
    try:
        cur = conn.execute(
            "UPDATE users SET password_hash = ? WHERE username = ?",
            (hash_password(password), username),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_user(username: str, path: Path | None = None) -> bool:
    """Delete a user, their sessions, config and shares. Returns False if absent."""
    conn = _connect(path)
    try:
        conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
        conn.execute("DELETE FROM user_configs WHERE username = ?", (username,))
        # 用户既是 owner 也是 target:两方向的行都删
        conn.execute(
            "DELETE FROM user_shares WHERE owner = ? OR target = ?",
            (username, username),
        )
        cur = conn.execute("DELETE FROM users WHERE username = ?", (username,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def create_session(username: str, path: Path | None = None) -> str:
    """Create a session token for ``username`` (lazy-expiring old rows)."""
    conn = _connect(path)
    try:
        now = datetime.now(timezone.utc)
        conn.execute(
            "DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),)
        )
        token = secrets.token_urlsafe(32)
        expires = (now + timedelta(days=SESSION_TTL_DAYS)).isoformat()
        conn.execute(
            "INSERT INTO sessions (token, username, expires_at) VALUES (?,?,?)",
            (token, username, expires),
        )
        conn.commit()
        return token
    finally:
        conn.close()


def get_session_user(
    token: str, path: Path | None = None
) -> dict[str, Any] | None:
    """Return the session's user dict, or None if unknown/expired."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT s.expires_at, u.username, u.is_admin, u.created_at "
            "FROM sessions s JOIN users u ON u.username = s.username "
            "WHERE s.token = ?",
            (token,),
        ).fetchone()
        if not row:
            return None
        if row[0] < datetime.now(timezone.utc).isoformat():
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            conn.commit()
            return None
        return _user_dict(row[1:])
    finally:
        conn.close()


def delete_session(token: str, path: Path | None = None) -> None:
    conn = _connect(path)
    try:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
    finally:
        conn.close()


def delete_user_sessions(
    username: str,
    keep_token: str | None = None,
    path: Path | None = None,
) -> None:
    """Drop all sessions for ``username``, optionally keeping one token."""
    conn = _connect(path)
    try:
        if keep_token is None:
            conn.execute("DELETE FROM sessions WHERE username = ?", (username,))
        else:
            conn.execute(
                "DELETE FROM sessions WHERE username = ? AND token != ?",
                (username, keep_token),
            )
        conn.commit()
    finally:
        conn.close()


# ---- 站点设置(KV) ----


def get_setting(key: str, path: Path | None = None) -> str | None:
    """Return the setting value, or None if unset."""
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT value FROM settings WHERE key = ?", (key,)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def set_setting(key: str, value: str, path: Path | None = None) -> None:
    """INSERT OR REPLACE a setting row."""
    conn = _connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?,?)",
            (key, value),
        )
        conn.commit()
    finally:
        conn.close()


# ---- 用户平台配置与可见性(多用户隔离) ----

def get_user_config(
    username: str, path: Path | None = None
) -> dict[str, Any]:
    """Return a user's platform config (``{"platforms": {key: section}}``).

    Missing row or corrupt JSON → ``{}`` (never crashes).
    """
    conn = _connect(path)
    try:
        row = conn.execute(
            "SELECT config FROM user_configs WHERE username = ?", (username,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {}
    try:
        data = json.loads(row[0])
    except (ValueError, TypeError):
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def set_user_config(
    username: str, config: dict[str, Any], path: Path | None = None
) -> None:
    """INSERT OR REPLACE a user's own platform config."""
    conn = _connect(path)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO user_configs (username, config, updated_at) "
            "VALUES (?,?,?)",
            (username, json.dumps(config, ensure_ascii=False), _now_iso()),
        )
        conn.commit()
    finally:
        conn.close()


def delete_user_config(username: str, path: Path | None = None) -> None:
    """Drop a user's stored platform config."""
    conn = _connect(path)
    try:
        conn.execute(
            "DELETE FROM user_configs WHERE username = ?", (username,)
        )
        conn.commit()
    finally:
        conn.close()


def list_shared_platforms(
    username: str, path: Path | None = None
) -> list[dict[str, str]]:
    """Platforms shared to ``username`` (directly or publicly), owner-sorted.

    Joins ``users`` so a deleted owner's stale rows are invisible.
    """
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT s.owner, s.platform FROM user_shares s "
            "JOIN users u ON u.username = s.owner "
            "WHERE s.target = ? OR s.target = '*' "
            "ORDER BY s.owner",
            (username,),
        ).fetchall()
        return [{"owner": r[0], "platform": r[1]} for r in rows]
    finally:
        conn.close()


def list_my_visibility(
    username: str, path: Path | None = None
) -> list[dict[str, str]]:
    """A user's own share rows: ``[{"platform", "target"}]``."""
    conn = _connect(path)
    try:
        rows = conn.execute(
            "SELECT platform, target FROM user_shares WHERE owner = ? "
            "ORDER BY platform, target",
            (username,),
        ).fetchall()
        return [{"platform": r[0], "target": r[1]} for r in rows]
    finally:
        conn.close()


def set_platform_visibility(
    username: str, platform: str, target: str, path: Path | None = None
) -> None:
    """Replace one platform's visibility rows; empty ``target`` = clear only."""
    conn = _connect(path)
    try:
        conn.execute(
            "DELETE FROM user_shares WHERE owner = ? AND platform = ?",
            (username, platform),
        )
        if target:
            conn.execute(
                "INSERT INTO user_shares (owner, platform, target) VALUES (?,?,?)",
                (username, platform, target),
            )
        conn.commit()
    finally:
        conn.close()


def replace_platform_visibility(
    username: str, platform: str, targets: list[str], path: Path | None = None
) -> None:
    """Replace one platform's visibility rows with a target list (atomically).

    Targets are the deduped normalized user list from the visibility API:
    ``["*"]`` = public, ``[]`` = private, otherwise per-user targets.
    """
    conn = _connect(path)
    try:
        conn.execute(
            "DELETE FROM user_shares WHERE owner = ? AND platform = ?",
            (username, platform),
        )
        for target in targets:
            conn.execute(
                "INSERT INTO user_shares (owner, platform, target) VALUES (?,?,?)",
                (username, platform, target),
            )
        conn.commit()
    finally:
        conn.close()