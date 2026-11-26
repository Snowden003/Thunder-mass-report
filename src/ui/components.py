# -*- coding: utf-8 -*-
"""
UI Components for THUNDER Reporter.

Reusable widgets that take state data and return Rich renderables.
Each component is a function that accepts relevant data and produces
a Panel, Table, or other Rich renderable.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any, Optional

from rich.box import ROUNDED
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .layout import (
    header_panel,
    info_panel,
    section_divider,
    status_panel,
    stats_panel,
    warning_panel,
)
from .theme import (
    BORDER_HEX,
    DEFAULT_WIDTH,
    ICON_AI,
    ICON_CLOCK,
    ICON_DEVICES,
    ICON_FAILURE,
    ICON_GEM,
    ICON_INFO,
    ICON_LIGHTNING,
    ICON_PROXY,
    ICON_SESSION,
    ICON_SHIELD,
    ICON_SKULL,
    ICON_SUCCESS,
    ICON_TARGET,
    MUTED_HEX,
    PRIMARY_HEX,
    PRIMARY_STYLE,
    SECONDARY_HEX,
    SILVER_HEX,
    SILVER_STYLE,
    SUCCESS_HEX,
    SUCCESS_STYLE,
    UI_THEME,
    VIOLET_HEX,
    WARNING_HEX,
    WARNING_STYLE,
)


# ── Helper ──────────────────────────────────────────────────────────────────

def _style_text(text: str, style_hex: str) -> Text:
    """Create a Text object with the given hex color style."""
    return Text(text, style=style_hex)


# ── Stats Component ─────────────────────────────────────────────────────────

def stats_component(
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
) -> stats_panel:
    """Render the disruption statistics panel."""
    rate_pct = (accepted / total * 100) if total else 0.0

    metrics = [
        ("Accepted", f"[{SUCCESS_HEX}]{accepted}[/{SUCCESS_HEX}]"),
        ("Deflected", f"[{PRIMARY_HEX}]{rejected}[/{PRIMARY_HEX}]"),
        ("Total Volume", f"[{WARNING_HEX}]{total}[/{WARNING_HEX}]"),
        ("Success Rate", f"[{SUCCESS_HEX}]{rate_pct:.1f}%[/{SUCCESS_HEX}]"),
        ("Captcha Hits", f"[{WARNING_HEX}]{captcha_hits}[/{WARNING_HEX}]"),
        ("Cooldowns", f"[{PRIMARY_HEX}]{cooldowns}[/{PRIMARY_HEX}]"),
        ("Time Elapsed", f"[{SILVER_HEX}]{elapsed}[/{SILVER_HEX}]"),
        ("Pacing Delay", f"[{PRIMARY_HEX}]{delay}[/{PRIMARY_HEX}]"),
    ]
    if compact:
        metrics = [metrics[i] for i in (0, 1, 3, 6, 7)]

    return stats_panel(
        "Disruption Statistics",
        metrics,
        border_style=PRIMARY_HEX,
        width=width or DEFAULT_WIDTH,
    )


# ── Target Info Component ──────────────────────────────────────────────────

def target_info_component(
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
) -> status_panel:
    """Render the target reconnaissance panel."""
    items = [
        ("Username", f"@{username}", PRIMARY_HEX),
        ("Nickname", nickname, SILVER_HEX),
        ("Numeric UID", uid, SILVER_HEX),
        ("Followers", followers, SUCCESS_HEX),
        ("Following", following, SUCCESS_HEX),
        ("Likes", likes, WARNING_HEX),
        ("Region", country, SILVER_HEX),
        ("Privacy", private, PRIMARY_HEX),
        ("Registered", registered, SILVER_HEX),
    ]
    if compact:
        items = [items[0], items[2], items[3], items[6]]
    elif bio and bio != "—":
        bio_display = bio[:52] + "…" if len(bio) > 52 else bio
        items.append(("Bio Signature", bio_display, SILVER_HEX))

    return status_panel(
        "Target Reconnaissance",
        items,
        border_style=SECONDARY_HEX,
        width=width or DEFAULT_WIDTH,
    )


# ── Request Log Component ──────────────────────────────────────────────────

def request_log_component(
    requests: list[dict],
    width: Optional[int] = None,
) -> Table:
    """
    Render the recent request transmission log as a table.
    """
    table = Table(
        box=None,
        show_header=True,
        header_style="table.header",
        padding=(0, 0),
        show_footer=False,
    )
    table.add_column("#", style="muted", width=4, no_wrap=True)
    table.add_column("Status", width=14, no_wrap=True)
    table.add_column("Identity", width=16, no_wrap=True)
    table.add_column("Device", width=20, no_wrap=True)
    table.add_column("Vector", width=24, no_wrap=True)
    table.add_column("Score", style="success", width=8, no_wrap=True)
    table.add_column("Latency", width=10, no_wrap=True)
    table.add_column("HTTP", width=8, no_wrap=True)
    table.add_column("API Code", width=10, no_wrap=True)
    table.add_column("Proxy", width=20, no_wrap=True)
    table.add_column("Echo", style="muted", width=24, no_wrap=True)

    for r in requests:
        status_text = "INFILTRATED" if r.get("success") else "DEFLECTED"
        status_style = "success" if r.get("success") else "error"

        table.add_row(
            Text(str(r.get("num", "?")), style="muted"),
            Text(status_text, style=status_style),
            Text(f"@{r.get('account', '—')}", style="silver"),
            Text((f"{r.get('brand', '')} {r.get('device', '')}".strip() or "—")[:18], style="info"),
            Text(r.get("vector", "—")[:22], style="silver"),
            Text(f"{r.get('score', 0):.1f}/10", style="success"),
            Text(f"{r.get('latency', 0):.0f}ms", style="info"),
            Text(str(r.get("http_status", "—")), style="warning"),
            Text(str(r.get("server_code", "—")), style="warning"),
            Text(r.get("proxy", "—")[:18], style="muted"),
            Text(r.get("snippet", "")[:22], style="muted"),
        )

    return table


def response_body_component(
    last_req: Optional[dict],
    width: Optional[int] = None,
    max_chars: int = 4000,
) -> Panel:
    """Render the raw response body of the most recent transmission."""
    if not last_req:
        header = Text("Awaiting first response…", style=MUTED_HEX)
        body = Text("No request has been transmitted yet.", style=MUTED_HEX)
        border = BORDER_HEX
    else:
        success = bool(last_req.get("success"))
        status = "ACCEPTED" if success else "REJECTED"
        header = Text.assemble(
            ("● ", SUCCESS_HEX if success else PRIMARY_HEX),
            (status, SUCCESS_HEX if success else PRIMARY_HEX),
            (
                f"   HTTP {last_req.get('http_status', '—')}"
                f"   API {last_req.get('server_code', '—')}"
                f"   {float(last_req.get('latency', 0)):.0f}ms"
                f"   score {last_req.get('score', 0)}/10",
                MUTED_HEX,
            ),
        )
        raw = last_req.get("response_full") or last_req.get("snippet") or "(empty response)"
        raw = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", str(raw))
        body_text = raw[:max_chars]
        body = Text(body_text, style=SILVER_HEX)
        if len(raw) > max_chars:
            body.append(f"\n… ({len(raw) - max_chars} more characters)", style=MUTED_HEX)
        border = SUCCESS_HEX if success else PRIMARY_HEX

    return Panel(
        Text.assemble(header, "\n\n", body),
        title=Text.assemble(("◈ ", SECONDARY_HEX), ("Last Response Body", SILVER_HEX)),
        border_style=border,
        box=ROUNDED,
        padding=(0, 1),
        width=width or DEFAULT_WIDTH,
        title_align="left",
    )


# ── Session Vault Component ────────────────────────────────────────────────

def session_vault_component(
    sessions: list[dict],
    width: Optional[int] = None,
) -> status_panel:
    """Render the proxy/session vault panel."""
    if not sessions:
        return status_panel(
            "Session Vault",
            [("Status", "Empty (Ghost Recon Mode)", MUTED_HEX)],
            border_style=BORDER_HEX,
            width=width or DEFAULT_WIDTH,
        )

    items = []
    for idx, s in enumerate(sessions[:10], 1):
        label = s.get("label") or s.get("identifier") or "Unnamed"
        sid = s.get("sessionid", "")
        sid_masked = f"{sid[:12]}…{sid[-4:]}" if len(sid) > 16 else sid
        items.append((f"Identity #{idx:02d}", f"@{label}  sid:{sid_masked}", SILVER_HEX))

    if len(sessions) > 10:
        items.append(("…", f"and {len(sessions) - 10} more", MUTED_HEX))

    return status_panel(
        f"Session Vault ({len(sessions)} nodes)",
        items,
        border_style=PRIMARY_HEX,
        width=width or DEFAULT_WIDTH,
    )


# ── Proxy Status Component ─────────────────────────────────────────────────

def proxy_status_component(
    proxy_stats: str = "Direct (no proxy)",
    healthy: int = 0,
    total: int = 0,
    width: Optional[int] = None,
) -> status_panel:
    """Render the proxy tunnel mesh panel."""
    items = [
        ("Routing Mode", proxy_stats, SILVER_HEX),
        ("Healthy Nodes", f"{healthy}/{total}", SUCCESS_HEX if healthy else PRIMARY_HEX),
    ]

    return status_panel(
        "Proxy Tunnel Mesh",
        items,
        border_style=BORDER_HEX,
        width=width or DEFAULT_WIDTH,
    )


# ── AI Sentinel Component ──────────────────────────────────────────────────

def ai_sentinel_component(
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
) -> status_panel:
    """Render the AI sentinel panel."""
    risk_color = {
        "Low": SUCCESS_HEX,
        "Medium": WARNING_HEX,
        "High": PRIMARY_HEX,
        "Critical": PRIMARY_HEX,
    }.get(risk_level, MUTED_HEX)

    items = [
        ("Sentinel Core", status, SILVER_HEX),
        ("Neural Calls", str(neural_calls), SILVER_HEX),
        ("Threat Rating", f"[{risk_color}]{risk_level}[/{risk_color}]", risk_color),
        ("Target Est.", f"{est_requests} payloads", WARNING_HEX),
        ("Deadline", est_duration, WARNING_HEX),
        ("Max Safe Rate", f"{max_safe_rate} req/min", SUCCESS_HEX),
    ]
    if adaptive_delay and adaptive_delay != "—":
        items.append(("Adaptive Delay", adaptive_delay, PRIMARY_HEX))
    if next_action and next_action != "—" and not compact:
        items.append(("Next Directive", next_action[:40], VIOLET_HEX))
    if strategy and strategy != "—":
        items.append(("AI Strategy", strategy[:40 if compact else 64], SILVER_HEX))
    if account_summary and account_summary != "—":
        items.append(("Account Read", account_summary[:40 if compact else 64], MUTED_HEX))
    if compact:
        keep = {"Sentinel Core", "Threat Rating", "Max Safe Rate",
                "AI Strategy", "Account Read", "Adaptive Delay"}
        items = [it for it in items if it[0] in keep][:6]

    return status_panel(
        "Neural AI Sentinel",
        items,
        border_style=VIOLET_HEX,
        width=width or DEFAULT_WIDTH,
    )


# ── Pacing Component ───────────────────────────────────────────────────────

def pacing_component(
    current_sleep: float,
    delay_min: float,
    delay_max: float,
    request_delay: float,
    is_random: bool = True,
    width: Optional[int] = None,
) -> Panel:
    """Render the pacing/adaptive sleep panel."""
    from .layout import info_panel

    mode_text = "Randomized Jitter" if is_random else "Fixed Pacing"
    jitter_text = f"[{delay_min:.1f}s - {delay_max:.1f}s]" if is_random else f"[{request_delay}s]"

    content = Text.assemble(
        ("Pacing Budget: ", "muted"),
        (f"{request_delay:.2f}s", PRIMARY_HEX),
        ("  (dynamic delay)\n", "muted"),
        ("Active Sleep: ", "muted"),
        (f"{current_sleep:.2f}s", PRIMARY_HEX),
        (f"  [Jitter: {jitter_text}]", "muted"),
    )

    return info_panel(
        "Pacing / Sleep Engaged",
        content.plain,
        border_style=SECONDARY_HEX,
        width=width or DEFAULT_WIDTH,
    )


# ── Progress Bar ────────────────────────────────────────────────────────────

def progress_component(
    accepted: int,
    total: int,
    width: int = 20,
) -> Text:
    """Render a colored progress bar."""
    if total == 0:
        filled = 0
    else:
        filled = round((accepted / total) * width)

    bar = f"[{PRIMARY_HEX}]{'█' * filled}[/{PRIMARY_HEX}][{MUTED_HEX}]{'░' * (width - filled)}[/{MUTED_HEX}]"
    pct = f"{(accepted / total * 100) if total else 0:.1f}%"
    return Text.assemble(bar, "  ", (pct, SUCCESS_HEX))