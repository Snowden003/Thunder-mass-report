"""Offline response scoring utilities.

This module is deliberately transport-independent.  It accepts an already
recorded response observation and computes a deterministic score without
sending requests, selecting sessions, or changing request scheduling.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class ResponseObservation:
    """Immutable response data captured by an external recorder."""

    http_status: int | str | None
    latency_ms: float = 0.0
    body: str = ""
    server_code: str | int | None = None
    success: bool | None = None
    target_state_changed: bool | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ScoreBreakdown:
    """Separate dimensions so delivery and system-pressure signals are clear."""

    score: float
    impact_percent: float
    status_component: float
    body_component: float
    latency_component: float
    pressure_component: float
    shadow_report: bool
    restriction: str | None
    reasons: tuple[str, ...]


def _status(value: int | str | None) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _body_flags(body: str) -> tuple[bool, bool, bool, bool, bool]:
    """Return success, duplicate, restriction, captcha, and shadow markers."""
    text = body or ""
    lowered = text.lower()
    success_marker = False
    try:
        payload = json.loads(text) if text.strip() else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}

    if isinstance(payload, dict):
        success_marker = (
            payload.get("status") == "success"
            or payload.get("reported") is True
            or str(payload.get("status_code", "")) == "0"
        )

    success_marker = success_marker or '"reported":true' in lowered.replace(" ", "")
    duplicate = "already_reported" in lowered or "already reported" in lowered
    restriction = (
        "action_block" in lowered
        or "action blocked" in lowered
        or "session_blocked" in lowered
    )
    captcha = any(marker in lowered for marker in ("captcha", "verify_center", "bdturing"))
    shadow = False
    return success_marker, duplicate, restriction, captcha, shadow


def score_response(observation: ResponseObservation) -> ScoreBreakdown:
    """Score one recorded response without performing I/O.

    ``score`` is a delivery-oriented 0-10 score for compatibility with the
    existing UI. ``impact_percent`` is intentionally separate: a 5xx, high
    latency, or defensive response can indicate pressure even when delivery
    was unsuccessful.
    """

    status = _status(observation.http_status)
    success_marker, duplicate, restricted, captcha, _ = _body_flags(observation.body)
    server_code = str(observation.server_code) if observation.server_code is not None else ""
    reasons: list[str] = []

    if status == 200:
        status_component = 1.0
    elif status in (403, 429):
        status_component = 0.2
        reasons.append("defense_or_rate_limit")
    elif status is not None and 500 <= status <= 503:
        status_component = 0.1
        reasons.append("server_error")
    elif status is not None and 200 <= status < 300:
        status_component = 0.8
    else:
        status_component = 0.0
        reasons.append("non_success_status")

    if restricted:
        body_component = 0.0
        restriction = "action_block"
        reasons.append("session_restriction")
    elif captcha:
        body_component = 0.1
        restriction = "captcha"
        reasons.append("captcha")
    elif duplicate:
        body_component = 0.35
        restriction = "already_reported"
        reasons.append("duplicate_or_deduplicated")
    elif success_marker or observation.success is True or server_code == "0":
        body_component = 1.0
        restriction = None
    elif observation.success is False:
        body_component = 0.2
        restriction = None
        reasons.append("body_not_accepted")
    else:
        body_component = 0.5
        restriction = None
        reasons.append("ambiguous_body")

    latency = max(0.0, float(observation.latency_ms or 0.0))
    if latency <= 1200:
        latency_component = 1.0
    elif latency <= 2500:
        latency_component = 0.7
        reasons.append("elevated_latency")
    else:
        latency_component = 0.3
        reasons.append("high_latency")

    # A 200 response with no positive marker is treated as potentially shadowed.
    shadow_report = (
        status == 200
        and not (success_marker or observation.success is True or server_code == "0")
        and not (duplicate or restricted or captcha)
    )
    if shadow_report:
        reasons.append("possible_shadow_response")
        body_component = min(body_component, 0.25)

    score = round(
        max(
            0.0,
            min(
                10.0,
                (0.45 * status_component + 0.4 * body_component + 0.15 * latency_component)
                * 10,
            ),
        ),
        1,
    )
    if shadow_report:
        score = min(score, 4.0)

    pressure = 0.0
    if status in (403, 429):
        pressure += 0.65
    if status is not None and 500 <= status <= 503:
        pressure += 0.8
    if latency > 1200:
        pressure += min(0.35, (latency - 1200) / 4000)
    if captcha or restricted:
        pressure += 0.45
    if observation.target_state_changed is True:
        pressure += 0.5
    impact_percent = round(max(0.0, min(100.0, pressure * 100.0)), 1)

    return ScoreBreakdown(
        score=score,
        impact_percent=impact_percent,
        status_component=round(status_component, 3),
        body_component=round(body_component, 3),
        latency_component=round(latency_component, 3),
        pressure_component=round(min(1.0, pressure), 3),
        shadow_report=shadow_report,
        restriction=restriction,
        reasons=tuple(dict.fromkeys(reasons)),
    )
