# -*- coding: utf-8 -*-
"""Data types for the THUNDER async reporting engine.

This module is intentionally dependency-free (stdlib only) so it can be
imported by tests and tooling without pulling in the heavy ``core`` package.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Mapping
from urllib.parse import quote

# Same accepted formats as core.proxy_manager._parse_line
_SCHEME_RE = re.compile(
    r"^(https?|socks5)://"
    r"(?:([^:@\s]+):([^@\s]+)@)?"
    r"([a-zA-Z0-9._-]+)"
    r":(\d{1,5})"
    r"\s*$"
)
_PLAIN_RE = re.compile(r"^([a-zA-Z0-9._-]+):(\d{1,5})\s*$")
_AUTH_COLON_RE = re.compile(
    r"^([a-zA-Z0-9._-]+):(\d{1,5}):([^:@\s]+):([^@\s]+)\s*$"
)


class Outcome(str, Enum):
    """Result of one HTTP attempt, classified for backoff/health decisions."""

    OK = "ok"
    WAF = "waf"
    CAPTCHA = "captcha"
    RATE_LIMIT = "rate_limit"
    PROXY_ERROR = "proxy_error"
    TIMEOUT = "timeout"
    NETWORK = "network"
    HTTP_ERROR = "http_error"

    @property
    def success(self) -> bool:
        return self is Outcome.OK

    @property
    def retryable(self) -> bool:
        return self in {
            Outcome.WAF,
            Outcome.CAPTCHA,
            Outcome.RATE_LIMIT,
            Outcome.PROXY_ERROR,
            Outcome.TIMEOUT,
            Outcome.NETWORK,
        }

    @property
    def proxy_fault(self) -> bool:
        return self in {
            Outcome.PROXY_ERROR,
            Outcome.TIMEOUT,
            Outcome.NETWORK,
        }


@dataclass(frozen=True, slots=True)
class ProxyConfig:
    """A parsed proxy endpoint."""

    scheme: str
    host: str
    port: int
    username: str = ""
    password: str = ""

    @property
    def url(self) -> str:
        auth = ""
        if self.username:
            auth = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    @property
    def label(self) -> str:
        auth = f"{self.username}:***@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return self.label


def parse_proxy_line(raw: str) -> ProxyConfig | None:
    """Parse one proxy line, mirroring the formats accepted by the sync pool."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None

    match = _SCHEME_RE.match(line)
    if match:
        scheme, user, passwd, host, port_str = match.groups()
        port = int(port_str)
        if not 1 <= port <= 65535:
            return None
        return ProxyConfig(scheme, host, port, user or "", passwd or "")

    match = _AUTH_COLON_RE.match(line)
    if match:
        host, port_str, user, passwd = match.groups()
        port = int(port_str)
        if not 1 <= port <= 65535:
            return None
        return ProxyConfig("http", host, port, user or "", passwd or "")

    match = _PLAIN_RE.match(line)
    if match:
        host, port_str = match.groups()
        port = int(port_str)
        if not 1 <= port <= 65535:
            return None
        return ProxyConfig("http", host, port)

    return None


def load_proxy_lines(path: str) -> list[ProxyConfig]:
    """Load a proxy file, skipping blanks/comment lines silently."""
    proxies: list[ProxyConfig] = []
    with open(path, "r", encoding="utf-8", errors="ignore") as fh:
        for raw in fh:
            cfg = parse_proxy_line(raw)
            if cfg is not None:
                proxies.append(cfg)
    return proxies


@dataclass(slots=True)
class Job:
    """One HTTP request to execute."""

    url: str
    method: str = "GET"
    params: Mapping[str, Any] | None = None
    headers: Mapping[str, str] | None = None
    name: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Result:
    """Outcome of one job (after retries)."""

    job: Job
    outcome: Outcome
    proxy: str = "direct"
    status_code: int | None = None
    latency_ms: float = 0.0
    attempts: int = 1
    bytes: int = 0
    snippet: str = ""
    error: str = ""
    server_code: str | None = None
    retry_after: float | None = None

    @property
    def success(self) -> bool:
        return self.outcome.success

    @property
    def label(self) -> str:
        return self.job.name or self.job.url


@dataclass
class Stats:
    """Aggregate counters for one engine run. Single-event-loop owned."""

    total: int = 0
    ok: int = 0
    by_outcome: dict[str, int] = field(default_factory=dict)
    latency_sum_ms: float = 0.0
    latency_count: int = 0
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float | None = None

    def add(self, result: Result) -> None:
        self.total += 1
        key = result.outcome.value
        self.by_outcome[key] = self.by_outcome.get(key, 0) + 1
        if result.outcome.success:
            self.ok += 1
        if result.latency_ms > 0:
            self.latency_sum_ms += result.latency_ms
            self.latency_count += 1

    def finish(self) -> None:
        self.finished_at = time.monotonic()

    @property
    def failed(self) -> int:
        return self.total - self.ok

    @property
    def success_rate(self) -> float:
        return (self.ok / self.total) if self.total else 0.0

    @property
    def avg_latency_ms(self) -> float:
        return (self.latency_sum_ms / self.latency_count) if self.latency_count else 0.0

    @property
    def duration_s(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return max(0.0, end - self.started_at)

    def as_dict(self) -> dict[str, Any]:
        return {
            "total": self.total,
            "ok": self.ok,
            "failed": self.failed,
            "success_rate": round(self.success_rate, 4),
            "avg_latency_ms": round(self.avg_latency_ms, 1),
            "duration_s": round(self.duration_s, 2),
            "by_outcome": dict(sorted(self.by_outcome.items())),
        }


def iter_proxy_configs(lines: Iterable[str]) -> list[ProxyConfig]:
    """Convenience helper for tests/in-memory proxy lists."""
    out: list[ProxyConfig] = []
    for line in lines:
        cfg = parse_proxy_line(line)
        if cfg is not None:
            out.append(cfg)
    return out
