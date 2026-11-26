# -*- coding: utf-8 -*-
"""curl_cffi client construction for the async engine.

curl_cffi drives libcurl-impersonate, so every request carries a real browser
TLS/JA3 and HTTP/2 fingerprint (``impersonate=...``) instead of the generic
Python TLS stack. One ``AsyncSession`` is bound to one proxy because the
connection pool is session-scoped.
"""

from __future__ import annotations

import os

from curl_cffi.requests import AsyncSession

from .models import ProxyConfig

DEFAULT_TIMEOUT: tuple[float, float] = (10.0, 25.0)
DEFAULT_IMPERSONATE = os.environ.get("CURL_CFFI_IMPERSONATE", "chrome")
DEFAULT_USER_AGENT: str | None = None


def build_client(
    proxy: ProxyConfig | None,
    *,
    timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
    max_connections: int = 20,
    max_keepalive_connections: int = 4,
    http2: bool = False,
    verify: bool = True,
    user_agent: str | None = DEFAULT_USER_AGENT,
    impersonate: str | None = DEFAULT_IMPERSONATE,
    headers: dict[str, str] | None = None,
) -> AsyncSession:
    """Create an AsyncSession bound to one proxy with a browser TLS fingerprint.

    ``impersonate`` selects the browser profile used for the TLS ClientHello,
    HTTP/2 settings, header order and default headers. Passing ``None`` disables
    impersonation and falls back to libcurl's own fingerprint.
    """
    session_headers = dict(headers or {})
    if user_agent:
        session_headers["User-Agent"] = user_agent

    kwargs: dict = {
        "timeout": timeout,
        "verify": verify,
        "allow_redirects": True,
        "impersonate": impersonate or None,
        "max_clients": max(1, int(max_connections)),
    }
    if impersonate:
        kwargs["default_headers"] = True
    if session_headers:
        kwargs["headers"] = session_headers
    if proxy is not None:
        kwargs["proxy"] = proxy.url
    return AsyncSession(**kwargs)
