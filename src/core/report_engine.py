# -*- coding: utf-8 -*-
"""
Report Execution Engine for THUNDER Reporter.

Ports the proven reporting loop (previously embedded in the Flask dashboard)
into a standalone, thread-safe engine that the CLI UI can drive directly.

The engine owns no UI. It exposes a thread-safe snapshot() for rendering and
start()/stop()/pause()/resume() controls.
"""

from __future__ import annotations

import json
import os
import random
import threading
import time
from binascii import hexlify
from collections import deque
from datetime import datetime
from random import randbytes
from urllib.parse import urlencode
from uuid import uuid4

import requests

from mobile.Argus import Argus
from mobile.Gorgon import Gorgon
from mobile.Ladon import Ladon
from mobile.TikTokAg import UserAgent
from web.web_devices import generate_web_device_id, pick_web_device
from .proxy_manager import make_request_with_proxy
from .account_rotation import AccountRotation
from .project_logger import ProjectLogger

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DATA_DIR = os.path.join(_REPO_ROOT, "data")
LOGS_DIR = os.path.join(_REPO_ROOT, "logs")


def _ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(LOGS_DIR, exist_ok=True)


def calculate_request_score(success: bool, http_status, server_code, latency_ms: float) -> float:
    """Score a request 0-10 based on success, status, server code and latency."""
    if success:
        score = 10.0
    elif str(http_status) == "200" and str(server_code) in ("0", "—"):
        score = 6.0
    elif str(http_status) == "200":
        score = 5.0
    elif str(http_status) in ("429", "403"):
        score = 2.0
    else:
        score = 1.0
    if latency_ms > 2500:
        score = max(0.0, score - 1.5)
    elif latency_ms > 1200:
        score = max(0.0, score - 0.5)
    return round(score, 1)


