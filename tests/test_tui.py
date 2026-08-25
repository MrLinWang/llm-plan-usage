"""TUI input tests: stdin EOF sentinel vs poll timeout."""

from __future__ import annotations

import os
from typing import Any

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
