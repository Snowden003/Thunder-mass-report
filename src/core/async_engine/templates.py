# -*- coding: utf-8 -*-
"""Declarative request template system for generic REST APIs.

Provides RequestTemplate with placeholder substitution, pluggable signers,
and authentication specs. Fully typed, dependency-free (stdlib + httpx types).
"""

from __future__ import annotations

import re
import time
import uuid
import hmac
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import urljoin

import httpx

from .models import Job
from .session_pool import SessionConfig


# ──────────────────────────────────────────────────────────────────────────────
# Placeholder Resolution
# ──────────────────────────────────────────────────────────────────────────────

_PLACEHOLDER_RE = re.compile(r"\{([^{}]+)\}")

# Built-in resolver functions: name -> callable(context) -> value
BuiltinResolver = Callable[[dict[str, Any]], Any]


def _resolver_timestamp(_: dict[str, Any]) -> str:
    return str(int(time.time()))


def _resolver_timestamp_ms(_: dict[str, Any]) -> str:
    return str(int(time.time() * 1000))


def _resolver_nonce(_: dict[str, Any]) -> str:
    return secrets.token_hex(16)


def _resolver_uuid(_: dict[str, Any]) -> str:
    return str(uuid.uuid4())


def _resolver_device_id(ctx: dict[str, Any]) -> str:
    session = ctx.get("session")
    if isinstance(session, SessionConfig):
        return session.meta.get("device_id", "")
    return ctx.get("device_id") or (ctx.get("session", {}).get("meta", {}).get("device_id", "") if isinstance(ctx.get("session"), dict) else "")


def _resolver_session_id(ctx: dict[str, Any]) -> str:
    return ctx.get("session_id") or (ctx.get("session").sessionid if isinstance(ctx.get("session"), SessionConfig) else "")


def _resolver_user_id(ctx: dict[str, Any]) -> str:
    return ctx.get("user_id") or (ctx.get("session").user_id if isinstance(ctx.get("session"), SessionConfig) else "")


def _resolver_session(ctx: dict[str, Any], key: str) -> Any:
    """Resolver for session.* placeholders like {session.cookies.ttwid}."""
    session = ctx.get("session")
    if not isinstance(session, SessionConfig):
        return ""
    # Support nested access: session.cookies.ttwid, session.meta.followers, etc.
    parts = key.split(".")[1:]  # skip "session"
    obj: Any = session
    for part in parts:
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif hasattr(obj, part):
            obj = getattr(obj, part)
        else:
            return ""
        if obj is None:
            return ""
    return obj


# Default built-in resolvers
DEFAULT_RESOLVERS: dict[str, BuiltinResolver] = {
    "timestamp": _resolver_timestamp,
    "timestamp_ms": _resolver_timestamp_ms,
    "nonce": _resolver_nonce,
    "uuid": _resolver_uuid,
    "device_id": _resolver_device_id,
    "session_id": _resolver_session_id,
    "user_id": _resolver_user_id,
}


# ──────────────────────────────────────────────────────────────────────────────
# Authentication Specifications
# ──────────────────────────────────────────────────────────────────────────────

class AuthType(str):
    """Supported authentication types."""
    BEARER = "bearer"
    API_KEY = "api_key"
    BASIC = "basic"
    CUSTOM = "custom"
    COOKIE = "cookie"  # Handled separately via session cookies


@dataclass(frozen=True, slots=True)
class AuthSpec:
    """Authentication specification for a request template."""
    type: str = AuthType.BEARER
    # For bearer/api_key: header name (default: Authorization)
    header: str = "Authorization"
    # For api_key: prefix (e.g., "Bearer ", "ApiKey ")
    prefix: str = "Bearer "
    # For basic: username/password keys in context
    username_key: str = "username"
    password_key: str = "password"
    # For custom: callable(context) -> dict[str, str] (headers to add)
    custom_fn: Callable[[dict[str, Any]], dict[str, str]] | None = None


# ──────────────────────────────────────────────────────────────────────────────
# Signer Interface
# ──────────────────────────────────────────────────────────────────────────────

class Signer(Protocol):
    """Pluggable request signer/modifier.

    Implementations receive the prepared httpx.Request and a context dict,
    and return a modified request (can add headers, modify body, etc.).
    """
    def __call__(self, request: httpx.Request, context: dict[str, Any]) -> httpx.Request:
        ...


# Type alias for signer callables
SignerFn = Callable[[httpx.Request, dict[str, Any]], httpx.Request]


# ──────────────────────────────────────────────────────────────────────────────
# Built-in Signers
# ──────────────────────────────────────────────────────────────────────────────

