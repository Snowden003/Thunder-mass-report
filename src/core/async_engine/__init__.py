# -*- coding: utf-8 -*-
"""Async worker engine for THUNDER (asyncio + curl_cffi).

Public API:
    AsyncProxyPool   – health-checked, scored proxy clients
    AsyncReportEngine – bounded queue + N workers + retries + stats
    TokenBucket      – global requests-per-minute pacing

Run the standalone probe harness with:
    python -m core.async_engine.runner --requests 10 --workers 2
"""

from .engine import AsyncReportEngine, classify_response
from .events import Event, EventBus
from .models import (
    Job,
    Outcome,
    ProxyConfig,
    Result,
    Stats,
    load_proxy_lines,
    parse_proxy_line,
)
from .proxy_pool import AsyncProxyPool, NoProxyAvailable, ProxyHandle
from .rate import TokenBucket
from .session_pool import (
    NoSessionAvailable,
    RotationStrategy,
    SessionConfig,
    SessionHandle,
    SessionPool,
    load_sessions_from_file,
    load_sessions_from_redis,
)
from .templates import (
    AuthSpec,
    AuthType,
    RequestTemplate,
    Signer,
    SignerFn,
    load_template,
    load_templates_from_file,
    render,
    api_key_signer,
    hmac_signer,
)
from .transport import DEFAULT_IMPERSONATE, build_client

__all__ = [
    "AsyncProxyPool",
    "AsyncReportEngine",
    "AuthSpec",
    "DEFAULT_IMPERSONATE",
    "AuthType",
    "Event",
    "EventBus",
    "Job",
    "NoProxyAvailable",
    "NoSessionAvailable",
    "Outcome",
    "ProxyConfig",
    "ProxyHandle",
    "RequestTemplate",
    "RotationStrategy",
    "SessionConfig",
    "SessionHandle",
    "SessionPool",
    "Signer",
    "SignerFn",
    "Stats",
    "TokenBucket",
    "api_key_signer",
    "build_client",
    "classify_response",
    "hmac_signer",
    "load_proxy_lines",
    "load_sessions_from_file",
    "load_sessions_from_redis",
    "load_template",
    "load_templates_from_file",
    "parse_proxy_line",
    "render",
]
