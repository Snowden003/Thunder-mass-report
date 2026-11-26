# -*- coding: utf-8 -*-
"""Ranked, shuffled account rotation for the report engine."""

from __future__ import annotations

import random
from typing import Any, Iterable


class AccountRotation:
    """Build a repeatable request schedule from ranked account sessions.

    Accounts are ranked by followers and likes, split into primary and
    secondary groups, shuffled within their groups, and emitted in cycles:

    * each primary account gets ``primary_rounds`` requests;
    * ``secondary_interleave`` secondary accounts get one request each;
    * after all primary accounts, every secondary gets ``secondary_rounds``;
    * a new cycle reshuffles both groups.
    """

    def __init__(
        self,
        sessions: Iterable[dict[str, Any]] | None = None,
        *,
        primary_count: int = 10,
        primary_rounds: int = 3,
        secondary_interleave: int = 3,
        secondary_rounds: int = 3,
        seed: int | None = None,
    ) -> None:
        self.primary_count = max(1, int(primary_count))
        self.primary_rounds = max(1, int(primary_rounds))
        self.secondary_interleave = max(0, int(secondary_interleave))
        self.secondary_rounds = max(1, int(secondary_rounds))
        self._rng = random.Random(seed)
        self._sessions: list[dict[str, Any]] = []
        self._cycle: list[dict[str, Any]] = []
        self._position = 0
        self._primary_count = 0
        self._secondary_count = 0
        self.set_sessions(sessions or [])

    @staticmethod
    def _number(value: Any) -> int:
        try:
            return int(str(value or 0).replace(",", ""))
        except (TypeError, ValueError):
            return 0

    @classmethod
    def _strength(cls, session: dict[str, Any]) -> tuple[int, int]:
        return (
            cls._number(session.get("followers", 0)),
            cls._number(session.get("likes", 0)),
        )

    def set_sessions(self, sessions: Iterable[dict[str, Any]]) -> None:
        """Replace the available sessions and start a fresh shuffled cycle."""
        self._sessions = [session for session in sessions if isinstance(session, dict)]
        self._build_cycle()

    def _build_cycle(self) -> None:
        ranked = sorted(self._sessions, key=self._strength, reverse=True)

        if len(ranked) > 1:
            # Preserve at least one secondary account whenever possible.
            primary_count = min(self.primary_count, len(ranked) - 1)
        else:
            primary_count = len(ranked)

        primaries = ranked[:primary_count]
        secondaries = ranked[primary_count:]
        self._rng.shuffle(primaries)
        self._rng.shuffle(secondaries)

        cycle: list[dict[str, Any]] = []
        if primaries:
            for primary_index, primary in enumerate(primaries):
                cycle.extend([primary] * self.primary_rounds)
                if secondaries and self.secondary_interleave:
                    start = primary_index * self.secondary_interleave
                    for offset in range(self.secondary_interleave):
                        cycle.append(secondaries[(start + offset) % len(secondaries)])

        # Once the primary rotation is complete, rotate all secondary accounts
        # in the shuffled order with the same per-account round size.
        for secondary in secondaries:
            cycle.extend([secondary] * self.secondary_rounds)

        # If primary_count is zero in a future configuration, the secondary
        # phase above still provides a valid schedule. With no sessions there
        # is intentionally no schedule and callers can fall back to anonymous.
        self._cycle = cycle
        self._position = 0
        self._primary_count = len(primaries)
        self._secondary_count = len(secondaries)

    def next_account(self) -> dict[str, Any] | None:
        """Return the next scheduled account, rebuilding cycles as needed."""
        if not self._sessions:
            return None
        if self._position >= len(self._cycle):
            self._build_cycle()
        if not self._cycle:
            return None
        account = self._cycle[self._position]
        self._position += 1
        return account

    def describe(self) -> dict[str, int]:
        """Return scheduler counts useful for diagnostics and tests."""
        return {
            "sessions": len(self._sessions),
            "primaries": self._primary_count,
            "secondaries": self._secondary_count,
            "cycle_length": len(self._cycle),
            "position": self._position,
        }
