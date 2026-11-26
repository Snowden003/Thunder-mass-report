# -*- coding: utf-8 -*-
"""
THUNDER UI Package.

Clean, polished, and appealing CLI interface for the THUNDER reporter.
Replaces the monolithic ANSI-based UI with a Rich-based, theme-driven design.
"""

from .console import create_console, prompt_yes_no, prompt_text
from .renderer import Renderer
from .state import AppState
from .screens import Screen, WelcomeScreen, ConfigScreen, ExecutionScreen, PauseMenu, ResultsScreen
from .theme import (
    PRIMARY_HEX, SECONDARY_HEX, SUCCESS_HEX, INFO_HEX, WARNING_HEX,
    MUTED_HEX, SILVER_HEX, SURFACE_HEX, BORDER_HEX, VIOLET_HEX,
    ICON_SKULL, ICON_GEM, ICON_LIGHTNING, ICON_SUCCESS, ICON_FAILURE,
    ICON_INFO, ICON_TARGET, ICON_SESSION, ICON_PROXY, ICON_DEVICES,
    ICON_AI, ICON_CLOCK, ICON_SHIELD, ICON_LOCK,
    UI_THEME,
)
from .layout import (
    header_panel, info_panel, status_panel, stats_panel,
    warning_panel, success_panel, section_divider, logo_panel,
    get_console_width,
)

__all__ = [
    "create_console",
    "prompt_yes_no",
    "prompt_text",
    "Renderer",
    "AppState",
    "Screen",
    "WelcomeScreen",
    "ConfigScreen",
    "ExecutionScreen",
    "PauseMenu",
    "ResultsScreen",
    "PRIMARY_HEX", "SECONDARY_HEX", "SUCCESS_HEX", "INFO_HEX", "WARNING_HEX",
    "MUTED_HEX", "SILVER_HEX", "SURFACE_HEX", "BORDER_HEX", "VIOLET_HEX",
    "ICON_SKULL", "ICON_GEM", "ICON_LIGHTNING", "ICON_SUCCESS", "ICON_FAILURE",
    "ICON_INFO", "ICON_TARGET", "ICON_SESSION", "ICON_PROXY", "ICON_DEVICES",
    "ICON_AI", "ICON_CLOCK", "ICON_SHIELD", "ICON_LOCK",
    "UI_THEME",
    "header_panel", "info_panel", "status_panel", "stats_panel",
    "warning_panel", "success_panel", "section_divider", "logo_panel",
    "get_console_width",
]