# -*- coding: utf-8 -*-
"""THUNDER async worker engine (asyncio + curl_cffi).

Phase 1 scope: proxy pool, bounded job queue, N workers, retries/backoff,
response classification, stats and a clean shutdown path. Request building
for TikTok reports is added in Phase 2; nothing here imports the legacy
``report-tiktok.py`` script (which executes top-level code on import).
"""

from __future__ import annotations

import asyncio
import logging
import math
import random
import time
from typing import Any, Iterable, Protocol

from curl_cffi.requests.exceptions import (
    ProxyError as CurlProxyError,
    RequestException as CurlRequestError,
    Timeout as CurlTimeout,
)

try:
    import httpx

    _HTTPX_TIMEOUT_ERRORS: tuple[type[BaseException], ...] = (httpx.TimeoutException,)
    _HTTPX_PROXY_ERRORS: tuple[type[BaseException], ...] = (httpx.ProxyError,)
    _HTTPX_REQUEST_ERRORS: tuple[type[BaseException], ...] = (httpx.RequestError,)
except ImportError:  # pragma: no cover - httpx is optional at runtime
    _HTTPX_TIMEOUT_ERRORS = ()
    _HTTPX_PROXY_ERRORS = ()
    _HTTPX_REQUEST_ERRORS = ()

_TIMEOUT_ERRORS = _HTTPX_TIMEOUT_ERRORS + (CurlTimeout,)
_PROXY_ERRORS = _HTTPX_PROXY_ERRORS + (CurlProxyError,)
_REQUEST_ERRORS = _HTTPX_REQUEST_ERRORS + (CurlRequestError,)

from .events import EventBus
from .models import Job, Outcome, Result, Stats
from .proxy_pool import NoProxyAvailable, ProxyHandle
from .rate import TokenBucket
from .session_pool import NoSessionAvailable, SessionHandle

WAF_MARKERS = ("slardarwaf", "please wait", "_wafchallengeid", "waf-aiso", "wafchallenge")
CAPTCHA_MARKERS = (
    "verify you are human",
    "secsdk-captcha",
    "tiktok-captcha",
    "captcha_verify",
    "please complete the captcha",
    "slide to verify",
)
# Upper bound on how long a server-supplied Retry-After may delay a retry.
MAX_RETRY_AFTER = 60.0

logger = logging.getLogger(__name__)


class PoolLike(Protocol):
    async def acquire(self) -> ProxyHandle: ...
    async def release(self, handle: ProxyHandle, result: Result) -> None: ...


class SessionPoolLike(Protocol):
    async def acquire(self) -> SessionHandle: ...
    async def release(self, handle: SessionHandle, result: Result | None) -> None: ...
    def mark_compromised(self, handle: SessionHandle, reason: str) -> None: ...


def classify_response(resp: Any) -> tuple[Outcome, str | None, str]:
    """Classify one HTTP response and return (outcome, server_code, snippet)."""
    status = resp.status_code
    try:
        text = resp.text
    except Exception:
        text = ""
    low = text.lower()
    snippet = " ".join(text[:240].split())

    server_code: str | None = None
    content_type = resp.headers.get("content-type", "")
    looks_json = "json" in content_type or text.lstrip().startswith(("{", "["))
    if looks_json:
        try:
            data = resp.json()
            if isinstance(data, dict) and "status_code" in data:
                server_code = str(data.get("status_code"))
        except Exception:
            pass

    if status == 429:
        return Outcome.RATE_LIMIT, server_code, snippet
    if status in (401, 407):
        return Outcome.PROXY_ERROR, server_code, snippet
    if any(marker in low for marker in WAF_MARKERS):
        return Outcome.WAF, server_code, snippet
    if any(marker in low for marker in CAPTCHA_MARKERS):
        return Outcome.CAPTCHA, server_code, snippet
    if status >= 400:
        return Outcome.HTTP_ERROR, server_code, snippet
    # A 0-byte body on a 2xx status is the signature of a dead/dying SOCKS5
    # hop that closed the connection without forwarding the response. Treat it
    # as a retryable transport fault instead of a success so it cannot inflate
    # the success rate. 204/304 legitimately carry no body.
    if status not in (204, 304) and not text.strip():
        return Outcome.NETWORK, server_code, "(empty body)"
    return Outcome.OK, server_code, snippet


def _parse_retry_after(resp: Any) -> float | None:
    """Read a numeric ``Retry-After`` header (seconds). HTTP-date form is ignored."""
    raw = resp.headers.get("retry-after")
    if not raw:
        return None
    try:
        seconds = float(raw.strip())
    except ValueError:
        return None
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


