# -*- coding: utf-8 -*-
"""
Centralized State Management for THUNDER Reporter.

Replaces all global variables from report-tiktok.py with a single,
explicitly-passed state object. This eliminates hidden dependencies
and makes the code testable and maintainable.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class AppState:
    """
    Central application state for the THUNDER reporter.

    All mutable state lives here. Screens and components read from
    this object; the renderer formats it for display.
    """

    # ── Target Information ──────────────────────────────────────────
    target: str = ""
    target_id: str = "—"
    nickname: str = "—"
    followers: str = "—"
    following: str = "—"
    likes: str = "—"
    country: str = "—"
    private: str = "—"
    registered: str = "—"
    bio: str = "—"
    sec_uid: str = ""

    # ── Operating Mode ──────────────────────────────────────────────
    run_mode: str = "mobile"  # "mobile" or "web"
    chosen_reason: str = "90084"
    chosen_category: str = "porn"
    chosen_label: str = "Nudity & Sexual Content / Pornography"
    chosen_reasons: list = field(default_factory=list)  # list of (code, category, label)

    # ── Session & Proxy ─────────────────────────────────────────────
    session_pool: list = field(default_factory=list)
    web_cookies: dict = field(default_factory=dict)
    user_sessionid: str = ""
    proxy_manager: Any = None
    proxy_stats: str = "Direct (no proxy)"
    proxy_healthy: int = 0
    proxy_total: int = 0

    # ── Pacing / Delay ──────────────────────────────────────────────
    request_delay: float = 5.0
    delay_min: float = 5.0
    delay_max: float = 17.0
    is_random_delay: bool = True
    delay_display_str: str = "Jitter (5s - 17s)"
    current_sleep: float = 0.0

    # ── Counters ────────────────────────────────────────────────────
    gg: int = 0          # good (accepted) reports
    bb: int = 0          # bad (rejected) reports
    captcha_hits: int = 0
    consecutive_successes: int = 0
    consecutive_failures: int = 0

    # ── Timing ──────────────────────────────────────────────────────
    script_start_time: float = field(default_factory=time.time)
    current_attack_log_file: str = ""

    # ── Device / Cooldown ───────────────────────────────────────────
    blocked_devices: dict = field(default_factory=dict)
    block_duration: int = 600  # 10 minutes

    # ── AI / Neural ─────────────────────────────────────────────────
    ai_client: Any = None
    ai_status: str = "Offline"
    ai_neural_calls: int = 0
    ai_risk_level: str = "—"
    ai_max_safe_rate: str = "—"
    ai_est_requests: str = "—"
    ai_est_duration: str = "—"
    ai_strategy: str = "—"
    ai_account_summary: str = "—"
    ai_next_action: str = "—"
    ai_adaptive_delay: str = "—"

    # ── Consultation Results ────────────────────────────────────────
    consultation_risk: str = "—"
    consultation_post_types: list = field(default_factory=list)
    consultation_recommended_vectors: list = field(default_factory=list)
    consultation_avoid_vectors: list = field(default_factory=list)
    consultation_pacing: str = "—"
    consultation_session_rotation: bool = False
    consultation_device_mutation: bool = False
    consultation_reasoning: str = "—"
    consultation_confidence: float = 0.0

    # ── Telegram / Remote ───────────────────────────────────────────
    telegram_bot: Any = None
    telegram_bot_username: str = ""

    # ── Execution Control ───────────────────────────────────────────
    is_running_attack: bool = False
    report_engine: Any = None
    next_vector_override: Optional[tuple] = None
    force_device_mutation: bool = False
    force_session_rotation: bool = False
    current_session_index: int = 0

    # ── Derived / Computed ──────────────────────────────────────────

    @property
    def total(self) -> int:
        return self.gg + self.bb

    @property
    def success_rate(self) -> float:
        return (self.gg / self.total * 100) if self.total else 0.0

    @property
    def elapsed(self) -> str:
        elapsed_sec = int(time.time() - self.script_start_time)
        eh, er = divmod(elapsed_sec, 3600)
        em, es = divmod(er, 60)
        return f"{eh:02d}:{em:02d}:{es:02d}"

    @property
    def active_cooldowns(self) -> int:
        now = time.time()
        return sum(1 for exp in self.blocked_devices.values() if now < exp)

    @property
    def session_list(self) -> list:
        """Return session list formatted for display."""
        result = []
        for s in self.session_pool:
            result.append({
                "label": s.get("label", s.get("identifier", "Unnamed")),
                "identifier": s.get("identifier", ""),
                "sessionid": s.get("sessionid", ""),
            })
        return result

    def reset_counters(self) -> None:
        """Reset attack counters."""
        self.gg = 0
        self.bb = 0
        self.captcha_hits = 0
        self.consecutive_successes = 0
        self.consecutive_failures = 0

    def increment_success(self) -> None:
        self.gg += 1
        self.consecutive_successes += 1
        self.consecutive_failures = 0

    def increment_failure(self) -> None:
        self.bb += 1
        self.consecutive_failures += 1
        self.consecutive_successes = 0

    def block_device(self, device_name: str) -> None:
        """Block a device model for the configured duration."""
        self.blocked_devices[device_name] = time.time() + self.block_duration

    def is_device_blocked(self, device_name: str) -> bool:
        """Check if a device model is currently blocked."""
        now = time.time()
        blocked_until = self.blocked_devices.get(device_name, 0)
        return now < blocked_until

    def to_dict(self) -> dict:
        """Export state as a dictionary (for logging/debugging)."""
        return {
            "target": self.target,
            "target_id": self.target_id,
            "run_mode": self.run_mode,
            "chosen_reason": self.chosen_reason,
            "gg": self.gg,
            "bb": self.bb,
            "total": self.total,
            "success_rate": self.success_rate,
            "elapsed": self.elapsed,
            "captcha_hits": self.captcha_hits,
            "active_cooldowns": self.active_cooldowns,
            "request_delay": self.request_delay,
            "delay_min": self.delay_min,
            "delay_max": self.delay_max,
            "is_random_delay": self.is_random_delay,
        }