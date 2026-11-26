# -*- coding: utf-8 -*-
"""Proxy acquisition for high-speed bursts.

Two acquisition paths are supported:

``file``
    Read a proxy list from disk. Lines may carry a pre-measured latency
    (``host:port | 180``); anything slower than the latency ceiling is dropped
    without being dialled.

``pool``
    Pull candidates from a proxy pool URL (a plain-text endpoint returning one
    proxy per line) and keep only the endpoints that answer the health check
    faster than the latency ceiling. Fetched proxies are cached so a pool that
    rate-limits or briefly goes down does not stall a burst.

The ceiling exists because a burst fans out many simultaneous requests for a
single account: one slow hop serialises the whole batch, so a 300 ms budget is
enforced at acquisition time and again by the pool's own selector.
"""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Sequence
from urllib.request import Request, urlopen

from .models import ProxyConfig, load_proxy_sources
from .proxy_pool import AsyncProxyPool, NoProxyAvailable
from .transport import DEFAULT_IMPERSONATE, DEFAULT_TIMEOUT, build_client

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DEFAULT_PROXY_FILE = os.path.join(BASE_DIR, "data", "proxies.txt")

MAX_LATENCY_MS = 300.0
HEALTH_CHECK_URL = "https://httpbin.org/ip"
# Free public proxy lists are mostly HTTP-only (no CONNECT tunnel), while the
# report endpoint is HTTPS. A proxy is therefore probed against the HTTPS
# health URL and, when that fails, retried over plain HTTP so CONNECT-capable
# and forward-proxy-only entries can be told apart instead of lumped together.
HTTP_HEALTH_URL = "http://httpbin.org/ip"
DEFAULT_FETCH_TIMEOUT = 10.0

FetchFn = Callable[[str, float], Awaitable[str]]


class ProxySourceError(RuntimeError):
    """Raised when no usable proxy can be acquired for a burst."""


@dataclass
class ProxyCandidate:
    """A proxy plus the latency evidence used to admit or reject it."""

    config: ProxyConfig
    latency_ms: float | None = None

    @property
    def label(self) -> str:
        return self.config.label


@dataclass
class ProbeResult:
    """Outcome of one proxy health probe."""

    latency_ms: float
    https_tunnel: bool = True

    @property
    def usable_for_https(self) -> bool:
        return self.https_tunnel


@dataclass
class AcquiredProxies:
    """Result of a proxy acquisition, with the rejections kept for reporting."""

    proxies: list[ProxyConfig] = field(default_factory=list)
    source: str = "none"
    rejected: list[tuple[str, str]] = field(default_factory=list)
    latency_ms: dict[str, float] = field(default_factory=dict)
    # Proxies that only forward plain HTTP and cannot CONNECT-tunnel TLS.
    http_only: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.proxies)

    def summary(self) -> dict:
        return {
            "source": self.source,
            "usable": len(self.proxies),
            "rejected": len(self.rejected),
            "http_only": len(self.http_only),
            "max_latency_ms": round(max(self.latency_ms.values()), 1) if self.latency_ms else None,
        }


