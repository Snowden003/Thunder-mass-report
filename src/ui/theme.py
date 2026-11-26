# -*- coding: utf-8 -*-
"""
UI Theme System for THUNDER Reporter.

Centralized color palette, Rich styles, and design tokens.
All UI rendering should reference these constants instead of raw ANSI codes.
"""

from __future__ import annotations

from rich.style import Style
from rich.theme import Theme

# ── Color Palette (Darkness & Hacking Theme) ─────────────────────────────────

# Neon blood red / hacker crimson
PRIMARY_HEX = "#ff0040"
PRIMARY_RGB = "196"

# Deep crimson / blood maroon
SECONDARY_HEX = "#a04040"
SECONDARY_RGB = "160"

# Electric green / bright green
SUCCESS_HEX = "#00ff00"
SUCCESS_RGB = "46"

# Electric blue / cyan
INFO_HEX = "#00aaff"
INFO_RGB = "51"

# Amber / golden
WARNING_HEX = "#f5a623"
WARNING_RGB = "214"

# Muted / dim grey
MUTED_HEX = "#a5a5a5"
MUTED_RGB = "245"

# Silver bone / light grey
SILVER_HEX = "#fafafa"
SILVER_RGB = "250"

# Void background / obsidian
SURFACE_HEX = "#121212"
SURFACE_RGB = "234"

# Border / slate
BORDER_HEX = "#4a4a4a"
BORDER_RGB = "238"

# Void violet / purple
VIOLET_HEX = "#bf5af2"
VIOLET_RGB = "129"

# ── Rich Style Objects ───────────────────────────────────────────────────────

def _style(hex_color: str, bold: bool = False) -> Style:
    return Style(color=hex_color, bold=bold)

PRIMARY_STYLE = _style(PRIMARY_HEX, bold=True)
SECONDARY_STYLE = _style(SECONDARY_HEX)
SUCCESS_STYLE = _style(SUCCESS_HEX, bold=True)
INFO_STYLE = _style(INFO_HEX)
WARNING_STYLE = _style(WARNING_HEX, bold=True)
MUTED_STYLE = _style(MUTED_HEX)
SILVER_STYLE = _style(SILVER_HEX)
SURFACE_STYLE = _style(SURFACE_HEX)
BORDER_STYLE = _style(BORDER_HEX)
VIOLET_STYLE = _style(VIOLET_HEX)

# ── Rich Theme (for Console) ─────────────────────────────────────────────────

UI_THEME = Theme({
    "primary": PRIMARY_HEX,
    "secondary": SECONDARY_HEX,
    "success": SUCCESS_HEX,
    "info": INFO_HEX,
    "warning": WARNING_HEX,
    "error": PRIMARY_HEX,
    "muted": MUTED_HEX,
    "silver": SILVER_HEX,
    "surface": SURFACE_HEX,
    "border": BORDER_HEX,
    "violet": VIOLET_HEX,
    "box.border": BORDER_HEX,
    "box.border.primary": PRIMARY_HEX,
    "box.border.success": SUCCESS_HEX,
    "box.border.warning": WARNING_HEX,
    "box.border.error": PRIMARY_HEX,
    "table.header": PRIMARY_HEX + " bold",
    "table.footer": MUTED_HEX,
    "prompt": PRIMARY_HEX + " bold",
    "status.spinner": PRIMARY_HEX,
})

# ── Design Tokens ────────────────────────────────────────────────────────────

ICON_SKULL = "☠"
ICON_GEM = "◈"
ICON_LIGHTNING = "⚡"
ICON_SUCCESS = "✔"
ICON_FAILURE = "✖"
ICON_WARNING = "!"
ICON_INFO = "→"
ICON_ARROW = "▸"
ICON_LOADING = "⠋"
ICON_DEVICES = "📱"
ICON_PROXY = "🌐"
ICON_TARGET = "🎯"
ICON_SESSION = "👤"
ICON_AI = "🧠"
ICON_CLOCK = "⏱"
ICON_SHIELD = "🛡"
ICON_LOCK = "🔐"

BOX_STYLE_DOUBLE = "double"
BOX_STYLE_SINGLE = "single"
BOX_STYLE_ROUNDED = "rounded"

DEFAULT_WIDTH = 80
DEFAULT_PADDING = (0, 1)