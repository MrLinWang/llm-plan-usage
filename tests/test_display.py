"""Display and store tests: rendering, JSON output, snapshot persistence."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
from rich.console import Console

from llm_usage.display import (
    _to_local_time,
    render_history,
    render_results,
    results_to_json,
)
from llm_usage.models import PlatformResult, UsageEntry
from llm_usage.store import query_history, save_snapshot


def _make_console(width: int = 120) -> tuple[Console, "io.StringIO"]:
    import io

    buf = io.StringIO()
    return Console(file=buf, width=width, force_terminal=False, color_system=None), buf


def _local(s: str) -> str:
    """Reference conversion: ISO input (Z/offset) -> local time, as displayed."""
    return (
        datetime.fromisoformat(s.replace("Z", "+00:00"))
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


class TestDisplay:
    def test_render_results_contains_platform_names(self) -> None:
        e = UsageEntry("kimi", "5小时", 80, 120, 40, 66.7, None, "%", False)
        r = PlatformResult("kimi", "Kimi Code", entries=[e])
        console, buf = _make_console()
        render_results([r], console=console)
        out = buf.getvalue()
        assert "Kimi Code" in out
        assert "5小时" in out

    def test_render_error_row(self) -> None:
        r = PlatformResult("kimi", "Kimi Code", error="未配置")
        console, buf = _make_console()
        render_results([r], console=console)
        assert "未配置" in buf.getvalue()

    def test_render_progress_bar(self) -> None:
        e = UsageEntry("kimi", "5小时", 80, 120, 40, 66.7, None, "%", False)
        r = PlatformResult("kimi", "Kimi Code", entries=[e])
        console, buf = _make_console()
        render_results([r], console=console)
        out = buf.getvalue()
        assert "OK" in out
        assert "█" in out  # progress bar character
        assert "░" in out  # empty bar character
        assert "66.7%" in out

    def test_results_to_json_structure(self) -> None:
        e = UsageEntry("kimi", "5小时", 80, 120, 40, 66.7, None, "%", False)
        r = PlatformResult("kimi", "Kimi Code", entries=[e])
        r_err = PlatformResult("x", "X", error="bad")
        data = json.loads(results_to_json([r, r_err]))
        assert "platforms" in data
        assert len(data["platforms"]) == 2
        assert data["platforms"][0]["name"] == "kimi"
        assert data["platforms"][0]["entries"][0]["used"] == 80
        assert data["platforms"][1]["error"] == "bad"

    def test_render_history_empty(self) -> None:
        console, buf = _make_console()
        render_history([], console=console)
        assert "无历史记录" in buf.getvalue()

    def test_render_history_with_rows(self) -> None:
        rows = [{"ts": "2026-08-18T09:00:00+00:00", "platform": "kimi", "label": "5小时",
                 "used": 80, "limit": 120, "remaining": 40, "percent": 66.7,
                 "reset_at": None, "unit": "%", "is_manual": 0}]
        console, buf = _make_console()
        render_history(rows, console=console)
        assert "kimi" in buf.getvalue()
        assert "LLM 用量历史" in buf.getvalue()
        # snapshot ts is converted to local time (UTC+00:00 input)
        assert _local("2026-08-18T09:00:00+00:00") in buf.getvalue()

    def test_reset_time_rendered_in_local_time(self) -> None:
        # reset_at is stored in UTC by providers; the table must show local time
        e = UsageEntry("kimi", "每周", 80, 120, 40, 66.7, "2026-08-19T16:48:27.482983Z", "%", False)
        r = PlatformResult("kimi", "Kimi Code", entries=[e])
        console, buf = _make_console()
        render_results([r], console=console)
        assert _local("2026-08-19T16:48:27.482983Z") in buf.getvalue()
        # raw UTC ISO form (with 'T') must not leak into the render
        assert "2026-08-19T16:48:27" not in buf.getvalue()

    def test_to_local_time_conversion(self) -> None:
        local = datetime.now().astimezone().utcoffset() or timezone.utc
        # explicit Z suffix
        assert _to_local_time("2026-08-19T06:21:37.000000Z") == _local("2026-08-19T06:21:37.000000Z")
        # explicit +00:00 offset
        assert _to_local_time("2026-08-25T00:00:00+00:00") == _local("2026-08-25T00:00:00+00:00")
        # naive treated as UTC
        assert _to_local_time("2026-08-25T00:00:00") == _local("2026-08-25T00:00:00+00:00")
        # empty / None passthrough
        assert _to_local_time("") is None
        assert _to_local_time(None) is None
        # unparsable returns input unchanged
        assert _to_local_time("garbage") == "garbage"


class TestStore:
    def test_save_and_query(self, tmp_db_path: Path) -> None:
        e = UsageEntry("kimi", "5小时", 80, 120, 40, 66.7, None, "%", False)
        r = PlatformResult("kimi", "Kimi Code", entries=[e])
        n = save_snapshot([r])
        assert n == 1
        rows = query_history()
        assert len(rows) == 1
        assert rows[0]["platform"] == "kimi"
        assert rows[0]["used"] == 80.0

    def test_failed_platform_not_saved(self, tmp_db_path: Path) -> None:
        r = PlatformResult("kimi", "Kimi Code", error="bad")
        assert save_snapshot([r]) == 0
        assert query_history() == []

    def test_query_filter_by_platform(self, tmp_db_path: Path) -> None:
        e1 = UsageEntry("kimi", "5小时", 10, 100, 90, 10.0, None, "%", False)
        e2 = UsageEntry("ollama", "5小时", 20, 100, 80, 20.0, None, "次", True)
        save_snapshot([PlatformResult("kimi", "K", entries=[e1]),
                       PlatformResult("ollama", "O", entries=[e2])])
        kimi_rows = query_history(platform="kimi")
        assert len(kimi_rows) == 1
        assert kimi_rows[0]["platform"] == "kimi"

    def test_two_snapshots_yield_two_rows(self, tmp_db_path: Path) -> None:
        e1 = UsageEntry("kimi", "5小时", 50, 100, 50, 50.0, None, "%", False)
        save_snapshot([PlatformResult("kimi", "Kimi", entries=[e1])])
        e2 = UsageEntry("kimi", "5小时", 60, 100, 40, 60.0, None, "%", False)
        save_snapshot([PlatformResult("kimi", "Kimi", entries=[e2])])
        rows = query_history()
        assert len(rows) == 2

    def test_manual_flag_persisted(self, tmp_db_path: Path) -> None:
        e = UsageEntry("ollama", "5小时", 60, 100, 40, 60.0, None, "次", True)
        save_snapshot([PlatformResult("ollama", "O", entries=[e])])
        assert query_history()[0]["is_manual"] == 1