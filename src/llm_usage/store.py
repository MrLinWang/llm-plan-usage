"""SQLite snapshot storage for usage history.

Each successful ``llm-usage show`` writes one row per ``UsageEntry``.
``llm-usage history`` reads them back for trend display.
"""

from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timezone
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
  is_manual INTEGER DEFAULT 0
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
    return conn


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def save_snapshot(
    results: list[PlatformResult],
    path: Path | None = None,
) -> int:
    """Persist all entries from successful platforms. Returns row count."""
    conn = _connect(path)
    ts = _now_iso()
    rows = 0
    try:
        for res in results:
            if not res.ok or not res.entries:
                continue
            for e in res.entries:
                conn.execute(
                    "INSERT INTO snapshots "
                    "(ts, platform, label, used, \"limit\", remaining, percent, "
                    "reset_at, unit, is_manual) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?)",
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
              "reset_at, unit, is_manual FROM snapshots"
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