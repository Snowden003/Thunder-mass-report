# -*- coding: utf-8 -*-
"""Single-account high-speed burst.

The regular engine walks a job queue one proxy permit at a time. A burst is the
opposite shape: many simultaneous requests aimed at **one** account, each from a
different exit IP, so the fan-out is limited by proxy supply rather than by
worker count.

Design notes:

* Only proxies measured under ``max_latency_ms`` (300 ms by default) are used,
  and the pool enforces the same ceiling before every acquire.
* The session (cookies/headers) is resolved once and shared by every request,
  so all hits are attributed to the same account.
* Proxies are shuffled before fan-out so repeated bursts do not hammer the same
  exits; a proxy that fails is written off by the pool's own failure counters.
* Pacing (``TokenBucket``) is optional and off by default: the point of a burst
  is throughput. An operator-supplied cap still applies when configured.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from .engine import AsyncReportEngine
from .events import EventBus
from .models import Job, ProxyConfig, Result, Stats
from .proxy_pool import AsyncProxyPool, NoProxyAvailable
from .proxy_sources import (
    MAX_LATENCY_MS,
    AcquiredProxies,
    ProxySource,
    build_pool,
)
from .rate import TokenBucket
from .session_pool import SessionConfig
from .transport import DEFAULT_IMPERSONATE, DEFAULT_TIMEOUT


@dataclass
class BurstConfig:
    """Knobs for one burst against a single account."""

    requests: int = 50
    per_proxy_concurrency: int = 8
    max_latency_ms: float = MAX_LATENCY_MS
    max_retries: int = 1
    retry_backoff: float = 0.25
    request_timeout: float | tuple[float, float] = DEFAULT_TIMEOUT
    impersonate: str | None = DEFAULT_IMPERSONATE
    rate_per_minute: float = 0.0
    # Direct connections are never a silent fallback for a proxy burst, but an
    # operator may run without proxies on purpose (local tests, trusted egress).
    allow_direct: bool = False

    def __post_init__(self) -> None:
        self.requests = max(1, int(self.requests))
        self.per_proxy_concurrency = max(1, int(self.per_proxy_concurrency))
        self.max_latency_ms = max(1.0, float(self.max_latency_ms))
        self.max_retries = max(0, int(self.max_retries))


@dataclass
class BurstReport:
    """Outcome of one burst, safe to serialize for a dashboard or CLI."""

    account: str
    requests: int
    stats: Stats
    source: str = "none"
    proxies_used: int = 0
    rejected_proxies: list[tuple[str, str]] = field(default_factory=list)
    proxy_latency_ms: dict[str, float] = field(default_factory=dict)
    results: list[Result] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and self.stats.ok > 0

    @property
    def requests_per_second(self) -> float:
        duration = self.stats.duration_s
        return (self.stats.total / duration) if duration > 0 else 0.0

    def as_dict(self) -> dict[str, Any]:
        payload = self.stats.as_dict()
        payload.update(
            {
                "account": self.account,
                "source": self.source,
                "proxies_used": self.proxies_used,
                "rejected_proxies": [{"proxy": p, "reason": r} for p, r in self.rejected_proxies],
                "proxy_latency_ms": {
                    label: round(ms, 1) for label, ms in self.proxy_latency_ms.items()
                },
                "requests_per_second": round(self.requests_per_second, 2),
                "error": self.error,
            }
        )
        return payload


def build_burst_jobs(
    url: str,
    *,
    account: str,
    requests: int,
    method: str = "GET",
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    body: Any = None,
    session: SessionConfig | None = None,
) -> list[Job]:
    """Fan a single account's request out into ``requests`` identical jobs."""
    merged_headers = dict(headers or {})
    if session is not None:
        cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.items())
        if cookie_header:
            merged_headers["Cookie"] = cookie_header
        for key, value in session.headers.items():
            merged_headers.setdefault(key, value)

    meta: dict[str, Any] = {"account": account}
    if body is not None:
        meta["body"] = body

    return [
        Job(
            url=url,
            method=method,
            params=dict(params or {}),
            headers=dict(merged_headers),
            name=f"{account or 'burst'}-{idx + 1}",
            meta=dict(meta),
        )
        for idx in range(max(1, int(requests)))
    ]