def hmac_signer(secret_key: str, *, header: str = "X-Signature", algorithm: str = "sha256") -> SignerFn:
    """Create an HMAC signer for request body."""
    def sign(request: httpx.Request, context: dict[str, Any]) -> httpx.Request:
        body = request.content or b""
        if isinstance(body, str):
            body = body.encode()
        mac = hmac.new(secret_key.encode(), body, getattr(hashlib, algorithm)).hexdigest()
        headers = dict(request.headers)
        headers[header] = mac
        return httpx.Request(
            method=request.method,
            url=request.url,
            headers=headers,
            content=request.content,
            extensions=request.extensions,
        )
    return sign


def api_key_signer(api_key: str, *, header: str = "X-API-Key") -> SignerFn:
    """Simple API key header signer."""
    def sign(request: httpx.Request, context: dict[str, Any]) -> httpx.Request:
        headers = dict(request.headers)
        headers[header] = api_key
        return httpx.Request(
            method=request.method,
            url=request.url,
            headers=headers,
            content=request.content,
            extensions=request.extensions,
        )
    return sign


# ──────────────────────────────────────────────────────────────────────────────
# Request Template
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class RequestTemplate:
    """Declarative HTTP request template with placeholder support.

    Example:
        template = RequestTemplate(
            method="POST",
            path="/v1/users/{user_id}/reports",
            base_url="https://api.example.com",
            params={"reason": "{reason_code}", "ts": "{timestamp}"},
            headers={"Content-Type": "application/json"},
            body={"target": "{user_id}", "device": "{device_id}"},
            auth=AuthSpec(type=AuthType.BEARER),
            signers=[hmac_signer("secret")],
        )
        job = render(template, {"user_id": "123", "reason_code": "spam", "device_id": "dev-1"})
    """

    method: str = "GET"
    path: str = "/"
    base_url: str = ""
    params: dict[str, Any] = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    body: dict[str, Any] = field(default_factory=dict)
    auth: AuthSpec | None = None
    signers: list[SignerFn] = field(default_factory=list)
    name: str = ""  # Optional name for logging/metrics

    # Advanced options
    timeout: float | None = None
    follow_redirects: bool = True


# ──────────────────────────────────────────────────────────────────────────────
# Rendering Engine
# ──────────────────────────────────────────────────────────────────────────────

def _substitute(value: Any, context: dict[str, Any], resolvers: dict[str, BuiltinResolver]) -> Any:
    """Recursively substitute placeholders in strings, dicts, lists."""
    if isinstance(value, str):
        return _substitute_string(value, context, resolvers)
    if isinstance(value, dict):
        return {k: _substitute(v, context, resolvers) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, context, resolvers) for v in value]
    return value


def _substitute_string(s: str, context: dict[str, Any], resolvers: dict[str, BuiltinResolver]) -> str:
    """Replace {placeholder} patterns in a string."""
    def repl(match: re.Match) -> str:
        key = match.group(1)
        # Check custom resolvers first
        if key in resolvers:
            val = resolvers[key](context)
            return str(val) if val is not None else ""
        # Check context directly
        if key in context:
            val = context[key]
            return str(val) if val is not None else ""
        # Check session.* nested access
        if key.startswith("session."):
            val = _resolver_session(context, key)
            return str(val) if val is not None else ""
        # Unknown placeholder - leave as-is
        return match.group(0)

    return _PLACEHOLDER_RE.sub(repl, s)


def _apply_auth(request: httpx.Request, auth: AuthSpec, context: dict[str, Any]) -> httpx.Request:
    """Apply authentication to request."""
    headers = dict(request.headers)

    if auth.type == AuthType.BEARER:
        token = context.get("access_token") or context.get("bearer_token", "")
        if token:
            headers[auth.header] = f"{auth.prefix}{token}"

    elif auth.type == AuthType.API_KEY:
        api_key = context.get("api_key", "")
        if api_key:
            headers[auth.header] = f"{auth.prefix}{api_key}"

    elif auth.type == AuthType.BASIC:
        username = context.get(auth.username_key, "")
        password = context.get(auth.password_key, "")
        if username or password:
            import base64
            creds = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {creds}"

    elif auth.type == AuthType.CUSTOM and auth.custom_fn:
        custom_headers = auth.custom_fn(context)
        headers.update(custom_headers)

    # COOKIE auth is handled separately via session cookies merged into headers

    # Create new request with updated headers (httpx.Request is immutable)
    # httpx.Request stores params in the URL
    return httpx.Request(
        method=request.method,
        url=request.url,
        headers=headers,
        content=request.content,
        extensions=request.extensions,
    )


def _apply_signers(request: httpx.Request, signers: list[SignerFn], context: dict[str, Any]) -> httpx.Request:
    """Apply all signers in sequence."""
    for signer in signers:
        request = signer(request, context)
    return request


