# -*- coding: utf-8 -*-
"""Async proxy pool: health checks, scoring and per-proxy client isolation.

Proxy settings live on the client/session, so every proxy owns exactly one
``curl_cffi.requests.AsyncSession``. That keeps connection pools and TLS
fingerprints isolated and makes disabling a bad proxy cheap.
"""

from __future__ import annotations

import asyncio
import inspect
import math
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable

from .models import Outcome, ProxyConfig, Result
from .transport import DEFAULT_IMPERSONATE, DEFAULT_TIMEOUT, DEFAULT_USER_AGENT, build_client

SUCCESS_WEIGHT = 0.4
LATENCY_WEIGHT = 0.6
MAX_LATENCY_MS = 500.0


async def _close_client(client: Any) -> None:
    """Close either a curl_cffi AsyncSession or an httpx AsyncClient."""
    for name in ("aclose", "close"):
        closer = getattr(client, name, None)
        if closer is None:
            continue
        try:
            result = closer()
            if inspect.isawaitable(result):
                await result
        except Exception:
            pass
        return


class NoProxyAvailable(RuntimeError):
    """Raised when every proxy is disabled or the pool is empty."""


@dataclass
class ProxyHandle:
    state: "ProxyState"
    released: bool = False

    @property
    def client(self) -> Any:
        return self.state.client

    @property
    def label(self) -> str:
        return self.state.label


@dataclass
class ProxyState:
    config: ProxyConfig | None
    client: Any
    semaphore: asyncio.Semaphore
    label: str
    successes: int = 0
    failures: int = 0          # hard failures (auth/refused/explicit proxy error)
    soft_failures: int = 0     # transient transport glitches (malformed socks reply, reset)
    neutral: int = 0
    latency_ms: float = math.inf
    enabled: bool = True
    inflight: int = 0
    last_error: str = ""
    max_latency_ms: float = MAX_LATENCY_MS

    @property
    def attempts(self) -> int:
        return self.successes + self.failures + self.soft_failures

    @property
    def within_latency_limit(self) -> bool:
        """Eligible until a measurement proves the latency is too high."""
        return not math.isfinite(self.latency_ms) or self.latency_ms <= self.max_latency_ms

    @property
    def success_rate(self) -> float:
        return (self.successes / self.attempts) if self.attempts else 1.0

    @property
    def score(self) -> float:
        if not self.enabled:
            return -1.0
        latency_score = (
            1.0 / (1.0 + self.latency_ms / 1000.0)
            if math.isfinite(self.latency_ms)
            else 0.0
        )
        return SUCCESS_WEIGHT * self.success_rate + LATENCY_WEIGHT * latency_score

    def record_success(self, latency_ms: float) -> None:
        self.latency_ms = latency_ms
        self.successes += 1
        self.failures = 0
        self.soft_failures = 0
        self.last_error = ""
        self.enabled = True  # a health-check success can revive a disabled proxy

    def record_failure(self, error: str = "", *, soft: bool = False) -> None:
        if soft:
            self.soft_failures += 1
        else:
            self.failures += 1
        self.last_error = error

    def record_neutral(self) -> None:
        self.neutral += 1


