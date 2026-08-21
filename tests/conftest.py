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
                "OLLAMA_API_KEY", "OPENCODE_GO_API_KEY", "LLM_GATEWAY_API_KEY"):
        monkeypatch.delenv(var, raising=False)