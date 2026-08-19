"""TUI mode: live-refreshing usage dashboard.

Uses rich's ``Live`` for auto-refresh.  The terminal is switched to cbreak
mode so single keypresses are delivered immediately (no Enter required):
``q`` quits, ``r`` forces an immediate refresh, ``+``/``-`` adjust the
refresh interval.  The key-poll timeout is derived from the remaining time
until the next fetch, so the loop sleeps instead of busy-spinning.
"""

from __future__ import annotations

import contextlib
import sys
import time
from typing import Any, Iterator

from rich.console import Console
from rich.live import Live
from rich.text import Text

from llm_usage.display import TOTAL_WIDTH, build_overview
from llm_usage.providers import fetch_all

DEFAULT_INTERVAL = 60  # seconds
MIN_INTERVAL = 5.0
MAX_INTERVAL = 3600.0


@contextlib.contextmanager
def _cbreak() -> Iterator[None]:
    """Switch the terminal to cbreak; restore the original mode on exit."""
    fd = sys.stdin.fileno()
    import termios
    import tty

    saved = termios.tcgetattr(fd)
    tty.setcbreak(fd)
    try:
        yield
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, saved)
        except Exception:  # noqa: BLE001 — already leaving the TUI
            pass


def _read_key(timeout: float) -> str | None:
    """Read a single keypress if available within ``timeout``, else None."""
    try:
        import select

        if not select.select([sys.stdin], [], [], timeout)[0]:
            return None
        return sys.stdin.read(1)
    except Exception:  # noqa: BLE001 — non-interactive fallback
        return None


def run_tui(cfg: dict[str, Any], interval: float = DEFAULT_INTERVAL) -> None:
    """Run the interactive dashboard until the user presses ``q``."""
    console = Console(width=TOTAL_WIDTH + 4)
    interval = max(min(interval, MAX_INTERVAL), MIN_INTERVAL)
    last_fetch = 0.0
    results = []

    def render(elapsed: float) -> Any:
        overview = build_overview(results)  # type: ignore[arg-type]
        # Countdown to next refresh (seconds only, no progress bar)
        remaining = max(interval - elapsed, 0.0)
        footer = Text.assemble(
            ("间隔 ", "dim"),
            (f"{interval:.0f}s", "cyan"),
            (" · 下次刷新 ", "dim"),
            (f"{remaining:4.0f}s", "cyan"),
            ("   ", ""),
            ("q 退出 · r 刷新 · +/- 调间隔", "dim"),
        )
        from rich.console import Group

        return Group(overview, footer)

    with _cbreak():
        with Live(render(0.0), console=console, refresh_per_second=10, screen=True) as live:
            tick = 0.25
            while True:
                now = time.monotonic()
                elapsed = now - last_fetch
                if elapsed >= interval:
                    last_fetch = now
                    elapsed = 0.0
                    try:
                        results = fetch_all(cfg)
                    except Exception as exc:  # noqa: BLE001
                        from llm_usage.models import PlatformResult

                        results = [PlatformResult("tui", "TUI", error=str(exc))]

                # Recompute elapsed right before render so countdown ticks.
                elapsed_now = time.monotonic() - last_fetch
                live.update(render(elapsed_now))
                key = _read_key(tick)
                if key is None:
                    continue
                if key.lower() == "q":
                    break
                elif key.lower() == "r":
                    last_fetch = 0.0
                elif key in ("+", "="):
                    interval = min(interval * 2, MAX_INTERVAL)  # + 增加间隔
                elif key in ("-", "_"):
                    interval = max(interval / 2, MIN_INTERVAL)  # - 减少间隔