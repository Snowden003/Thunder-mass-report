# -*- coding: utf-8 -*-
"""
Pre-Flight Analytical Module: Profile Search & Diagnostic Panel

Provides target discovery, AI-powered diagnostic assessment, and
execution forecasting for the THUNDER reporting engine.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field, asdict
from typing import Any, Dict, List, Optional

from .gemini_client import GeminiClient, _extract_json_dict

log = logging.getLogger("ProfileAnalyzer")


# ──────────────────────────────────────────────────────────────────────────────
# Data Types
# ──────────────────────────────────────────────────────────────────────────────

@dataclass(slots=True)
class ProfileMetadata:
    """Raw profile data fetched from TikTok."""
    username: str
    user_id: str = ""
    sec_uid: str = ""
    display_name: str = ""
    followers: int = 0
    following: int = 0
    likes: int = 0
    country: str = "—"
    region: str = "—"
    private: bool = False
    verified: bool = False
    bio: str = ""
    joined_date: str = "—"
    video_count: int = 0
    is_live: bool = False
    avatar_url: str = ""
    language: str = "—"
    last_post_timestamp: int = 0
    engagement_rate: float = 0.0
    raw_data: Dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AIDiagnosticResult:
    """AI diagnostic assessment output."""
    risk_level: str = "Medium"                    # Low / Medium / High / Critical
    activity_level: str = "Moderate"              # Dormant / Low / Moderate / High / Viral
    engagement_quality: str = "Average"           # Poor / Below Average / Average / High / Exceptional
    account_health: str = "Clean"                 # Clean / Flagged / Shadowbanned / Restricted
    content_category: str = "General"             # General / Entertainment / Education / etc.
    operational_risk: str = "Moderate"            # Low / Moderate / High / Critical
    confidence_score: float = 0.7                 # 0.0 - 1.0
    primary_concerns: List[str] = field(default_factory=list)
    recommended_strategy: str = "Standard rotation with moderate pacing"
    optimal_vectors: List[str] = field(default_factory=list)
    avoid_vectors: List[str] = field(default_factory=list)
    pacing_recommendation: str = "5-15s randomized jitter"
    session_rotation: bool = False
    device_mutation: bool = False
    raw_response: str = ""


@dataclass(slots=True)
class ExecutionForecast:
    """Resource & execution forecasting."""
    estimated_requests_to_review: int = 50
    estimated_duration_minutes: float = 10.0
    safe_rate_rpm: int = 12                       # requests per minute
    recommended_workers: int = 2
    recommended_concurrent_sessions: int = 3
    memory_estimate_mb: int = 128
    cpu_estimate_percent: int = 15
    network_estimate_mbps: float = 2.0
    proxy_bandwidth_mbps: float = 1.5
    estimated_success_rate: float = 0.65
    estimated_captcha_rate: float = 0.05
    estimated_skip3_rate: float = 0.10
    total_budget_seconds: float = 600.0
    confidence: float = 0.7


@dataclass(slots=True)
class PreFlightReport:
    """Complete pre-flight analysis report."""
    timestamp: str = field(default_factory=lambda: time.strftime("%Y-%m-%d %H:%M:%S"))
    target_username: str = ""
    profile: Optional[ProfileMetadata] = None
    ai_diagnostic: Optional[AIDiagnosticResult] = None
    forecast: Optional[ExecutionForecast] = None
    status: str = "pending"  # pending / success / partial / failed
    errors: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "target_username": self.target_username,
            "profile": asdict(self.profile) if self.profile else None,
            "ai_diagnostic": asdict(self.ai_diagnostic) if self.ai_diagnostic else None,
            "forecast": asdict(self.forecast) if self.forecast else None,
            "status": self.status,
            "errors": self.errors,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)


# ──────────────────────────────────────────────────────────────────────────────
# Profile Fetcher
# ──────────────────────────────────────────────────────────────────────────────

class ProfileFetcher:
    """Fetches TikTok profile metadata using multiple strategies."""

    def __init__(self, proxy_manager=None):
        self.proxy_manager = proxy_manager

    def fetch(self, username: str) -> ProfileMetadata:
        """Fetch profile using primary and fallback methods."""
        # Strategy 1: Web scraping
        profile = self._fetch_web(username)
        if profile and profile.user_id:
            return profile

        # Strategy 2: ms4 library fallback
        profile = self._fetch_ms4(username)
        if profile and profile.user_id:
            return profile

        # Return minimal profile on total failure
        return ProfileMetadata(username=username, raw_data={"error": "All fetch methods failed"})

    def _fetch_web(self, username: str) -> Optional[ProfileMetadata]:
        """Fetch via web page parsing."""
        import re

        from .proxy_manager import tls_get

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9',
        }
        url = f'https://www.tiktok.com/@{username}'

        proxies = None
        if self.proxy_manager:
            try:
                proxy_dict = self.proxy_manager.get()
                if proxy_dict:
                    proxies = {k: v for k, v in proxy_dict.items() if k != '_entry'}
            except Exception:
                pass

        try:
            res = tls_get(url, headers=headers, proxies=proxies, timeout=15)
            if res.status_code != 200:
                return None

            # Parse __UNIVERSAL_DATA_FOR_REHYDRATION__
            match = re.search(r'<script id="__UNIVERSAL_DATA_FOR_REHYDRATION__"[^>]*>(.*?)</script>', res.text)
            if match:
                data = json.loads(match.group(1))
                return self._parse_universal_data(data, username)

            # Parse webapp.user-detail
            if 'webapp.user-detail"' in res.text:
                getting = str(res.text.split('webapp.user-detail"')[1]).split('"RecommendUserList"')[0]
                def ex(k, end='",'):
                    try: return str(getting.split(k)[1]).split(end)[0]
                    except: return ''

                uid = ex('id":"')
                if uid and uid.isdigit():
                    return ProfileMetadata(
                        username=username,
                        user_id=uid,
                        display_name=ex('nickname":"'),
                        sec_uid=ex('secUid":"'),
                        followers=int(ex('followerCount":', ',"') or 0),
                        following=int(ex('followingCount":', ',"') or 0),
                        likes=int(ex('heart":', ',"') or 0),
                        country=ex('region":"'),
                        private=ex('privateAccount":', ',"').lower() == 'true',
                        bio=ex('signature":"'),
                        raw_data={"source": "webapp.user-detail"},
                    )
        except Exception as e:
            log.debug("Web fetch failed for @%s: %s", username, e)

        return None

    def _fetch_ms4(self, username: str) -> Optional[ProfileMetadata]:
        """Fetch via ms4 library."""
        try:
            from ms4 import InfoTik
            ms_info = InfoTik.TikTok_Info(username)
            if ms_info and ms_info.get('status') == 'ok' and ms_info.get('id'):
                dt = ms_info.get('Date')
                date_str = dt.strftime('%Y-%m-%d') if hasattr(dt, 'strftime') else str(dt or '—')
                return ProfileMetadata(
                    username=username,
                    user_id=str(ms_info.get('id', '')),
                    display_name=ms_info.get('name', '') or username,
                    sec_uid=ms_info.get('secuid', ''),
                    followers=ms_info.get('followers', 0),
                    following=ms_info.get('following', 0),
                    likes=ms_info.get('like', 0),
                    country=ms_info.get('country', '') or '—',
                    private=str(ms_info.get('private', 'false')).lower() == 'true',
                    bio=ms_info.get('bio', ''),
                    joined_date=date_str,
                    raw_data={"source": "ms4"},
                )
        except Exception as e:
            log.debug("ms4 fetch failed for @%s: %s", username, e)

        return None

    def _parse_universal_data(self, data: Dict, username: str) -> Optional[ProfileMetadata]:
        """Parse TikTok's __UNIVERSAL_DATA_FOR_REHYDRATION__ structure."""
        try:
            default_scope = data.get('__DEFAULT_SCOPE__', {})
            user_detail = default_scope.get('webapp.user-detail', {})
            if user_detail.get('statusCode') == 10221:
                return None

            user_info = user_detail.get('userInfo', {}).get('user', {})
            stats = user_detail.get('userInfo', {}).get('stats', {})
            if not user_info or not user_info.get('id'):
                return None

            uid = str(user_info.get('id', ''))
            reg_date = '—'
            if uid.isdigit():
                binary = '{0:b}'.format(int(uid))
                if len(binary) >= 32:
                    try:
                        timestamp = int(binary[:31], 2)
                        reg_date = time.strftime('%Y-%m-%d', time.localtime(timestamp))
                    except Exception:
                        pass

            region = user_info.get('region', '')
            c_name = region
            try:
                import pycountry
                c_obj = pycountry.countries.get(alpha_2=region)
                if c_obj:
                    c_name = c_obj.name
            except Exception:
                pass

            return ProfileMetadata(
                username=username,
                user_id=uid,
                display_name=user_info.get('nickname', ''),
                sec_uid=user_info.get('secUid', ''),
                followers=stats.get('followerCount', 0),
                following=stats.get('followingCount', 0),
                likes=stats.get('heartCount', 0),
                country=c_name,
                region=region,
                private=user_info.get('privateAccount', False),
                verified=user_info.get('verified', False),
                bio=user_info.get('signature', ''),
                joined_date=reg_date,
                video_count=stats.get('videoCount', 0),
                is_live=user_info.get('isLive', False),
                avatar_url=user_info.get('avatarLarger', ''),
                language=user_info.get('language', '—'),
                engagement_rate=self._calc_engagement(stats),
                raw_data={"source": "universal_data"},
            )
        except Exception as e:
            log.debug("Universal data parse failed: %s", e)

        return None

    @staticmethod
    def _calc_engagement(stats: Dict) -> float:
        """Calculate engagement rate from stats."""
        followers = stats.get('followerCount', 0)
        likes = stats.get('heartCount', 0)
        if followers > 0:
            return round((likes / followers) * 100, 2)
        return 0.0


