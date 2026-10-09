"""运行日志(logging_setup)测试:文件落盘、幂等、uvicorn 合并、降级路径。"""

from __future__ import annotations

import io
import logging
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from llm_usage import logging_setup
from llm_usage.cli import main


@pytest.fixture
def log_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """隔离日志路径,并保存/恢复全局 logging 状态(含 uvicorn/httpx logger)。"""
    path = tmp_path / "llm-usage.log"
    monkeypatch.setenv("LLM_USAGE_LOG", str(path))
    root = logging.getLogger()
    saved_root = (root.handlers[:], root.level)
    saved = {}
    for name in (*logging_setup._UVICORN_LOGGERS, logging_setup._HTTPX_LOGGER):
        lg = logging.getLogger(name)
        saved[name] = (lg.handlers[:], lg.propagate, lg.level)
    yield path
    for handler in root.handlers:
        if getattr(handler, "_llm_usage_handler", False):
            handler.close()
    root.handlers = saved_root[0]
    root.setLevel(saved_root[1])
    for name, (handlers, propagate, level) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers = handlers
        lg.propagate = propagate
        lg.setLevel(level)


def _our_handlers() -> list[logging.Handler]:
    return [h for h in logging.getLogger().handlers if getattr(h, "_llm_usage_handler", False)]


def _flush() -> None:
    for handler in _our_handlers():
        handler.flush()


class TestLogPath:
    def test_env_override(self, log_env: Path) -> None:
        assert logging_setup.log_path() == log_env

    def test_default_in_log_dir_beside_config(
        self, monkeypatch: pytest.MonkeyPatch, tmp_config_path: Path
    ) -> None:
        monkeypatch.delenv("LLM_USAGE_LOG", raising=False)
        assert logging_setup.log_path() == tmp_config_path.parent / "log" / "llm-usage.log"

    def test_default_follows_llm_usage_config_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Docker 场景:LLM_USAGE_CONFIG=/data/config.toml ⇒ 日志落 /data/log。"""
        monkeypatch.delenv("LLM_USAGE_LOG", raising=False)
        monkeypatch.setenv("LLM_USAGE_CONFIG", str(tmp_path / "data" / "config.toml"))
        assert logging_setup.log_path() == tmp_path / "data" / "log" / "llm-usage.log"


class TestSetupCliLogging:
    def test_httpx_pinned_to_warning(self, log_env: Path) -> None:
        """httpx 每次成功请求打 INFO,必须压到 WARNING,否则刷屏并写满文件。"""
        logging_setup.setup_cli_logging()
        assert logging.getLogger("httpx").level == logging.WARNING
        logging.getLogger("httpx").info('HTTP Request: GET http://x/v1/usage "HTTP/1.0 200 OK"')
        logging.getLogger("httpx").warning("HTTP Request: GET http://x/v1/usage 500")
        _flush()
        content = log_env.read_text(encoding="utf-8")
        assert "200 OK" not in content
        assert "500" in content

    def test_repeated_setup_keeps_httpx_pinned(self, log_env: Path) -> None:
        logging_setup.setup_cli_logging()
        logging.getLogger("httpx").setLevel(logging.INFO)  # 外部改动
        logging_setup.setup_cli_logging()  # 二次调用仍应重新压制
        assert logging.getLogger("httpx").level == logging.WARNING

    def test_record_reaches_stderr_and_file(
        self, log_env: Path, capsys: pytest.CaptureFixture
    ) -> None:
        logging_setup.setup_cli_logging()
        logging.getLogger("llm_usage.providers").warning("provider kimi 拉取失败")
        _flush()
        assert "provider kimi 拉取失败" in capsys.readouterr().err
        content = log_env.read_text(encoding="utf-8")
        assert "provider kimi 拉取失败" in content
        assert "WARNING" in content
        assert "llm_usage.providers" in content

    def test_idempotent_handler_installation(self, log_env: Path) -> None:
        logging_setup.setup_cli_logging()
        logging_setup.setup_cli_logging()
        handlers = _our_handlers()
        assert len(handlers) == 2  # stderr + 文件
        assert sum(isinstance(h, logging.FileHandler) for h in handlers) == 1
        logging.getLogger("llm_usage").error("单行不重复")
        _flush()
        assert log_env.read_text(encoding="utf-8").count("单行不重复") == 1

    def test_env_override_creates_parent_dirs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_env: Path
    ) -> None:
        nested = tmp_path / "sub" / "deep" / "app.log"
        monkeypatch.setenv("LLM_USAGE_LOG", str(nested))
        logging_setup.setup_cli_logging()
        logging.getLogger("llm_usage").info("初始化完成")
        _flush()
        assert nested.exists()
        assert "初始化完成" in nested.read_text(encoding="utf-8")

    def test_rotation_creates_backup(self, log_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(logging_setup, "MAX_BYTES", 200)
        monkeypatch.setattr(logging_setup, "BACKUP_COUNT", 2)
        logging_setup.setup_cli_logging()
        logger = logging.getLogger("llm_usage.rotate")
        for i in range(50):
            logger.info("padding-padding-padding-padding line %d", i)
        _flush()
        assert log_env.exists()
        assert log_env.with_name(log_env.name + ".1").exists()

    def test_uncreatable_path_falls_back_to_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_env: Path
    ) -> None:
        blocker = tmp_path / "blocker"
        blocker.write_text("file", encoding="utf-8")
        monkeypatch.setenv("LLM_USAGE_LOG", str(blocker / "x.log"))
        fake_stderr = io.StringIO()
        monkeypatch.setattr(sys, "stderr", fake_stderr)
        logging_setup.setup_cli_logging()
        handlers = _our_handlers()
        assert len(handlers) == 1
        assert not isinstance(handlers[0], logging.FileHandler)
        assert "无法创建日志文件" in fake_stderr.getvalue()
        logging.getLogger("llm_usage").error("仍然可记录")
        _flush()
        assert "仍然可记录" in fake_stderr.getvalue()


class TestSetupWebLogging:
    def test_uvicorn_loggers_merged_into_root_file(self, log_env: Path) -> None:
        # 模拟 uvicorn 自带 dictConfig handler:setup_web_logging 应移除并改为 propagate
        uvicorn_error = logging.getLogger("uvicorn.error")
        uvicorn_error.addHandler(logging.StreamHandler(io.StringIO()))
        logging_setup.setup_web_logging()
        assert uvicorn_error.handlers == []
        assert uvicorn_error.propagate is True
        logging.getLogger("uvicorn.access").info('%s - "GET / HTTP/1.1" 200', "127.0.0.1:1")
        uvicorn_error.warning("Started server process [1]")
        _flush()
        content = log_env.read_text(encoding="utf-8")
        assert '"GET / HTTP/1.1" 200' in content
        assert content.count("Started server process") == 1  # 只经 root 写一次,不双写


class TestCliIntegration:
    def test_any_subcommand_initializes_log_file(
        self, log_env: Path, tmp_config_path: Path
    ) -> None:
        result = CliRunner().invoke(main, ["config"])
        assert result.exit_code == 0
        assert log_env.exists()

    def test_subcommand_help_writes_nothing(
        self, log_env: Path, tmp_config_path: Path
    ) -> None:
        """click 的 `sub --help` 也会触发组回调:日志初始化必须在各命令体内,
        否则纯帮助调用会凭空创建日志文件。"""
        for args in (["show", "--help"], ["web", "--help"], ["--help"]):
            result = CliRunner().invoke(main, args)
            assert result.exit_code == 0
        assert not log_env.exists()
