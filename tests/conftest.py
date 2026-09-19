"""Shared pytest fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture
def tmp_config_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect config file to a temp path for the duration of the test."""
    p = tmp_path / "config.toml"
    monkeypatch.setenv("LLM_USAGE_CONFIG", str(p))
    return p


@pytest.fixture
def tmp_db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect history DB to a temp path for the duration of the test."""
    p = tmp_path / "history.db"
    monkeypatch.setenv("LLM_USAGE_DB", str(p))
    return p


@pytest.fixture(autouse=True)
def _clear_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure no stray env API keys leak into provider tests."""
    for var in ("KIMI_API_KEY", "VOLCENGINE_API_KEY",
                "VOLCENGINE_ACCESS_KEY", "VOLCENGINE_SECRET_KEY",
                "OLLAMA_API_KEY", "OPENCODE_GO_API_KEY", "LLM_GATEWAY_API_KEY",
                "CLINEPASS_API_KEY", "COMMANDCODE_API_KEY"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _fast_pbkdf2(monkeypatch: pytest.MonkeyPatch) -> None:
    """测试用低迭代数:hash_password 读模块全局,
    verify_password 从存储串解析迭代数 → 双路径均生效。"""
    monkeypatch.setattr("llm_usage.store.PBKDF2_ITERATIONS", 1_000)


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """消除重试退避的真实等待;返回记录到的退避秒数供测试断言。

    patch 模块级别名 ``llm_usage.providers._sleep``(from time import sleep as _sleep,
    而非 patch 全局 time.sleep),避免污染其他模块的 time 行为。
    """
    slept: list[float] = []
    monkeypatch.setattr("llm_usage.providers._sleep", slept.append)
    return slept
