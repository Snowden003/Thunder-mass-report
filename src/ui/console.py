# -*- coding: utf-8 -*-
"""
Console wrapper and input helpers for THUNDER Reporter.

Provides a configured Rich Console and convenience input functions.
"""

from __future__ import annotations

from rich.console import Console

from .theme import UI_THEME


def create_console(force_terminal: bool = True) -> Console:
    """Create a Rich Console with the THUNDER theme."""
    return Console(
        theme=UI_THEME,
        force_terminal=force_terminal,
        width=None,  # Auto-detect
    )


def prompt_yes_no(console: Console, text: str, default: bool = True) -> bool:
    """Prompt for a yes/no confirmation."""
    default_str = "Y/n" if default else "y/N"
    result = console.input(f"[primary]☠[/primary] [primary]{text}[/primary] [muted]({default_str})[/muted] [primary]::[/primary] ").strip().lower()
    if not result:
        return default
    return result in ("y", "yes")


def prompt_text(console: Console, text: str, default: str = "") -> str:
    """Prompt for text input."""
    if default:
        prompt = f"[primary]☠[/primary] [primary]{text}[/primary] [muted](default: {default})[/muted] [primary]::[/primary] "
    else:
        prompt = f"[primary]☠[/primary] [primary]{text}[/primary] [primary]::[/primary] "
    return console.input(prompt).strip() or default