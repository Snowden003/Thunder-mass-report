# -*- coding: utf-8 -*-
"""
Core package for THUNDER TikTok Reporter.
Exposes cryptographic signers, network/proxy handlers, and device utilities.
"""

import sys
import os

# Ensure this directory is in sys.path so submodules can import each other seamlessly
_CORE_DIR = os.path.dirname(os.path.abspath(__file__))
if _CORE_DIR not in sys.path:
    sys.path.insert(0, _CORE_DIR)

from mobile.Gorgon import Gorgon
from mobile.Argus import Argus
from mobile.Ladon import Ladon
from mobile.TikTokAg import UserAgent
from mobile.tiktok import device_register, tt_encrypt
from mobile.ttencrypt import ttencrypt
from web.web_devices import pick_web_device, generate_web_device_id, get_web_device_profiles, WEB_DEVICE_PROFILES

from .proxy_manager import ProxyManager, make_request_with_proxy, tls_get
from .redis_manager import RedisSessionManager, get_redis_client
from .telegram_bot import TelegramReporterBot
from .gemini_client import GeminiClient, SessionMonitor
from .profile_analyzer import (
    PreFlightAnalyzer,
    ProfileFetcher,
    AIDiagnosticEngine,
    ExecutionForecaster,
    ProfileMetadata,
    AIDiagnosticResult,
    ExecutionForecast,
    PreFlightReport,
    run_preflight_analysis,
)
from .preflight_consultation import (
    PreflightConsultation,
    ConsultationReport,
    ScreenshotAnalysis,
    run_consultation_cli,
)
from .report_engine import ReportEngine
from .project_logger import ProjectLogger

__all__ = [
    "Gorgon",
    "Argus",
    "Ladon",
    "UserAgent",
    "ProxyManager",
    "make_request_with_proxy",
    "tls_get",
    "device_register",
    "tt_encrypt",
    "ttencrypt",
    "RedisSessionManager",
    "get_redis_client",
    "TelegramReporterBot",
    "pick_web_device",
    "generate_web_device_id",
    "get_web_device_profiles",
    "WEB_DEVICE_PROFILES",
    "GeminiClient",
    "SessionMonitor",
    "PreFlightAnalyzer",
    "ProfileFetcher",
    "AIDiagnosticEngine",
    "ExecutionForecaster",
    "ProfileMetadata",
    "AIDiagnosticResult",
    "ExecutionForecast",
    "PreFlightReport",
    "run_preflight_analysis",
    "PreflightConsultation",
    "ConsultationReport",
    "ScreenshotAnalysis",
    "run_consultation_cli",
    "ReportEngine",
    "ProjectLogger",
]