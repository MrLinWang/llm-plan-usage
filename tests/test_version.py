"""Build metadata for the dashboard's About panel."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from llm_usage import version


@pytest.fixture
def isolated_commit_cache() -> Iterator[None]:
    version.app_commit.cache_clear()
    yield
    version.app_commit.cache_clear()


@pytest.mark.usefixtures("isolated_commit_cache")
class TestAppCommit:
    def test_prefers_build_revision(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_USAGE_COMMIT", "ABCDEF0123456789ABCDEF0123456789ABCDEF01")
        monkeypatch.setattr(version, "_git_revision", lambda: "1234567890abcdef")
        assert version.app_commit() == "abcdef0"

    @pytest.mark.parametrize("revision", [None, "", "  "])
    def test_blank_build_revision_falls_back_to_git(
        self, monkeypatch: pytest.MonkeyPatch, revision: str | None
    ) -> None:
        if revision is None:
            monkeypatch.delenv("LLM_USAGE_COMMIT", raising=False)
        else:
            monkeypatch.setenv("LLM_USAGE_COMMIT", revision)
        monkeypatch.setattr(version, "_git_revision", lambda: "1234567890abcdef")
        assert version.app_commit() == "1234567"

    @pytest.mark.parametrize("revision", ["not-a-commit", "abc123"])
    def test_rejects_invalid_build_revision(
        self, monkeypatch: pytest.MonkeyPatch, revision: str
    ) -> None:
        monkeypatch.setenv("LLM_USAGE_COMMIT", revision)
        monkeypatch.setattr(version, "_git_revision", lambda: "1234567890abcdef")
        assert version.app_commit() == ""

    def test_installed_package_without_checkout(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.delenv("LLM_USAGE_COMMIT", raising=False)
        monkeypatch.setattr(version, "__file__", str(tmp_path / "src/llm_usage/version.py"))
        monkeypatch.setattr(
            version.subprocess, "run", lambda *args, **kwargs: pytest.fail("unexpected Git lookup")
        )
        assert version.app_commit() == ""


def test_docker_receives_and_preserves_the_source_commit() -> None:
    """CI must pass the commit to the image; runtime must keep it available."""
    root = Path(__file__).resolve().parents[1]
    workflow = (root / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "LLM_USAGE_COMMIT=${{ github.sha }}" in workflow
    assert "ARG LLM_USAGE_COMMIT" in dockerfile
    assert "LLM_USAGE_COMMIT=${LLM_USAGE_COMMIT}" in dockerfile
