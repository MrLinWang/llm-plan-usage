"""Web dashboard: FastAPI app serving the usage overview with a TTL cache."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from importlib.resources import files
from typing import Any

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

from llm_usage.display import results_to_dict
from llm_usage.models import PlatformResult
from llm_usage.providers import fetch_all

MIN_INTERVAL = 5.0    # 与 tui.py 一致
MAX_INTERVAL = 3600.0


class UsageCache:
    """TTL cache around fetch_all; refreshes at most once per interval."""

    def __init__(self, config: dict[str, Any], interval: float) -> None:
        self._config = config
        self._interval = max(min(interval, MAX_INTERVAL), MIN_INTERVAL)
        self._lock = threading.Lock()
        self._results: list[PlatformResult] | None = None
        self._fetched_at: float = 0.0  # time.monotonic()
        self._fetched_at_iso: str = ""

    @property
    def interval(self) -> float:
        return self._interval

    def get(self) -> tuple[list[PlatformResult], str]:
        """Return (results, fetched_at_utc_iso), refreshing when stale.

        Holds the lock across the fetch so concurrent requests wait for the
        same fresh result instead of thundering the provider APIs.
        Double-check staleness after acquiring so only one thread fetches.
        """
        with self._lock:
            stale = (
                self._results is None
                or time.monotonic() - self._fetched_at >= self._interval
            )
            if stale:
                results = fetch_all(self._config)
                self._results = results
                self._fetched_at = time.monotonic()
                self._fetched_at_iso = datetime.now(timezone.utc).isoformat()
            return self._results, self._fetched_at_iso


def create_app(config: dict[str, Any], interval: float = 60.0) -> FastAPI:
    cache = UsageCache(config, interval)
    app = FastAPI(title="llm-usage")
    index_html = (
        files("llm_usage") / "static" / "index.html"
    ).read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(index_html)

    @app.get("/api/usage")
    def usage() -> JSONResponse:
        try:
            results, fetched_at = cache.get()
        except Exception as exc:  # noqa: BLE001 — fetch_all 内部已隔离单平台失败,这里兜底
            return JSONResponse({"error": f"获取用量失败:{exc}"}, status_code=500)
        return JSONResponse({
            "fetched_at": fetched_at,
            "interval": cache.interval,
            "platforms": results_to_dict(results)["platforms"],
        })

    return app
