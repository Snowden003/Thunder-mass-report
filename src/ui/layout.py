# -*- coding: utf-8 -*-
"""
UI Layout Components for THUNDER Reporter.

Reusable Rich-based panels, boxes, headers, and footers that replace
the old manual ANSI box-drawing code. All components accept the theme
and adapt to terminal width automatically.
"""

from __future__ import annotations

from typing import Optional

from rich.box import DOUBLE, HEAVY, ROUNDED, SIMPLE, SQUARE
from rich.console import Console, ConsoleOptions, RenderResult
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .theme import (
    BORDER_STYLE,
    BORDER_HEX,
    ICON_GEM,
    ICON_LIGHTNING,
    ICON_SKULL,
    PRIMARY_HEX,
    PRIMARY_STYLE,
    SECONDARY_HEX,
    SECONDARY_STYLE,
    SILVER_STYLE,
    SUCCESS_HEX,
    SUCCESS_STYLE,
    UI_THEME,
    WARNING_HEX,
    WARNING_STYLE,
)


# ── Reusable Panel Helpers ──────────────────────────────────────────────────

def header_panel(
    title: str,
    subtitle: Optional[str] = None,
    border_style: str = PRIMARY_HEX,
    padding: tuple = (0, 1),
    width: Optional[int] = None,
) -> Panel:
    """
    Create a styled header panel with title and optional subtitle.
    """
    text_parts = [Text.assemble(("☠ ", PRIMARY_STYLE), (title, PRIMARY_STYLE))]
    if subtitle:
        text_parts.append(Text.assemble(("  ", SILVER_STYLE), (subtitle, SECONDARY_STYLE)))

    content = Text.assemble(*text_parts, justify="left")

    return Panel(
        content,
        border_style=border_style,
        box=DOUBLE,
        padding=padding,
        width=width,
        title_align="left",
    )


def info_panel(
    title: str,
    content: str,
    border_style: str = SECONDARY_HEX,
    padding: tuple = (0, 1),
    width: Optional[int] = None,
) -> Panel:
    """
    Create an informational panel with a title and body content.
    """
    return Panel(
        Text.from_markup(content, style=None),
        title=Text.assemble(("→ ", SECONDARY_STYLE), (title, SILVER_STYLE)),
        border_style=border_style,
        box=ROUNDED,
        padding=padding,
        width=width,
        title_align="left",
    )


def status_panel(
    title: str,
    items: list[tuple[str, str, str]],  # (label, value, style_hex)
    border_style: str = BORDER_HEX,
    padding: tuple = (0, 1),
    width: Optional[int] = None,
) -> Panel:
    """
    Create a panel with key-value status rows.
    """
    table = Table(box=None, show_header=False, show_footer=False, padding=(0, 0))
    table.add_column("label", style="muted", width=16, no_wrap=True)
    table.add_column("value", style="silver", no_wrap=False)

    for label, value, style in items:
        table.add_row(Text(label, style="muted"), Text.from_markup(value, style=None))

    return Panel(
        table,
        title=Text.assemble(("◈ ", SECONDARY_STYLE), (title, SILVER_STYLE)),
        border_style=border_style,
        box=ROUNDED,
        padding=padding,
        width=width,
        title_align="left",
    )


def stats_panel(
    title: str,
    metrics: list[tuple[str, str]],  # (label, value)
    border_style: str = SUCCESS_HEX,
    padding: tuple = (0, 1),
    width: Optional[int] = None,
) -> Panel:
    """
    Create a stats panel with metric rows.
    """
    table = Table(box=None, show_header=False, show_footer=False, padding=(0, 0))
    table.add_column("label", style="muted", width=18, no_wrap=True)
    table.add_column("value", style="success", no_wrap=False)

    for label, value in metrics:
        table.add_row(Text(label, style="muted"), Text.from_markup(value, style=None))

    return Panel(
        table,
        title=Text.assemble(("⚡ ", PRIMARY_STYLE), (title, SILVER_STYLE)),
        border_style=border_style,
        box=ROUNDED,
        padding=padding,
        width=width,
        title_align="left",
    )


def warning_panel(
    title: str,
    message: str,
    border_style: str = WARNING_HEX,
    padding: tuple = (0, 1),
    width: Optional[int] = None,
) -> Panel:
    """
    Create a warning/error panel.
    """
    return Panel(
        Text(message, style="warning"),
        title=Text.assemble(("✖ ", PRIMARY_HEX), (title, PRIMARY_HEX)),
        border_style=border_style,
        box=ROUNDED,
        padding=padding,
        width=width,
        title_align="left",
    )


def success_panel(
    title: str,
    message: str,
    border_style: str = SUCCESS_HEX,
    padding: tuple = (0, 1),
    width: Optional[int] = None,
) -> Panel:
    """
    Create a success panel.
    """
    return Panel(
        Text(message, style="success"),
        title=Text.assemble(("✔ ", SUCCESS_HEX), (title, SILVER_STYLE)),
        border_style=border_style,
        box=ROUNDED,
        padding=padding,
        width=width,
        title_align="left",
    )


# ── Section Divider ─────────────────────────────────────────────────────────

def section_divider(
    title: str,
    style: str = BORDER_HEX,
    width: Optional[int] = None,
) -> Text:
    """
    Create a section divider line with a centered title.
    """
    if width is None:
        width = DEFAULT_WIDTH

    title_text = f" {title} "
    available = width - len(title_text) - 2
    left = available // 2
    right = available - left

    line = "─" * max(1, left) + title_text + "─" * max(1, right)
    return Text(line, style=style)


# ── Logo ────────────────────────────────────────────────────────────────────

def logo_panel(width: int = 80) -> Panel:
    """
    Create the main application logo panel.
    """
    lines = [
        "[primary]   ████████╗██╗  ██╗██╗   ██╗███╗   ██╗██████╗ ███████╗██████╗",
        "   ╚══██╔══╝██║  ██║██║   ██║████╗  ██║██╔══██╗██╔════╝██╔══██╗[/primary]",
        "      ██║   ███████║██║   ██║██╔██╗ ██║██║  ██║██████╗  ██████╔╝",
        "      ██║   ██╔══██║██║   ██║██║╚██╗██║██║  ██║██╔══╝  ██╔══██╗",
        "      ██║   ██║  ██║╚════██╔╝██║ ╚████║██████╔╝███████╗██║  ██║",
        "      ╚═╝   ╚═╝  ╚═╝ ╚═════╝ ╚═╝  ╚══════╝╚═╝  ╚═╝",
        "",
        "[secondary]   WHITE RAVEN  //  PROJECT VOID STRIKE[/secondary]",
        "",
        "[silver]   SYSTEM   : THUNDER  //  TIKTOK MASS DISRUPTOR ARSENAL[/silver]",
        "[silver]   OPERATOR  : WhiteRaven  [BLACK-HAT LABS // SHADOW OPS][/silver]",
        "[silver]   CODENAME  : PROJECT VOID STRIKE  ::  ARMED & LETHAL[/silver]",
        "[silver]   ENGINE    : Gorgon-Argus v2.1  |  Residential Mesh  |  STABLE[/silver]",
    ]

    logo_text = Text.from_markup("\n".join(lines), style=None)

    return Panel(
        logo_text,
        border_style=PRIMARY_HEX,
        box=DOUBLE,
        padding=(0, 1),
        width=width,
        title_align="left",
    )


# ── Console Width Detection ─────────────────────────────────────────────────

DEFAULT_WIDTH = 80


def get_console_width(console: Console) -> int:
    """Get the current console width, with a sensible default."""
    try:
        return console.size.width
    except Exception:
        return DEFAULT_WIDTH