class AsyncReportEngine:
    def __init__(
        self,
        pool: PoolLike,
        *,
        session_pool: SessionPoolLike | None = None,
        workers: int = 2,
        max_retries: int = 2,
        retry_backoff: float = 0.6,
        rate: TokenBucket | None = None,
        events: EventBus | None = None,
        worker_jitter: tuple[float, float] = (0.0, 0.0),
    ) -> None:
        self.pool = pool
        self.session_pool = session_pool
        self.workers = max(1, int(workers))
        self.max_retries = max(0, int(max_retries))
        self.retry_backoff = max(0.0, float(retry_backoff))
        self.rate = rate
        self.events = events
        self.worker_jitter = worker_jitter
        self._tasks: list[asyncio.Task] = []
        self._stop = asyncio.Event()
        self._session_warned = False
        self.stats = Stats()

    # ── public API ────────────────────────────────────────────────────────────
    async def run(self, jobs: Iterable[Job]) -> Stats:
        self.stats = Stats()
        self._stop.clear()
        self._session_warned = False
        queue: asyncio.Queue[Job | None] = asyncio.Queue(maxsize=max(4, self.workers * 4))

        self._tasks = [
            asyncio.create_task(self._worker(i, queue), name=f"thunder-worker-{i}")
            for i in range(self.workers)
        ]
        producer = asyncio.create_task(self._produce(jobs, queue), name="thunder-producer")
        stop_task = asyncio.create_task(self._stop.wait(), name="thunder-stop")
        try:
            # Wait until all jobs are queued (or stop() is called).
            await asyncio.wait({producer, stop_task}, return_when=asyncio.FIRST_COMPLETED)
            if not self._stop.is_set():
                join_task = asyncio.create_task(queue.join(), name="thunder-drain")
                try:
                    await asyncio.wait(
                        {join_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                finally:
                    if not join_task.done():
                        join_task.cancel()
                    await asyncio.gather(join_task, return_exceptions=True)
        finally:
            stop_task.cancel()
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, stop_task, return_exceptions=True)
            for task in self._tasks:
                task.cancel()
            await asyncio.gather(*self._tasks, return_exceptions=True)
            self._tasks.clear()
            self.stats.finish()
        return self.stats

    async def _produce(self, jobs: Iterable[Job], queue: asyncio.Queue) -> None:
        for job in jobs:
            if self._stop.is_set():
                return
            await queue.put(job)
        for _ in range(self.workers):
            await queue.put(None)

    async def stop(self) -> None:
        self._stop.set()
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # ── internals ─────────────────────────────────────────────────────────────
    async def _worker(self, worker_id: int, queue: asyncio.Queue) -> None:
        while True:
            job = await queue.get()
            if job is None:
                queue.task_done()
                return
            session_handle: SessionHandle | None = None
            result: Result | None = None
            try:
                session_handle = await self._acquire_session(job)
                if session_handle is not None:
                    job = self._apply_session(job, session_handle)
                elif self.session_pool is not None:
                    self._warn_sessions_exhausted()
                result = await self.execute(job)
                self._record(result)
                if (
                    session_handle is not None
                    and self.session_pool is not None
                    and result.outcome in (Outcome.CAPTCHA, Outcome.PROXY_ERROR)
                ):
                    self.session_pool.mark_compromised(session_handle, result.outcome.value)
                if self.worker_jitter[1] > 0:
                    await asyncio.sleep(random.uniform(*self.worker_jitter))
            except asyncio.CancelledError:
                raise
            except NoProxyAvailable as exc:
                self._stop.set()
                result = Result(
                    job=job,
                    outcome=Outcome.PROXY_ERROR,
                    error=f"pool exhausted: {exc}"[:200],
                )
                self._record(result)
                if self.events is not None:
                    self.events.log(f"stopping: {exc}")
                return
            except Exception as exc:  # never let a worker die silently
                logger.exception("worker %s failed on %s", worker_id, job.name or job.url)
                result = Result(
                    job=job,
                    outcome=Outcome.NETWORK,
                    error=f"{type(exc).__name__}: {exc}"[:200],
                )
                self._record(result)
            finally:
                # task_done() FIRST: if releasing the session raises, the
                # queue's unfinished-task counter must still drop or
                # queue.join() hangs forever inside run().
                queue.task_done()
                await self._release_session(session_handle, result)

    def _record(self, result: Result) -> None:
        """Update stats and publish the result. Never raises."""
        self.stats.add(result)
        if self.events is not None:
            try:
                self.events.result(result)
            except Exception:  # noqa: BLE001 - a bus hiccup must not kill a worker
                logger.exception("failed to publish result event")

    async def _acquire_session(self, job: Job) -> SessionHandle | None:
        """Reuse the session pre-attached to the job, or take one from the pool.

        Returns ``None`` when no session pool is configured or the pool ran
        dry; the job then executes without a session instead of failing every
        remaining request in the queue.
        """
        if self.session_pool is None:
            return None
        attached = job.meta.get("session_handle")
        if isinstance(attached, SessionHandle) and attached.pool is self.session_pool:
            return attached
        try:
            return await self.session_pool.acquire()
        except NoSessionAvailable:
            return None

    def _warn_sessions_exhausted(self) -> None:
        if self._session_warned:
            return
        self._session_warned = True
        logger.warning("session pool exhausted; continuing without a session")
        if self.events is not None:
            self.events.log("session pool exhausted; continuing without a session")

    async def _release_session(
        self, handle: SessionHandle | None, result: Result | None
    ) -> None:
        if handle is None or self.session_pool is None or result is None:
            return
        try:
            await self.session_pool.release(handle, result)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - release must not break the worker loop
            ident = handle.config.label or handle.config.identifier or "?"
            logger.exception("failed to release session %s", ident)
    def _apply_session(self, job: Job, session_handle: SessionHandle) -> Job:
        """Merge session cookies and headers into the job."""
        session = session_handle.config
        # Merge cookies into headers (httpx uses Cookie header)
        cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.items())
        merged_headers = dict(job.headers or {})
        if cookie_header:
            merged_headers["Cookie"] = cookie_header
        # Merge custom session headers
        for k, v in session.headers.items():
            merged_headers.setdefault(k, v)
        return Job(
            url=job.url,
            method=job.method,
            params=job.params,
            headers=merged_headers,
            name=job.name,
            meta=job.meta,
        )

    async def _attempt(self, job: Job) -> Result:
        # Pace every physical request (retries included) and do it before a
        # proxy permit is taken, so waiting for a token never occupies a
        # proxy slot or inflates queue latency.
        if self.rate is not None:
            await self.rate.acquire()
        handle = await self.pool.acquire()
        started = time.monotonic()
        result: Result | None = None
        # Extract body from job.meta for POST/PUT/PATCH requests
        body = job.meta.get("body") if job.method in ("POST", "PUT", "PATCH") else None
        try:
            if isinstance(body, dict):
                resp = await handle.client.request(
                    job.method,
                    job.url,
                    params=job.params,
                    headers=job.headers,
                    json=body,
                )
            else:
                resp = await handle.client.request(
                    job.method,
                    job.url,
                    params=job.params,
                    headers=job.headers,
                    data=body,
                )
            latency = (time.monotonic() - started) * 1000.0
            outcome, server_code, snippet = classify_response(resp)
            result = Result(
                job=job,
                outcome=outcome,
                proxy=handle.label,
                status_code=resp.status_code,
                latency_ms=latency,
                bytes=len(resp.content),
                snippet=snippet,
                server_code=server_code,
                retry_after=_parse_retry_after(resp),
            )
        except _TIMEOUT_ERRORS as exc:
            result = Result(
                job=job,
                outcome=Outcome.TIMEOUT,
                proxy=handle.label,
                latency_ms=(time.monotonic() - started) * 1000.0,
                error=f"timeout: {exc}"[:200],
            )
        except _PROXY_ERRORS as exc:
            result = Result(
                job=job,
                outcome=Outcome.PROXY_ERROR,
                proxy=handle.label,
                latency_ms=(time.monotonic() - started) * 1000.0,
                error=f"proxy: {exc}"[:200],
            )
        except _REQUEST_ERRORS as exc:
            result = Result(
                job=job,
                outcome=Outcome.NETWORK,
                proxy=handle.label,
                latency_ms=(time.monotonic() - started) * 1000.0,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
        except Exception as exc:
            # socksio raises ProtocolError outside both client hierarchies;
            # treat any transport-level surprise as a proxy fault so it retries.
            is_socks = exc.__class__.__module__.startswith("socksio")
            result = Result(
                job=job,
                outcome=Outcome.PROXY_ERROR if is_socks else Outcome.NETWORK,
                proxy=handle.label,
                latency_ms=(time.monotonic() - started) * 1000.0,
                error=f"{type(exc).__name__}: {exc}"[:200],
            )
        finally:
            if result is None:
                result = Result(
                    job=job,
                    outcome=Outcome.NETWORK,
                    proxy=handle.label,
                    error="cancelled before response",
                )
            await self.pool.release(handle, result)
        return result

    async def execute(self, job: Job) -> Result:
        last: Result | None = None
        for attempt in range(1, self.max_retries + 2):
            last = await self._attempt(job)
            last.attempts = attempt
            if last.outcome.success or not last.outcome.retryable:
                return last
            if attempt <= self.max_retries:
                backoff = self.retry_backoff * (2 ** (attempt - 1)) + random.uniform(0, 0.25)
                if last.retry_after:
                    # Honour the server's Retry-After on 429/WAF responses,
                    # clamped so a bogus header cannot park a worker for hours.
                    backoff = max(backoff, min(last.retry_after, MAX_RETRY_AFTER))
                await asyncio.sleep(backoff)
        assert last is not None
        return last
