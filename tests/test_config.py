"""Config module tests: init, load, save, platform config merge, env: keys, 0600 perms."""

from __future__ import annotations

from pathlib import Path
from typing import Any

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
        assert "[platforms.llm-gateway]" in text
        # 多计费套餐注释(注释行,不进 TOML 本体)
        assert "credentials" in text

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

    def test_init_config_creates_0600(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        C.init_config(p)
        assert p.stat().st_mode & 0o777 == 0o600


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

    def test_failed_serialization_preserves_existing_file(self, tmp_path: Path) -> None:
        """序列化失败(不支持的值)不得触碰目标文件:旧内容逐字节保留。"""
        p = tmp_path / "config.toml"
        C.init_config(p)
        baseline = p.read_bytes()
        with pytest.raises(TypeError):
            C.save_config({"ok": 1, "bad": object()}, p)
        assert p.read_bytes() == baseline

    def test_mutate_config_serializes_concurrent_updates(self, tmp_path: Path) -> None:
        """读-改-写临界区串行:并发回调不丢失更新,两个键都落盘。"""
        import threading
        from concurrent.futures import ThreadPoolExecutor

        p = tmp_path / "config.toml"
        C.init_config(p)
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()

        def first(cfg: dict[str, Any]) -> None:
            entered.set()
            assert release.wait(timeout=5)
            cfg["first"] = 1

        def second(cfg: dict[str, Any]) -> None:
            second_entered.set()
            cfg["second"] = 2

        with ThreadPoolExecutor(max_workers=2) as pool:
            f1 = pool.submit(C.mutate_config, first, p)
            assert entered.wait(timeout=5)
            f2 = pool.submit(C.mutate_config, second, p)
            # 第一个回调持锁期间,第二个回调不得进入临界区
            assert not second_entered.wait(timeout=0.2)
            release.set()
            f1.result(timeout=5)
            f2.result(timeout=5)
        cfg = C.load_config(p)
        assert cfg["first"] == 1
        assert cfg["second"] == 2



class TestEnvPrefix:
    def test_env_prefix_stored_verbatim(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        C.init_config(p)
        cfg = C.load_config(p)
        assert cfg["platforms"]["kimi"]["api_key"] == "env:KIMI_API_KEY"


class TestPlatformOrder:
    def test_set_and_get_platform_order(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        C.init_config(p)
        C.set_platform_order(["ollama", "kimi"], p)
        assert C.load_config(p)["platform_order"] == ["ollama", "kimi"]
        # 非字符串元素被过滤;非列表类型/缺失返回空
        assert C.get_platform_order({"platform_order": ["kimi", 1, "ollama"]}) == [
            "kimi",
            "ollama",
        ]
        assert C.get_platform_order({"platform_order": "kimi"}) == []
        assert C.get_platform_order({}) == []