def render(
    template: RequestTemplate,
    context: dict[str, Any],
    *,
    resolvers: dict[str, BuiltinResolver] | None = None,
    session: SessionConfig | None = None,
) -> Job:
    """Render a RequestTemplate into an executable Job.

    Args:
        template: The RequestTemplate to render.
        context: Runtime values for placeholder substitution.
        resolvers: Optional additional placeholder resolvers.
        session: Optional SessionConfig for cookie/header injection.

    Returns:
        Job ready for execution by AsyncReportEngine.
    """
    # Merge resolvers
    all_resolvers = {**DEFAULT_RESOLVERS, **(resolvers or {})}

    # Add session to context for session.* placeholders
    render_context = {**context}
    if session:
        render_context["session"] = session

    # Substitute placeholders in path
    rendered_path = _substitute_string(template.path, render_context, all_resolvers)

    # Build full URL
    base = template.base_url.rstrip("/")
    url = urljoin(base + "/", rendered_path.lstrip("/"))

    # Substitute params, headers, body
    rendered_params = _substitute(template.params, render_context, all_resolvers)
    rendered_headers = _substitute(template.headers, render_context, all_resolvers)
    rendered_body = _substitute(template.body, render_context, all_resolvers)

    # Apply authentication and signers by building the request incrementally
    req = httpx.Request(
        method=template.method,
        url=url,
        params=rendered_params if rendered_params else None,
        headers=rendered_headers if rendered_headers else None,
        json=rendered_body if rendered_body and template.method in ("POST", "PUT", "PATCH") else None,
    )

    # Apply authentication
    if template.auth:
        req = _apply_auth(req, template.auth, render_context)

    # Apply signers
    if template.signers:
        req = _apply_signers(req, template.signers, render_context)

    # Merge session cookies and headers if session provided
    # httpx normalizes headers to lowercase, so we need case-insensitive merge
    final_headers = {k.lower(): v for k, v in req.headers.items()} if req.headers else {}
    if session:
        # Merge session cookies as Cookie header
        if session.cookies:
            cookie_header = "; ".join(f"{k}={v}" for k, v in session.cookies.items())
            if cookie_header:
                final_headers["cookie"] = cookie_header
        # Merge session headers (lowercase keys, template headers take precedence)
        for k, v in session.headers.items():
            final_headers.setdefault(k.lower(), v)
    # Convert back to original case for Job (httpx will re-normalize)
    final_headers = {k.title(): v for k, v in final_headers.items()}

    # Build final Job - use original rendered values since httpx.Request is immutable
    job_name = template.name or f"{template.method} {template.path}"
    job = Job(
        url=str(req.url),
        method=req.method,
        params=rendered_params if rendered_params else None,
        headers=final_headers if final_headers else None,
        name=job_name,
        meta={
            "template_path": template.path,
            "template_base_url": template.base_url,
        },
    )

    # Attach body for non-GET requests
    if rendered_body and template.method in ("POST", "PUT", "PATCH"):
        job.meta["body"] = rendered_body

    return job


# ──────────────────────────────────────────────────────────────────────────────
# Template Loading (YAML/JSON)
# ──────────────────────────────────────────────────────────────────────────────

def load_template(data: dict[str, Any]) -> RequestTemplate:
    """Load RequestTemplate from a dictionary (parsed YAML/JSON)."""
    auth = data.get("auth")
    if auth and isinstance(auth, dict):
        auth = AuthSpec(**auth)

    signers = data.get("signers", [])
    # Signers are callables - cannot be serialized directly.
    # They must be registered separately or passed at runtime.

    return RequestTemplate(
        method=data.get("method", "GET"),
        path=data.get("path", "/"),
        base_url=data.get("base_url", ""),
        params=data.get("params", {}),
        headers=data.get("headers", {}),
        body=data.get("body", {}),
        auth=auth,
        signers=signers,
        name=data.get("name", ""),
        timeout=data.get("timeout"),
        follow_redirects=data.get("follow_redirects", True),
    )


def load_templates_from_file(path: str) -> dict[str, RequestTemplate]:
    """Load multiple templates from a YAML/JSON file.

    Expected format:
        templates:
          create_report:
            method: POST
            path: "/v1/reports"
            ...
    """
    import json
    from pathlib import Path

    p = Path(path)
    if not p.exists():
        return {}

    content = p.read_text(encoding="utf-8")
    if p.suffix in (".yaml", ".yml"):
        try:
            import yaml
            data = yaml.safe_load(content)
        except ImportError:
            raise RuntimeError("PyYAML required for .yaml template files")
    else:
        data = json.loads(content)

    templates = {}
    for name, tpl_data in data.get("templates", {}).items():
        templates[name] = load_template(tpl_data)
    return templates