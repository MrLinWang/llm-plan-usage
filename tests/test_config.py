"""Config module tests: init, load, save, platform config merge, env: keys, 0600 perms."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from llm_usage import config as C


def _table_headers(text: str) -> str:
    """配置文本中的所有表头行(忽略注释里的示例)。"""
    return "\n".join(
        line for line in text.splitlines() if line.lstrip().startswith("[")
    )


class TestConfigInit:
    def test_init_creates_file(self, tmp_path: Path) -> None:
        p = tmp_path / "config.toml"
        C.init_config(p)
        assert p.exists()
        text = p.read_text(encoding="utf-8")
        assert "[platforms.kimi]" in text
        assert "[platforms.ollama]" in text
        assert "[platforms.opencode-go]" in text
        assert "[platforms.opendesign]" in text
        assert "[platforms.sub2api]" in text
        # 每个供应商只有一个表,accounts 写在表内,不使用子表
        assert "accounts = [" in text
        assert "[[platforms." not in _table_headers(text)

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

    def test_checked_in_example_matches_template(self) -> None:
        """config.toml.example 是模板的检入副本;改动模板必须同步该文件。"""
        example = Path(__file__).resolve().parents[1] / "config.toml.example"
        assert example.read_text(encoding="utf-8") == C.EXAMPLE_CONFIG


class TestConfigLoadSave:
    @pytest.mark.parametrize("api_key", ["x", "env:KIMI_API_KEY", "sk-" + "a" * 120])
    def test_account_format_is_stable_for_short_and_long_keys(
        self, tmp_path: Path, api_key: str,
    ) -> None:
        path = tmp_path / "config.toml"
        original = {"platforms": {
            "kimi": {"api_key": api_key, "enabled": False},
            "sub2api": {"enabled": True, "base_url": "https://sub2api.test", "groups": [
                {"name": "共享账号", "api_keys": [{"name": "主 Key", "value": "env:KEY"}]},
            ]},
        }}
        C.save_config(original, path)
        text = path.read_text()
        assert "[platforms.kimi]" in text
        assert "[platforms.sub2api]" in text
        assert "accounts = [" in text
        assert "[[platforms." not in _table_headers(text)
        raw = tomllib.loads(text)
        assert raw["platforms"]["kimi"] == {"enabled": False, "accounts": [{"api_key": api_key}]}
        assert raw["platforms"]["sub2api"]["accounts"] == [
            {"name": "共享账号", "api_keys": [{"name": "主 Key", "value": "env:KEY"}]},
        ]
        assert C.load_config(path) == original
        C.save_config(C.load_config(path), path)
        assert path.read_text() == text

    def test_account_formatter_handles_quoted_instance_and_extension_keys(self) -> None:
        original = {"platforms": {"kimi#2": {
            "enabled": True, "api_key": "env:KIMI_API_KEY",
            "附加设置": {"quoted = key": {"a.b": [1, 2]}},
        }}}
        text = C.format_config(original)
        assert '[platforms."kimi#2"]' in text
        assert "[[platforms." not in _table_headers(text)
        assert C.to_runtime_config(tomllib.loads(text)) == original

    def test_every_provider_keeps_accounts_in_its_own_platform_table(self) -> None:
        """9 个供应商(含实例)都只占一个表:无子表头,accounts 在表内。"""
        original = {"platforms": {
            "kimi": {"enabled": True, "api_key": "env:KIMI"},
            "kimi#2": {"enabled": False, "credentials": [
                {"name": "套餐A", "api_key": "env:KIMI_A"},
                {"name": "套餐B", "api_key": "env:KIMI_B"},
            ]},
            "volcengine-coding": {"enabled": True, "tier": "pro", "access_key": "AK",
                                  "secret_key": "SK"},
            "volcengine-agent": {"enabled": True, "credentials": [
                {"access_key": "AK", "secret_key": "SK"},
                {"access_key": "AK2", "secret_key": "SK2"},
            ]},
            "ollama": {"api_key": "env:OLLAMA"},
            "opencode-go": {"api_key": "env:OPENCODE"},
            "clinepass": {"api_key": "env:CLINE"},
            "commandcode": {"api_key": "env:COMMAND"},
            "opendesign": {"enabled": True, "max_balance": 300,
                           "credentials": [{"name": "套餐1", "control_key": "vela_ctrl_x"}]},
            "sub2api": {"enabled": True, "base_url": "https://g.test", "groups": [
                {"name": "组A", "api_keys": ["k1", "k2"]},
                {"name": "组B", "daily_limit": 20, "api_keys": [
                    {"name": "主 Key", "value": "k3"}]},
            ]},
        }}
        text = C.format_config(original)
        assert "[[platforms." not in _table_headers(text)
        # 每个平台表头正好出现一次,且没有任何 [platforms.<key>.<child>] 子表
        for key in original["platforms"]:
            header = f"[platforms.{C._toml_key(key)}]"
            assert text.count(header) == 1, key
        for line in _table_headers(text).splitlines():
            assert line.startswith("[platforms.") and line.endswith("]")
            assert line.removeprefix("[platforms.").removesuffix("]").count(".") == 0, line
        parsed = tomllib.loads(text)
        assert parsed == C.to_accounts_config(original)
        assert C.to_runtime_config(parsed) == C.to_runtime_config(original)
        assert len(parsed["platforms"]["kimi#2"]["accounts"]) == 2
        assert len(parsed["platforms"]["sub2api"]["accounts"]) == 2
        assert C.format_config(parsed) == text

    def test_long_named_key_group_wraps_inside_its_account_line(self) -> None:
        """超长命名 Key:key 数组在 account 行内换行,仍不产生子表。"""
        original = {"platforms": {"sub2api": {"enabled": True, "groups": [
            {"name": "组1", "daily_limit": 200, "api_keys": [
                {"name": "主 Key", "value": "sk-" + "1" * 80},
                {"name": "备用 Key", "value": "sk-" + "2" * 80},
            ]},
        ]}}}
        text = C.format_config(original)
        assert "[[platforms." not in _table_headers(text)
        assert _table_headers(text).count("[platforms.") == 1
        assert "api_keys = [\n" in text
        assert C.to_runtime_config(tomllib.loads(text)) == C.to_runtime_config(original)
        assert C.format_config(tomllib.loads(text)) == text

    def test_empty_and_unknown_account_values_stay_in_the_platform_table(self) -> None:
        """空账号数组、未知供应商的非表值都原样留在平台表内。"""
        cases = {
            "empty_accounts": {"kimi": {"enabled": False, "accounts": []}},
            "unknown_provider": {"future-provider": {
                "accounts": "unknown", "api_key": 123, "legacy": ["anything"]}},
            "empty_section": {"ollama": {}},
            "dormant_legacy": {"opendesign": {
                "legacy": {"api_key": "sk-amr-model"},
                "accounts": [{"name": "套餐1", "control_key": "vela"}],
            }},
        }
        for name, platforms in cases.items():
            text = C.format_config({"platforms": platforms})
            assert "[[platforms." not in _table_headers(text), name
            parsed = tomllib.loads(text)
            assert parsed == C.to_accounts_config({"platforms": platforms}), name
            assert C.format_config(parsed) == text, name

    @pytest.mark.parametrize(("config", "expected"), [
        ({"platforms": {"kimi": "oops"}}, '[platforms]\nkimi = "oops"\n'),
        ({"platforms": [{"kimi": 1}]}, "[[platforms]]\nkimi = 1\n"),
        ({"platforms": {}}, "[platforms]\n"),
        ({}, ""),
    ])
    def test_malformed_or_missing_platforms_never_serialize_to_an_empty_file(
        self, config: dict[str, Any], expected: str,
    ) -> None:
        """畸形但可解析的 platforms 必须原样回写,不得静默清空整份配置。

        ``[platforms] kimi = "oops"`` 与非表数组曾是合法输入;写入器若丢弃
        它们就会把 config.toml 变成 0 字节(数据丢失)。
        """
        text = C.format_config(config)
        assert text == expected
        assert tomllib.loads(text) == config
        assert C.format_config(tomllib.loads(text)) == text

    def test_malformed_platforms_are_never_truncated_on_save(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('[platforms]\nkimi = "oops"\n\n[platforms.ollama]\nenabled = true\n')
        original = C.load_config(path)
        C.save_config(original, path)
        text = path.read_text()
        assert text, "保存后配置文件不得为空"
        raw = tomllib.loads(text)
        assert raw["platforms"]["kimi"] == "oops"
        # 已知供应商会被补成标准 accounts 形态;关键是其设置没有丢。
        assert raw["platforms"]["ollama"]["enabled"] is True
        assert raw == C.to_accounts_config(original)
        assert C.load_config(path) == original

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
        assert cfg["platforms"]["opendesign"]["control_key"] == "env:OPENDESIGN_CONTROL_KEY"
        assert cfg["platforms"]["opendesign"]["base_url"] == (
            "https://amr-link.open-design.ai/v1"
        )


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


class TestConfigFormatCommand:
    def test_format_migrates_current_file_without_printing_credentials(
        self, tmp_config_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from click.testing import CliRunner

        from llm_usage.cli import main

        monkeypatch.setattr("llm_usage.cli.setup_cli_logging", lambda: None)
        tmp_config_path.write_text('[platforms.kimi]\nenabled = true\napi_key = "private-key"\n')
        result = CliRunner().invoke(main, ["config", "--format"])
        assert result.exit_code == 0, result.output
        assert "private-key" not in result.output
        assert "已统一配置格式" in result.output
        raw = tomllib.loads(tmp_config_path.read_text())
        assert raw["platforms"]["kimi"]["accounts"] == [{"api_key": "private-key"}]
        assert tmp_config_path.stat().st_mode & 0o777 == 0o600
        before = tmp_config_path.read_bytes()
        assert CliRunner().invoke(main, ["config", "--format"]).exit_code == 0
        assert tmp_config_path.read_bytes() == before

    def test_format_error_preserves_the_file(
        self, tmp_config_path: Path, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from click.testing import CliRunner

        from llm_usage.cli import main

        monkeypatch.setattr("llm_usage.cli.setup_cli_logging", lambda: None)
        original = '[platforms.kimi]\naccounts = "private-invalid-value"\n'
        tmp_config_path.write_text(original)
        result = CliRunner().invoke(main, ["config", "--format"])
        assert result.exit_code == 2
        assert "private-invalid-value" not in result.output
        assert tmp_config_path.read_text() == original
