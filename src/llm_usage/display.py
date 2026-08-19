"""Rich rendering: usage overview, history, JSON output.

Uses a manual text layout (not rich.Table) so a progress bar can span the
columns below each data row up to the reset-time column (its right edge
aligns with the seconds digit of the reset time).  CJK display width is handled via
``_disp_width`` so columns align in terminals that render CJK as 2 columns.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from llm_usage.models import PlatformResult, UsageEntry

# Percent thresholds for color coding.
RED_THRESHOLD = 95.0
YELLOW_THRESHOLD = 80.0

# Column layout: (key, header, width, align)  — width in display columns.
# CJK header widths: 平台=4, 窗口=4, 已用=4, 限额=4, 剩余=4, 重置时间=8, 状态=4
COLS = [
    ("platform",  "平台",     22, "left"),
    ("window",    "窗口",     6,  "left"),
    ("used",      "已用",     10, "right"),
    ("limit",     "限额",     10, "right"),
    ("remaining", "剩余",     10, "right"),
    ("reset",     "重置时间", 19, "left"),
    ("status",    "状态",     4,  "left"),
]
# Inter-column gap (display columns).
COL_GAP = 2
N_COLS = len(COLS)

PLATFORM_W = COLS[0][2]
# Bar area after "platform": columns up to "重置时间" (excludes "状态"),
# extended 8 cells so the bar tapers into the status area.
BAR_SPAN_WIDTH = sum(w for _, _, w, _ in COLS[1:-1]) + COL_GAP * (N_COLS - 3) + 8
# Total display width of all columns + gaps.
TOTAL_WIDTH = sum(w for _, _, w, _ in COLS) + COL_GAP * (N_COLS - 1)


def _percent_color(percent: float | None) -> str:
    if percent is None:
        return "white"
    if percent >= RED_THRESHOLD:
        return "red"
    if percent >= YELLOW_THRESHOLD:
        return "yellow"
    return "green"


def _fmt_value(value: float | None, unit: str) -> str:
    if value is None:
        return "-"
    if unit == "$":
        return f"${value:g}"
    if unit == "%":
        return f"{value:g}%"
    if unit == "tokens":
        return f"{value:g} tokens"
    if float(value).is_integer():
        return f"{int(value)} {unit}".strip()
    return f"{value:g} {unit}".strip()


def _disp_width(s: str) -> int:
    """Display width of a string: CJK chars count as 2, others as 1."""
    w = 0
    for ch in s:
        w += 2 if ord(ch) > 0x2E80 else 1
    return w


def _pad(s: str, width: int, align: str) -> str:
    """Pad string to exactly ``width`` display columns, truncating if too wide."""
    dw = _disp_width(s)
    # truncate from the right until it fits
    while dw > width and s:
        s = s[:-1]
        dw = _disp_width(s)
    pad = width - dw
    return (" " * pad + s) if align == "right" else (s + " " * pad)


def _make_header() -> Text:
    """Build the column header line."""
    parts: list[tuple[str, str]] = []
    for i, (_, header, width, align) in enumerate(COLS):
        parts.append((_pad(header, width, align), "bold dim"))
        if i < N_COLS - 1:
            parts.append((" " * COL_GAP, "bold dim"))
    return Text.assemble(*parts)


def _to_local_time(value: str | None) -> str | None:
    """Convert a UTC/offset ISO timestamp to local time for display.

    Providers return UTC (``Z``/``+00:00`` or naive).  Naive values are
    treated as UTC.  Returns ``None`` for empty input, the original string
    when unparsable.
    """
    if not value:
        return None
    s = value.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _make_data_line(entry: UsageEntry, is_first: bool, display_name: str, status: str) -> Text:
    """Build one data line (no bar) as a styled Text."""
    name = display_name if is_first else ""
    if entry.limit is None and entry.used == 0.0 and entry.percent is not None:
        used = limit = remaining = "-"
    else:
        used = _fmt_value(entry.used, entry.unit)
        limit = _fmt_value(entry.limit, entry.unit) if entry.limit is not None else "∞"
        remaining = _fmt_value(entry.remaining, entry.unit) if entry.remaining is not None else "-"

    reset = _to_local_time(entry.reset_at) or "-"

    values = [name, entry.label, used, limit, remaining, reset, status]
    styles = ["bold" if is_first else "", "", "", "", "", "dim", ""]
    parts: list[tuple[str, str]] = []
    for i, ((_, _, width, align), val, style) in enumerate(zip(COLS, values, styles)):
        parts.append((_pad(val, width, align), style))
        if i < N_COLS - 1:
            parts.append((" " * COL_GAP, style))
    return Text.assemble(*parts)


def _make_bar_line(percent: float | None) -> Text:
    """Build a progress bar spanning the columns after "平台" up to "重置时间".

    The bar + label total exactly ``BAR_SPAN_WIDTH`` (reset column + 8 extra
    cells) so all bars align.  The label is right-aligned in a fixed-width
    slot, offset 2 cells left of the far edge.
    """
    if percent is None:
        return Text(" " * (PLATFORM_W + COL_GAP + BAR_SPAN_WIDTH))
    color = _percent_color(percent)

    label = f"{percent:g}%"
    label_w = _disp_width(label)
    label_slot = 7  # max label like "100.0%" fits; right-align within
    label_pad = label_slot - label_w
    if label_pad < 0:
        label_pad = 0

    bar_visual = BAR_SPAN_WIDTH - label_slot - 1  # 1 gap between bar and label
    if bar_visual < 0:
        bar_visual = 0
    filled = round(percent / 100 * bar_visual)
    empty = bar_visual - filled
    bar = "█" * filled + "░" * empty

    indent = _pad("", PLATFORM_W + COL_GAP, "left")
    return Text.assemble(
        (indent, ""),
        (bar, color),
        (" " * (1 + label_pad - 2) + label, color),
    )


def _make_error_line(display_name: str, error: str) -> Text:
    name = _pad(display_name, PLATFORM_W, "left")
    return Text.assemble(
        (name, "bold red"),
        (error, "red"),
    )


def _make_separator() -> Text:
    """A dashed line spanning the full content width, separating platform groups."""
    return Text("─" * TOTAL_WIDTH, style="dim")


def build_overview(results: list[PlatformResult], width: int | None = None) -> Panel:
    """Build the overview Panel renderable (used by render_results and TUI)."""
    lines: list[Any] = [_make_header(), _make_separator()]

    for idx, res in enumerate(results):
        # separator between platforms (not before the first one)
        if idx > 0:
            lines.append(_make_separator())

        if res.error:
            lines.append(_make_error_line(res.display_name, res.error))
            continue
        if not res.entries:
            name = _pad(res.display_name, PLATFORM_W, "left")
            lines.append(Text.assemble((name, "bold"), ("无数据", "dim")))
            continue

        for i, entry in enumerate(res.entries):
            status = "OK" if i == 0 else ""
            lines.append(_make_data_line(entry, is_first=(i == 0),
                                         display_name=res.display_name, status=status))
            lines.append(_make_bar_line(entry.percent))

    title = f"LLM 用量总览 ({datetime.now().strftime('%Y-%m-%d %H:%M')})"
    return Panel(Group(*lines), title=title, border_style="cyan")


def render_results(
    results: list[PlatformResult],
    console: Console | None = None,
    plain: bool = False,
) -> None:
    """Render the main usage overview with full-width progress bars."""
    # Force a fixed width so CJK/box lines never wrap regardless of terminal.
    if console is None:
        console = Console(width=TOTAL_WIDTH + 8)
    else:
        console = Console(file=console.file, width=TOTAL_WIDTH + 8)

    console.print(build_overview(results, width=TOTAL_WIDTH + 8))


def results_to_json(results: list[PlatformResult]) -> str:
    """Serialize results to a JSON string for scripting."""
    payload = {
        "platforms": [
            {
                "name": res.platform,
                "display_name": res.display_name,
                "error": res.error,
                "entries": [
                    {
                        "label": e.label,
                        "used": e.used,
                        "limit": e.limit,
                        "remaining": e.remaining,
                        "percent": e.percent,
                        "reset_at": e.reset_at,
                        "unit": e.unit,
                        "is_manual": e.is_manual,
                    }
                    for e in res.entries
                ],
            }
            for res in results
        ]
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_history(rows: list[dict[str, Any]], console: Console | None = None) -> None:
    """Render snapshot rows with progress bars."""
    console = console or Console()
    if not rows:
        console.print("[yellow]无历史记录[/yellow]")
        return

    table = Table(title="LLM 用量历史")
    table.add_column("时间", style="dim")
    table.add_column("平台")
    table.add_column("窗口")
    table.add_column("已用", justify="right")
    table.add_column("限额", justify="right")
    table.add_column("百分比", justify="right")

    for r in reversed(rows):
        ts = _to_local_time(str(r.get("ts", ""))) or ""
        limit = r.get("limit")
        limit_str = f"{limit:g}" if limit is not None else "∞"
        percent = r.get("percent")
        color = _percent_color(percent)
        percent_str = f"{percent:g}%" if percent is not None else "-"
        table.add_row(
            ts,
            str(r.get("platform", "")),
            str(r.get("label", "")),
            str(r.get("used", "")),
            limit_str,
            f"[{color}]{percent_str}[/{color}]",
        )
    console.print(table)