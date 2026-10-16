"""TUI tests: stdin EOF sentinel vs poll timeout, fetch schedule."""

from __future__ import annotations

import os
from typing import Any

import pytest

from llm_usage import tui


class _FakeStdin:
    """stdin 替身:fileno()/read() 走给定 fd,不接管其余属性。"""

    def __init__(self, fd: int) -> None:
        self._fd = fd

    def fileno(self) -> int:
        return self._fd

    def read(self, n: int = 1) -> str:
        return os.read(self._fd, n).decode()


def _pipe_stdin(monkeypatch: Any) -> tuple[int, int]:
    r, w = os.pipe()
    monkeypatch.setattr(tui.sys, "stdin", _FakeStdin(r))
    return r, w


def test_read_key_eof_sentinel(monkeypatch: Any) -> None:
    """stdin EOF(写端关闭):select 恒就绪、read 返回 '' → 哨兵空串(非 None)。"""
    r, w = _pipe_stdin(monkeypatch)
    os.close(w)
    try:
        assert tui._read_key(0.01) == ""
    finally:
        os.close(r)


def test_read_key_timeout_none(monkeypatch: Any) -> None:
    """写端保持打开且无数据 → 超时返回 None(tick 等待路径不变)。"""
    r, w = _pipe_stdin(monkeypatch)
    try:
        assert tui._read_key(0.05) is None
    finally:
        os.close(r)
        os.close(w)


def _run_tui(monkeypatch: Any, keys: list[str | None], *, now: float = 30.0) -> int:
    """按给定按键序列运行 run_tui(interval=60),返回 fetch_all 调用次数。

    ``now`` 是固定的 monotonic 读数(≈ 主机开机时长),默认 30s < interval,
    即「主机刚开机」:旧实现用 0.0 当哨兵时既不会启动拉取,按 r 也无效。
    """
    r, w = _pipe_stdin(monkeypatch)  # 非 TTY stdin:_cbreak 直接跳过
    calls = [0]

    def fake_fetch(_cfg: Any) -> list:
        calls[0] += 1
        return []

    pending = iter(keys)
    monkeypatch.setattr(tui, "fetch_all", fake_fetch)
    monkeypatch.setattr(tui, "_monotonic", lambda: now)
    monkeypatch.setattr(tui, "_read_key", lambda _timeout: next(pending))
    try:
        tui.run_tui({"platforms": {}}, interval=60)
    finally:
        os.close(r)
        os.close(w)
    return calls[0]


@pytest.mark.parametrize("now", [30.0, 4_000_000.0])
@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ([None, "q"], 1),  # 启动即拉取
        ([None, None, "q"], 1),  # 间隔内不重复拉取
        ([None, "r", None, "q"], 2),  # 按 r 立即重新拉取
    ],
)
def test_run_tui_fetch_schedule_regardless_of_monotonic_origin(
    monkeypatch: Any, keys: list[str | None], expected: int, now: float
) -> None:
    """启动与按 r 都立即拉取,与 monotonic 零点(≈ 主机开机时长)无关。"""
    assert _run_tui(monkeypatch, keys, now=now) == expected
