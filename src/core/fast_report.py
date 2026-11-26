# -*- coding: utf-8 -*-
"""Fast (concurrent) report path for a single account.

The dashboard's classic worker sends one report, sleeps ``delay`` seconds, and
repeats. That serialises every request onto one connection and one exit IP, so
throughput is capped at ``1 / delay`` regardless of how many proxies are
available.

``FastReporter`` instead:

* loads proxies from a file (``data/proxy_list.txt`` by default),
* probes them concurrently and keeps only those answering under a latency
  ceiling (300 ms by default) that can also tunnel HTTPS,
* fans out N worker threads, each holding its own proxy, all hitting the same
  target/account simultaneously,
* recycles a proxy only after it exceeds its own concurrency budget, so a small
  pool is still fully used.

It is synchronous and thread-based on purpose: the dashboard is a Werkzeug/
Flask app and the report builder is blocking, so an asyncio loop would need a
second thread boundary anyway. Results are emitted through ``on_result`` so the
caller can log/emit them exactly like the classic path.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Optional, Sequence

from .async_engine.models import ProxyConfig, load_proxy_sources
from .async_engine.proxy_sources import (
    DEFAULT_PROXY_FILE,
    MAX_LATENCY_MS,
    AcquiredProxies,
    ProxyCandidate,
    filter_fast_candidates,
    probe_tunnel,
)
from .async_engine.transport import DEFAULT_IMPERSONATE, DEFAULT_TIMEOUT, build_client

ReportFn = Callable[[dict], Any]


@dataclass
class FastReportConfig:
    """Tuning for one fast-report run."""

    workers: int = 8
    max_requests: int = 0            # 0 = run until stopped
    max_latency_ms: float = MAX_LATENCY_MS
    per_proxy_concurrency: int = 4
    request_timeout: float | tuple[float, float] = DEFAULT_TIMEOUT
    impersonate: str | None = DEFAULT_IMPERSONATE
    delay: float = 0.0               # optional per-worker pause (usually 0)
    recycle_failures: int = 3        # failures before a proxy is dropped
    probe_concurrency: int = 40

    def __post_init__(self) -> None:
        self.workers = max(1, int(self.workers))
        self.max_requests = max(0, int(self.max_requests))
        self.max_latency_ms = max(1.0, float(self.max_latency_ms))
        self.per_proxy_concurrency = max(1, int(self.per_proxy_concurrency))
        self.delay = max(0.0, float(self.delay))
        self.recycle_failures = max(1, int(self.recycle_failures))
        self.probe_concurrency = max(1, int(self.probe_concurrency))


@dataclass
class ProxySlot:
    """A proxy plus its own client and concurrency budget."""

    config: ProxyConfig
    client: Any
    in_flight: int = 0
    failures: int = 0
    requests: int = 0
    successes: int = 0
    disabled: bool = False

    @property
    def label(self) -> str:
        return self.config.label

    @property
    def available(self) -> bool:
        return not self.disabled and self.in_flight == 0

    def close(self) -> None:
        closer = getattr(self.client, "close", None)
        if closer is None:
            return
        try:
            closer()
        except Exception:
            pass


@dataclass
class FastStats:
    """Live counters for a fast-report run."""

    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None
    sent: int = 0          # requests claimed against the budget
    completed: int = 0     # requests that actually finished (success or not)
    accepted: int = 0
    rejected: int = 0
    errors: int = 0

    @property
    def duration_s(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)

    @property
    def rate_per_second(self) -> float:
        duration = self.duration_s
        return (self.completed / duration) if duration > 0 else 0.0

    def snapshot(self) -> dict:
        return {
            "sent": self.sent,
            "completed": self.completed,
            "accepted": self.accepted,
            "rejected": self.rejected,
            "errors": self.errors,
            "duration_s": round(self.duration_s, 2),
            "rate_per_second": round(self.rate_per_second, 2),
        }


def load_fast_proxies(
    path: str = DEFAULT_PROXY_FILE,
    *,
    max_latency_ms: float = MAX_LATENCY_MS,
    limit: int | None = None,
    concurrency: int = 40,
    require_https: bool = True,
) -> AcquiredProxies:
    """Probe a proxy-list file and return the ones fast enough to use."""
    if not path:
        path = DEFAULT_PROXY_FILE
    if not os.path.exists(path):
        return AcquiredProxies(source="file")
    try:
        entries = load_proxy_sources(path)
    except OSError:
        return AcquiredProxies(source="file")
    candidates = [ProxyCandidate(cfg, lat) for cfg, lat in entries]
    if not candidates:
        return AcquiredProxies(source="file")

    import asyncio

    acquired = asyncio.run(
        filter_fast_candidates(
            candidates,
            max_latency_ms=max_latency_ms,
            limit=limit,
            concurrency=concurrency,
            probe=probe_tunnel,
            require_https=require_https,
        )
    )
    acquired.source = "file"
    return acquired


class FastReporter:
    """Run a single account's report requests concurrently across proxies.

    Example::

        reporter = FastReporter(config, on_result=print)
        stats = reporter.run(report_fn, proxies)
    """

    def __init__(
        self,
        config: FastReportConfig | None = None,
        *,
        on_result: Callable[[dict], None] | None = None,
        on_log: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config or FastReportConfig()
        self._on_result = on_result
        self._on_log = on_log
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.stats = FastStats()
        self._slots: list[ProxySlot] = []
        self._threads: list[threading.Thread] = []

    # ── lifecycle ────────────────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return any(t.is_alive() for t in self._threads)

    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()

    def _log(self, message: str) -> None:
        if self._on_log is not None:
            try:
                self._on_log(message)
            except Exception:
                pass

    # ── slot handling ────────────────────────────────────────────────────────
    def _build_slots(self, proxies: Sequence[ProxyConfig]) -> list[ProxySlot]:
        slots: list[ProxySlot] = []
        for proxy in proxies:
            client = build_client(
                proxy,
                timeout=self.config.request_timeout,
                impersonate=self.config.impersonate,
                max_connections=max(4, self.config.per_proxy_concurrency * 2),
            )
            slots.append(ProxySlot(config=proxy, client=client))
        return slots

    def _lease(self) -> Optional[ProxySlot]:
        """Return a proxy under its concurrency budget, or ``None`` if none free."""
        with self._lock:
            for slot in self._slots:
                if slot.disabled:
                    continue
                if slot.in_flight < self.config.per_proxy_concurrency:
                    slot.in_flight += 1
                    return slot
        return None

    def _has_live_slot(self) -> bool:
        """True while at least one proxy could still serve a request."""
        with self._lock:
            return any(not slot.disabled for slot in self._slots)

    def _release(self, slot: ProxySlot, success: bool) -> None:
        with self._lock:
            slot.in_flight = max(0, slot.in_flight - 1)
            slot.requests += 1
            if success:
                slot.successes += 1
                slot.failures = 0
            else:
                slot.failures += 1
                if slot.failures >= self.config.recycle_failures:
                    slot.disabled = True
                    self._log(f"proxy dropped after {slot.failures} failures: {slot.label}")

    def _claim_budget(self) -> bool:
        """Reserve one request against the cap, or return False when spent.

        The check and the increment happen under the same lock; testing the
        budget in the loop condition and incrementing after the request let
        ``workers`` in-flight requests overshoot the cap (47 sends for a cap of
        40). ``sent`` therefore counts *claimed* requests, and a claim is only
        released by :meth:`_release_claim` when no proxy could be leased.
        """
        cap = self.config.max_requests
        with self._lock:
            if cap > 0 and self.stats.sent >= cap:
                return False
            self.stats.sent += 1
            return True

    def _release_claim(self) -> None:
        """Give back a reservation that never turned into a request."""
        with self._lock:
            self.stats.sent = max(0, self.stats.sent - 1)

    def _record_outcome(self, success: bool) -> None:
        with self._lock:
            if success:
                self.stats.accepted += 1
            else:
                self.stats.rejected += 1
            self.stats.completed += 1

    # ── worker ───────────────────────────────────────────────────────────────
    def _worker(self, report_fn: ReportFn, worker_id: int) -> None:
        while not self._stop.is_set():
            if not self._claim_budget():
                return
            slot = self._lease()
            if slot is None:
                # Undo the reservation: no request is going to be sent this
                # turn, so the slot must not be consumed.
                self._release_claim()
                if not self._has_live_slot():
                    # Every proxy was dropped: no future turn can ever lease
                    # one, so exit instead of idling until an external stop.
                    self._stop.set()
                    return
                # All proxies are merely busy: back off instead of spinning.
                if self._stop.wait(0.01):
                    return
                continue

            payload = {
                "proxy": slot.config,
                "client": slot.client,
                "worker_id": worker_id,
                "slot_label": slot.label,
            }
            success = False
            errored = False
            try:
                result = report_fn(payload)
                success = bool(result.get("success")) if isinstance(result, dict) else bool(result)
            except Exception as exc:  # a bad request must not kill the worker
                errored = True
                self._log(f"worker {worker_id} error: {type(exc).__name__}: {exc}")
            finally:
                # An exception is its own outcome: it is counted as an error,
                # never also as a rejected report, so errors + accepted +
                # rejected always equals completed.
                if errored:
                    with self._lock:
                        self.stats.errors += 1
                        self.stats.completed += 1
                else:
                    self._record_outcome(success)
                self._release(slot, success)

            if self.config.delay > 0 and self._stop.wait(self.config.delay):
                return

    # ── public API ───────────────────────────────────────────────────────────
    def run(
        self,
        report_fn: ReportFn,
        proxies: Sequence[ProxyConfig],
    ) -> FastStats:
        """Block until every worker finishes (or :meth:`stop` is called)."""
        if not proxies:
            raise ValueError("fast report needs at least one proxy")

        self._stop.clear()
        self.stats = FastStats()
        # Fresh slots per run: reusing a previous run's slots would silently
        # inherit disabled proxies and their failure counters.
        for slot in self._slots:
            slot.close()
        self._slots = self._build_slots(proxies)
        self._threads = []
        try:
            for idx in range(self.config.workers):
                thread = threading.Thread(
                    target=self._worker,
                    args=(report_fn, idx + 1),
                    name=f"fast-report-{idx + 1}",
                    daemon=True,
                )
                thread.start()
                self._threads.append(thread)
            for thread in self._threads:
                thread.join()
        finally:
            self.stats.finished_at = time.monotonic()
            for slot in self._slots:
                slot.close()
            self._threads = []
        return self.stats

    def proxy_snapshot(self) -> list[dict]:
        with self._lock:
            slots = list(self._slots)
        return [
            {
                "proxy": slot.label,
                "requests": slot.requests,
                "successes": slot.successes,
                "failures": slot.failures,
                "in_flight": slot.in_flight,
                "disabled": slot.disabled,
            }
            for slot in slots
        ]


def iter_fast_proxy_labels(acquired: AcquiredProxies) -> Iterable[str]:
    """Convenience helper for UI listings."""
    for proxy in acquired.proxies:
        yield proxy.label
