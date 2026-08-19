"""Click CLI: ``show`` / ``config`` / ``history`` commands."""

from __future__ import annotations

import sys
from typing import Any

import click
from rich.console import Console
from rich.syntax import Syntax

from llm_usage import config as config_mod
from llm_usage.display import render_history, render_results, results_to_json
from llm_usage.models import PlatformResult
from llm_usage.providers import PROVIDERS, fetch_all
from llm_usage.store import query_history, save_snapshot
from llm_usage.tui import run_tui

console = Console()


def _load() -> dict[str, Any]:
    try:
        return config_mod.load_config()
    except RuntimeError as exc:
        console.print(f"[red]{exc}[/red]")
        sys.exit(2)


@click.group()
@click.version_option(package_name="llm-usage")
def main() -> None:
    """llm-usage: unified LLM coding plan usage monitor."""


# --- show -------------------------------------------------------------------

@main.command()
@click.option("--json", "as_json", is_flag=True, help="Output JSON for scripting.")
@click.option("--plain", is_flag=True, help="Plain text table (no box).")
@click.option("--no-save", is_flag=True, help="Do not persist a snapshot to history.")
def show(as_json: bool, plain: bool, no_save: bool) -> None:
    """Fetch all platforms and display current usage."""
    cfg = _load()
    if not cfg.get("platforms"):
        console.print("[yellow]未配置任何平台。运行 `llm-usage config --init` 生成模板。[/yellow]")
        sys.exit(0)
    results = fetch_all(cfg)
    if as_json:
        click.echo(results_to_json(results))
    else:
        render_results(results, console=console, plain=plain)
    if not no_save:
        save_snapshot(results)
    # exit 1 if any platform errored
    if any(not r.ok for r in results):
        sys.exit(1)




# --- tui --------------------------------------------------------------------

@main.command()
@click.option("--interval", type=float, default=60,
              help="Refresh interval in seconds (default 60).")
def tui(interval: float) -> None:
    """Interactive live-refreshing dashboard (q 退出, r 刷新, +/- 调间隔)."""
    cfg = _load()
    if not cfg.get("platforms"):
        console.print("[yellow]未配置任何平台。运行 `llm-usage config --init` 生成模板。[/yellow]")
        sys.exit(0)
    try:
        run_tui(cfg, interval=interval)
    except KeyboardInterrupt:
        pass


# --- config -----------------------------------------------------------------

@main.command()
@click.option("--init", "do_init", is_flag=True, help="Generate example config template.")
@click.option("--force", is_flag=True, help="Overwrite an existing config with --init.")
def config(do_init: bool, force: bool) -> None:
    """Print config path and contents, or generate a template."""
    path = config_mod.config_path()
    if do_init:
        try:
            p = config_mod.init_config(path, overwrite=force)
        except FileExistsError as exc:
            console.print(f"[red]{exc}[/red]")
            sys.exit(2)
        console.print(f"[green]已生成配置模板：{p}[/green]")
        return
    console.print(f"[bold]配置路径：[/bold]{path}")
    if path.exists():
        text = path.read_text(encoding="utf-8")
        console.print(Syntax("toml", text))
    else:
        console.print("[yellow]配置文件不存在。运行 `llm-usage config --init` 生成。[/yellow]")


# --- history ----------------------------------------------------------------

@main.command()
@click.option("--platform", default=None, help="Filter to one platform key.")
@click.option("--days", type=int, default=None, help="Lookback window in days.")
def history(platform: str | None, days: int | None) -> None:
    """Show usage history snapshots."""
    rows = query_history(platform=platform, days=days)
    render_history(rows, console=console)


if __name__ == "__main__":
    main()