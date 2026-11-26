# -*- coding: utf-8 -*-
"""
proxy_manager.py  –  Proxy management for THUNDER
Supports: http, https, socks5 (with or without credentials)

Proxy file format (one per line, comments with #):
  http://host:port
  https://host:port
  socks5://host:port
  http://user:pass@host:port
  socks5://user:pass@host:port
  host:port               <- plain → treated as http
"""

import math
import os
import re
import time
import threading
from urllib.parse import urlparse
from typing import Any, Optional

from curl_cffi.requests import Session as CurlSession
from curl_cffi.requests.exceptions import (
    ProxyError as CurlProxyError,
    RequestException as CurlRequestError,
    Timeout as CurlTimeout,
)

# ── tunables ──────────────────────────────────────────────────────────────────
VALIDATION_TIMEOUT   = 12      # seconds for health-check request (residential proxies are slow)
REQUEST_TIMEOUT      = 15      # seconds used during actual reports (alisg API can take ~11s)
MAX_FAILURES         = 3       # consecutive failures before a proxy is disabled
HEALTH_CHECK_URL     = "https://httpbin.org/ip"
LATENCY_WEIGHT       = 0.6     # how strongly latency influences selection score
SUCCESS_WEIGHT       = 0.4     # how strongly success-rate influences selection score
MIN_HEALTHY_PROXIES  = 1       # warn when fewer healthy proxies remain
TLS_IMPERSONATE      = os.environ.get("CURL_CFFI_IMPERSONATE", "chrome")
MAX_LATENCY_MS       = 500     # maximum acceptable measured proxy latency
# ─────────────────────────────────────────────────────────────────────────────

_TLS_SESSIONS = threading.local()


def _get_shared_tls_session() -> CurlSession:
    """Return a per-thread keep-alive session for direct (proxy-less) requests."""
    session = getattr(_TLS_SESSIONS, "session", None)
    if session is None:
        profile = os.environ.get("CURL_CFFI_IMPERSONATE", TLS_IMPERSONATE)
        session = CurlSession(
            impersonate=profile or None,
            default_headers=False,
            timeout=REQUEST_TIMEOUT,
        )
        _TLS_SESSIONS.session = session
    return session


def _get_tls_session() -> CurlSession:
    """Backwards-compatible alias for the shared direct-request session."""
    return _get_shared_tls_session()


def _new_tls_session() -> CurlSession:
    """Return a one-shot curl_cffi session with a browser TLS fingerprint.

    Rotating proxy gateways bind the exit IP to the TCP connection, so the
    shared keep-alive session would pin every request to a single exit IP.
    A fresh session per proxied request forces the gateway to rotate.
    """
    profile = os.environ.get("CURL_CFFI_IMPERSONATE", TLS_IMPERSONATE)
    return CurlSession(
        impersonate=profile or None,
        default_headers=False,
        timeout=REQUEST_TIMEOUT,
    )


_SCHEME_RE = re.compile(
    r"^(https?|socks5)://"
    r"(?:([^:@\s]+):([^@\s]+)@)?"   # optional user:pass
    r"([a-zA-Z0-9._-]+)"            # host
    r":(\d{1,5})"                    # port
    r"\s*$"
)
_PLAIN_RE = re.compile(r"^([a-zA-Z0-9._-]+):(\d{1,5})\s*$")
_AUTH_COLON_RE = re.compile(r"^([a-zA-Z0-9._-]+):(\d{1,5}):([^:@\s]+):([^@\s]+)\s*$")