# ──────────────────────────────────────────────────────────────────────────────
# AI Diagnostic Engine
# ──────────────────────────────────────────────────────────────────────────────

class AIDiagnosticEngine:
    """AI-powered diagnostic assessment using Gemini."""

    def __init__(self, gemini_client: GeminiClient):
        self.gemini = gemini_client

    def analyze(
        self,
        profile: ProfileMetadata,
        reporting_context: Dict[str, Any],
    ) -> AIDiagnosticResult:
        """Run comprehensive AI diagnostic on target profile."""
        prompt = self._build_diagnostic_prompt(profile, reporting_context)
        raw = self.gemini._ask(prompt, max_tokens=800)

        result = AIDiagnosticResult()
        if raw:
            parsed = _extract_json_dict(raw)
            if parsed and isinstance(parsed, dict):
                result.risk_level = str(parsed.get("risk_level", result.risk_level))
                result.activity_level = str(parsed.get("activity_level", result.activity_level))
                result.engagement_quality = str(parsed.get("engagement_quality", result.engagement_quality))
                result.account_health = str(parsed.get("account_health", result.account_health))
                result.content_category = str(parsed.get("content_category", result.content_category))
                result.operational_risk = str(parsed.get("operational_risk", result.operational_risk))
                result.confidence_score = float(parsed.get("confidence_score", result.confidence_score))
                result.primary_concerns = list(parsed.get("primary_concerns", []))
                result.recommended_strategy = str(parsed.get("recommended_strategy", result.recommended_strategy))
                result.optimal_vectors = list(parsed.get("optimal_vectors", []))
                result.avoid_vectors = list(parsed.get("avoid_vectors", []))
                result.pacing_recommendation = str(parsed.get("pacing_recommendation", result.pacing_recommendation))
                result.session_rotation = bool(parsed.get("session_rotation", result.session_rotation))
                result.device_mutation = bool(parsed.get("device_mutation", result.device_mutation))
                result.raw_response = raw.strip()
            else:
                result.raw_response = raw.strip()
                result.recommended_strategy = "AI response parse failed; using defaults"

        # Update Gemini panel for live display
        self.gemini.panel.update({
            "preflight_done": True,
            "preflight_risk": result.risk_level,
            "preflight_activity": result.activity_level,
            "preflight_strategy": result.recommended_strategy,
            "preflight_vectors": ", ".join(result.optimal_vectors) if result.optimal_vectors else "auto",
        })

        return result

    def _build_diagnostic_prompt(
        self,
        profile: ProfileMetadata,
        context: Dict[str, Any],
    ) -> str:
        """Build comprehensive diagnostic prompt for Gemini."""

        def fmt(v):
            try: return f"{int(v):,}"
            except: return str(v or "—")

        vectors = context.get("available_vectors", [])
        vector_labels = [f"{v[0]}:{v[2][:30]}" for v in vectors] if vectors else ["default"]

        return (
            "You are an AI Sentinel analyzing a TikTok target account for automated moderation reporting.\n"
            "Provide a comprehensive diagnostic assessment. Reply ONLY with valid JSON containing EXACTLY these keys:\n\n"
            "{\n"
            '  "risk_level": "Low|Medium|High|Critical",\n'
            '  "activity_level": "Dormant|Low|Moderate|High|Viral",\n'
            '  "engagement_quality": "Poor|Below Average|Average|High|Exceptional",\n'
            '  "account_health": "Clean|Flagged|Shadowbanned|Restricted",\n'
            '  "content_category": "string",\n'
            '  "operational_risk": "Low|Moderate|High|Critical",\n'
            '  "confidence_score": <float 0.0-1.0>,\n'
            '  "primary_concerns": ["concern1", "concern2"],\n'
            '  "recommended_strategy": "1-2 sentences: best approach to maximize accepted reports",\n'
            '  "optimal_vectors": ["vector_code1", "vector_code2"],\n'
            '  "avoid_vectors": ["vector_code1"],\n'
            '  "pacing_recommendation": "string (e.g. \\"8-20s randomized jitter\\")",\n'
            '  "session_rotation": <bool>,\n'
            '  "device_mutation": <bool>\n'
            "}\n\n"
            "--- TARGET PROFILE ---\n"
            f"  Username          : @{profile.username}\n"
            f"  User ID           : {profile.user_id or '—'}\n"
            f"  Display Name      : {profile.display_name or '—'}\n"
            f"  Followers         : {fmt(profile.followers)}\n"
            f"  Following         : {fmt(profile.following)}\n"
            f"  Likes             : {fmt(profile.likes)}\n"
            f"  Video Count       : {fmt(profile.video_count)}\n"
            f"  Country/Region    : {profile.country} ({profile.region})\n"
            f"  Private           : {profile.private}\n"
            f"  Verified          : {profile.verified}\n"
            f"  Joined            : {profile.joined_date}\n"
            f"  Engagement Rate   : {profile.engagement_rate:.2f}%\n"
            f"  Bio               : {profile.bio[:200] if profile.bio else '—'}\n"
            f"  Last Active       : {'Live' if profile.is_live else 'Unknown'}\n\n"
            "--- REPORTING CONTEXT ---\n"
            f"  Run Mode          : {context.get('run_mode', 'web')}\n"
            f"  Available Vectors : {', '.join(vector_labels)}\n"
            f"  Sessions Available: {context.get('num_sessions', 1)}\n"
            f"  Proxy Mesh        : {context.get('proxy_count', 0)} nodes\n\n"
            "ANALYSIS GUIDELINES:\n"
            "- High followers + high engagement = Low risk, can use aggressive pacing\n"
            "- Private accounts = Higher operational risk, use conservative settings\n"
            "- New accounts (< 30 days) = High risk, minimal requests\n"
            "- Shadowbanned/flagged indicators: low engagement despite high followers\n"
            "- Match vectors to content category (e.g. spam→spam vector)\n"
            "- Multiple sessions = enable rotation, single session = device mutation\n"
            "Reply ONLY with valid JSON. No markdown, no explanation."
        )


