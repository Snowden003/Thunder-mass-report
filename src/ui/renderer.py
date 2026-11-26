# -*- coding: utf-8 -*-
"""
UI Renderer for THUNDER Reporter.

Central rendering engine that combines UI components into full screen layouts.
Uses Rich's Live display for real-time updates during execution.
"""

from __future__ import annotations

from typing import Optional

from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel

from .components import (
    ai_sentinel_component,
    pacing_component,
    progress_component,
    proxy_status_component,
    request_log_component,
    response_body_component,
    session_vault_component,
    stats_component,
    target_info_component,
)
from .layout import header_panel, logo_panel, section_divider
from .theme import (
    DEFAULT_WIDTH,
    ICON_SKULL,
    PRIMARY_HEX,
    SECONDARY_HEX,
    SILVER_STYLE,
    UI_THEME,
)


class Renderer:
    """
    Central rendering engine for the THUNDER CLI.

    Responsible for rendering all UI screens and components using Rich.
    Supports both static rendering and Live display for real-time updates.
    """

    def __init__(self, console: Optional[Console] = None):
        self.console = console or Console(theme=UI_THEME, force_terminal=True)
        if console is not None:
            self.console.push_theme(UI_THEME)
        self.width = self.console.size.width or DEFAULT_WIDTH
        self._live: Optional[Live] = None

    # ── Console Helpers ─────────────────────────────────────────────────────

    def clear(self) -> None:
        """Clear the terminal screen."""
        self.console.clear()

    def print(self, *args, **kwargs) -> None:
        """Print to the console."""
        self.console.print(*args, **kwargs)

    def input(self, prompt: str = "") -> str:
        """Read input from the console."""
        return self.console.input(prompt)

    # ── Static Renders ──────────────────────────────────────────────────────

    def render_logo(self, width: Optional[int] = None) -> Panel:
        """Render the application logo banner."""
        w = width or self.width
        return logo_panel(width=w)

    def render_stats(
        self,
        accepted: int,
        rejected: int,
        total: int,
        success_rate: float,
        elapsed: str,
        delay: str,
        captcha_hits: int = 0,
        cooldowns: int = 0,
        width: Optional[int] = None,
        compact: bool = False,
    ):
        """Render the disruption statistics panel."""
        return stats_component(
            accepted=accepted,
            rejected=rejected,
            total=total,
            success_rate=success_rate,
            elapsed=elapsed,
            delay=delay,
            captcha_hits=captcha_hits,
            cooldowns=cooldowns,
            width=width or self.width,
            compact=compact,
        )

    def render_target_info(
        self,
        username: str,
        nickname: str = "—",
        uid: str = "—",
        followers: str = "—",
        following: str = "—",
        likes: str = "—",
        country: str = "—",
        private: str = "—",
        registered: str = "—",
        bio: str = "—",
        width: Optional[int] = None,
        compact: bool = False,
    ):
        """Render the target reconnaissance panel."""
        return target_info_component(
            username=username,
            nickname=nickname,
            uid=uid,
            followers=followers,
            following=following,
            likes=likes,
            country=country,
            private=private,
            registered=registered,
            bio=bio,
            width=width or self.width,
            compact=compact,
        )

    def render_proxy_status(
        self,
        proxy_stats: str = "Direct (no proxy)",
        healthy: int = 0,
        total: int = 0,
        width: Optional[int] = None,
    ):
        """Render the proxy tunnel mesh panel."""
        return proxy_status_component(
            proxy_stats=proxy_stats,
            healthy=healthy,
            total=total,
            width=width or self.width,
        )

    def render_ai_sentinel(
        self,
        status: str = "Offline",
        neural_calls: int = 0,
        risk_level: str = "—",
        est_requests: str = "—",
        est_duration: str = "—",
        max_safe_rate: str = "—",
        account_summary: str = "—",
        strategy: str = "—",
        next_action: str = "—",
        adaptive_delay: str = "—",
        width: Optional[int] = None,
        compact: bool = False,
    ):
        """Render the AI sentinel panel."""
        return ai_sentinel_component(
            status=status,
            neural_calls=neural_calls,
            risk_level=risk_level,
            est_requests=est_requests,
            est_duration=est_duration,
            max_safe_rate=max_safe_rate,
            account_summary=account_summary,
            strategy=strategy,
            next_action=next_action,
            adaptive_delay=adaptive_delay,
            width=width or self.width,
            compact=compact,
        )

    def render_request_log(
        self,
        requests: list[dict],
        width: Optional[int] = None,
    ) -> Table:
        """Render the recent request transmission log."""
        return request_log_component(
            requests=requests,
            width=width or self.width,
        )

    def render_session_vault(
        self,
        sessions: list[dict],
        width: Optional[int] = None,
    ):
        """Render the session vault panel."""
        return session_vault_component(
            sessions=sessions,
            width=width or self.width,
        )

    def render_pacing(
        self,
        current_sleep: float,
        delay_min: float,
        delay_max: float,
        request_delay: float,
        is_random: bool = True,
        width: Optional[int] = None,
    ):
        """Render the pacing/sleep panel."""
        return pacing_component(
            current_sleep=current_sleep,
            delay_min=delay_min,
            delay_max=delay_max,
            request_delay=request_delay,
            is_random=is_random,
            width=width or self.width,
        )

    def render_response_body(
        self,
        last_req: Optional[dict],
        width: Optional[int] = None,
        max_chars: int = 4000,
    ):
        """Render the raw response body of the latest request."""
        return response_body_component(
            last_req=last_req,
            width=width or self.width,
            max_chars=max_chars,
        )

    def render_progress(
        self,
        accepted: int,
        total: int,
        width: int = 20,
    ) -> Text:
        """Render a progress bar."""
        return progress_component(accepted=accepted, total=total, width=width)

    # ── Dashboard Composition ───────────────────────────────────────────────

    def render_dashboard(
        self,
        state,
        requests: list[dict],
        compact: bool = False,
    ) -> Group:
        """
        Render the full execution dashboard as a Group of panels.

        Layout:
          [ Logo ] (compact: one-line header)
          [ Stats | Target Info ]
          [ Proxy | AI Sentinel ]
          [ Session Vault ]  (full mode only)
          [ Pacing ]         (full mode only)
          [ Recent Request Log ]
          [ Raw Response Body ]
        """
        w = self.width
        half = max(1, w // 2 - 2)

        panels = []

        # Logo banner
        if compact:
            panels.append(header_panel(
                "LIVE STRIKE",
                f"@{state.target or '—'}  •  {state.run_mode.upper()}  •  AI {state.ai_status}",
                width=w,
            ))
        else:
            panels.append(self.render_logo(width=w))
            panels.append(section_divider("DISRUPTION STRIKE TELEMETRY", style=PRIMARY_HEX, width=w))

        # Row 1: Stats + Target Info
        stats_panel = self.render_stats(
            accepted=state.gg,
            rejected=state.bb,
            total=state.gg + state.bb,
            success_rate=state.success_rate,
            elapsed=state.elapsed,
            delay=state.delay_display_str,
            captcha_hits=state.captcha_hits,
            cooldowns=state.active_cooldowns,
            width=half,
            compact=compact,
        )
        target_panel = self.render_target_info(
            username=state.target,
            nickname=state.nickname,
            uid=state.target_id,
            followers=state.followers,
            following=state.following,
            likes=state.likes,
            country=state.country,
            private=state.private,
            registered=state.registered,
            bio=state.bio,
            width=half,
            compact=compact,
        )
        panels.append(Group(stats_panel, target_panel))

        # Row 2: Proxy + AI Sentinel
        proxy_panel = self.render_proxy_status(
            proxy_stats=state.proxy_stats,
            healthy=state.proxy_healthy,
            total=state.proxy_total,
            width=half,
        )
        ai_panel = self.render_ai_sentinel(
            status=state.ai_status,
            neural_calls=state.ai_neural_calls,
            risk_level=state.ai_risk_level,
            est_requests=state.ai_est_requests,
            est_duration=state.ai_est_duration,
            max_safe_rate=state.ai_max_safe_rate,
            account_summary=state.ai_account_summary,
            strategy=state.ai_strategy,
            next_action=state.ai_next_action,
            adaptive_delay=state.ai_adaptive_delay,
            width=half,
            compact=compact,
        )
        panels.append(Group(proxy_panel, ai_panel))

        if not compact:
            # Session Vault
            panels.append(section_divider("SHADOW SESSION VAULT", style=SECONDARY_HEX, width=w))
            panels.append(self.render_session_vault(
                sessions=state.session_list,
                width=w,
            ))

            # Pacing
            panels.append(section_divider("TRAFFIC PACING / SLEEP ENGAGED", style=SECONDARY_HEX, width=w))
            panels.append(self.render_pacing(
                current_sleep=state.current_sleep,
                delay_min=state.delay_min,
                delay_max=state.delay_max,
                request_delay=state.request_delay,
                is_random=state.is_random_delay,
                width=w,
            ))
            panels.append(section_divider("RECENT TRANSMISSION LOG", style=PRIMARY_HEX, width=w))

        # On short terminals, sacrifice the log table so the AI box and
        # response body remain visible instead of being clipped.
        very_small = compact and (self.console.size.height or 24) < 34

        # Recent Request Log (compact mode keeps only the latest few rows)
        if not very_small:
            panels.append(self.render_request_log(
                requests=requests[:3] if compact else requests,
                width=w,
            ))

        # Raw response body of the most recent transmission
        if not compact:
            panels.append(section_divider("RAW RESPONSE TELEMETRY", style=PRIMARY_HEX, width=w))
        panels.append(self.render_response_body(
            requests[0] if requests else None,
            width=w,
            max_chars=240 if very_small else (1200 if compact else 4000),
        ))

        return Group(*panels)

    # ── Live Display ────────────────────────────────────────────────────────

    def start_live(self, refresh_per_second: int = 4) -> Live:
        """Start a Live display context for real-time updates."""
        self._live = Live(
            console=self.console,
            refresh_per_second=refresh_per_second,
            transient=False,
        )
        self._live.start()
        return self._live

    def update_live(self, renderable) -> None:
        """Update the Live display with a new renderable."""
        if self._live:
            self._live.update(renderable)

    def stop_live(self) -> None:
        """Stop the Live display."""
        if self._live:
            self._live.stop()
            self._live = None

    # ── Input Helpers ───────────────────────────────────────────────────────

    def prompt(self, text: str, default: str = "") -> str:
        """Prompt the user for input with a styled prompt."""
        if default:
            prompt_text = f"[primary]{ICON_SKULL}[/primary] [primary]{text}[/primary] [muted](default: {default})[/muted] [primary]::[/primary] "
        else:
            prompt_text = f"[primary]{ICON_SKULL}[/primary] [primary]{text}[/primary] [primary]::[/primary] "
        return self.console.input(prompt_text).strip() or default

    def confirm(self, text: str, default: bool = True) -> bool:
        """Prompt the user for a yes/no confirmation."""
        default_str = "y/n" if default else "y/n"
        prompt_text = f"[primary]{ICON_SKULL := '☠ '}[/primary] [primary]{text}[/primary] [muted]({default_str})[/muted] [primary]::[/primary] "
        result = self.console.input(prompt_text).strip().lower()
        if not result:
            return default
        return result in ("y", "yes")