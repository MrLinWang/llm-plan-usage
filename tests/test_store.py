"""Store module tests: atomic first-admin creation, file permissions."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from llm_usage import store


class TestCreateFirstAdmin:
    def test_create_first_admin_succeeds_once(self, tmp_db_path: Path) -> None:
        assert store.create_first_admin("admin", "secret1") is True
        assert store.get_user("admin")["is_admin"] is True
        # 已有用户 → False,不再创建
        assert store.create_first_admin("other", "secret2") is False
        assert store.get_user("other") is None
        assert store.count_users() == 1

    def test_create_first_admin_concurrent(self, tmp_db_path: Path) -> None:
        """并发首管理员竞态:恰好一个请求胜出,库里只有一个管理员。"""
        with ThreadPoolExecutor(2) as pool:
            futures = [
                pool.submit(store.create_first_admin, "admin", "secret1"),
                pool.submit(store.create_first_admin, "hacker", "secret2"),
            ]
            results = [f.result() for f in futures]
        assert sorted(results, reverse=True) == [True, False]
        assert store.count_users() == 1
        assert store.count_admins() == 1
        admins = [u["username"] for u in store.list_users() if u["is_admin"]]
        assert len(admins) == 1
        assert admins[0] in ("admin", "hacker")


class TestDbPermissions:
    def test_db_created_0600(self, tmp_db_path: Path) -> None:
        store.count_users()
        mode = tmp_db_path.stat().st_mode & 0o777
        assert mode == 0o600


class TestSchemaInit:
    def test_db_indexes_created(self, tmp_db_path: Path) -> None:
        conn = store._connect(tmp_db_path)
        try:
            names = {
                row[1] for row in conn.execute("PRAGMA index_list(snapshots)")
            }
        finally:
            conn.close()
        assert {"idx_snapshots_ts", "idx_snapshots_platform"} <= names

    def test_wal_journal_mode(self, tmp_db_path: Path) -> None:
        conn = store._connect(tmp_db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        finally:
            conn.close()
        assert str(mode).lower() == "wal"

    def test_schema_init_once(
        self, tmp_db_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """同路径二次 _connect 不再执行 DDL(否则无 IF NOT EXISTS 的语句重复建表报错)。"""
        monkeypatch.setattr(
            store,
            "_SCHEMA",
            store._SCHEMA + "\nCREATE TABLE init_probe (x INTEGER);\n",
        )
        conn1 = store._connect(tmp_db_path)
        conn1.close()
        conn2 = store._connect(tmp_db_path)
        conn2.close()


class TestQueryHistoryFilters:
    @staticmethod
    def _insert(conn: sqlite3.Connection, ts: str, platform: str) -> None:
        conn.execute(
            'INSERT INTO snapshots (ts, platform, label, used, "limit", remaining,'
            " percent, reset_at, unit, is_manual, plan)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ts, platform, "标签", 1.0, 2.0, 1.0, 50.0, None, "%", 0, None),
        )

    def test_days_window_filters_old_rows(self, tmp_db_path: Path) -> None:
        now = datetime.now(UTC)
        recent = (now - timedelta(hours=2)).isoformat()
        current = now.isoformat()
        conn = store._connect(tmp_db_path)
        try:
            self._insert(conn, "2020-01-01T00:00:00+00:00", "kimi")
            self._insert(conn, recent, "kimi")
            self._insert(conn, current, "ollama")
            conn.commit()
        finally:
            conn.close()
        rows = store.query_history(days=1, path=tmp_db_path)
        assert [r["ts"] for r in rows] == [recent, current]  # 窗口外旧行被过滤
        rows = store.query_history(days=1, platform="kimi", path=tmp_db_path)
        assert [r["ts"] for r in rows] == [recent]  # days 与 platform 过滤可叠加
        rows = store.query_history(platform="kimi", path=tmp_db_path)
        assert len(rows) == 2  # 不带 days 时旧行仍在
