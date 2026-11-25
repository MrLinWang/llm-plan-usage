"""Resolve the version reported by the CLI and the dashboard's 关于 panel.

Both frontends must agree, so the resolution lives here instead of in
``web.py`` (which ``cli.py`` cannot import without pulling in the optional
FastAPI dependency).  ``cli.py`` evaluates ``app_version()`` at import time,
so this must never raise.
"""

from __future__ import annotations

import os
import re
import subprocess
from functools import lru_cache
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version
from pathlib import Path

import llm_usage

_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,40}")


def app_version() -> str:
    """Return the running version.

    ``llm_usage.__version__`` wins: it is versioned with the code, so an
    editable install whose metadata was not refreshed after a version bump
    cannot report a stale number (which would also mismatch the newest
    changelog entry).  The distribution metadata is the fallback for the rare
    case where the attribute is absent or empty, e.g. a partially vendored
    tree.  ``getattr`` (not ``from llm_usage import __version__``) keeps that
    fallback reachable instead of raising ``ImportError``.
    """
    version = getattr(llm_usage, "__version__", "")
    if version:
        return version
    try:
        return distribution_version("llm-usage")
    except PackageNotFoundError:
        return "未知"


def _git_revision() -> str:
    """Read HEAD in a development checkout without exposing errors to the UI."""
    try:
        checkout = Path(__file__).resolve().parents[2]
        if not (checkout / ".git").exists():
            return ""
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=checkout,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if result.returncode:
        return ""
    return result.stdout.strip()


@lru_cache(maxsize=1)
def app_commit() -> str:
    """Return the first seven hex digits of the running build's Git revision.

    CI passes the source revision in ``LLM_USAGE_COMMIT``. If the value is
    blank, look for ``HEAD`` in the source checkout. Cache the result for
    the lifetime of the process. Missing or invalid revisions return ``""``.
    """
    revision = os.environ.get("LLM_USAGE_COMMIT", "").strip()
    if not revision:
        revision = _git_revision()
    if not _COMMIT_RE.fullmatch(revision):
        return ""
    return revision[:7].lower()
