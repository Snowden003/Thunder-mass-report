# -*- coding: utf-8 -*-
"""Async token bucket used to enforce a global requests-per-minute budget.

The sync script's pacing delay is global, so running N workers naively
multiplies the effective request rate by N. The bucket keeps the rate honest
regardless of worker count.
"""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    """Classic token bucket with an async ``acquire``.

    ``rate_per_minute`` is the sustained rate; ``burst`` is the bucket size.
    A rate <= 0 disables throttling.
    """

    def __init__(self, rate_per_minute: float, burst: float | None = None) -> None:
        self._rate_per_second = max(0.0, float(rate_per_minute)) / 60.0
        self._burst_explicit = burst is not None
        self._burst = float(burst) if burst is not None else max(1.0, self._rate_per_second)
        self._tokens = self._burst
        self._updated = time.monotonic()
        self._lock = asyncio.Lock()
        self._disabled = self._rate_per_second <= 0.0

    @property
    def rate_per_minute(self) -> float:
        return self._rate_per_second * 60.0

    @property
    def burst(self) -> float:
        return self._burst

    @property
    def disabled(self) -> bool:
        return self._disabled

    def set_rate(self, rate_per_minute: float) -> None:
        self._rate_per_second = max(0.0, float(rate_per_minute)) / 60.0
        self._disabled = self._rate_per_second <= 0.0
        if not self._burst_explicit:
            self._burst = max(1.0, self._rate_per_second)
        self._tokens = min(self._tokens, self._burst)

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self._updated
        if elapsed > 0:
            self._tokens = min(self._burst, self._tokens + elapsed * self._rate_per_second)
            self._updated = now

    async def acquire(self, tokens: float = 1.0) -> None:
        """Wait until ``tokens`` are available and consume them.

        ``tokens`` larger than the bucket capacity can never be satisfied, so
        it is clamped to the burst size instead of spinning forever.
        """
        if self._disabled:
            return
        want = min(max(0.0, tokens), self._burst)
        while True:
            async with self._lock:
                self._refill()
                if self._tokens >= want:
                    self._tokens -= want
                    return
                deficit = want - self._tokens
                wait = deficit / self._rate_per_second
            # Never sleep zero: deficit > 0 always implies wait > 0, but guard
            # against clock/float edge cases so we cannot busy-spin.
            await asyncio.sleep(max(wait, 0.001))