class ProxyEntry:
    """Holds one proxy's config and live stats."""

    __slots__ = (
        "scheme", "host", "port", "username", "password",
        "latency_ms", "failures", "success_count", "total_count",
        "enabled", "_lock",
    )

    def __init__(self, scheme: str, host: str, port: int,
                 username: str = "", password: str = ""):
        self.scheme        = scheme
        self.host          = host
        self.port          = port
        self.username      = username
        self.password      = password
        self.latency_ms    = float("inf")
        self.failures      = 0
        self.success_count = 0
        self.total_count   = 0
        self.enabled       = True
        self._lock         = threading.Lock()

    # ── requests-compatible proxy dict ───────────────────────────────────────
    def as_requests_proxy(self) -> dict:
        auth = f"{self.username}:{self.password}@" if self.username else ""
        url  = f"{self.scheme}://{auth}{self.host}:{self.port}"
        return {"http": url, "https": url}

    # ── stats ─────────────────────────────────────────────────────────────────
    @property
    def success_rate(self) -> float:
        if self.total_count == 0:
            return 1.0           # optimistic prior for untested proxy
        return self.success_count / self.total_count

    @property
    def score(self) -> float:
        """Higher is better. Combines success-rate and inverse latency."""
        if not self.enabled:
            return -1.0
        lat_score = 1.0 / (1.0 + self.latency_ms / 1000.0)
        return SUCCESS_WEIGHT * self.success_rate + LATENCY_WEIGHT * lat_score

    @property
    def within_latency_limit(self) -> bool:
        """True until a measured latency is known to exceed MAX_LATENCY_MS.

        An untested proxy (latency == inf) is eligible so that pools loaded with
        ``validate=False`` still work; it is excluded once a real measurement
        proves it is too slow.
        """
        return not math.isfinite(self.latency_ms) or self.latency_ms <= MAX_LATENCY_MS

    def record_success(self, latency_ms: Optional[float] = None):
        with self._lock:
            # Only health checks provide a proxy latency; a report request's
            # end-to-end time (often seconds) must not disqualify the proxy.
            if latency_ms is not None:
                self.latency_ms = latency_ms
            self.success_count += 1
            self.total_count   += 1
            self.failures       = 0

    def record_failure(self):
        with self._lock:
            self.total_count += 1
            self.failures    += 1
            if self.failures >= MAX_FAILURES:
                self.enabled = False

    # ── display helper (no credentials leaked) ───────────────────────────────
    def safe_repr(self) -> str:
        auth = f"{self.username}:***@" if self.username else ""
        return f"{self.scheme}://{auth}{self.host}:{self.port}"

    def snapshot(self) -> dict:
        """Structured, credential-safe view of this proxy for dashboards."""
        with self._lock:
            lat = self.latency_ms
            return {
                "label": self.safe_repr(),
                "scheme": self.scheme,
                "host": self.host,
                "port": self.port,
                "latency_ms": None if not math.isfinite(lat) else round(lat, 1),
                "enabled": self.enabled,
                "within_latency_limit": (not math.isfinite(lat)) or lat <= MAX_LATENCY_MS,
                "success_count": self.success_count,
                "total_count": self.total_count,
                "success_rate": round(self.success_rate, 3),
                "failures": self.failures,
                "score": round(self.score, 3),
            }

    def __repr__(self) -> str:
        return (
            f"<Proxy {self.safe_repr()} "
            f"lat={self.latency_ms:.0f}ms "
            f"ok={self.success_count}/{self.total_count} "
            f"{'✓' if self.enabled else '✗'}>"
        )


# ── parsing ───────────────────────────────────────────────────────────────────

def _parse_line(raw: str) -> Optional[ProxyEntry]:
    """Parse a single proxy line; return None on failure."""
    line = raw.strip()
    if not line or line.startswith("#"):
        return None

    # fully qualified with scheme
    m = _SCHEME_RE.match(line)
    if m:
        scheme, user, passwd, host, port_str = m.groups()
        port = int(port_str)
        if not (1 <= port <= 65535):
            return None
        return ProxyEntry(scheme, host, port,
                          username=user or "", password=passwd or "")

    # format  host:port:user:pass  (common commercial proxy format)
    m = _AUTH_COLON_RE.match(line)
    if m:
        host, port_str, user, passwd = m.groups()
        port = int(port_str)
        if not (1 <= port <= 65535):
            return None
        return ProxyEntry("http", host, port,
                          username=user or "", password=passwd or "")

    # plain  host:port  → http
    m = _PLAIN_RE.match(line)
    if m:
        host, port_str = m.groups()
        port = int(port_str)
        if not (1 <= port <= 65535):
            return None
        return ProxyEntry("http", host, port)

    return None


def load_proxy_file(path: str) -> list:
    """
    Read a proxy list file and return a list of ProxyEntry objects.
    Skips blank lines and comment lines (#).
    """
    if not os.path.exists(path):
        _repo_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        candidate = os.path.join(_repo_root, "data", os.path.basename(path))
        if os.path.exists(candidate):
            path = candidate
    entries = []
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for lineno, raw in enumerate(fh, 1):
                entry = _parse_line(raw)
                if entry is None and raw.strip() and not raw.strip().startswith("#"):
                    print(f"\033[1;33m[!] Line {lineno}: unrecognised format – skipped: "
                          f"{raw.strip()[:60]}\033[0m")
                elif entry:
                    entries.append(entry)
    except FileNotFoundError:
        print(f"\033[1;31m[✗] Proxy file not found: {path}\033[0m")
    except PermissionError:
        print(f"\033[1;31m[✗] Cannot read proxy file: {path}\033[0m")
    return entries


# ── health checking ────────────────────────────────────────────────────────────

