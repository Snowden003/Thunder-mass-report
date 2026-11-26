# -*- coding: utf-8 -*-
"""Session / cookie rotation pool for the async engine.

Provides pluggable rotation strategies and integrates with the existing
saved_sessions.json format and RedisSessionManager.
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .models import Result


class RotationStrategy(str, Enum):
    """Session selection strategy."""

    ROUND_ROBIN = "round_robin"
    LEAST_USED = "least_used"
    SCORED = "scored"
    RANDOM = "random"


class NoSessionAvailable(RuntimeError):
    """Raised when every session in the pool is disabled or the pool is empty."""


@dataclass(slots=True)
class SessionConfig:
    """A single session identity with cookies and metadata."""

    cookies: dict[str, str]
    headers: dict[str, str] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    # Identity fields (for logging/selection)
    identifier: str = ""
    label: str = ""
    user_id: str = ""
    sessionid: str = ""

    # Runtime stats
    uses: int = 0
    successes: int = 0
    failures: int = 0
    last_used: float = 0.0
    last_success: float = 0.0
    disabled: bool = False
    disable_reason: str = ""

    @property
    def success_rate(self) -> float:
        total = self.successes + self.failures
        return (self.successes / total) if total else 1.0

    @property
    def score(self) -> float:
        """Composite score for SCORED strategy (higher = better)."""
        if self.disabled:
            return -1.0
        # Weight: 70% success rate, 30% recency (inverse of time since last success)
        recency = 1.0 / (1.0 + (time.monotonic() - self.last_success) / 3600.0) if self.last_success else 0.5
        return 0.7 * self.success_rate + 0.3 * recency


@dataclass
class SessionHandle:
    """Opaque handle returned when acquiring a session."""

    config: SessionConfig
    pool: "SessionPool"

    def __enter__(self) -> "SessionHandle":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        # Release is handled explicitly by the engine
        pass


class SessionPool:
    """Concurrency-safe session pool with pluggable rotation strategies."""

    def __init__(
        self,
        sessions: list[SessionConfig],
        *,
        strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
        on_disable: Callable[[SessionConfig, str], None] | None = None,
        seed: int | None = None,
    ) -> None:
        self._sessions = sessions
        self._strategy = strategy
        self._on_disable = on_disable
        self._rng = random.Random(seed)
        self._index = 0  # for round-robin
        self._lock = asyncio.Lock()

    # ── Public API ─────────────────────────────────────────────────────────────

    async def acquire(self) -> SessionHandle:
        """Get the next session per the configured strategy."""
        async with self._lock:
            session = self._select()
            if session is None:
                raise NoSessionAvailable("No available sessions in pool")
            session.uses += 1
            session.last_used = time.monotonic()
            return SessionHandle(config=session, pool=self)

    async def release(self, handle: SessionHandle, result: Result | None) -> None:
        """Record outcome and optionally disable compromised sessions.

        ``result`` may be ``None`` (e.g. a caller that only borrowed a session
        for rendering); nothing is recorded in that case.
        """
        if result is None:
            return
        session = handle.config
        if result.outcome.success:
            session.successes += 1
            session.last_success = time.monotonic()
        else:
            session.failures += 1

    def mark_compromised(self, handle: SessionHandle, reason: str) -> None:
        """Permanently disable a session (e.g., on CAPTCHA, auth failure)."""
        session = handle.config
        if not session.disabled:
            session.disabled = True
            session.disable_reason = reason
            if self._on_disable:
                self._on_disable(session, reason)

    def get_available(self) -> list[SessionConfig]:
        """Return all non-disabled sessions."""
        return [s for s in self._sessions if not s.disabled]

    def stats(self) -> dict[str, Any]:
        """Return pool statistics."""
        available = self.get_available()
        return {
            "total": len(self._sessions),
            "available": len(available),
            "disabled": len(self._sessions) - len(available),
            "strategy": self._strategy.value,
            "sessions": [
                {
                    "label": s.label or s.identifier,
                    "identifier": s.identifier,
                    "uses": s.uses,
                    "successes": s.successes,
                    "failures": s.failures,
                    "success_rate": round(s.success_rate, 3),
                    "disabled": s.disabled,
                    "disable_reason": s.disable_reason,
                }
                for s in self._sessions
            ],
        }

    # ── Internal selection logic ──────────────────────────────────────────────

    def _select(self) -> SessionConfig | None:
        available = self.get_available()
        if not available:
            return None

        if self._strategy == RotationStrategy.ROUND_ROBIN:
            return self._select_round_robin(available)
        elif self._strategy == RotationStrategy.LEAST_USED:
            return self._select_least_used(available)
        elif self._strategy == RotationStrategy.SCORED:
            return self._select_scored(available)
        elif self._strategy == RotationStrategy.RANDOM:
            return self._rng.choice(available)
        return available[0]

    def _select_round_robin(self, available: list[SessionConfig]) -> SessionConfig:
        session = available[self._index % len(available)]
        self._index += 1
        return session

    def _select_least_used(self, available: list[SessionConfig]) -> SessionConfig:
        return min(available, key=lambda s: s.uses)

    def _select_scored(self, available: list[SessionConfig]) -> SessionConfig:
        # Weighted random by score
        scores = [max(0.01, s.score) for s in available]
        total = sum(scores)
        r = self._rng.random() * total
        cumulative = 0.0
        for session, score in zip(available, scores):
            cumulative += score
            if r <= cumulative:
                return session
        return available[-1]

    # ── Factory methods ───────────────────────────────────────────────────────

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
        only_active: bool = True,
        on_disable: Callable[[SessionConfig, str], None] | None = None,
        seed: int | None = None,
    ) -> "SessionPool":
        """Load sessions from saved_sessions.json format."""
        sessions = load_sessions_from_file(path, only_active=only_active)
        return cls(sessions, strategy=strategy, on_disable=on_disable, seed=seed)

    @classmethod
    def from_redis(
        cls,
        redis_manager,  # RedisSessionManager instance
        *,
        strategy: RotationStrategy = RotationStrategy.ROUND_ROBIN,
        on_disable: Callable[[SessionConfig, str], None] | None = None,
        seed: int | None = None,
    ) -> "SessionPool":
        """Load sessions from RedisSessionManager."""
        sessions = load_sessions_from_redis(redis_manager)
        return cls(sessions, strategy=strategy, on_disable=on_disable, seed=seed)


# ── Loaders ───────────────────────────────────────────────────────────────────


def load_sessions_from_file(path: str | Path, *, only_active: bool = True) -> list[SessionConfig]:
    """Parse saved_sessions.json into SessionConfig objects."""
    path = Path(path)
    if not path.exists():
        return []

    raw = path.read_text(encoding="utf-8", errors="ignore").strip()
    if not raw:
        return []
    data = json.loads(raw)
    if isinstance(data, dict):
        data = data.get("sessions") or []
    if not isinstance(data, list):
        return []

    sessions: list[SessionConfig] = []
    for entry in data:
        if not isinstance(entry, dict):
            continue
        if only_active and entry.get("status") != "active":
            continue

        cookies_dict = entry.get("cookies_dict", {})
        if not cookies_dict:
            continue

        session = SessionConfig(
            cookies=cookies_dict,
            headers={},
            meta={
                "followers": entry.get("followers", 0),
                "likes": entry.get("likes", 0),
                "source_file": entry.get("source_file", ""),
                "added_at": entry.get("added_at", 0),
                "last_verified": entry.get("last_verified", 0),
            },
            identifier=entry.get("identifier", ""),
            label=entry.get("label", entry.get("identifier", "")),
            user_id=entry.get("user_id", "") or entry.get("uid_tt", ""),
            sessionid=entry.get("sessionid", "") or cookies_dict.get("sessionid", ""),
        )
        sessions.append(session)

    return sessions


def load_sessions_from_redis(redis_manager) -> list[SessionConfig]:
    """Load active sessions from RedisSessionManager."""
    sessions: list[SessionConfig] = []
    try:
        active = redis_manager.get_active_sessions()
        for entry in active:
            if not isinstance(entry, dict):
                continue
            cookies_dict = entry.get("cookies_dict", {})
            if not cookies_dict:
                continue

            session = SessionConfig(
                cookies=cookies_dict,
                headers={},
                meta={
                    "followers": entry.get("followers", 0),
                    "likes": entry.get("likes", 0),
                },
                identifier=entry.get("identifier", ""),
                label=entry.get("label", entry.get("identifier", "")),
                user_id=entry.get("user_id", "") or cookies_dict.get("uid_tt", ""),
                sessionid=entry.get("sessionid", "") or cookies_dict.get("sessionid", ""),
            )
            sessions.append(session)
    except Exception:
        pass
    return sessions