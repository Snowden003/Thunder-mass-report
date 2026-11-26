# -*- coding: utf-8 -*-
"""Sliding-window request limiter for the synchronous report paths.

The report engine sends one request, then sleeps the configured pacing delay.
Pacing alone does not guarantee a hard budget: short delays, extra workers, or
retries can push the effective rate past what the operator intends.

``RequestLimiter`` enforces the rule "at most N requests within any rolling
T-second window". Unlike a token bucket it never allows a burst larger than the
budget, so a "100 requests / 10 minutes" cap really means 100 in 10 minutes.

Usage (engine loop)::

    if not limiter.acquire(stop_event, pause_event):
        continue   # stopped or paused while waiting

Usage (single request)::

    if limiter.try_acquire():
        send_request()
"""

from __future__ import annotations

import threading
import time
from collections import deque


class RequestLimiter:
    """Thread-safe sliding-window limiter.

    Args:
        max_requests: Requests allowed inside one window.
        window_seconds: Length of the rolling window in seconds.
        enabled: When ``False`` the limiter is a no-op and ``acquire`` always
            succeeds immediately.
    """

    def __init__(
        self,
        max_requests: int = 100,
        window_seconds: float = 600.0,
        enabled: bool = True,
    ) -> None:
        self._lock = threading.Lock()
        self._times: deque[float] = deque()
        self._max = 1
        self._window = 600.0
        self._enabled = True
        self.configure(max_requests, window_seconds, enabled)

    # ── configuration ───────────────────────────────────────────────────────

    def configure(
        self,
        max_requests: int | None = None,
        window_seconds: float | None = None,
        enabled: bool | None = None,
    ) -> None:
        """Update any subset of the limiter settings at runtime.

        ``max_requests`` is the hard report cap for the whole run; once that
        many requests have been logged the engine stops. ``window_seconds`` is
        only kept for the snapshot/UI window label.
        """
        with self._lock:
            if max_requests is not None:
                self._max = max(1, int(max_requests))
            if window_seconds is not None:
                self._window = max(0.1, float(window_seconds))
            if enabled is not None:
                self._enabled = bool(enabled)

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def max_requests(self) -> int:
        return self._max

    @property
    def window_seconds(self) -> float:
        return self._window

    # ── internals ───────────────────────────────────────────────────────────

    def _prune(self, now: float) -> None:
        """Drop timestamps that have fallen out of the window. Caller holds lock."""
        window = self._window
        while self._times and (now - self._times[0]) >= window:
            self._times.popleft()

    def _wait_locked(self, now: float) -> float:
        """Seconds until the next slot opens. Caller holds lock."""
        self._prune(now)
        if len(self._times) < self._max:
            return 0.0
        return max(0.0, self._window - (now - self._times[0]))

    # ── public API ──────────────────────────────────────────────────────────

    def wait_seconds(self) -> float:
        """Seconds the next request must wait (0 when a slot is free)."""
        if not self._enabled:
            return 0.0
        with self._lock:
            return self._wait_locked(time.time())

    def try_acquire(self) -> bool:
        """Claim a slot without blocking. Returns ``False`` when the window is full."""
        if not self._enabled:
            return True
        with self._lock:
            now = time.time()
            self._prune(now)
            if len(self._times) < self._max:
                self._times.append(now)
                return True
            return False

    def acquire(self, stop_event=None, pause_event=None, poll: float = 0.1) -> bool:
        """Block until a slot is free and claim it.

        Returns ``True`` when a slot was claimed, ``False`` if ``stop_event`` or
        ``pause_event`` was set while waiting (so the caller can abort cleanly).
        """
        if not self._enabled:
            return True
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            if pause_event is not None and pause_event.is_set():
                return False

            wait = self.wait_seconds()
            if wait <= 0.0:
                if self.try_acquire():
                    return True
                continue

            deadline = time.time() + wait
            while True:
                if stop_event is not None and stop_event.is_set():
                    return False
                if pause_event is not None and pause_event.is_set():
                    return False
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(poll, remaining))

    def used(self) -> int:
        """Number of requests recorded in the current window."""
        with self._lock:
            self._prune(time.time())
            return len(self._times)

    def remaining(self) -> int:
        """Slots left in the window, or ``-1`` when the limiter is disabled."""
        if not self._enabled:
            return -1
        with self._lock:
            self._prune(time.time())
            return max(0, self._max - len(self._times))

    def reset(self) -> None:
        """Forget all recorded requests."""
        with self._lock:
            self._times.clear()

    def snapshot(self) -> dict:
        """Serialisable view for UIs / logs."""
        with self._lock:
            now = time.time()
            self._prune(now)
            used = len(self._times)
            wait = self._wait_locked(now)
            return {
                "enabled": self._enabled,
                "max_requests": self._max,
                "window_seconds": self._window,
                "used": used,
                "remaining": max(0, self._max - used) if self._enabled else -1,
                "wait_seconds": round(wait, 1),
            }