# ──────────────────────────────────────────────────────────────────────────────
# Execution Forecaster
# ──────────────────────────────────────────────────────────────────────────────

class ExecutionForecaster:
    """Calculates execution forecasts based on profile, AI diagnostic, and config."""

    def __init__(self, gemini_client: GeminiClient):
        self.gemini = gemini_client

    def forecast(
        self,
        profile: ProfileMetadata,
        diagnostic: AIDiagnosticResult,
        config: Dict[str, Any],
    ) -> ExecutionForecast:
        """Generate execution forecast using heuristics + AI refinement."""
        forecast = self._heuristic_forecast(profile, diagnostic, config)

        # Refine with AI if available
        if self.gemini and self.gemini.panel.get("status", "").startswith("OK"):
            ai_forecast = self._ai_refine_forecast(profile, diagnostic, config, forecast)
            if ai_forecast:
                forecast = ai_forecast

        return forecast

    def _heuristic_forecast(
        self,
        profile: ProfileMetadata,
        diagnostic: AIDiagnosticResult,
        config: Dict[str, Any],
    ) -> ExecutionForecast:
        """Heuristic-based forecast."""
        followers = profile.followers
        engagement = profile.engagement_rate
        risk = diagnostic.risk_level.lower() if diagnostic.risk_level else "medium"

        # Base estimates by risk level
        risk_multipliers = {
            "low": {"requests": 1.0, "duration": 1.0, "rate": 1.2, "success": 1.1},
            "medium": {"requests": 1.2, "duration": 1.2, "rate": 1.0, "success": 1.0},
            "high": {"requests": 1.8, "duration": 1.6, "rate": 0.7, "success": 0.8},
            "critical": {"requests": 3.0, "duration": 2.5, "rate": 0.4, "success": 0.5},
        }
        mult = risk_multipliers.get(risk, risk_multipliers["medium"])

        # Adjust for engagement
        if engagement > 5.0:
            mult["success"] *= 1.15
        elif engagement < 1.0:
            mult["success"] *= 0.85

        # Adjust for account size
        if followers > 1_000_000:
            mult["rate"] *= 0.8  # Large accounts watched more closely
        elif followers < 10_000:
            mult["rate"] *= 1.2  # Small accounts more permissive

        base_requests = config.get("target_requests", 50)
        base_rate = config.get("base_rate_rpm", 12)

        est_requests = int(base_requests * mult["requests"])
        safe_rate = max(1, min(60, int(base_rate * mult["rate"])))
        duration_min = max(1.0, (est_requests / max(1, safe_rate)) * mult["duration"])
        success_rate = max(0.1, min(0.95, 0.65 * mult["success"]))

        workers = config.get("workers", 2)
        sessions = config.get("num_sessions", 1)
        proxy_count = config.get("proxy_count", 0)

        return ExecutionForecast(
            estimated_requests_to_review=est_requests,
            estimated_duration_minutes=round(duration_min, 1),
            safe_rate_rpm=safe_rate,
            recommended_workers=min(workers, max(1, sessions)),
            recommended_concurrent_sessions=min(sessions, max(1, proxy_count // 2)),
            estimated_success_rate=round(success_rate, 2),
            estimated_captcha_rate=round(0.05 * mult.get("captcha_mult", 1.0), 3),
            estimated_skip3_rate=round(0.10 * mult.get("skip3_mult", 1.0), 3),
            confidence=0.7,
        )

    def _ai_refine_forecast(
        self,
        profile: ProfileMetadata,
        diagnostic: AIDiagnosticResult,
        config: Dict[str, Any],
        heuristic: ExecutionForecast,
    ) -> Optional[ExecutionForecast]:
        """Ask AI to refine the forecast."""
        prompt = (
            "Refine this execution forecast for a TikTok reporting campaign. "
            "Reply ONLY with JSON containing these keys:\n"
            '{\n'
            '  "estimated_requests_to_review": <int>,\n'
            '  "estimated_duration_minutes": <float>,\n'
            '  "safe_rate_rpm": <int 1-60>,\n'
            '  "estimated_success_rate": <float 0.0-1.0>,\n'
            '  "estimated_captcha_rate": <float 0.0-1.0>,\n'
            '  "estimated_skip3_rate": <float 0.0-1.0>,\n'
            '  "confidence": <float 0.0-1.0>\n'
            "}\n\n"
            f"Current heuristic: {heuristic.estimated_requests_to_review} reqs, "
            f"{heuristic.estimated_duration_minutes:.1f}min, "
            f"{heuristic.safe_rate_rpm} rpm, "
            f"{heuristic.estimated_success_rate:.0%} success\n\n"
            f"Profile: @{profile.username}, {profile.followers:,} followers, "
            f"{profile.engagement_rate:.1f}% engagement, risk={diagnostic.risk_level}\n"
            f"Context: {config.get('workers', 2)} workers, {config.get('num_sessions', 1)} sessions, "
            f"{config.get('proxy_count', 0)} proxies\n\n"
            "Output ONLY the JSON."
        )
        raw = self.gemini._ask(prompt, max_tokens=300)
        if raw:
            parsed = _extract_json_dict(raw)
            if parsed and isinstance(parsed, dict):
                return ExecutionForecast(
                    estimated_requests_to_review=int(parsed.get("estimated_requests_to_review", heuristic.estimated_requests_to_review)),
                    estimated_duration_minutes=float(parsed.get("estimated_duration_minutes", heuristic.estimated_duration_minutes)),
                    safe_rate_rpm=int(parsed.get("safe_rate_rpm", heuristic.safe_rate_rpm)),
                    estimated_success_rate=float(parsed.get("estimated_success_rate", heuristic.estimated_success_rate)),
                    estimated_captcha_rate=float(parsed.get("estimated_captcha_rate", heuristic.estimated_captcha_rate)),
                    estimated_skip3_rate=float(parsed.get("estimated_skip3_rate", heuristic.estimated_skip3_rate)),
                    confidence=float(parsed.get("confidence", heuristic.confidence)),
                    recommended_workers=heuristic.recommended_workers,
                    recommended_concurrent_sessions=heuristic.recommended_concurrent_sessions,
                )
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Pre-Flight Orchestrator
# ──────────────────────────────────────────────────────────────────────────────

class PreFlightAnalyzer:
    """Main orchestrator for pre-flight analysis pipeline."""

    def __init__(
        self,
        gemini_client: GeminiClient,
        proxy_manager=None,
    ):
        self.gemini = gemini_client
        self.fetcher = ProfileFetcher(proxy_manager)
        self.diagnostic_engine = AIDiagnosticEngine(gemini_client)
        self.forecaster = ExecutionForecaster(gemini_client)

    def run(
        self,
        username: str,
        reporting_config: Dict[str, Any],
        show_progress: bool = True,
    ) -> PreFlightReport:
        """Execute full pre-flight analysis pipeline."""
        report = PreFlightReport(target_username=username)

        if show_progress:
            self._print_banner("PRE-FLIGHT ANALYTICAL MODULE")

        # Phase 1: Profile Discovery
        if show_progress:
            self._print_step("Phase 1/3", "Target Discovery & Profile Lookup")

        try:
            profile = self.fetcher.fetch(username)
            report.profile = profile
            if not profile.user_id:
                report.errors.append("Could not resolve target user ID")
                report.status = "partial"
            else:
                report.status = "success"
            if show_progress:
                self._print_profile_summary(profile)
        except Exception as e:
            report.errors.append(f"Profile fetch failed: {e}")
            report.status = "failed"
            if show_progress:
                print(f"  ✗ Profile fetch error: {e}")

        # Phase 2: AI Diagnostic
        if show_progress and report.profile:
            self._print_step("Phase 2/3", "AI Diagnostic Assessment")

        if report.profile and report.profile.user_id:
            try:
                diagnostic = self.diagnostic_engine.analyze(
                    report.profile,
                    reporting_config,
                )
                report.ai_diagnostic = diagnostic
                if show_progress:
                    self._print_diagnostic_summary(diagnostic)
            except Exception as e:
                report.errors.append(f"AI diagnostic failed: {e}")
                if show_progress:
                    print(f"  ✗ AI diagnostic error: {e}")

        # Phase 3: Execution Forecast
        if show_progress and report.profile and report.ai_diagnostic:
            self._print_step("Phase 3/3", "Execution Forecasting")

        if report.profile and report.ai_diagnostic:
            try:
                forecast = self.forecaster.forecast(
                    report.profile,
                    report.ai_diagnostic,
                    reporting_config,
                )
                report.forecast = forecast
                if show_progress:
                    self._print_forecast_summary(forecast)
            except Exception as e:
                report.errors.append(f"Forecast failed: {e}")
                if show_progress:
                    print(f"  ✗ Forecast error: {e}")

        if show_progress:
            self._print_completion(report)

        return report

    # ─── Display Helpers ──────────────────────────────────────────────────────

    def _print_banner(self, title: str):
        print()
        print(f"  ╔{'═' * 60}╗")
        print(f"  ║  {title:<56}║")
        print(f"  ╚{'═' * 60}╝")

    def _print_step(self, phase: str, title: str):
        print(f"\n  ▶ {phase}: {title}")
        print(f"    {'─' * 50}")

    def _print_profile_summary(self, p: ProfileMetadata):
        risk_indicator = "🟢" if p.followers > 10000 else ("🟡" if p.followers > 1000 else "🔴")
        print(f"  {risk_indicator} @{p.username} ({p.display_name})")
        print(f"    UID: {p.user_id or '—'} | SecUID: {p.sec_uid[:16] + '...' if p.sec_uid else '—'}")
        print(f"    Followers: {p.followers:,} | Following: {p.following:,} | Likes: {p.likes:,}")
        print(f"    Videos: {p.video_count:,} | Engagement: {p.engagement_rate:.2f}%")
        print(f"    Country: {p.country} | Private: {'Yes' if p.private else 'No'} | Verified: {'Yes' if p.verified else 'No'}")
        print(f"    Joined: {p.joined_date} | Bio: {(p.bio[:60] + '...') if p.bio and len(p.bio) > 60 else (p.bio or '—')}")

    def _print_diagnostic_summary(self, d: AIDiagnosticResult):
        risk_colors = {"Low": "🟢", "Medium": "🟡", "High": "🟠", "Critical": "🔴"}
        risk_icon = risk_colors.get(d.risk_level, "⚪")
        print(f"  {risk_icon} Risk Level: {d.risk_level} (Confidence: {d.confidence_score:.0%})")
        print(f"  Activity: {d.activity_level} | Engagement: {d.engagement_quality} | Health: {d.account_health}")
        print(f"  Content Category: {d.content_category}")
        if d.primary_concerns:
            print(f"  ⚠ Concerns: {', '.join(d.primary_concerns)}")
        print(f"  Strategy: {d.recommended_strategy}")
        if d.optimal_vectors:
            print(f"  ✓ Optimal Vectors: {', '.join(d.optimal_vectors)}")
        if d.avoid_vectors:
            print(f"  ✗ Avoid Vectors: {', '.join(d.avoid_vectors)}")
        print(f"  Pacing: {d.pacing_recommendation}")
        print(f"  Session Rotation: {'Yes' if d.session_rotation else 'No'} | Device Mutation: {'Yes' if d.device_mutation else 'No'}")

    def _print_forecast_summary(self, f: ExecutionForecast):
        print(f"  📊 Estimated Requests to Review: {f.estimated_requests_to_review:,}")
        print(f"  ⏱ Estimated Duration: {f.estimated_duration_minutes:.1f} minutes")
        print(f"  ⚡ Safe Rate: {f.safe_rate_rpm} req/min")
        print(f"  🎯 Success Rate: {f.estimated_success_rate:.0%} | Captcha: {f.estimated_captcha_rate:.1%} | Skip3: {f.estimated_skip3_rate:.1%}")
        print(f"  Workers: {f.recommended_workers} | Concurrent Sessions: {f.recommended_concurrent_sessions}")
        print(f"  Resources: ~{f.memory_estimate_mb}MB RAM, ~{f.cpu_estimate_percent}% CPU, {f.network_estimate_mbps:.1f} Mbps")
        print(f"  Confidence: {f.confidence:.0%}")

    def _print_completion(self, report: PreFlightReport):
        status_icons = {"success": "✅", "partial": "⚠️", "failed": "❌", "pending": "⏳"}
        icon = status_icons.get(report.status, "❓")
        print(f"\n  {icon} Pre-Flight Analysis: {report.status.upper()}")
        if report.errors:
            print(f"  Errors: {len(report.errors)}")


# ──────────────────────────────────────────────────────────────────────────────
# Convenience Functions
# ──────────────────────────────────────────────────────────────────────────────

def run_preflight_analysis(
    username: str,
    gemini_client: GeminiClient,
    proxy_manager=None,
    config: Optional[Dict[str, Any]] = None,
    show_progress: bool = True,
) -> PreFlightReport:
    """Convenience function to run full pre-flight analysis."""
    default_config = {
        "run_mode": "web",
        "base_rate_rpm": 12,
        "target_requests": 50,
        "workers": 2,
        "num_sessions": 1,
        "proxy_count": 0,
        "available_vectors": [],
    }
    if config:
        default_config.update(config)

    analyzer = PreFlightAnalyzer(gemini_client, proxy_manager)
    return analyzer.run(username, default_config, show_progress)