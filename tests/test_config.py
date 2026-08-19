"""Config module tests: init, load, save, manual entry updates, env: keys."""

from __future__ import annotations

from pathlib import Path

import pytest

from llm_usage import config as C


class TestConfigInit:
    def test_init_creates_file(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        C.init_config(p)
        assert p.exists()
        text = p.read_text(encoding="utf-8")
        assert "[platforms.kimi]" in text
        assert "[platforms.ollama]" in text
        assert "[platforms.opencode-go]" in text

    def test_init_refuses_overwrite_without_flag(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        C.init_config(p)
        with pytest.raises(FileExistsError):
            C.init_config(p)

    def test_init_overwrite_with_flag(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        C.init_config(p)
        C.init_config(p, overwrite=True)
        assert p.exists()


class TestConfigLoadSave:
    def test_load_missing_returns_empty(self, tmp_path: Path) -> None:
        assert C.load_config(tmp_path / "nonexistent.toml") == {}

    def test_roundtrip(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        C.init_config(p)
        cfg = C.load_config(p)
        assert "kimi" in cfg["platforms"]
        C.save_config(cfg, p)
        cfg2 = C.load_config(p)
        assert cfg2 == cfg



class TestEnvPrefix:
    def test_env_prefix_stored_verbatim(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        C.init_config(p)
        cfg = C.load_config(p)
        assert cfg["platforms"]["kimi"]["api_key"] == "env:KIMI_API_KEY"