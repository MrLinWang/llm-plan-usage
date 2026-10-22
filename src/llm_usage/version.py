"""Resolve the version reported by the CLI and the dashboard's 关于 panel.

Both frontends must agree, so the resolution lives here instead of in
``web.py`` (which ``cli.py`` cannot import without pulling in the optional
FastAPI dependency).  ``cli.py`` evaluates ``app_version()`` at import time,
so this must never raise.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

import llm_usage


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