class AsyncProxyPool:
    """Concurrency-safe (single event loop) proxy pool with scoring."""

    def __init__(
        self,
        proxies: Iterable[ProxyConfig],
        *,
        allow_direct: bool = False,
        health_url: str = "https://httpbin.org/ip",
        health_timeout: float = 15.0,
        max_failures: int = 3,
        max_soft_failures: int = 10,
        per_proxy_concurrency: int = 2,
        request_timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
        user_agent: str | None = DEFAULT_USER_AGENT,
        impersonate: str | None = DEFAULT_IMPERSONATE,
        on_disable: Callable[[str], None] | None = None,
        max_latency_ms: float = MAX_LATENCY_MS,
    ) -> None:
        self._configs = list(proxies)
        self._allow_direct = allow_direct
        self._health_url = health_url
        self._health_timeout = health_timeout
        self._max_failures = max(1, int(max_failures))
        self._max_soft_failures = max(1, int(max_soft_failures))
        self._per_proxy_concurrency = max(1, int(per_proxy_concurrency))
        self._request_timeout = request_timeout
        self._user_agent = user_agent
        self._impersonate = impersonate
        self._on_disable = on_disable
        self._max_latency_ms = max_latency_ms
        self._states: list[ProxyState] = []
        self._started = False

    # ── lifecycle ─────────────────────────────────────────────────────────────
    async def start(self, *, validate: bool = True, health_concurrency: int = 10) -> "AsyncProxyPool":
        if self._started:
            return self
        if not self._configs and not self._allow_direct:
            raise ValueError("No proxies supplied and allow_direct is False")

        configs: list[ProxyConfig | None] = list(self._configs)
        if self._allow_direct and not configs:
            configs = [None]

        for cfg in configs:
            self._states.append(
                ProxyState(
                    config=cfg,
                    client=build_client(
                        cfg,
                        timeout=self._request_timeout,
                        max_connections=max(10, self._per_proxy_concurrency * 2),
                        max_keepalive_connections=2,
                        user_agent=self._user_agent,
                        impersonate=self._impersonate,
                    ),
                    semaphore=asyncio.Semaphore(self._per_proxy_concurrency),
                    label=cfg.label if cfg else "direct",
                    max_latency_ms=self._max_latency_ms,
                )
            )
        self._started = True

        if validate:
            await self.check_all(concurrency=health_concurrency)
            if not any(s.enabled for s in self._states):
                raise NoProxyAvailable("No proxies passed the health check")
        return self

    async def close(self) -> None:
        for state in self._states:
            await _close_client(state.client)
        self._states.clear()
        self._started = False

    # ── selection ─────────────────────────────────────────────────────────────
    async def acquire(self) -> ProxyHandle:
        """Pick the best *idle* proxy; fall back to the best busy one.

        Blocking on a single top-scored proxy while other proxies sit idle is
        the classic head-of-line bottleneck, so every candidate whose
        semaphore already has capacity is tried first (in score order). The
        check and the acquire happen without an intervening await point, so
        the fast path never suspends.
        """
        while True:
            enabled = [s for s in self._states if s.enabled and s.within_latency_limit]
            if not enabled:
                raise NoProxyAvailable("Every proxy is disabled or above the latency limit")
            ordered = sorted(enabled, key=lambda s: s.score, reverse=True)
            state = next((s for s in ordered if not s.semaphore.locked()), ordered[0])
            await state.semaphore.acquire()
            if state.enabled:
                state.inflight += 1
                return ProxyHandle(state)
            state.semaphore.release()

    async def release(self, handle: ProxyHandle, result: Result) -> None:
        if handle.released:
            return  # already released: never over-fill the semaphore
        handle.released = True
        state = handle.state
        state.inflight = max(0, state.inflight - 1)
        if result.outcome.proxy_fault:
            error = (result.error or result.outcome.value)
            lowered = error.lower()
            soft = (
                result.outcome in (Outcome.TIMEOUT, Outcome.NETWORK)
                or "socksio" in lowered
                or "malformed" in lowered
                or "protocolerror" in lowered
                or "reset" in lowered
            )
            state.record_failure(error, soft=soft)
            limit = self._max_soft_failures if soft else self._max_failures
            count = state.soft_failures if soft else state.failures
            if count >= limit and state.enabled:
                state.enabled = False
                if self._on_disable is not None:
                    self._on_disable(state.label)
        elif result.outcome.success:
            state.record_success(result.latency_ms)
        else:
            state.record_neutral()
        state.semaphore.release()

    # ── health checks ─────────────────────────────────────────────────────────
    async def _check_one(self, state: ProxyState) -> bool:
        if state.config is None:
            return True
        started = time.monotonic()
        try:
            resp = await state.client.get(
                self._health_url,
                timeout=self._health_timeout,
            )
            latency = (time.monotonic() - started) * 1000.0
            if resp.status_code == 200 and latency <= self._max_latency_ms:
                state.record_success(latency)
                return True
            if resp.status_code == 200:
                state.record_failure(
                    f"latency {latency:.0f}ms > {self._max_latency_ms:.0f}ms"
                )
                return False
            state.record_failure(f"health http {resp.status_code}")
            return False
        except Exception as exc:
            state.record_failure(f"{type(exc).__name__}: {exc}"[:160])
            return False

    async def check_all(self, *, concurrency: int = 10) -> int:
        sem = asyncio.Semaphore(max(1, concurrency))

        async def guarded(state: ProxyState) -> bool:
            async with sem:
                return await self._check_one(state)

        results = await asyncio.gather(*(guarded(s) for s in self._states))
        return sum(1 for ok in results if ok)

    async def health_loop(self, stop_event: asyncio.Event, interval: float = 300.0) -> None:
        while not stop_event.is_set():
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
            await self.check_all()

    # ── introspection ─────────────────────────────────────────────────────────
    @property
    def size(self) -> int:
        return len(self._states)

    @property
    def healthy(self) -> int:
        return sum(1 for s in self._states if s.enabled)

    def snapshot(self) -> list[dict]:
        return [
            {
                "label": s.label,
                "enabled": s.enabled,
                "score": round(s.score, 3),
                "success_rate": round(s.success_rate, 3),
                "latency_ms": None if not math.isfinite(s.latency_ms) else round(s.latency_ms),
                "inflight": s.inflight,
                "successes": s.successes,
                "failures": s.failures,
                "soft_failures": s.soft_failures,
                "neutral": s.neutral,
                "last_error": s.last_error,
            }
            for s in self._states
        ]

    def stats_line(self) -> str:
        healthy = self.healthy
        total = self.size
        best = max((s for s in self._states if s.enabled), key=lambda s: s.score, default=None)
        if best is None:
            return f"proxies={healthy}/{total} best=N/A"
        lat = "?" if not math.isfinite(best.latency_ms) else f"{best.latency_ms:.0f}ms"
        return f"proxies={healthy}/{total} best=[{best.label} lat={lat}]"
