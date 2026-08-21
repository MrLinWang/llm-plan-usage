"""Display and store tests: rendering, JSON output, snapshot persistence."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from rich.console import Console

from llm_usage.display import (
    _fmt_countdown,
    _to_local_time,
    render_history,
    render_results,
    results_to_json,
)
from llm_usage.models import PlatformResult, UsageEntry
from llm_usage.store import get_setting, query_history, save_snapshot, set_setting


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


class _FixedNow(datetime):
    """datetime stand-in pinned to a fixed UTC "now" for deterministic countdowns."""

    _fixed = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # noqa: ANN001 — mirror datetime.now() signature
        return cls._fixed


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
        assert "67%" in out  # percent label, rounded to integer

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

    def test_countdown_column_rendered(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # reset ~1.5h after a fixed "now" -> countdown shows 小时/分;
        # fixed now keeps the assertion deterministic
        monkeypatch.setattr("llm_usage.display.datetime", _FixedNow)
        future = _FixedNow._fixed + timedelta(hours=1, minutes=30)
        e = UsageEntry("kimi", "5小时", 80, 120, 40, 66.7,
                       future.isoformat(), "%", False)
        r = PlatformResult("kimi", "Kimi Code", entries=[e])
        console, buf = _make_console()
        render_results([r], console=console)
        out = buf.getvalue()
        assert "重置倒计时" in out
        assert "1小时" in out and "分" in out

    def test_fmt_countdown(self) -> None:
        now = datetime(2026, 8, 19, 12, 0, 0, tzinfo=timezone.utc)
        assert _fmt_countdown("2026-08-21T15:00:00Z", now) == "2天3小时"
        assert _fmt_countdown("2026-08-19T17:20:00Z", now) == "5小时20分"
        assert _fmt_countdown("2026-08-19T12:32:00Z", now) == "32分"
        # past / missing / unparsable
        assert _fmt_countdown("2026-08-19T11:59:00Z", now) == "已重置"
        assert _fmt_countdown(None, now) == "-"
        assert _fmt_countdown("", now) == "-"
        assert _fmt_countdown("garbage", now) == "-"

    def test_countdown_right_aligned(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # the countdown column is right-aligned: values of different widths
        # must share the same right edge
        from llm_usage.display import COLS, COL_GAP

        pos = 0
        starts: dict[str, int] = {}
        for key, _, width, _ in COLS:
            starts[key] = pos
            pos += width + COL_GAP
        cd_start = starts["countdown"]
        cd_width = next(w for k, _, w, _ in COLS if k == "countdown")
        cd_end = cd_start + cd_width

        monkeypatch.setattr("llm_usage.display.datetime", _FixedNow)
        fixed = _FixedNow._fixed
        e1 = UsageEntry("kimi", "5小时", 80, 120, 40, 66.7,
                        (fixed + timedelta(minutes=32)).isoformat(), "%", False)
        e2 = UsageEntry("kimi", "每周", 10, 100, 90, 10.0,
                        (fixed + timedelta(days=2, hours=3)).isoformat(), "%", False)
        r = PlatformResult("kimi", "Kimi Code", entries=[e1, e2])
        console, buf = _make_console()
        render_results([r], console=console)
        lines = buf.getvalue().splitlines()

        def disp_width(s: str) -> int:
            return sum(2 if ord(c) > 0x2E80 else 1 for c in s)

        # both countdown values ("32分" / "2天2小时") end at the column's
        # right edge, i.e. right-aligned with no trailing padding
        for ln in lines:
            cell = ln[cd_start:cd_end]
            if cell.strip() not in ("32分", "2天2小时"):
                continue
            assert disp_width(cell.rstrip()) == cd_end - cd_start


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

    def test_settings_roundtrip(self, tmp_db_path: Path) -> None:
        assert get_setting("registration_enabled") is None
        set_setting("registration_enabled", "1")
        assert get_setting("registration_enabled") == "1"
        set_setting("registration_enabled", "0")
        assert get_setting("registration_enabled") == "0"