def _check_proxy(entry: ProxyEntry) -> bool:
    """
    Attempt a quick GET through the proxy.
    Returns True if the proxy responds within VALIDATION_TIMEOUT and latency <= MAX_LATENCY_MS.
    """
    try:
        t0  = time.monotonic()
        session = _new_tls_session()
        try:
            r = session.get(
                HEALTH_CHECK_URL,
                proxies=entry.as_requests_proxy(),
                timeout=VALIDATION_TIMEOUT,
                allow_redirects=True,
            )
        finally:
            try:
                session.close()
            except Exception:
                pass
        lat = (time.monotonic() - t0) * 1000.0
        if r.status_code == 200 and lat <= MAX_LATENCY_MS:
            entry.record_success(lat)
            return True
        entry.record_failure()
        return False
    except Exception:
        entry.record_failure()
        return False


def validate_proxies(entries: list, max_workers: int = 30) -> list:
    """
    Health-check all proxies concurrently.
    Prints a progress summary and returns only the healthy ones.
    """
    if not entries:
        return []

    print(f"\033[1;36m[»] Validating {len(entries)} proxies "
          f"(timeout={VALIDATION_TIMEOUT}s) …\033[0m")

    results   = [None] * len(entries)
    semaphore = threading.Semaphore(max_workers)

    def worker(i, e):
        with semaphore:
            results[i] = _check_proxy(e)

    threads = []
    for i, e in enumerate(entries):
        t = threading.Thread(target=worker, args=(i, e), daemon=True)
        threads.append(t)
        t.start()
    for t in threads:
        t.join()

    healthy = [e for e, ok in zip(entries, results) if ok]
    dead    = len(entries) - len(healthy)

    print(f"\033[1;32m[✔] {len(healthy)} healthy\033[0m  "
          f"\033[1;31m{dead} dead\033[0m  "
          f"(from {len(entries)} total)")

    if len(healthy) < MIN_HEALTHY_PROXIES:
        print("\033[1;33m[!] Warning: very few healthy proxies available.\033[0m")

    return healthy


# ── ProxyManager class ────────────────────────────────────────────────────────

class ProxyManager:
    """
    Thread-safe proxy pool with connection pooling for stable latency.

    Usage
    -----
    pm = ProxyManager.from_file("proxies.txt")  # load + validate
    proxy_dict = pm.get()                        # pick best proxy
    pm.mark_success(proxy_dict, latency_ms)
    pm.mark_failure(proxy_dict)
    """

    def __init__(self, entries: list):
        self._pool  : list[ProxyEntry] = entries
        self._lock  = threading.Lock()
        self._index = 0                  # round-robin cursor
        self._local = threading.local()  # per-thread "last proxy used"

    @property
    def session(self) -> CurlSession:
        """Thread-local curl_cffi session with browser TLS impersonation."""
        return _get_shared_tls_session()

    @property
    def last_label(self) -> Optional[str]:
        """Human-readable endpoint of the proxy used by the latest request."""
        return getattr(self._local, "last_label", None)

    # ── factory ──────────────────────────────────────────────────────────────
    @classmethod
    def from_file(cls, path: str, validate: bool = True) -> "ProxyManager":
        entries = load_proxy_file(path)
        if not entries:
            raise ValueError(f"No valid proxies parsed from '{path}'")
        if validate:
            entries = validate_proxies(entries)
            if not entries:
                raise ValueError("No proxies passed health check.")
        return cls(entries)

    # ── selection ─────────────────────────────────────────────────────────────
    def get(self) -> Optional[dict]:
        """
        Return a requests-compatible proxy dict, rotating round-robin through
        every eligible proxy so the whole pool is actually used.

        Proxies that are disabled or have a measured latency above
        MAX_LATENCY_MS are skipped. Returns None if no proxy is eligible.
        """
        with self._lock:
            if not self._pool:
                return None
            total = len(self._pool)
            chosen: Optional[ProxyEntry] = None
            for offset in range(total):
                idx = (self._index + offset) % total
                entry = self._pool[idx]
                if entry.enabled and entry.within_latency_limit:
                    chosen = entry
                    self._index = (idx + 1) % total
                    break
        if chosen is None:
            return None
        self._local.last_label = chosen.safe_repr()
        return {"_entry": chosen, **chosen.as_requests_proxy()}

    def mark_success(self, proxy_dict: dict, latency_ms: Optional[float] = None):
        entry: ProxyEntry = proxy_dict.get("_entry")
        if entry:
            entry.record_success(latency_ms)

    def mark_failure(self, proxy_dict: dict):
        entry: ProxyEntry = proxy_dict.get("_entry")
        if entry:
            entry.record_failure()
        with self._lock:
            remaining = sum(1 for e in self._pool if e.enabled)
        if remaining < MIN_HEALTHY_PROXIES:
            print("\033[1;33m[!] Proxy pool critically low. "
                  "Consider adding more proxies.\033[0m")

    # ── stats ─────────────────────────────────────────────────────────────────
    def stats(self) -> str:
        with self._lock:
            total   = len(self._pool)
            healthy = sum(1 for e in self._pool if e.enabled)
            best    = max((e for e in self._pool if e.enabled),
                          key=lambda e: e.score, default=None)
        best_str = "N/A"
        if best is not None:
            lat = ("unmeasured" if not math.isfinite(best.latency_ms)
                   else f"{best.latency_ms:.0f}ms")
            best_str = f"{best.safe_repr()} lat={lat}"
        return (f"proxies={healthy}/{total}  "
                f"best=[{best_str}]")

    def __len__(self):
        return len(self._pool)

    def healthy_count(self) -> int:
        return sum(1 for e in self._pool if e.enabled)

    def snapshot(self) -> list:
        """Return per-proxy details for the dashboard's proxy panel."""
        with self._lock:
            pool = list(self._pool)
        return [entry.snapshot() for entry in pool]