class SingleAccountBurst:
    """Send many near-simultaneous requests for one account through many IPs."""

    def __init__(
        self,
        source: ProxySource,
        config: BurstConfig | None = None,
        *,
        events: EventBus | None = None,
        on_disable: Callable[[str], None] | None = None,
        pool_factory: Callable[..., Any] = build_pool,
        engine_factory: Callable[..., AsyncReportEngine] = AsyncReportEngine,
        shuffle_seed: int | None = None,
    ) -> None:
        self.source = source
        self.config = config or BurstConfig()
        self.events = events
        self.on_disable = on_disable
        self._pool_factory = pool_factory
        self._engine_factory = engine_factory
        self._shuffle = random.Random(shuffle_seed)

    async def _make_pool(self, acquired: AcquiredProxies) -> AsyncProxyPool:
        proxies: Sequence[ProxyConfig] = list(acquired.proxies)
        if len(proxies) > 1:
            proxies = list(proxies)
            self._shuffle.shuffle(proxies)
        return await self._pool_factory(
            proxies,
            max_latency_ms=self.config.max_latency_ms,
            per_proxy_concurrency=self.config.per_proxy_concurrency,
            impersonate=self.config.impersonate,
            request_timeout=self.config.request_timeout,
            allow_direct=self.config.allow_direct and not proxies,
            on_disable=self.on_disable,
        )

    async def run(
        self,
        url: str,
        *,
        account: str = "",
        method: str = "GET",
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        body: Any = None,
        session: SessionConfig | None = None,
        requests: int | None = None,
    ) -> BurstReport:
        count = max(1, int(requests if requests is not None else self.config.requests))
        label = account or (session.label or session.identifier if session else "")

        acquired = await self.source.acquire()
        report = BurstReport(
            account=label,
            requests=count,
            stats=Stats(),
            source=acquired.source,
            rejected_proxies=list(acquired.rejected),
            proxy_latency_ms=dict(acquired.latency_ms),
        )
        if not acquired.ok and not self.config.allow_direct:
            report.error = f"no proxy under {self.config.max_latency_ms:.0f}ms available"
            report.stats.finish()
            return report

        report.proxies_used = len(acquired.proxies)
        pool: AsyncProxyPool | None = None
        try:
            pool = await self._make_pool(acquired)
            rate = (
                TokenBucket(self.config.rate_per_minute)
                if self.config.rate_per_minute and self.config.rate_per_minute > 0
                else None
            )
            engine = self._engine_factory(
                pool,
                workers=count,
                max_retries=self.config.max_retries,
                retry_backoff=self.config.retry_backoff,
                rate=rate,
                events=self.events,
            )
            jobs = build_burst_jobs(
                url,
                account=label,
                requests=count,
                method=method,
                params=params,
                headers=headers,
                body=body,
                session=session,
            )
            report.stats = await engine.run(jobs)
            report.results = list(getattr(engine, "results", []))
        except NoProxyAvailable as exc:
            report.error = f"pool exhausted: {exc}"[:200]
            report.stats.finish()
        finally:
            if pool is not None:
                await pool.close()
        return report


async def run_burst(
    url: str,
    *,
    account: str = "",
    proxy_file: str | None = None,
    proxy_pool_url: str | None = None,
    session: SessionConfig | None = None,
    config: BurstConfig | None = None,
    method: str = "GET",
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    body: Any = None,
    requests: int | None = None,
    events: EventBus | None = None,
    source: ProxySource | None = None,
) -> BurstReport:
    """Convenience wrapper: build a source, run one burst, return the report."""
    cfg = config or BurstConfig()
    src = source or ProxySource(
        file_path=proxy_file,
        pool_url=proxy_pool_url,
        max_latency_ms=cfg.max_latency_ms,
        impersonate=cfg.impersonate,
        timeout=cfg.request_timeout,
    )
    burst = SingleAccountBurst(src, cfg, events=events)
    return await burst.run(
        url,
        account=account,
        method=method,
        params=params,
        headers=headers,
        body=body,
        session=session,
        requests=requests,
    )


__all__ = [
    "BurstConfig",
    "BurstReport",
    "SingleAccountBurst",
    "build_burst_jobs",
    "run_burst",
]