class ReportEngine:
    """Threaded TikTok report engine."""

    def __init__(
        self,
        target: dict,
        mode: str = "mobile",
        reason_code: str = "90084",
        category: str = "porn",
        reason_label: str = "Nudity & Sexual Content / Pornography",
        delay_min: float = 5.0,
        delay_max: float = 17.0,
        is_random: bool = True,
        proxy_manager=None,
        session_manager=None,
        sessions: list | None = None,
        reasons: list | None = None,
        ai_client=None,
        primary_account_count: int = 10,
        primary_account_rounds: int = 3,
        secondary_interleave_count: int = 3,
        secondary_account_rounds: int = 3,
        rotation_seed: int | None = None,
    ):
        self.target = dict(target or {})
        self.mode = mode
        self.reason_code = reason_code
        self.category = category
        self.reason_label = reason_label
        self.delay_min = float(delay_min)
        self.delay_max = float(delay_max)
        self.is_random = bool(is_random)
        self.proxy_manager = proxy_manager
        self.session_manager = session_manager
        self._rotation = AccountRotation(
            sessions or [],
            primary_count=primary_account_count,
            primary_rounds=primary_account_rounds,
            secondary_interleave=secondary_interleave_count,
            secondary_rounds=secondary_account_rounds,
            seed=rotation_seed,
        )
        self._sessions: list = []
        self.sessions = list(sessions or [])
        self._current_session_label = ""
        self.reasons = list(reasons or [])
        self._reason_index = 0

        # ── AI Sentinel ─────────────────────────────────────────────────────
        self.ai_client = ai_client
        self.ai_monitor = None
        self._ai_lock = threading.Lock()
        self._ai_busy = False
        self._ai_last_call = 0.0
        self._ai_delay_override: float | None = None
        self._ai_reason_code: str | None = None
        self._consec_success = 0
        self._consec_fail = 0

        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._pause = threading.Event()
        self._thread: threading.Thread | None = None

        self.accepted = 0
        self.rejected = 0
        self.captcha_hits = 0
        self.last_request: dict = {}
        self.request_log: deque = deque(maxlen=50)
        self.blocked_devices: dict = {}
        self.current_sleep: float = 0.0
        self.last_error: str = ""
        self.start_time: float | None = None
        self.running = False
        self.finished = False

        self._register = threading.Lock()
        self._data_lock = threading.Lock()
        self._log_file = ""
        self.project_logger: ProjectLogger | None = None
        self._project_log_dir = ""
        _ensure_dirs()

    @property
    def sessions(self) -> list:
        return self._sessions

    @sessions.setter
    def sessions(self, value: list | None) -> None:
        self._sessions = list(value or [])
        self._rotation.set_sessions(self._sessions)

    # ── Controls ────────────────────────────────────────────────────────────

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._pause.clear()
        self.finished = False
        self.running = True
        self.start_time = time.time()
        self._log_file = os.path.join(
            LOGS_DIR,
            f"attack_{self.target.get('username', 'target')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl",
        )
        try:
            if self.project_logger is not None:
                self.project_logger.close()
            project_name = self.target.get('username', 'target')
            self.project_logger = ProjectLogger(project_name)
            self._project_log_dir = self.project_logger.path
            self.project_logger.log_event(
                "attack_started",
                target=dict(self.target),
                config={
                    "mode": self.mode,
                    "reason_code": self.reason_code,
                    "category": self.category,
                    "reason_label": self.reason_label,
                    "reasons": list(self.reasons),
                    "delay_min": self.delay_min,
                    "delay_max": self.delay_max,
                    "is_random": self.is_random,
                    "summary_log": os.path.basename(self._log_file),
                },
            )
        except Exception:
            self.project_logger = None
            self._project_log_dir = ""
        if self.ai_client is not None:
            self._start_ai()
        self._thread = threading.Thread(target=self._loop, name="report-engine", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._pause.clear()
        if self.ai_monitor:
            try:
                self.ai_monitor.stop()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=3.0)
        if self.project_logger is not None:
            try:
                stats = {
                    "accepted": self.accepted,
                    "rejected": self.rejected,
                    "captcha_hits": self.captcha_hits,
                }
                self.project_logger.log_event("attack_stopped", stats=stats)
                self.project_logger.close(stats=stats)
            except Exception:
                pass
        self.running = False
        self.finished = True

    # ── AI Sentinel ─────────────────────────────────────────────────────────

    def _start_ai(self):
        """Start health checks and the observational AI monitor."""
        try:
            from .gemini_client import SessionMonitor

            threading.Thread(
                target=self._ai_health_check, name="ai-health", daemon=True
            ).start()

            def _num(v):
                try:
                    return int(str(v).replace(",", ""))
                except (TypeError, ValueError):
                    return 0

            info = {
                "username": self.target.get("username", "target"),
                "followers": _num(self.target.get("followers", 0)),
                "following": _num(self.target.get("following", 0)),
                "likes": _num(self.target.get("likes", 0)),
                "country": self.target.get("country", "—"),
                "private": self.target.get("private", "false"),
                "bio": self.target.get("bio", "—"),
                "date": self.target.get("date", "—"),
                "mode": self.mode,
                "reason": self.reason_code,
            }
            self.ai_monitor = SessionMonitor(
                self.sessions,
                self.session_manager,
                self.ai_client,
                telegram_bot=None,
                target_info=info,
            )
            self.ai_monitor.start()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"AI monitor failed: {exc}"

    def _maybe_adapt_ai(self, req_entry: dict, account_id: str):
        """Trigger an AI adaptation cycle at most once every 30 seconds."""
        if self.ai_client is None:
            return
        with self._ai_lock:
            if self._ai_busy or (time.time() - self._ai_last_call) < 30.0:
                return
            self._ai_busy = True
        base_delay = (
            (self.delay_min + self.delay_max) / 2.0 if self.is_random else self.delay_min
        )
        threading.Thread(
            target=self._ai_adapt,
            args=(
                dict(req_entry),
                base_delay,
                account_id or "anonymous",
                self.target.get("username", "target"),
            ),
            name="ai-adapt",
            daemon=True,
        ).start()

    def _ai_health_check(self):
        try:
            self.ai_client.check_models(timeout=12.0)
        except Exception:
            pass

    def _ai_adapt(self, last_req: dict, current_delay: float, account_id: str, label: str):
        """Run one AI adaptation cycle off the report thread."""
        try:
            vectors = list(self.reasons) or [
                (self.reason_code, self.category, self.reason_label)
            ]
            if self.ai_monitor:
                decision = self.ai_monitor.adapt_after_request(
                    account_id=account_id,
                    label=label,
                    last_req=last_req,
                    current_delay=current_delay,
                    is_random_delay=self.is_random,
                    available_vectors=vectors,
                    consec_successes=self._consec_success,
                    consec_fails=self._consec_fail,
                )
            else:
                decision = self.ai_client.adapt_request_and_pacing(
                    target_user=label,
                    target_info=self.target,
                    last_req=last_req,
                    current_delay=current_delay,
                    is_random_delay=self.is_random,
                    available_vectors=vectors,
                    consecutive_successes=self._consec_success,
                    consecutive_failures=self._consec_fail,
                )
            delay = float(decision.get("recommended_delay", current_delay) or current_delay)
            self._ai_delay_override = max(1.0, min(60.0, delay))
            code = str(decision.get("recommended_vector_code", "auto"))
            if code and code != "auto":
                self._ai_reason_code = code
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"AI adaptation failed: {exc}"
        finally:
            with self._ai_lock:
                self._ai_busy = False
                self._ai_last_call = time.time()

    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    @property
    def is_paused(self) -> bool:
        return self._pause.is_set()

    # ── Snapshot for UI ─────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        with self._lock:
            total = self.accepted + self.rejected
            elapsed_s = int(time.time() - self.start_time) if self.start_time else 0
            h, rem = divmod(elapsed_s, 3600)
            m, s = divmod(rem, 60)
            p_stats = None
            p_healthy = 0
            p_total = 0
            if self.proxy_manager:
                try:
                    p_stats = self.proxy_manager.stats()
                    p_healthy = self.proxy_manager.healthy_count()
                    p_total = len(self.proxy_manager)
                except Exception:
                    pass
            ai_panel = {}
            if self.ai_client is not None:
                try:
                    ai_panel = self.ai_client.get_panel()
                except Exception:
                    ai_panel = {}
            return {
                "accepted": self.accepted,
                "rejected": self.rejected,
                "total": total,
                "rate": round(self.accepted / total * 100, 1) if total else 0.0,
                "captcha_hits": self.captcha_hits,
                "elapsed": f"{h:02d}:{m:02d}:{s:02d}",
                "last_request": dict(self.last_request),
                "request_log": list(self.request_log),
                "blocked_count": sum(1 for exp in self.blocked_devices.values() if time.time() < exp),
                "current_sleep": self.current_sleep,
                "proxy_stats": p_stats,
                "proxy_healthy": p_healthy,
                "proxy_total": p_total,
                "running": self.running,
                "paused": self.is_paused,
                "finished": self.finished,
                "last_error": self.last_error,
                "project_log_dir": self._project_log_dir,
                "ai": ai_panel,
            }

    # ── Worker loop ─────────────────────────────────────────────────────────

    def _loop(self):
        while not self._stop.is_set():
            if self._pause.is_set():
                time.sleep(0.2)
                continue
            try:
                self._do_report()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                if self.project_logger is not None:
                    try:
                        self.project_logger.log_event(
                            "worker_error",
                            error=str(exc),
                            error_type=type(exc).__name__,
                        )
                    except Exception:
                        pass
                time.sleep(1.0)
            delay = (
                random.uniform(self.delay_min, self.delay_max)
                if self.is_random
                else self.delay_min
            )
            with self._lock:
                if self._ai_delay_override is not None:
                    delay = self._ai_delay_override
                    self._ai_delay_override = None
                self.current_sleep = delay
            end = time.time() + delay
            while time.time() < end:
                if self._stop.is_set() or self._pause.is_set():
                    break
                time.sleep(0.1)
            with self._lock:
                self.current_sleep = 0.0
        self.running = False
        self.finished = True
        if self.project_logger is not None and not self.project_logger.closed:
            try:
                stats = {
                    "accepted": self.accepted,
                    "rejected": self.rejected,
                    "captcha_hits": self.captcha_hits,
                }
                self.project_logger.log_event("attack_finished", stats=stats)
                self.project_logger.close(stats=stats)
            except Exception:
                pass

    # ── Helpers ─────────────────────────────────────────────────────────────

    def _pick_device(self) -> dict:
        now = time.time()
        for _ in range(20):
            cand = UserAgent()
            name = f"{cand['brand']} {cand['type']}"
            with self._lock:
                blocked_until = self.blocked_devices.get(name, 0)
            if now >= blocked_until:
                return cand
        return UserAgent()

    def _pick_session(self) -> tuple[dict, str]:
        s = None
        if self.sessions:
            s = self._rotation.next_account()
        elif self.session_manager:
            try:
                s = self.session_manager.get_random_session()
            except Exception:
                s = None
        if not s:
            self._current_session_label = ""
            return {}, ""

        self._current_session_label = (
            s.get("label") or s.get("identifier") or s.get("username") or ""
        )
        cookies = s.get("cookies_dict") or {}
        if not cookies and s.get("cookies"):
            try:
                cookies = json.loads(s["cookies"])
            except Exception:
                cookies = {}
        return cookies, s.get("sessionid", "")

    def _save_registered_device(self, entry: dict):
        path = os.path.join(DATA_DIR, "registered_devices.json")
        with self._register:
            try:
                data = []
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, list):
                        data = []
                data.append(entry)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass

    def _save_important(self, entry: dict):
        path = os.path.join(DATA_DIR, "important_requests.json")
        with self._data_lock:
            try:
                data = []
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                    if not isinstance(data, list):
                        data = []
                data.append(entry)
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(data, f, indent=2)
            except Exception:
                pass

    def _log_request(self, entry: dict):
        try:
            with self._data_lock:
                with open(self._log_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            pass

    # ── Core request ────────────────────────────────────────────────────────

    def _do_report(self):
        target = self.target
        target_id = str(target.get("id", "") or "")
        target_name = target.get("name", "") or target.get("username", "")
        target_user = target.get("username", "")
        sec_uid = target.get("sec_uid", "")

        reason_code = self.reason_code
        category = self.category
        reason_label = self.reason_label
        ai_override = self._ai_reason_code
        if ai_override:
            self._ai_reason_code = None
            match = next(
                (r for r in self.reasons if str(r[0]) == str(ai_override)), None
            )
            if match:
                reason_code, category, reason_label = match
            elif not self.reasons and str(ai_override) not in ("auto", ""):
                reason_code = str(ai_override)
        elif self.reasons:
            with self._lock:
                code, cat, label = self.reasons[self._reason_index % len(self.reasons)]
                self._reason_index += 1
            reason_code, category, reason_label = code, cat, label

        web_cookies, session_id = self._pick_session()

        if self.mode == "web":
            url = "https://www.tiktok.com/aweme/v2/aweme/feedback/"
            web_dev = pick_web_device("random")
            brand = web_dev["brand"]
            dev_type = web_dev["type"]
            device_full_name = web_dev["full_name"]
            agent = web_dev["user_agent"]
            now_str = datetime.now().strftime("%H:%M:%S")
            _did = web_cookies.get("device_id") or generate_web_device_id()
            _iid = "web"
            _udid = "web"
            _cdid = "web"

            self._save_registered_device({
                "registered_at": now_str,
                "brand": brand,
                "model": dev_type,
                "device_id": _did,
                "install_id": _iid,
                "openudid": _udid,
                "cdid": _cdid,
            })

            params = {
                "aid": "1988",
                "app_language": "en",
                "app_name": "tiktok_web",
                "browser_language": "en-US",
                "browser_name": web_dev.get("browser_name", "Mozilla"),
                "browser_online": "true",
                "browser_platform": web_dev.get("browser_platform", "Win32"),
                "browser_version": web_dev.get("browser_version", agent),
                "channel": "tiktok_web",
                "cookie_enabled": "true",
                "current_region": "US",
                "device_id": _did,
                "device_platform": web_dev.get("device_platform", "web_pc"),
                "focus_state": "true",
                "from_page": "user",
                "history_len": str(random.randint(3, 9)),
                "is_fullscreen": "false",
                "is_page_visible": "true",
                "lang": "en",
                "nickname": target_name,
                "object_id": target_id,
                "owner_id": target_id,
                "priority_region": "US",
                "reason": reason_code,
                "report_type": "user",
                "reporter_id": web_cookies.get("user_id", web_cookies.get("uid_tt", "7694474723518858262")),
                "secUid": sec_uid,
                "target": target_id,
                "uniqueId": target_user,
                "user_is_login": "true" if session_id else "false",
                "webcast_language": "en",
            }
            headers = {
                "User-Agent": agent,
                "Accept": "*/*",
                "Accept-Language": "en-US,en;q=0.9",
                "Referer": f"https://www.tiktok.com/@{target_user}",
                "Sec-Fetch-Dest": "empty",
                "Sec-Fetch-Mode": "cors",
                "Sec-Fetch-Site": "same-origin",
            }
            if web_dev.get("sec_ch_ua"):
                headers["Sec-Ch-Ua"] = web_dev["sec_ch_ua"]
            if web_dev.get("sec_ch_ua_mobile"):
                headers["Sec-Ch-Ua-Mobile"] = web_dev["sec_ch_ua_mobile"]
            if web_dev.get("sec_ch_ua_platform"):
                headers["Sec-Ch-Ua-Platform"] = web_dev["sec_ch_ua_platform"]
            cookies = web_cookies if web_cookies else ({"sessionid": session_id} if session_id else None)

        else:
            url = "https://api16-normal-c-alisg.ttapis.com/aweme/v2/aweme/feedback/"
            dev = self._pick_device()
            agent = dev["User-Agent"]
            brand = dev["brand"]
            dev_type = dev["type"]
            device_full_name = f"{brand} {dev_type}"
            tim = "{:.0f}".format(time.time() * 1000)
            _iid = "73" + "".join(random.choices("0123456789", k=16))
            _did = "73" + "".join(random.choices("0123456789", k=16))
            _udid = hexlify(randbytes(8)).decode()
            _cdid = str(uuid4())
            _ts = str(int(time.time()))
            now_str = datetime.now().strftime("%H:%M:%S")

            self._save_registered_device({
                "registered_at": now_str,
                "brand": brand,
                "model": dev_type,
                "device_id": _did,
                "install_id": _iid,
                "openudid": _udid,
                "cdid": _cdid,
            })

            params = {
                "owner_id": target_id,
                "object_id": target_id,
                "report_type": "user",
                "enter_from": "others_homepage",
                "isFirst": "1",
                "no_hw": "1",
                "report_desc": "",
                "uri": "",
                "reason": reason_code,
                "category": category,
                "request_tag_from": "h5",
                "iid": _iid,
                "device_id": _did,
                "ac": "MOBILE_4G",
                "channel": "googleplay",
                "aid": "1233",
                "app_name": "musical_ly",
                "version_code": "310503",
                "version_name": "31.5.3",
                "device_platform": "android",
                "os": "android",
                "ab_version": "31.5.3",
                "ssmix": "a",
                "device_type": dev_type,
                "device_brand": brand,
                "language": "ar",
                "os_api": "29",
                "os_version": "10",
                "openudid": _udid,
                "manifest_version_code": "2023105030",
                "resolution": "720*1491",
                "dpi": "320",
                "update_version_code": "2023105030",
                "_rticket": tim,
                "is_pad": "0",
                "current_region": "YE",
                "app_type": "normal",
                "mcc_mnc": "42102",
                "timezone_name": "Asia/Aden",
                "carrier_region_v2": "421",
                "residence": "YE",
                "app_language": "ar",
                "carrier_region": "YE",
                "ac2": "lte",
                "uoo": "1",
                "op_region": "YE",
                "timezone_offset": "10800",
                "build_number": "31.5.3",
                "host_abi": "arm64-v8a",
                "locale": "ar",
                "ts": _ts,
                "cdid": _cdid,
            }

            now_unix = int(time.time())
            now_ms = str(now_unix * 1000)
            query_str = urlencode(params)

            try:
                gorgon_sig = Gorgon(query_str, now_unix).get_value()["x-gorgon"]
            except Exception:
                gorgon_sig = ""
            try:
                ladon_sig = Ladon.encrypt(now_unix, "1611921764", 1233)
            except Exception:
                ladon_sig = ""
            try:
                argus_sig = Argus.get_sign(queryhash=query_str, timestamp=now_unix)
            except Exception:
                argus_sig = ""

            headers = {
                "User-Agent": agent,
                "sdk-version": "2",
                "passport-sdk-version": "19",
                "x-tt-dm-status": "login=0;ct=0;rt=7",
                "x-vc-bdturing-sdk-version": "2.3.3.i18n",
                "x-tt-store-region": "ye",
                "x-tt-store-region-src": "did",
                "x-ss-dp": "1233",
                "x-ss-req-ticket": now_ms,
                "x-khronos": str(now_unix),
                "x-gorgon": gorgon_sig,
                "x-ladon": ladon_sig,
                "x-argus": argus_sig,
            }

            if session_id:
                headers["cookie"] = f"sessionid={session_id}; sid_tt={session_id}"
                params["reporter_id"] = web_cookies.get("user_id", web_cookies.get("uid_tt", "7694474723518858262"))
                params["user_is_login"] = "1"
                cookies = {"sessionid": session_id, "sid_tt": session_id}
            else:
                cookies = None

        plog = self.project_logger
        request_id = plog.new_request_id() if plog else None
        if plog:
            try:
                plog.log_request(
                    request_id, "GET", url,
                    params=params, headers=headers, cookies=cookies,
                    target=target_user, target_id=target_id, mode=self.mode,
                    reason_code=reason_code, reason_label=reason_label,
                    category=category, device=device_full_name, device_id=_did,
                    session=self._current_session_label,
                )
            except Exception:
                pass

        t_start = time.monotonic()
        res = make_request_with_proxy(self.proxy_manager, url, params, headers, cookies=cookies)
        latency = (time.monotonic() - t_start) * 1000

        if self.proxy_manager:
            try:
                p_label = self.proxy_manager.last_label or "—"
            except Exception:
                p_label = "—"
        else:
            p_label = "Direct (no proxy)"

        captcha_detected = False
        full_response = ""
        if res is None:
            success = False
            http_status = "—"
            server_code = "—"
            raw_snippet = "(no response)"
        else:
            http_status = str(res.status_code)
            full_response = res.text if res is not None else ""
            try:
                j = res.json()
                server_code = str(j.get("status_code", "—"))
                data_obj = j.get("data") or {}
                need_captcha = data_obj.get("need_captcha")
                is_captcha = (
                    (need_captcha is True)
                    or (j.get("status_code") == 1000)
                    or ("verify_center" in data_obj)
                    or ("verify_center" in res.text)
                    or ("bdturing" in res.text.lower())
                )
                if is_captcha:
                    captcha_detected = True
                success = (
                    (j.get("status_code") == 0 or "skip_reason" in j or res.status_code == 200)
                    and not captcha_detected
                )
            except Exception:
                server_code = "—"
                raw_lower = (res.text or "").lower()
                if "verify_center" in raw_lower or "bdturing" in raw_lower:
                    captcha_detected = True
                    success = False
                else:
                    success = res.status_code == 200
            raw_snippet = full_response[:120].replace("\n", " ") if full_response else (
                f"HTTP {res.status_code} (OK)" if res else "(no response)"
            )

        if captcha_detected:
            with self._lock:
                self.captcha_hits += 1
                self.blocked_devices[device_full_name] = time.time() + 600
            self._save_important({
                "type": "CAPTCHA_TRIGGERED",
                "time": now_str,
                "device": device_full_name,
                "device_id": _did,
                "proxy": p_label,
                "http_status": http_status,
                "response": raw_snippet,
            })
            time.sleep(3.0)

        if plog:
            try:
                plog.log_response(
                    request_id,
                    status_code=res.status_code if res is not None else None,
                    headers=dict(res.headers) if res is not None else {},
                    body=full_response if res is not None else None,
                    latency_ms=latency,
                    error=None if res is not None else "no response",
                    success=success, server_code=server_code,
                    captcha=captcha_detected, proxy=p_label,
                    reason_code=reason_code, reason_label=reason_label,
                    session=self._current_session_label,
                )
            except Exception:
                pass

        with self._lock:
            if success:
                self.accepted += 1
                self._consec_success += 1
                self._consec_fail = 0
                self._save_important({
                    "type": "REPORT_ACCEPTED",
                    "time": now_str,
                    "target": target_user,
                    "target_id": target_id,
                    "reason": reason_code,
                    "device": device_full_name,
                    "device_id": _did,
                    "proxy": p_label,
                    "server_code": server_code,
                })
            else:
                self.rejected += 1
                self._consec_fail += 1
                self._consec_success = 0

            score = calculate_request_score(success, http_status, server_code, latency)
            total = self.accepted + self.rejected
            req_entry = {
                "num": total,
                "request_id": request_id,
                "time": now_str,
                "account": target_user,
                "session_account": self._current_session_label,
                "target": target_user,
                "success": success,
                "http_status": http_status,
                "server_code": server_code,
                "latency": round(latency, 1),
                "score": score,
                "vector": reason_label,
                "reason": reason_code,
                "brand": brand,
                "device": dev_type,
                "device_id": _did,
                "proxy": p_label,
                "ua_short": (agent[:55] + "…") if len(agent) > 55 else agent,
                "snippet": raw_snippet,
                "response_full": full_response,
                "captcha": captcha_detected,
            }
            self.last_request = req_entry
            self.request_log.appendleft(req_entry)

        self._log_request(req_entry)
        self._maybe_adapt_ai(req_entry, session_id or "anonymous")