# ── curl_cffi helper ───────────────────────────────────────────────────────────

def tls_get(url: str, **kwargs) -> Any:
    """GET via curl_cffi with a browser TLS signature.

    Proxied requests use a one-shot session so a rotating gateway is not
    pinned to a single exit IP by keep-alive connection reuse.
    """
    if kwargs.get("proxies"):
        session = _new_tls_session()
        try:
            resp = session.get(url, **kwargs)
            try:
                _ = resp.content  # materialize before the one-shot session closes
            except Exception:
                pass
            return resp
        finally:
            try:
                session.close()
            except Exception:
                pass
    return _get_shared_tls_session().get(url, **kwargs)


def make_request_with_proxy(proxy_manager: Optional["ProxyManager"],
                             url: str,
                             params: dict,
                             headers: dict,
                             cookies: Optional[dict] = None,
                             timeout: int = REQUEST_TIMEOUT,
                             max_retries: int = 2) -> Optional[Any]:
    """
    Perform a GET request, optionally routed through a proxy.
    Handles timeout + failure recording automatically.
    Returns a Response on success, None on final failure.

    Each proxied attempt uses a fresh curl_cffi session. Rotating gateways
    select the exit IP per connection, so reusing one session would silently
    send every request through the same IP. Direct requests still share the
    per-thread keep-alive session.
    """
    for attempt in range(max_retries + 1):
        proxy_dict = proxy_manager.get() if proxy_manager else None
        if proxy_manager is not None and proxy_dict is None:
            # Never silently bypass the proxy: that would leak the host IP.
            return None
        proxies    = {k: v for k, v in proxy_dict.items()
                      if k != "_entry"} if proxy_dict else None
        session    = _new_tls_session() if proxies else _get_shared_tls_session()
        close_session = bool(proxies)
        try:
            resp = session.get(
                url,
                params=params,
                headers=headers,
                cookies=cookies,
                proxies=proxies,
                timeout=timeout,
                allow_redirects=True,
            )
            try:
                _ = resp.content  # materialize before the one-shot session closes
            except Exception:
                pass

            # If response is an Akamai WAF / CDN maintenance or error HTML page, treat proxy attempt as failed
            is_html_error = resp.status_code != 200 or ('<html' in resp.text.lower() and 'status_code' not in resp.text)
            if is_html_error:
                if proxy_manager and proxy_dict:
                    proxy_manager.mark_failure(proxy_dict)
                if attempt == max_retries:
                    return resp
                time.sleep(0.3 * (attempt + 1))
                continue

            if proxy_manager and proxy_dict:
                proxy_manager.mark_success(proxy_dict)
            return resp

        except CurlTimeout:
            if proxy_manager and proxy_dict:
                proxy_manager.mark_failure(proxy_dict)
            if attempt == max_retries:
                return None

        except CurlProxyError:
            if proxy_manager and proxy_dict:
                proxy_manager.mark_failure(proxy_dict)
            if attempt == max_retries:
                return None

        except CurlRequestError:
            if proxy_manager and proxy_dict:
                proxy_manager.mark_failure(proxy_dict)
            if attempt == max_retries:
                return None

        finally:
            if close_session:
                try:
                    session.close()
                except Exception:
                    pass

        # short back-off before retry
        time.sleep(0.3 * (attempt + 1))

    return None
