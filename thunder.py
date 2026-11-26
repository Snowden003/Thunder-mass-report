# -*- coding: utf-8 -*-
"""
THUNDER Reporter — Structured Reporting Workflow

Flow
----
1. Goal capture      Ask the operator what this run should achieve and resolve it
                     into an explicit specification.
2. Session cloning   Pull sessions from Redis (the only authoritative source) and
                     clone them into a request pool of at least 1,000 entries.
3. Feedback config   Show every report reason for multi-select and pick the
                     feedback type (phone / web).
4. Timing control    Parse a user-defined delay range and arm the automatic
                     100-second pause that fires when an account is banned.
5. Execution         Run the paced request loop with live progress and a clean
                     Ctrl+C stop.

Redis is the only session source. ``data/saved_sessions.json`` is a cache that is
purged and rewritten from Redis on every start; it is never read as a fallback.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

# ── Path setup ──────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "src")
for _d in (BASE_DIR, SRC_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from core.profile_analyzer import ProfileFetcher
from core.redis_manager import RedisSessionManager
from core.report_engine import LOGS_DIR, ReportEngine
from core.persistence import PersistenceManager

# Initialize persistent storage
_storage = PersistenceManager()

# ── Constants ───────────────────────────────────────────────────────────────

DEFAULT_POOL_SIZE = 1000
BAN_PAUSE_SECONDS = 100.0

# key, code, category, label — mirrors the canonical TikTok reason catalog.
REASONS: list[tuple[str, str, str, str]] = [
    ("1", "90084", "porn", "Nudity & Sexual Content"),
    ("2", "90085", "hate_speech", "Hate Speech"),
    ("3", "90087", "harassment", "Harassment"),
    ("4", "90088", "violence", "Violence"),
    ("5", "90089", "self_harm", "Self Harm"),
    ("6", "90090", "illegal_activities", "Illegal Activities"),
    ("7", "90091", "frauds_and_scams", "Frauds & Scams"),
    ("8", "90092", "spam", "Spam"),
]

FEEDBACK_TYPES = {
    "1": "phone",
    "2": "web",
    "phone": "phone",
    "mobile": "phone",
    "web": "web",
    "browser": "web",
}

BAN_HTTP_STATUSES = {"403", "429"}
BAN_RESTRICTIONS = {"account_suspended", "action_block", "captcha", "banned"}
BAN_KEYWORDS = (
    "banned",
    "blocked",
    "too many requests",
    "rate limit",
    "rate-limit",
    "suspend",
    "restricted",
    "try again later",
    "temporarily unavailable",
)
BAN_SKIP_REASONS = {"3", "4", "5"}

console = Console()


# ── Configuration model ─────────────────────────────────────────────────────

@dataclass
class WorkflowConfig:
    target: str = ""
    goal: str = "report target account"
    reasons: list[tuple[str, str, str, str]] = field(default_factory=list)
    feedback_type: str = "phone"          # phone | web
    delay_min: float = 5.0
    delay_max: float = 17.0
    pool_size: int = DEFAULT_POOL_SIZE
    ban_pause: float = BAN_PAUSE_SECONDS
    max_requests: int = 0                 # 0 = run until interrupted
    proxy_file: str = ""
    direct: bool = False

    @property
    def reason_codes(self) -> list[tuple[str, str, str]]:
        """(code, category, label) tuples consumed by ReportEngine."""
        return [(code, cat, label) for _key, code, cat, label in self.reasons]

    @property
    def mode(self) -> str:
        return "web" if self.feedback_type == "web" else "mobile"


# ── Parsing helpers ─────────────────────────────────────────────────────────

_UNIT_SECONDS = {
    "h": 3600.0, "hr": 3600.0, "hrs": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "m": 60.0, "min": 60.0, "mins": 60.0, "minute": 60.0, "minutes": 60.0,
    "s": 1.0, "sec": 1.0, "secs": 1.0, "second": 1.0, "seconds": 1.0,
}

_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)\s*([a-zA-Z]*)")

_RANGE_SEPARATORS = re.compile(r"\s*(?:-|–|—|to|until|\.\.|تا)\s*", re.IGNORECASE)


def parse_duration(text: str) -> Optional[float]:
    """Parse a human duration into seconds.

    Accepts ``50``, ``50s``, ``1m``, ``1m30s``, ``1 minute``, ``1:30``.
    Returns ``None`` when nothing usable is found.
    """
    if text is None:
        return None
    raw = str(text).strip().lower().replace(",", ".")
    if not raw:
        return None

    # mm:ss / hh:mm:ss clock notation
    if ":" in raw:
        parts = raw.split(":")
        try:
            values = [float(p) for p in parts]
        except ValueError:
            return None
        if len(values) == 2:
            return values[0] * 60 + values[1]
        if len(values) == 3:
            return values[0] * 3600 + values[1] * 60 + values[2]
        return None

    total = 0.0
    matched = False
    for number, unit in _DURATION_TOKEN.findall(raw):
        unit = unit.strip()
        multiplier = 1.0 if unit == "" else _UNIT_SECONDS.get(unit)
        if multiplier is None:
            return None
        try:
            total += float(number) * multiplier
        except ValueError:
            return None
        matched = True

    return total if matched else None


def parse_range(text: str) -> Optional[tuple[float, float]]:
    """Parse a delay range such as ``1m-50s`` or ``1 minute to 50 seconds``.

    The two bounds are normalised so the smaller value is always the minimum.
    """
    if text is None:
        return None
    raw = str(text).strip()
    if not raw:
        return None

    parts = [p for p in _RANGE_SEPARATORS.split(raw) if p.strip()]
    if not parts:
        return None
    if len(parts) == 1:
        value = parse_duration(parts[0])
        if value is None:
            return None
        return value, value

    first = parse_duration(parts[0])
    second = parse_duration(parts[-1])
    if first is None or second is None:
        return None
    low, high = sorted((first, second))
    return low, high


def parse_reason_selection(text: str) -> Optional[list[tuple[str, str, str, str]]]:
    """Resolve ``1,3,5`` or ``90084 90087`` into reason tuples.

    Accepts both catalog keys and raw TikTok reason codes. Duplicates collapse.
    """
    raw = (text or "").replace(" ", ",")
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        return None

    by_key = {key: item for item in REASONS for key in (item[0], item[1])}
    chosen: list[tuple[str, str, str, str]] = []
    seen: set[str] = set()
    for token in tokens:
        item = by_key.get(token)
        if item is None:
            return None
        if item[1] in seen:
            continue
        seen.add(item[1])
        chosen.append(item)
    return chosen or None


# ── Interactive capture ─────────────────────────────────────────────────────

def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [dim]({default})[/dim]" if default else ""
    answer = console.input(f"[cyan]{prompt}[/cyan]{suffix}: ").strip()
    return answer or default


def _ask_yes_no(prompt: str, default: bool = True) -> bool:
    hint = "Y/n" if default else "y/N"
    answer = console.input(f"[cyan]{prompt}[/cyan] [{hint}]: ").strip().lower()
    if not answer:
        return default
    return answer in ("y", "yes", "1", "true", "بله")


def render_reason_catalog() -> None:
    table = Table(title="Feedback Reasons", show_lines=False)
    table.add_column("#", justify="right", style="bold cyan")
    table.add_column("Code", style="dim")
    table.add_column("Reason", style="white")
    for key, code, _cat, label in REASONS:
        table.add_row(key, code, label)
    console.print(table)


def capture_goal(cfg: WorkflowConfig) -> None:
    console.print(Panel(
        "Tell THUNDER what this run should accomplish.\n"
        "The goal is recorded in the run manifest and shown back for confirmation.",
        title="Step 1 · Goal Capture",
        border_style="cyan",
    ))
    cfg.goal = _ask("Goal", cfg.goal or "report target account")
    if not cfg.target:
        cfg.target = _ask("Target username (@handle)").lstrip("@").strip()


def capture_reasons(cfg: WorkflowConfig) -> None:
    console.print()
    console.print(Panel(
        "Select one or more reasons. Every option is available.\n"
        "Enter keys separated by commas (e.g. [bold]1,3,5[/bold]) or TikTok codes.",
        title="Step 3 · Feedback Reasons",
        border_style="cyan",
    ))
    render_reason_catalog()

    default = ",".join(item[0] for item in cfg.reasons) if cfg.reasons else "1"
    while True:
        raw = _ask("Reasons", default)
        selected = parse_reason_selection(raw)
        if selected:
            cfg.reasons = selected
            return
        console.print("[red]Invalid selection. Use 1-8 or the 900xx codes.[/red]")


def capture_feedback_type(cfg: WorkflowConfig) -> None:
    console.print()
    console.print(Panel(
        "[1] Phone  — mobile API (Android signatures)\n"
        "[2] Web    — browser feedback endpoint (requires a sessionid cookie)",
        title="Step 3 · Feedback Type",
        border_style="cyan",
    ))
    default = "2" if cfg.feedback_type == "web" else "1"
    while True:
        raw = _ask("Feedback type", default).strip().lower()
        resolved = FEEDBACK_TYPES.get(raw)
        if resolved:
            cfg.feedback_type = resolved
            return
        console.print("[red]Enter 1/phone or 2/web.[/red]")


def capture_timing(cfg: WorkflowConfig) -> None:
    console.print()
    console.print(Panel(
        "Send requests inside this time window. Both bounds are in seconds;\n"
        "suffixes s/m/h are supported.\n"
        "Examples: [bold]5s-17s[/bold], [bold]1m-50s[/bold], "
        "[bold]1 minute to 50 seconds[/bold], [bold]30s[/bold]",
        title="Step 4 · Send-Time Range",
        border_style="cyan",
    ))
    default = f"{cfg.delay_min:g}s-{cfg.delay_max:g}s"
    while True:
        raw = _ask("Delay range", default)
        parsed = parse_range(raw)
        if parsed is None:
            console.print("[red]Could not parse the range. Try 1m-50s.[/red]")
            continue
        low, high = parsed
        if low <= 0:
            console.print("[red]The range must be greater than zero.[/red]")
            continue
        cfg.delay_min, cfg.delay_max = low, high
        return


def capture_pool_size(cfg: WorkflowConfig) -> None:
    console.print()
    raw = _ask("Request-pool entries to clone", str(cfg.pool_size))
    try:
        value = int(raw)
    except ValueError:
        return
    if value >= 1:
        cfg.pool_size = value


def capture_proxy(cfg: WorkflowConfig) -> None:
    if cfg.direct:
        return
    console.print()
    default_yes = os.path.exists(os.path.join(BASE_DIR, "data", "proxies.txt"))
    if not _ask_yes_no("Route through the proxy mesh?", default_yes):
        cfg.direct = True
        cfg.proxy_file = ""
        return
    cfg.proxy_file = _ask(
        "Proxy file",
        cfg.proxy_file or os.path.join(BASE_DIR, "data", "proxies.txt"),
    )


# ── Target resolution ───────────────────────────────────────────────────────

def resolve_target(username: str, proxy_manager=None, interactive: bool = True) -> dict:
    """Fetch target metadata, with cache lookup first."""
    username = username.lstrip("@").strip()

    # ── 1. Check persistent cache first ──
    cached = _storage.get_profile(username)
    if cached:
        meta = cached.get("metadata", {})
        profile_data = {
            "user_id": cached.get("user_id"),
            "sec_uid": meta.get("sec_uid"),
            "display_name": cached.get("display_name"),
            "followers": meta.get("followers", 0),
            "following": meta.get("following", 0),
            "likes": meta.get("likes", 0),
            "country": meta.get("country", "—"),
            "private": meta.get("private", False),
            "bio": meta.get("bio", ""),
            "joined_date": meta.get("date", "—"),
        }
        profile = type('CacheProfile', (), profile_data)()
    else:
        # Fallback to ProfileFetcher
        profile = ProfileFetcher(proxy_manager).fetch(username)
    # ── End cache lookup ──

    target = {
        "username": username,
        "id": str(getattr(profile, "user_id", "") or ""),
        "sec_uid": str(getattr(profile, "sec_uid", "") or ""),
        "name": str(getattr(profile, "display_name", "") or username),
        "followers": int(getattr(profile, "followers", 0) or 0),
        "following": int(getattr(profile, "following", 0) or 0),
        "likes": int(getattr(profile, "likes", 0) or 0),
        "country": str(getattr(profile, "country", "—") or "—"),
        "private": bool(getattr(profile, "private", False)),
        "bio": str(getattr(profile, "bio", "") or ""),
        "date": str(getattr(profile, "joined_date", "—") or "—"),
    }

    if not target["id"] and interactive:
        console.print("[yellow]Automatic profile lookup failed.[/yellow]")
        target["id"] = _ask("Target numeric user id").strip()
    if not target["sec_uid"] and interactive:
        target["sec_uid"] = _ask("Target sec_uid (optional)", "").strip()
    # ── Save to cache ──
    if target["id"]:
        metadata = {
            "followers": target["followers"],
            "following": target["following"],
            "likes": target["likes"],
            "country": target["country"],
            "private": target["private"],
            "bio": target["bio"],
        }
        _storage.save_profile(target["id"], target["username"], target["name"], metadata)
    # ── End cache save ──
    return target


# ── Redis sessions + request pool ───────────────────────────────────────────

def load_sessions_from_redis() -> tuple[RedisSessionManager, list[dict]]:
    """Refresh the Redis cache and return the authoritative active sessions."""
    manager = RedisSessionManager()
    if not manager.is_connected:
        return manager, []
    try:
        # Single fetch: rebuild the cache and hand back the same snapshot.
        return manager, manager.reload_sessions()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[yellow]Redis refresh failed: {exc}[/yellow]")
        return manager, []


def clone_sessions(sessions: list[dict], minimum: int) -> list[dict]:
    """Clone sessions into a request pool with at least ``minimum`` entries."""
    if not sessions:
        return []

    copies = max(1, math.ceil(minimum / len(sessions)))
    pool: list[dict] = []
    for copy_index in range(copies):
        for session in sessions:
            clone = dict(session)
            if isinstance(clone.get("cookies_dict"), dict):
                clone["cookies_dict"] = dict(clone["cookies_dict"])
            origin = session.get("identifier") or session.get("label") or "session"
            clone["_origin"] = origin
            clone["_clone_index"] = copy_index
            clone["label"] = f"{origin}·c{copy_index + 1}"
            pool.append(clone)
    return pool


def persist_request_pool(pool: list[dict]) -> str:
    """Write a cookie-free manifest of the pool for auditability."""
    import json

    path = os.path.join(BASE_DIR, "data", "request_pool.json")
    manifest = [
        {
            "origin": entry.get("_origin", ""),
            "label": entry.get("label", ""),
            "clone_index": entry.get("_clone_index", 0),
            "identifier": entry.get("identifier", ""),
        }
        for entry in pool
    ]
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "generated_at": datetime.now().isoformat(timespec="seconds"),
                    "count": len(manifest),
                    "entries": manifest,
                },
                handle,
                indent=2,
                ensure_ascii=False,
            )
    except OSError as exc:
        console.print(f"[yellow]Could not write request pool manifest: {exc}[/yellow]")
        return ""
    return path


# ── Ban detection ───────────────────────────────────────────────────────────

def detect_ban(last_request: dict) -> tuple[bool, list[str]]:
    """Return ``(is_ban, signals)`` for a completed request record."""
    if not last_request:
        return False, []

    signals: list[str] = []
    status = str(last_request.get("http_status", "")).strip()
    restriction = str(last_request.get("restriction") or "").strip().lower()
    detail = str(last_request.get("detail") or "")
    body = str(last_request.get("response_full") or "")

    if status in BAN_HTTP_STATUSES:
        signals.append(f"HTTP {status}")
    if restriction in BAN_RESTRICTIONS:
        signals.append(restriction)

    haystack = f"{detail} {body}".lower()
    for keyword in BAN_KEYWORDS:
        if keyword in haystack:
            signals.append(keyword)
            break

    match = re.search(r'"skip_reason"\s*:\s*(\d+)', body)
    if match and match.group(1) in BAN_SKIP_REASONS:
        signals.append(f"skip_reason={match.group(1)}")

    return bool(signals), signals


def _pause(seconds: float, reason: str = "") -> None:
    """Sleep in one-second ticks so Ctrl+C stays responsive."""
    label = f" ({reason})" if reason else ""
    console.print(
        f"[bold yellow]⏸  Account ban detected{label} — pausing {seconds:.0f}s[/bold yellow]"
    )
    remaining = float(seconds)
    while remaining > 0:
        step = min(1.0, remaining)
        time.sleep(step)
        remaining -= step
        if int(remaining) % 10 == 0 and remaining > 0:
            console.print(f"[dim]   {remaining:.0f}s remaining…[/dim]", end="\r")
    console.print("[green]▶  Resuming request loop[/green]")


# ── Spec confirmation ───────────────────────────────────────────────────────

def render_spec(cfg: WorkflowConfig, target: dict, session_count: int, pool_size: int) -> None:
    reason_lines = "\n".join(f"  • {label} ({code})" for _k, code, _c, label in cfg.reasons)
    table = Table(title="Resolved Run Specification", show_header=False, box=None)
    table.add_column("field", style="bold cyan", no_wrap=True)
    table.add_column("value", style="white")
    table.add_row("Goal", cfg.goal)
    table.add_row("Target", f"@{target.get('username')}  (id={target.get('id') or '—'})")
    table.add_row("Feedback type", cfg.feedback_type)
    table.add_row("Reasons", f"{len(cfg.reasons)} selected")
    table.add_row("  selection", reason_lines.strip())
    table.add_row("Delay range", f"{cfg.delay_min:g}s – {cfg.delay_max:g}s")
    table.add_row("Ban pause", f"{cfg.ban_pause:.0f}s")
    table.add_row("Redis sessions", str(session_count))
    table.add_row("Request pool", f"{pool_size} entries")
    table.add_row("Request cap", "unlimited" if cfg.max_requests == 0 else str(cfg.max_requests))
    table.add_row("Egress", "direct" if cfg.direct else (cfg.proxy_file or "direct"))
    console.print(table)


# ── Execution ───────────────────────────────────────────────────────────────

def _fmt_count(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return "—"


def render_request_result(index: int, last: dict, next_label: str) -> None:
    """Print everything the operator needs about one completed request."""
    success = bool(last.get("success"))
    border = "green" if success else "red"
    status = last.get("http_status", "—")
    server_code = last.get("server_code", "—")
    latency = last.get("latency", 0)
    egress_ip = last.get("egress_ip") or "unknown"
    proxy_label = last.get("proxy") or "—"

    session_label = last.get("session_account") or "no-session"
    session_user = last.get("session_username") or ""
    followers = last.get("session_followers")
    likes = last.get("session_likes")

    vector = last.get("vector") or "—"
    reason = last.get("reason") or "—"

    result_mark = "[bold green]✓ ACCEPTED[/bold green]" if success else "[bold red]✗ REJECTED[/bold red]"

    meta = Table.grid(padding=(0, 2))
    meta.add_column(style="bold cyan", no_wrap=True)
    meta.add_column(style="white", overflow="fold")

    meta.add_row("Result", f"{result_mark}  ·  request #{index}")
    meta.add_row("HTTP status", str(status))
    meta.add_row("Server code", str(server_code))
    meta.add_row("Latency", f"{latency} ms")
    meta.add_row("Egress IP", str(egress_ip))
    if proxy_label and proxy_label not in (egress_ip, "—"):
        meta.add_row("Proxy", str(proxy_label))

    session_bits = [str(session_label)]
    if session_user and session_user != session_label:
        session_bits.append(f"@{session_user}")
    session_bits.append(f"{_fmt_count(followers)} followers")
    session_bits.append(f"{_fmt_count(likes)} likes")
    meta.add_row("Session", "  ·  ".join(session_bits))
    meta.add_row("Reason", f"{vector} ({reason})")
    meta.add_row("Next request", next_label)

    console.print(Panel(meta, title=f"Request #{index}", border_style=border))

    body = last.get("response_full") or ""
    if not body.strip():
        body = last.get("detail") or "(empty response body)"
    console.print(Panel(
        Text(str(body), overflow="fold"),
        title="Server response — full body",
        border_style=border,
    ))


def run_workflow(
    cfg: WorkflowConfig,
    target: dict,
    pool: list[dict],
    proxy_manager=None,
    session_manager: Optional[RedisSessionManager] = None,
) -> int:
    if not cfg.reasons:
        console.print("[red]No feedback reason selected.[/red]")
        return 2

    first_code, first_cat, first_label = cfg.reason_codes[0]
    engine = ReportEngine(
        target=target,
        mode=cfg.mode,
        reason_code=first_code,
        category=first_cat,
        reason_label=first_label,
        delay_min=cfg.delay_min,
        delay_max=cfg.delay_max,
        is_random=cfg.delay_min != cfg.delay_max,
        proxy_manager=proxy_manager,
        session_manager=session_manager or RedisSessionManager(),
        sessions=pool,
        reasons=cfg.reason_codes,
    )
    engine._log_file = os.path.join(
        LOGS_DIR,
        f"workflow_{target.get('username', 'target')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
    )

    console.print()
    console.print(Panel(
        f"Target [bold]@{target.get('username')}[/bold] · {cfg.feedback_type} · "
        f"{len(cfg.reasons)} reason(s) · pool {len(pool)}\n"
        "Press [bold]Ctrl+C[/bold] to stop.",
        title="Step 5 · Execution",
        border_style="green",
    ))

    sent = 0
    accepted = 0
    rejected = 0
    ban_pauses = 0
    started = time.time()

    # ── Duplicate check ──
    recent_history = _storage.get_feedback_history(user_id=target.get("id"))
    recent_reasons = {r["reason_code"] for r in recent_history if "ACCEPTED" in r["status"] or r["status"] == "success"}
    
    selected_reasons = [code for _k, code, _c, _l in cfg.reasons]
    already_reported = [code for code in selected_reasons if code in recent_reasons]
    
    if already_reported:
        console.print(f"[yellow]⚠️  Notice:[/yellow] You have already reported this account for: [bold]{', '.join(already_reported)}[/bold] recently.")
        if not _ask_yes_no("Continue with additional reports?", True):
            return 0
    # ── End duplicate check ──

    try:
        while True:
            if cfg.max_requests and sent >= cfg.max_requests:
                break

            try:
                engine._do_report()
            except Exception as exc:  # noqa: BLE001
                console.print(f"[red]request error:[/red] {exc}")
                _pause(1.0)
                continue

            sent += 1
            last = dict(engine.last_request or {})
            success = bool(last.get("success"))
            if success:
                accepted += 1
            else:
                rejected += 1

            # ── Log to feedback history ──
            reason_code = last.get("reason", "") or ""
            reason_label = last.get("vector", "") or ""
            status = "accepted" if success else "rejected"
            _storage.log_feedback(
                target.get("id", ""),
                target.get("username", ""),
                str(reason_code),
                str(reason_label),
                status
            )
            # ── End log ──

            banned, signals = detect_ban(last)
            if banned:
                ban_pauses += 1
                wait = cfg.ban_pause
                next_label = f"{wait:.0f}s  (ban pause: {', '.join(signals)})"
            else:
                wait = (
                    random.uniform(cfg.delay_min, cfg.delay_max)
                    if cfg.delay_max > cfg.delay_min
                    else cfg.delay_min
                )
                next_label = f"{wait:.1f}s"

            render_request_result(sent, last, next_label)

            if banned:
                _pause(wait, ", ".join(signals))
            else:
                time.sleep(wait)
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopping — waiting for the current request to settle…[/yellow]")

    elapsed = max(0.001, time.time() - started)
    summary = Table(title="Run Summary", show_header=False, box=None)
    summary.add_column("field", style="bold cyan", no_wrap=True)
    summary.add_column("value", style="white")
    summary.add_row("Requests sent", str(sent))
    summary.add_row("Accepted", str(accepted))
    summary.add_row("Rejected", str(rejected))
    summary.add_row("Accept rate", f"{(accepted / sent * 100):.1f}%" if sent else "—")
    summary.add_row("Ban pauses", str(ban_pauses))
    summary.add_row("Elapsed", f"{elapsed:.0f}s")
    summary.add_row("Throughput", f"{sent / elapsed:.2f} req/s")
    console.print(summary)

    return 0 if accepted or sent == 0 else 1


# ── CLI ─────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="report-tiktok.py",
        description="THUNDER structured reporting workflow",
    )
    parser.add_argument("-t", "--target", default="", help="target @username")
    parser.add_argument("-g", "--goal", default="", help="run goal (recorded in the spec)")
    parser.add_argument("-r", "--reasons", default="", help="comma list, e.g. 1,3,5 or 90084,90087")
    parser.add_argument("--type", dest="feedback_type", default="", help="phone | web")
    parser.add_argument("--range", dest="delay_range", default="", help="delay range, e.g. 1m-50s")
    parser.add_argument("--pool-size", type=int, default=None,
                        help=f"request-pool entries to clone (default {DEFAULT_POOL_SIZE})")
    parser.add_argument("--max-requests", type=int, default=0,
                        help="stop after N requests (0 = unlimited)")
    parser.add_argument("--ban-pause", type=float, default=BAN_PAUSE_SECONDS,
                        help="pause length in seconds when an account is banned")
    parser.add_argument("--proxy-file", default="", help="proxy list file")
    parser.add_argument("--direct", action="store_true", help="skip proxies entirely")
    parser.add_argument("-y", "--yes", action="store_true", help="skip the confirmation prompt")
    parser.add_argument("--history", action="store_true", help="view historical feedback records and exit")
    parser.add_argument("--user", default="", help="specify a User ID or username for detailed history")
    parser.add_argument("--register", action="store_true", help="register a new account profile and exit")
    parser.add_argument("--list-cached", action="store_true", help="view cached target profiles and exit")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)

    # ── Handle maintenance commands ──
    if args.history:
        if args.user:
            details = _storage.get_account_details(args.user)
            if not details:
                console.print(f"[red]No history or profile found for '{args.user}'.[/red]")
                return 1
            
            profile = details["profile"]
            records = details["records"]
            
            p_table = Table(show_header=False, box=None)
            p_table.add_column("Key", style="cyan")
            p_table.add_column("Value")
            p_table.add_row("User ID", profile.get("user_id"))
            p_table.add_row("Username", f"@{profile.get('username')}")
            p_table.add_row("Display Name", profile.get("display_name"))
            
            meta = profile.get("metadata", {})
            p_table.add_row("Followers", str(meta.get("followers", "—")))
            p_table.add_row("Sec UID", str(meta.get("sec_uid", "—"))[:32] + "...")
            
            console.print(Panel(p_table, title="Account Information", border_style="cyan"))

            h_table = Table(title="Feedback History Records")
            h_table.add_column("Timestamp", style="dim")
            h_table.add_column("Reason", style="white")
            h_table.add_column("Status", style="bold")
            h_table.add_column("Session", style="dim")
            
            for r in records[:100]: # last 100
                status_style = "green" if "ACCEPTED" in r["status"] or r["status"] == "success" else "red"
                h_table.add_row(
                    r["timestamp"],
                    f"{r['reason_label']} ({r['reason_code']})",
                    f"[{status_style}]{r['status']}[/{status_style}]",
                    str(r["session_id"] or "Unknown")
                )
            
            console.print(h_table)
            if len(records) > 100:
                console.print(f"[dim]... and {len(records)-100} more records.[/dim]")
            
            console.print("\n[yellow]Note:[/yellow] [bold]REPORT_ACCEPTED[/bold] only confirms API receipt. It is NOT proof that TikTok took moderation action.")
            return 0
        else:
            summaries = _storage.get_account_history_summaries()
            table = Table(title="Account Feedback History Overview")
            table.add_column("Username")
            table.add_column("User ID", style="dim")
            table.add_column("Total", justify="right")
            table.add_column("Accepted", style="green", justify="right")
            table.add_column("Failed", style="red", justify="right")
            table.add_column("Sessions", justify="right")
            table.add_column("Last Seen", style="dim")
            
            for s in summaries:
                table.add_row(
                    f"@{s['username']}",
                    s['user_id'],
                    str(s['total_records']),
                    str(s['accepted_count']),
                    str(s['failed_count']),
                    str(s['session_count']),
                    s['last_timestamp']
                )
            console.print(table)
            console.print(f"\n[dim]Use [bold]--history --user <id>[/bold] for detailed account records.[/dim]")
            return 0

    if args.register:
        target_id = args.target or ""
        if not target_id:
            target_id = _ask("Enter TikTok User ID to register").strip()
        
        username = _ask("Enter Username (optional)", "").strip().lstrip("@")
        
        if _storage.register_account_profile(target_id, username):
            console.print(f"[green]Successfully registered/updated account:[/green] ID={target_id}, User=@{username}")
        else:
            console.print("[red]Registration failed. User ID is required.[/red]")
        return 0

    if args.list_cached:
        profiles = _storage.list_all_profiles()
        table = Table(title="Cached Account Registry")
        table.add_column("User ID", style="dim")
        table.add_column("Username")
        table.add_column("Display Name")
        table.add_column("Last Updated", style="dim")
        for p in profiles:
            table.add_row(p['user_id'], f"@{p['username']}", p['display_name'], p['last_updated'])
        console.print(table)
        return 0
    # ── End maintenance commands ──

    console.print(Panel(
        "[bold]THUNDER[/bold] — structured TikTok reporting workflow\n"
        "[dim]Sessions are sourced from Redis only.[/dim]",
        border_style="cyan",
    ))

    cfg = WorkflowConfig(
        target=args.target.lstrip("@").strip(),
        goal=args.goal.strip() or "report target account",
        feedback_type=FEEDBACK_TYPES.get(args.feedback_type.lower(), "phone"),
        pool_size=max(1, args.pool_size) if args.pool_size is not None else DEFAULT_POOL_SIZE,
        max_requests=max(0, args.max_requests),
        ban_pause=max(1.0, args.ban_pause),
        proxy_file=args.proxy_file,
        direct=args.direct,
    )

    # ── 1. Goal capture ────────────────────────────────────────────────────
    # A fully specified run with -y needs no prompts.
    if not (args.yes and cfg.target):
        capture_goal(cfg)
    if not cfg.target:
        console.print("[red]A target username is required.[/red]")
        return 2

    # ── 3. Feedback configuration ──────────────────────────────────────────
    if args.reasons:
        parsed = parse_reason_selection(args.reasons)
        if not parsed:
            console.print(f"[red]Invalid --reasons value: {args.reasons}[/red]")
            return 2
        cfg.reasons = parsed
    else:
        capture_reasons(cfg)

    if args.feedback_type:
        resolved = FEEDBACK_TYPES.get(args.feedback_type.lower())
        if not resolved:
            console.print(f"[red]Invalid --type value: {args.feedback_type}[/red]")
            return 2
        cfg.feedback_type = resolved
    else:
        capture_feedback_type(cfg)

    # ── 4. Timing ──────────────────────────────────────────────────────────
    if args.delay_range:
        parsed = parse_range(args.delay_range)
        if not parsed or parsed[0] <= 0:
            console.print(f"[red]Invalid --range value: {args.delay_range}[/red]")
            return 2
        cfg.delay_min, cfg.delay_max = parsed
    else:
        capture_timing(cfg)

    if args.pool_size is None and not args.yes:
        capture_pool_size(cfg)

    if not args.direct and not args.proxy_file:
        if args.yes:
            default_proxy = os.path.join(BASE_DIR, "data", "proxies.txt")
            if os.path.exists(default_proxy):
                cfg.proxy_file = default_proxy
            else:
                cfg.direct = True
        else:
            capture_proxy(cfg)

    # ── Proxy setup ────────────────────────────────────────────────────────
    proxy_manager = None
    if not cfg.direct:
        proxy_file = cfg.proxy_file or os.path.join(BASE_DIR, "data", "proxies.txt")
        try:
            from core import ProxyManager

            proxy_manager = ProxyManager.from_file(proxy_file, validate=False)
            console.print(
                f"[green]Proxy mesh loaded:[/green] "
                f"{proxy_manager.healthy_count()}/{len(proxy_manager)} nodes"
            )
        except Exception as exc:  # noqa: BLE001
            console.print(f"[yellow]Proxy load failed: {exc} — using direct connection.[/yellow]")
            proxy_manager = None

    # ── 2. Redis sessions + request pool ───────────────────────────────────
    with console.status("[cyan]Refreshing sessions from Redis…[/cyan]", spinner="dots"):
        _manager, sessions = load_sessions_from_redis()
    if not sessions:
        console.print(
            "[red]No active sessions in Redis. The workflow cannot start.[/red]\n"
            "[dim]Add sessions first; Redis is the only permitted session source.[/dim]"
        )
        return 3

    pool = clone_sessions(sessions, cfg.pool_size)
    manifest = persist_request_pool(pool)
    console.print(
        f"[green]Sessions:[/green] {len(sessions)} unique → "
        f"[green]request pool:[/green] {len(pool)} entries"
        + (f" [dim](manifest: {os.path.relpath(manifest, BASE_DIR)})[/dim]" if manifest else "")
    )

    # ── Target metadata ────────────────────────────────────────────────────
    console.print("[dim]Resolving target metadata…[/dim]")
    target = resolve_target(cfg.target, proxy_manager, interactive=not args.yes)
    if not target.get("id"):
        console.print(
            "[red]Could not resolve the target's numeric user id.[/red]\n"
            "[dim]Re-run without -y and enter it manually, or check the username.[/dim]"
        )
        return 4

    # ── Confirmation ───────────────────────────────────────────────────────
    console.print()
    render_spec(cfg, target, len(sessions), len(pool))
    if not args.yes and not _ask_yes_no("Launch this run?", True):
        console.print("[yellow]Cancelled before execution.[/yellow]")
        return 0

    return run_workflow(cfg, target, pool, proxy_manager, _manager)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        console.print("\n[yellow]Interrupted by user.[/yellow]")
        sys.exit(130)