def _read_file_sync(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        return fh.read()


async def _url_fetch(url: str, timeout: float = DEFAULT_FETCH_TIMEOUT) -> str:
    def _get() -> str:
        request = Request(url, headers={"Accept": "text/plain,*/*"})
        with urlopen(request, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="ignore")

    return await asyncio.to_thread(_get)


def load_file_candidates(path: str) -> list[ProxyCandidate]:
    """Read proxies from a list file, honouring optional latency annotations."""
    if not os.path.exists(path):
        raise ProxySourceError(f"proxy file not found: {path}")
    return [ProxyCandidate(cfg, lat) for cfg, lat in load_proxy_sources(path)]


def parse_text_candidates(text: str) -> list[ProxyCandidate]:
    """Parse a fetched proxy-pool payload (one proxy per line)."""
    from .models import parse_proxy_line_with_latency

    out: list[ProxyCandidate] = []
    for raw in text.splitlines():
        cfg, latency = parse_proxy_line_with_latency(raw)
        if cfg is not None:
            out.append(ProxyCandidate(cfg, latency))
    return out


async def probe_latency(
    config: ProxyConfig,
    *,
    health_url: str = HEALTH_CHECK_URL,
    timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
    impersonate: str | None = DEFAULT_IMPERSONATE,
) -> float:
    """Measure a proxy's round-trip latency to the health URL, in milliseconds.

    Raises the transport exception when the proxy is unusable so the caller can
    record the reason.
    """
    client = build_client(config, timeout=timeout, impersonate=impersonate)
    started = time.monotonic()
    try:
        resp = await client.get(health_url, timeout=timeout)
        latency = (time.monotonic() - started) * 1000.0
        if resp.status_code != 200:
            raise ProxySourceError(f"health http {resp.status_code}")
        return latency
    finally:
        closer = getattr(client, "aclose", None) or getattr(client, "close", None)
        if closer is not None:
            try:
                result = closer()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass


async def probe_tunnel(
    config: ProxyConfig,
    *,
    health_url: str = HEALTH_CHECK_URL,
    http_health_url: str = HTTP_HEALTH_URL,
    timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
    impersonate: str | None = DEFAULT_IMPERSONATE,
) -> ProbeResult:
    """Probe a proxy, reporting whether it can tunnel HTTPS.

    HTTPS is tried first because that is what the report endpoint needs. A
    failure is retried over plain HTTP purely to classify the proxy: an entry
    that only forwards HTTP is still worth reporting as ``https_tunnel=False``
    rather than as fully dead, since the caller may run an HTTP-only target.
    """
    try:
        latency = await probe_latency(
            config, health_url=health_url, timeout=timeout, impersonate=impersonate
        )
        return ProbeResult(latency, https_tunnel=True)
    except Exception as https_error:
        try:
            latency = await probe_latency(
                config,
                health_url=http_health_url,
                timeout=timeout,
                impersonate=impersonate,
            )
        except Exception:
            raise https_error
        return ProbeResult(latency, https_tunnel=False)


async def filter_fast_candidates(
    candidates: Sequence[ProxyCandidate],
    *,
    max_latency_ms: float = MAX_LATENCY_MS,
    limit: int | None = None,
    concurrency: int = 25,
    health_url: str = HEALTH_CHECK_URL,
    timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
    impersonate: str | None = DEFAULT_IMPERSONATE,
    probe: Callable[..., Awaitable[Any]] = probe_latency,
    require_https: bool = True,
) -> AcquiredProxies:
    """Admit only proxies measured below ``max_latency_ms``.

    Candidates that already carry a latency annotation are judged without a
    network call. Everything else is probed concurrently; results are ordered
    fastest-first so the burst always starts on the snappiest exits.

    ``require_https`` (the default) drops proxies that only forward plain HTTP,
    because the report endpoint is TLS. They are listed in
    :attr:`AcquiredProxies.http_only` for diagnostics instead of being silently
    discarded.
    """
    acquired = AcquiredProxies()
    pending: list[ProxyCandidate] = []

    for candidate in candidates:
        if candidate.latency_ms is not None:
            if candidate.latency_ms <= max_latency_ms:
                acquired.latency_ms[candidate.label] = candidate.latency_ms
                pending.append(candidate)
            else:
                acquired.rejected.append(
                    (candidate.label, f"{candidate.latency_ms:.0f}ms > {max_latency_ms:.0f}ms")
                )
        else:
            pending.append(candidate)

    to_probe = [c for c in pending if c.latency_ms is None]
    sem = asyncio.Semaphore(max(1, concurrency))
    measured: dict[str, float] = {}

    async def guarded(candidate: ProxyCandidate) -> None:
        async with sem:
            try:
                outcome = await probe(
                    candidate.config,
                    health_url=health_url,
                    timeout=timeout,
                    impersonate=impersonate,
                )
            except Exception as exc:  # dead proxy, auth error, timeout, ...
                acquired.rejected.append(
                    (candidate.label, f"{type(exc).__name__}: {exc}"[:120])
                )
                return
            # Probes may return a bare latency or a ProbeResult.
            latency = getattr(outcome, "latency_ms", outcome)
            https_capable = getattr(outcome, "https_tunnel", True)
            measured[candidate.label] = latency
            if latency > max_latency_ms:
                acquired.rejected.append(
                    (candidate.label, f"{latency:.0f}ms > {max_latency_ms:.0f}ms")
                )
                return
            if require_https and not https_capable:
                acquired.http_only.append(candidate.label)
                return
            acquired.latency_ms[candidate.label] = latency

    if to_probe:
        await asyncio.gather(*(guarded(c) for c in to_probe))

    fast = [
        c for c in pending
        if acquired.latency_ms.get(c.label) is not None
    ]
    fast.sort(key=lambda c: acquired.latency_ms[c.label])
    if limit is not None and limit > 0:
        fast = fast[:limit]

    acquired.proxies = [c.config for c in fast]
    return acquired


class ProxySource:
    """Acquire a latency-filtered proxy set from a file or a pool endpoint.

    Args:
        file_path: Proxy list consulted first.
        pool_url: Endpoint polled when the file yields no usable proxy.
        max_latency_ms: Hard ceiling for a proxy to be admitted.
        limit: Maximum number of proxies to return (``None`` = all fast ones).
        fetch: Injectable fetcher for tests.
    """

    def __init__(
        self,
        file_path: str | None = None,
        pool_url: str | None = None,
        *,
        max_latency_ms: float = MAX_LATENCY_MS,
        limit: int | None = None,
        concurrency: int = 25,
        health_url: str = HEALTH_CHECK_URL,
        timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
        impersonate: str | None = DEFAULT_IMPERSONATE,
        fetch: FetchFn | None = None,
        probe: Callable[..., Awaitable[float]] = probe_latency,
        pool_cache_seconds: float = 30.0,
    ) -> None:
        self.file_path = file_path or DEFAULT_PROXY_FILE
        self.pool_url = (pool_url or "").strip() or None
        self.max_latency_ms = float(max_latency_ms)
        self.limit = limit
        self.concurrency = max(1, int(concurrency))
        self.health_url = health_url
        self.timeout = timeout
        self.impersonate = impersonate
        self._fetch = fetch or _url_fetch
        self._probe = probe
        self.pool_cache_seconds = max(0.0, float(pool_cache_seconds))
        self._pool_cache: tuple[float, list[ProxyCandidate]] | None = None

    # ── acquisition ──────────────────────────────────────────────────────────
    async def _from_file(self) -> AcquiredProxies:
        try:
            candidates = await asyncio.to_thread(load_file_candidates, self.file_path)
        except ProxySourceError:
            return AcquiredProxies(source="file")
        if not candidates:
            return AcquiredProxies(source="file")
        acquired = await self._filter(candidates)
        acquired.source = "file"
        return acquired

    async def _from_pool(self) -> AcquiredProxies:
        if not self.pool_url:
            return AcquiredProxies(source="pool")
        candidates = await self._pool_candidates()
        if not candidates:
            return AcquiredProxies(source="pool")
        acquired = await self._filter(candidates)
        acquired.source = "pool"
        return acquired

    async def _pool_candidates(self) -> list[ProxyCandidate]:
        now = time.monotonic()
        if self._pool_cache is not None:
            cached_at, cached = self._pool_cache
            if now - cached_at < self.pool_cache_seconds:
                return list(cached)
        try:
            text = await self._fetch(self.pool_url, DEFAULT_FETCH_TIMEOUT)
        except Exception:
            return []
        candidates = parse_text_candidates(text)
        if candidates:
            self._pool_cache = (now, candidates)
        return candidates

    async def _filter(self, candidates: Sequence[ProxyCandidate]) -> AcquiredProxies:
        return await filter_fast_candidates(
            candidates,
            max_latency_ms=self.max_latency_ms,
            limit=self.limit,
            concurrency=self.concurrency,
            health_url=self.health_url,
            timeout=self.timeout,
            impersonate=self.impersonate,
            probe=self._probe,
        )

    async def acquire(self) -> AcquiredProxies:
        """Return fast proxies from the file, falling back to the pool.

        A file with entries but no proxy under the ceiling counts as empty and
        triggers the pool fallback rather than failing the burst outright.
        """
        from_file = await self._from_file()
        if from_file.ok:
            return from_file

        from_pool = await self._from_pool()
        if from_pool.ok:
            # Keep the file-side rejections visible for diagnostics.
            from_pool.rejected = from_file.rejected + from_pool.rejected
            return from_pool

        merged = AcquiredProxies(source=from_file.source if not self.pool_url else "file+pool")
        merged.rejected = from_file.rejected + from_pool.rejected
        return merged


async def build_pool(
    proxies: Sequence[ProxyConfig],
    *,
    max_latency_ms: float = MAX_LATENCY_MS,
    per_proxy_concurrency: int = 8,
    allow_direct: bool = False,
    impersonate: str | None = DEFAULT_IMPERSONATE,
    request_timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
    on_disable: Callable[[str], None] | None = None,
    validate: bool = False,
) -> AsyncProxyPool:
    """Wrap acquired proxies in an :class:`AsyncProxyPool` with the same ceiling."""
    if not proxies and not allow_direct:
        raise NoProxyAvailable("no proxies available for burst")

    pool = AsyncProxyPool(
        proxies,
        allow_direct=allow_direct,
        per_proxy_concurrency=per_proxy_concurrency,
        impersonate=impersonate,
        request_timeout=request_timeout,
        max_latency_ms=max_latency_ms,
        on_disable=on_disable,
    )
    await pool.start(validate=validate)
    return pool
