# -*- coding: utf-8 -*-
"""
UI Screens for THUNDER Reporter.

Full-screen views as state machines. Each screen has:
  - render(renderer, state) -> None  (draw the screen)
  - handle_input(renderer, state) -> Optional[Screen]  (process input, return next screen)

Screens are UI-only; business logic (network, crypto, etc.) is invoked
via callbacks passed into the screen constructor.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Callable, Optional

from rich.console import Console
from rich.live import Live

from .renderer import Renderer
from .state import AppState

from core import (
    GeminiClient,
    PreflightConsultation,
    ProfileFetcher,
    RedisSessionManager,
    TelegramReporterBot,
)
from core.report_engine import ReportEngine

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PROXY_FILE = os.path.join(_REPO_ROOT, "data", "proxies.txt")


def _fmt_int(value) -> str:
    try:
        return f"{int(value):,}"
    except (TypeError, ValueError):
        return str(value) if value not in (None, "") else "—"


def _int_val(value) -> int:
    try:
        return int(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return 0


def _cached_target(username: str) -> dict:
    """Look up previously acquired target metadata as an offline fallback."""
    path = os.path.join(_REPO_ROOT, "data", "targets.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            for entry in json.load(f):
                if entry.get("username") == username and entry.get("id"):
                    return entry
    except Exception:
        pass
    return {}


def _read_key(timeout: float = 0.25) -> str:
    """Read a single keypress without requiring Enter (POSIX terminals)."""
    try:
        import select
        import termios
        import tty

        if not sys.stdin.isatty():
            return ""
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            if select.select([sys.stdin], [], [], timeout)[0]:
                return sys.stdin.read(1)
            return ""
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)
    except Exception:
        time.sleep(timeout)
        return ""


class Screen:
    """Base class for all UI screens."""

    def render(self, renderer: Renderer, state: AppState) -> None:
        raise NotImplementedError

    def handle_input(
        self, renderer: Renderer, state: AppState
    ) -> Optional["Screen"]:
        raise NotImplementedError


# ── Welcome Screen ──────────────────────────────────────────────────────────

class WelcomeScreen(Screen):
    """Target entry screen - first thing the user sees."""

    def __init__(self, on_target_entered: Callable[[str], None]):
        self.on_target_entered = on_target_entered
        self._input_buffer = ""

    def render(self, renderer: Renderer, state: AppState) -> None:
        renderer.clear()
        renderer.print(renderer.render_logo())
        renderer.print()

        # Target entry panel
        from .layout import info_panel
        renderer.print(info_panel(
            "Target Acquisition",
            f"Enter the target identifier to begin reconnaissance.\n"
            f"Previous target: [silver]{state.target or 'none'}[/silver]",
            width=renderer.width,
        ))
        renderer.print()

    def handle_input(
        self, renderer: Renderer, state: AppState
    ) -> Optional["Screen"]:
        target = renderer.prompt("Target Identifier", default=state.target or "")
        if not target:
            renderer.print("[error]Target identifier cannot be empty.[/error]")
            return self

        target = target.lstrip("@")
        state.target = target
        self.on_target_entered(target)

        renderer.print()
        renderer.print(f"[muted]Fetching target intelligence for @{target}...[/muted]")
        try:
            profile = ProfileFetcher(None).fetch(target)
        except Exception as exc:  # noqa: BLE001
            renderer.print(f"[error]Profile lookup failed: {exc}[/error]")
            return self

        if not profile or not profile.user_id:
            cached = _cached_target(target)
            if cached.get("id"):
                state.target_id = str(cached["id"])
                state.sec_uid = cached.get("sec_uid", "")
                state.nickname = cached.get("name", target)
                state.followers = _fmt_int(cached.get("followers", 0))
                state.following = _fmt_int(cached.get("following", 0))
                state.likes = _fmt_int(cached.get("likes", 0))
                renderer.print(
                    f"[warning]Live lookup blocked — using cached intelligence for @{target}.[/warning]"
                )
            else:
                renderer.print(
                    "[error]Target not found, banned, or lookup was blocked. "
                    "Check the username and try again.[/error]"
                )
                return self
        else:
            state.target_id = str(profile.user_id)
            state.sec_uid = profile.sec_uid or ""
            state.nickname = profile.display_name or target
            state.followers = _fmt_int(profile.followers)
            state.following = _fmt_int(profile.following)
            state.likes = _fmt_int(profile.likes)
            state.country = profile.country or "—"
            state.private = "Yes" if profile.private else "No"
            state.registered = profile.joined_date or "—"
            state.bio = (profile.bio or "—")[:80]

        renderer.print(
            f"[success]Target acquired:[/success] @{target} "
            f"[muted](uid {state.target_id}, {state.followers} followers)[/muted]"
        )
        return ConfigScreen()


# ── Configuration Screen ────────────────────────────────────────────────────

class ConfigScreen(Screen):
    """Configuration: proxy, vectors, mode, delay."""

    def __init__(self):
        self._step = 0  # 0=proxy, 1=vectors, 2=mode, 3=delay

    def render(self, renderer: Renderer, state: AppState) -> None:
        renderer.clear()
        renderer.print(renderer.render_logo())
        renderer.print()

        from .layout import info_panel

        if self._step == 0:
            # Proxy setup
            renderer.print(info_panel(
                "Proxy Tunnel Mesh",
                "Route traffic through SOCKS5 proxy mesh?\n"
                f"Current: [silver]{state.proxy_stats}[/silver]",
                width=renderer.width,
            ))
            renderer.print()
            renderer.print("[1] Use proxy mesh (recommended)")
            renderer.print("[2] Direct connection (no proxy)")
            renderer.print("[3] Skip configuration")

        elif self._step == 1:
            # Vector selection
            renderer.print(info_panel(
                "Exploit Vector Selection",
                "Select report reason vector(s):\n"
                f"Current: [silver]{state.chosen_label}[/silver]",
                width=renderer.width,
            ))
            renderer.print()
            renderer.print("[1] Single vector (default: Nudity & Sexual Content)")
            renderer.print("[2] All vectors (Full Spectrum Swarm)")
            renderer.print("[3] Custom selection")

        elif self._step == 2:
            # Operating mode
            renderer.print(info_panel(
                "Infiltration Protocol",
                "Select engine protocol:\n"
                f"Current: [silver]{state.run_mode.upper()}[/silver]",
                width=renderer.width,
            ))
            renderer.print()
            renderer.print("[1] Mobile App Infiltrator (Android, Gorgon signatures)")
            renderer.print("[2] Web Endpoint Disruptor (Browser cookies, aid=1988)")

        elif self._step == 3:
            # Delay & pacing
            renderer.print(info_panel(
                "Traffic Pacing Protocol",
                f"Current delay: [silver]{state.request_delay}s[/silver]\n"
                f"Mode: [silver]{'Randomized Jitter' if state.is_random_delay else 'Fixed'}[/silver]",
                width=renderer.width,
            ))
            renderer.print()
            renderer.print("[1] Natural Jitter Evasion (randomized)")
            renderer.print("[2] Fixed Velocity Strike (constant)")
            renderer.print("[3] Keep current settings")

    def handle_input(
        self, renderer: Renderer, state: AppState
    ) -> Optional["Screen"]:
        if self._step == 0:
            choice = renderer.prompt("Select proxy option [1-3]", default="1")
            if choice == "1":
                renderer.print("[muted]Loading proxy mesh...[/muted]")
                try:
                    from core import ProxyManager

                    if not os.path.exists(PROXY_FILE):
                        raise FileNotFoundError(f"Proxy file not found: {PROXY_FILE}")
                    manager = ProxyManager.from_file(PROXY_FILE, validate=False)
                    state.proxy_manager = manager
                    state.proxy_stats = manager.stats()
                    state.proxy_healthy = manager.healthy_count()
                    state.proxy_total = len(manager)
                    renderer.print(
                        f"[success]Proxy mesh loaded:[/success] "
                        f"[silver]{state.proxy_healthy}/{state.proxy_total} nodes[/silver]"
                    )
                except Exception as exc:  # noqa: BLE001
                    state.proxy_manager = None
                    state.proxy_stats = "Direct (no proxy)"
                    state.proxy_healthy = 0
                    state.proxy_total = 0
                    renderer.print(f"[warning]Proxy load failed: {exc} — using direct connection.[/warning]")
            elif choice == "2":
                state.proxy_manager = None
                state.proxy_stats = "Direct uplink (no proxy)"
                state.proxy_healthy = 0
                state.proxy_total = 0

            renderer.print("[muted]Loading shadow session vault...[/muted]")
            try:
                mgr = RedisSessionManager()
                state.session_pool = mgr.get_active_sessions()
                renderer.print(
                    f"[success]Session vault:[/success] "
                    f"[silver]{len(state.session_pool)} active identities[/silver]"
                )
            except Exception as exc:  # noqa: BLE001
                state.session_pool = []
                renderer.print(f"[warning]Session vault unavailable: {exc}[/warning]")

            self._step = 1
            return self

        elif self._step == 1:
            choice = renderer.prompt("Select vector option [1-3]", default="1")
            if choice == "1":
                state.chosen_reasons = [("90084", "porn", "Nudity & Sexual Content")]
                state.chosen_reason = "90084"
                state.chosen_category = "porn"
                state.chosen_label = "Nudity & Sexual Content / Pornography"
            elif choice == "2":
                state.chosen_reasons = [
                    ("90084", "porn", "Nudity & Sexual Content"),
                    ("90085", "hate_speech", "Hate Speech"),
                    ("90087", "harassment", "Harassment"),
                    ("90088", "violence", "Violence"),
                    ("90089", "self_harm", "Self Harm"),
                    ("90090", "illegal_activities", "Illegal Activities"),
                    ("90091", "frauds_and_scams", "Frauds & Scams"),
                    ("90092", "spam", "Spam"),
                ]
                state.chosen_reason = state.chosen_reasons[0][0]
                state.chosen_category = state.chosen_reasons[0][1]
                state.chosen_label = state.chosen_reasons[0][2]
            self._step = 2
            return self

        elif self._step == 2:
            choice = renderer.prompt("Select engine protocol [1-2]", default="1")
            state.run_mode = "mobile" if choice == "1" else "web"
            self._step = 3
            return self

        elif self._step == 3:
            choice = renderer.prompt("Select pacing mode [1-3]", default="1")
            if choice == "1":
                state.is_random_delay = True
                state.delay_min = 5.0
                state.delay_max = 17.0
                state.request_delay = 5.0
                state.delay_display_str = "Jitter (5s - 17s)"
            elif choice == "2":
                state.is_random_delay = False
                state.request_delay = 5.0
                state.delay_min = 5.0
                state.delay_max = 5.0
                state.delay_display_str = "Fixed 5s"
            # choice 3 keeps current settings
            engine = state.report_engine
            if engine and not engine.finished:
                engine.delay_min = state.delay_min
                engine.delay_max = state.delay_max
                engine.is_random = state.is_random_delay
                engine.mode = state.run_mode
                engine.reason_code = state.chosen_reason
                engine.category = state.chosen_category
                engine.reason_label = state.chosen_label
                engine.reasons = list(state.chosen_reasons or [])
                engine.proxy_manager = state.proxy_manager
                engine.sessions = list(state.session_pool or [])
                engine.resume()
                return ExecutionScreen()
            return AIConnectScreen()

        return self


# ── AI Connection / Account Review ──────────────────────────────────────────

class AIConnectScreen(Screen):
    """Mandatory AI connection gate before execution starts."""

    def __init__(self):
        self._error = ""
        self._models: list = []

    def render(self, renderer: Renderer, state: AppState) -> None:
        renderer.clear()
        renderer.print(renderer.render_logo())
        renderer.print()
        from .layout import info_panel

        renderer.print(info_panel(
            "Neural Link Required",
            "The AI Sentinel must establish a connection before the strike "
            "engine can start.\n"
            f"Provider: [silver]Token Club[/silver]   "
            f"Target: [silver]@{state.target}[/silver]\n"
            f"Current status: [silver]{state.ai_status}[/silver]",
            width=renderer.width,
        ))
        if self._models:
            renderer.print()
            for m in self._models:
                mark = "[success]OK[/success]" if m.get("ok") else "[error]FAIL[/error]"
                renderer.print(
                    f"  {mark}  [silver]{m.get('model', '?')}[/silver]  "
                    f"[muted]{m.get('status', '')}[/muted]"
                )
        if self._error:
            renderer.print()
            renderer.print(f"[error]{self._error}[/error]")
        renderer.print()

    def handle_input(
        self, renderer: Renderer, state: AppState
    ) -> Optional["Screen"]:
        client = state.ai_client or GeminiClient()
        state.ai_client = client
        state.ai_status = "Connecting…"

        try:
            with renderer.console.status(
                "[primary]Establishing neural link to Token Club…[/primary]",
                spinner="dots",
            ):
                self._models = client.check_models(timeout=12.0)
        except Exception as exc:  # noqa: BLE001
            self._models = []
            self._error = f"Connection failed: {exc}"

        connected = [m for m in self._models if m.get("ok")]
        if not connected:
            if not self._error:
                self._error = "No AI model responded. Check TOOKEN_API_KEY / endpoint."
            renderer.print("[1] Retry neural link")
            renderer.print("[2] Back to configuration")
            choice = renderer.prompt("Select action [1-2]", default="1")
            return self if choice == "1" else ConfigScreen()

        state.ai_status = str(client.get_panel().get("status", "OK"))
        renderer.print(f"[success]Neural link established:[/success] [silver]{state.ai_status}[/silver]")

        self._review_account(renderer, state, client)
        self._contact_admin(renderer, state, client)
        self._gather_materials(renderer, state, client)

        return ExecutionScreen()

    # ── account review ──────────────────────────────────────────────────────
    def _review_account(self, renderer: Renderer, state: AppState, client: GeminiClient) -> None:
        renderer.print("[muted]AI is reviewing the target account…[/muted]")
        try:
            with renderer.console.status(
                "[primary]Running account reconnaissance…[/primary]", spinner="dots"
            ):
                client.analyze_target_account(
                    username=state.target,
                    followers=_int_val(state.followers),
                    following=_int_val(state.following),
                    likes=_int_val(state.likes),
                    country=state.country,
                    private=state.private,
                    bio=state.bio,
                    joined=state.registered,
                    num_sessions=max(1, len(state.session_pool)),
                    run_mode=state.run_mode,
                    chosen_reason=state.chosen_label,
                )
        except Exception as exc:  # noqa: BLE001
            renderer.print(f"[warning]Account review failed: {exc}[/warning]")
            return

        panel = client.get_panel()
        state.ai_status = str(panel.get("status", state.ai_status))
        state.ai_risk_level = str(panel.get("risk_level", "—"))
        state.ai_max_safe_rate = str(panel.get("safe_rate", "—"))
        state.ai_est_requests = str(panel.get("est_requests", "—"))
        state.ai_est_duration = str(panel.get("est_duration", "—"))
        state.ai_strategy = str(panel.get("strategy", "—"))
        state.ai_account_summary = str(panel.get("account_summary", "—"))

        from .layout import info_panel
        renderer.print(info_panel(
            "AI Account Assessment",
            f"Risk: [warning]{state.ai_risk_level}[/warning]   "
            f"Safe rate: [success]{state.ai_max_safe_rate} req/min[/success]   "
            f"Est: [silver]{state.ai_est_requests} requests / {state.ai_est_duration}[/silver]\n"
            f"Account: [silver]{state.ai_account_summary}[/silver]\n"
            f"Strategy: [silver]{state.ai_strategy}[/silver]",
            width=renderer.width,
        ))

    # ── operator contact ────────────────────────────────────────────────────
    def _contact_admin(self, renderer: Renderer, state: AppState, client: GeminiClient) -> None:
        try:
            bot = TelegramReporterBot.from_env()
        except Exception:
            bot = None
        if not bot or not bot.admin_id:
            renderer.print("[muted]Telegram admin contact not configured; skipping outreach.[/muted]")
            return

        message = (
            f"🧠 <b>AI Account Review</b>\n"
            f"🎯 Target: <code>@{state.target}</code>\n"
            f"🆔 UID: <code>{state.target_id}</code>\n"
            f"📊 Risk: <code>{state.ai_risk_level}</code> | "
            f"Safe rate: <code>{state.ai_max_safe_rate}/min</code>\n"
            f"👥 Followers: <code>{state.followers}</code>\n"
            f"📝 Read: <i>{state.ai_account_summary}</i>\n"
            f"💡 Strategy: <i>{state.ai_strategy}</i>\n\n"
            f"Please send any screenshots, account data, or additional material "
            f"needed for a deeper review. Reply here with the files/details."
        )
        try:
            ok = bot.send_message(bot.admin_id, message)
        except Exception:
            ok = False
        if ok:
            renderer.print("[success]Admin contacted via Telegram — awaiting materials.[/success]")
        else:
            renderer.print("[warning]Telegram outreach failed; check bot token/admin ID.[/warning]")

    # ── screenshots / notes ─────────────────────────────────────────────────
    def _gather_materials(self, renderer: Renderer, state: AppState, client: GeminiClient) -> None:
        renderer.print()
        raw_paths = renderer.prompt(
            "Evidence image paths (comma-separated, blank to skip)", default=""
        )
        note = renderer.prompt("Additional context / account data (blank to skip)", default="")

        paths = [p.strip().strip("'\"") for p in raw_paths.split(",") if p.strip()]
        paths = [os.path.expanduser(p) for p in paths]
        if not paths and not note:
            return

        consult = PreflightConsultation(client)
        for path in paths:
            if not os.path.exists(path):
                renderer.print(f"[warning]Screenshot not found: {path}[/warning]")
                continue
            renderer.print(f"[muted]Analyzing {os.path.basename(path)}…[/muted]")
            try:
                result = consult.analyze_screenshot(path)
            except Exception as exc:  # noqa: BLE001
                renderer.print(f"[warning]Image analysis failed: {exc}[/warning]")
                continue
            state.consultation_post_types.extend(result.post_types_detected)
            renderer.print(
                f"[success]Image analyzed:[/success] {result.description}\n"
                f"  [muted]Types: {', '.join(result.post_types_detected) or 'none'} | "
                f"Action: {result.recommended_action}[/muted]"
            )

        if note:
            renderer.print("[muted]Consulting AI with operator notes…[/muted]")
            try:
                reply = consult.chat(note)
                renderer.print(f"[silver]AI:[/silver] {reply[:600]}")
            except Exception as exc:  # noqa: BLE001
                renderer.print(f"[warning]Consultation failed: {exc}[/warning]")

        vectors = state.chosen_reasons or [
            (state.chosen_reason, state.chosen_category, state.chosen_label)
        ]
        profile_ctx = {
            "followers": state.followers,
            "following": state.following,
            "likes": state.likes,
            "private": state.private,
            "bio": state.bio,
        }
        try:
            report = consult.finalize(state.target, profile_ctx, vectors)
        except Exception as exc:  # noqa: BLE001
            renderer.print(f"[warning]Consultation finalize failed: {exc}[/warning]")
            return

        state.consultation_risk = report.risk_assessment
        state.consultation_post_types = list(report.post_types_identified)
        state.consultation_recommended_vectors = list(report.recommended_vectors)
        state.consultation_avoid_vectors = list(report.avoid_vectors)
        state.consultation_pacing = report.pacing_recommendation
        state.consultation_session_rotation = report.session_rotation
        state.consultation_device_mutation = report.device_mutation
        state.consultation_reasoning = report.ai_reasoning
        state.consultation_confidence = report.confidence_score

        from .layout import success_panel
        renderer.print(success_panel(
            "Consultation Complete",
            f"Risk: [warning]{report.risk_assessment}[/warning]   "
            f"Confidence: [success]{report.confidence_score:.0%}[/success]\n"
            f"Recommended vectors: [silver]{', '.join(report.recommended_vectors) or '—'}[/silver]\n"
            f"Pacing: [silver]{report.pacing_recommendation}[/silver]\n"
            f"Reasoning: [silver]{report.ai_reasoning[:400]}[/silver]",
            width=renderer.width,
        ))


# ── Execution Screen ────────────────────────────────────────────────────────

class ExecutionScreen(Screen):
    """Live execution dashboard driven by the threaded ReportEngine."""

    def __init__(self, on_tick: Optional[Callable] = None):
        self.on_tick = on_tick

    def _ensure_engine(self, state: AppState) -> ReportEngine:
        engine = state.report_engine
        if engine is not None and not engine.finished:
            return engine

        engine = ReportEngine(
            target={
                "id": state.target_id,
                "name": state.nickname,
                "username": state.target,
                "sec_uid": state.sec_uid,
                "followers": state.followers,
                "following": state.following,
                "likes": state.likes,
                "country": state.country,
                "private": state.private,
                "bio": state.bio,
                "date": state.registered,
            },
            mode=state.run_mode,
            reason_code=state.chosen_reason,
            category=state.chosen_category,
            reason_label=state.chosen_label,
            delay_min=state.delay_min,
            delay_max=state.delay_max,
            is_random=state.is_random_delay,
            proxy_manager=state.proxy_manager,
            session_manager=RedisSessionManager(),
            sessions=state.session_pool,
            reasons=state.chosen_reasons,
            ai_client=state.ai_client or GeminiClient(),
        )
        state.report_engine = engine
        state.ai_client = engine.ai_client
        if state.ai_status in ("Offline", "—", ""):
            state.ai_status = "Connecting…"
        return engine

    def _sync(self, state: AppState, engine: ReportEngine, snap: dict) -> None:
        state.gg = snap["accepted"]
        state.bb = snap["rejected"]
        state.captcha_hits = snap["captcha_hits"]
        state.current_sleep = snap["current_sleep"]
        state.blocked_devices = engine.blocked_devices
        state.is_running_attack = snap["running"]
        if engine.start_time:
            state.script_start_time = engine.start_time
        if snap["proxy_stats"]:
            state.proxy_stats = snap["proxy_stats"]
            state.proxy_healthy = snap["proxy_healthy"]
            state.proxy_total = snap["proxy_total"]

        ai = snap.get("ai") or {}
        if ai:
            state.ai_status = str(ai.get("status", state.ai_status))
            state.ai_neural_calls = int(ai.get("total_calls", state.ai_neural_calls) or 0)
            state.ai_risk_level = str(ai.get("risk_level", "—"))
            state.ai_max_safe_rate = str(ai.get("safe_rate", "—"))
            state.ai_est_requests = str(ai.get("est_requests", "—"))
            state.ai_est_duration = str(ai.get("est_duration", "—"))
            state.ai_strategy = str(ai.get("strategy", "—"))
            state.ai_account_summary = str(ai.get("account_summary", "—"))
            state.ai_next_action = str(
                ai.get("next_action") or ai.get("last_decision") or "—"
            )
            last_sleep = ai.get("last_sleep")
            state.ai_adaptive_delay = (
                f"{float(last_sleep):.1f}s" if last_sleep else "—"
            )

        state.delay_display_str = (
            f"Jitter ({state.delay_min:.0f}s - {state.delay_max:.0f}s)"
            if state.is_random_delay
            else f"Fixed {state.delay_min:.0f}s"
        )

    def render(self, renderer: Renderer, state: AppState) -> None:
        renderer.clear()
        engine = state.report_engine
        requests = list(engine.request_log) if engine else []
        renderer.print(renderer.render_dashboard(state, requests, compact=True))

    def handle_input(
        self, renderer: Renderer, state: AppState
    ) -> Optional["Screen"]:
        engine = self._ensure_engine(state)
        if not engine.running and not engine.finished:
            engine.start()

        renderer.print("[muted]Live telemetry active — press [success]p[/success] to pause, [error]q[/error] to abort.[/muted]")

        try:
            with Live(
                renderer.render_dashboard(state, [], compact=True),
                console=renderer.console,
                refresh_per_second=4,
                transient=False,
            ) as live:
                while True:
                    snap = engine.snapshot()
                    self._sync(state, engine, snap)
                    live.update(
                        renderer.render_dashboard(state, snap["request_log"], compact=True)
                    )
                    if engine.finished:
                        break
                    key = _read_key(0.2)
                    if key in ("p", "P"):
                        engine.pause()
                        return PauseMenu()
                    if key in ("q", "Q", "\x03"):
                        engine.stop()
                        return ResultsScreen()
        except KeyboardInterrupt:
            engine.pause()
            return PauseMenu()

        return ResultsScreen()


# ── Pause Menu ──────────────────────────────────────────────────────────────

class PauseMenu(Screen):
    """Paused execution menu."""

    def render(self, renderer: Renderer, state: AppState) -> None:
        renderer.clear()
        renderer.print(renderer.render_logo())
        renderer.print()

        from .layout import info_panel
        renderer.print(info_panel(
            "Execution Paused",
            f"Total requests: [silver]{state.total}[/silver]\n"
            f"Accepted: [success]{state.gg}[/success] | "
            f"Deflected: [error]{state.bb}[/error]\n"
            f"Success rate: [success]{state.success_rate:.1f}%[/success]",
            width=renderer.width,
        ))
        renderer.print()

    def handle_input(
        self, renderer: Renderer, state: AppState
    ) -> Optional["Screen"]:
        renderer.print("[1] Resume Execution")
        renderer.print("[2] Reconfigure Pacing")
        renderer.print("[3] Toggle Pacing Mode")
        renderer.print("[4] Reconfigure Vectors")
        renderer.print("[5] Inspect Session Vault")
        renderer.print("[6] Abort & Exit")

        choice = renderer.prompt("Select action [1-6]", default="1")

        engine = state.report_engine
        if choice == "1":
            if engine:
                engine.resume()
            return ExecutionScreen()
        elif choice == "2":
            return ConfigScreen()
        elif choice == "3":
            state.is_random_delay = not state.is_random_delay
            if engine:
                engine.is_random = state.is_random_delay
            return self
        elif choice == "4":
            return ConfigScreen()
        elif choice == "5":
            return self
        elif choice == "6":
            if engine:
                engine.stop()
            state.is_running_attack = False
            return ResultsScreen()

        return self


# ── Results Screen ──────────────────────────────────────────────────────────

class ResultsScreen(Screen):
    """Final results screen after execution ends."""

    def render(self, renderer: Renderer, state: AppState) -> None:
        renderer.clear()
        renderer.print(renderer.render_logo())
        renderer.print()

        from .layout import success_panel, info_panel

        renderer.print(success_panel(
            "Operation Terminated",
            f"Total requests: [silver]{state.total}[/silver]\n"
            f"Accepted: [success]{state.gg}[/success] | "
            f"Deflected: [error]{state.bb}[/error]\n"
            f"Success rate: [success]{state.success_rate:.1f}%[/success]\n"
            f"Time elapsed: [silver]{state.elapsed}[/silver]",
            width=renderer.width,
        ))
        renderer.print()

    def handle_input(
        self, renderer: Renderer, state: AppState
    ) -> Optional["Screen"]:
        renderer.print("[1] Start New Operation")
        renderer.print("[2] Exit")

        choice = renderer.prompt("Select action [1-2]", default="2")

        if state.report_engine:
            state.report_engine.stop()
            state.report_engine = None
        state.is_running_attack = False

        if choice == "1":
            state.reset_counters()
            state.script_start_time = time.time()
            return WelcomeScreen(lambda t: None)
        return None