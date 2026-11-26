# -*- coding: utf-8 -*-
"""
Dummy AI Sentinel & Session Monitor (AI features removed / stubbed locally).
Provides local heuristic adaptation and nominal fallback responses without external API keys.
"""

from __future__ import annotations
import time
import threading
from typing import Any, Dict, List, Optional, Tuple

def _extract_json_dict(text: str) -> Optional[dict]:
    return None

class GeminiClient:
    """Stub AI Client operating locally without external API dependencies."""

    def __init__(self):
        self.provider = "disabled"
        self.active_backend = "Local Heuristic Engine"
        self._lock = threading.Lock()
        self.panel: Dict[str, Any] = {
            "status": "Offline (Local Engine)",
            "last_decision": "Nominal",
            "last_sleep": 0,
            "total_calls": 0,
            "active_sleeps": {},
            "active_engine": "Local Heuristic Engine",
            "startup_done": True,
            "risk_level": "Low",
            "safe_rate": "15",
            "est_requests": "30",
            "est_duration": "~5 minutes",
            "strategy": "Standard automated reporting rotation.",
            "account_summary": "Target account nominal.",
            "started_at": time.strftime("%H:%M:%S"),
        }

    def check_models(self, timeout: float = 15.0, on_result=None) -> List[Dict[str, Any]]:
        res = [{
            "provider": "Local",
            "model": "heuristic-engine",
            "ok": True,
            "status": "OK (Local)",
            "latency_ms": 1,
        }]
        self.panel["model_check"] = res
        self.panel["models_online"] = 1
        self.panel["models_total"] = 1
        self.panel["status"] = "OK (Local Engine)"
        if on_result:
            try:
                on_result(res[0])
            except Exception:
                pass
        return res

    def analyze_target_account(self, *args, **kwargs) -> Dict[str, Any]:
        result = {
            "risk_level": "Low",
            "safe_rate": 15,
            "est_requests": 30,
            "est_duration": "~5 minutes",
            "strategy": "Proceed with standard pacing and device rotation.",
            "account_summary": "Standard account profile.",
        }
        self.panel.update({
            "startup_done": True,
            "risk_level": result["risk_level"],
            "safe_rate": str(result["safe_rate"]),
            "est_requests": str(result["est_requests"]),
            "est_duration": result["est_duration"],
            "strategy": result["strategy"],
            "account_summary": result["account_summary"],
            "last_decision": "Startup: Low risk, 15 req/min",
        })
        return result

    def estimate_sleep_for_skip3(self, *args, **kwargs) -> int:
        return 15

    def analyze_session(self, *args, **kwargs) -> str:
        return "Session is healthy and operating within nominal parameters."

    def estimate_safe_rate(self, *args, **kwargs) -> int:
        return 15

    def adapt_request_and_pacing(
        self,
        target_user: str,
        target_info: Dict[str, Any],
        last_req: Dict[str, Any],
        current_delay: float,
        is_random_delay: bool,
        available_vectors: List[Any],
        consecutive_successes: int,
        consecutive_failures: int,
    ) -> Dict[str, Any]:
        success = bool(last_req.get('success', False))
        skip3 = bool(last_req.get('skip3', False))
        http_status = str(last_req.get('http_status', '—'))

        if skip3:
            rec_delay = min(30.0, current_delay + 5.0)
            mut_vec = True
            mut_dev = False
            rot_sess = True
            directive = "Duplicate ticket (skip3). Pivoting vector & backing off."
            action = "Pivoted vector"
        elif http_status in ('429', '403'):
            rec_delay = min(45.0, current_delay * 1.5)
            mut_vec = True
            mut_dev = True
            rot_sess = True
            directive = "Anti-bot friction detected. Regenerating device & throttling."
            action = "Throttled & rotated device"
        else:
            rec_delay = max(1.0, current_delay)
            mut_vec = (len(available_vectors) > 1 and consecutive_successes % 3 == 0)
            mut_dev = False
            rot_sess = False
            directive = "Pacing balanced. Standard rotation."
            action = "Standard rotation"

        alt_vectors = [v for v in available_vectors if str(v[0]) != str(last_req.get('reason', ''))]
        rec_code = str(alt_vectors[0][0]) if alt_vectors else "auto"

        return {
            "recommended_delay": round(rec_delay, 2),
            "mutate_vector": mut_vec,
            "recommended_vector_code": rec_code if mut_vec else "auto",
            "mutate_device": mut_dev,
            "rotate_session": rot_sess,
            "ai_directive": directive,
            "next_action": action,
        }

    def get_panel(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self.panel)


class AccountMetrics:
    def __init__(self, identifier: str, label: str):
        self.identifier = identifier
        self.label = label
        self.total = 0
        self.successes = 0
        self.total_lat = 0.0
        self.consec_fails = 0
        self.consec_skip3 = 0
        self.total_skip3 = 0
        self.cooldown_until = 0.0
        self.status = "active"
        self.sleep_until = 0.0
        self._lock = threading.Lock()

    def record(self, success: bool, latency_ms: float, skip3: bool = False):
        with self._lock:
            self.total += 1
            self.total_lat += latency_ms
            if skip3:
                self.consec_skip3 += 1
                self.total_skip3 += 1
            if success:
                self.successes += 1
                self.consec_fails = 0
                if not skip3:
                    self.consec_skip3 = 0
            else:
                self.consec_fails += 1

    @property
    def success_rate(self) -> float:
        return (self.successes / self.total * 100) if self.total else 100.0

    @property
    def avg_latency(self) -> float:
        return (self.total_lat / self.total) if self.total else 0.0

    def enter_cooldown(self, seconds: int = 300):
        with self._lock:
            self.status = "cooldown"
            self.cooldown_until = time.time() + seconds
            self.consec_fails = 0

    def enter_sleep(self, seconds: int):
        with self._lock:
            self.status = "sleeping"
            self.sleep_until = time.time() + seconds

    def restore(self):
        with self._lock:
            self.status = "active"
            self.cooldown_until = 0.0
            self.sleep_until = 0.0

    def is_in_cooldown(self) -> bool:
        return self.status == "cooldown" and time.time() < self.cooldown_until

    def is_sleeping(self) -> bool:
        return self.status == "sleeping" and time.time() < self.sleep_until

    def cooldown_remaining(self) -> int:
        if self.status == "cooldown":
            return max(0, int(self.cooldown_until - time.time()))
        if self.status == "sleeping":
            return max(0, int(self.sleep_until - time.time()))
        return 0


class SessionMonitor:
    def __init__(
        self,
        session_pool: List[Dict[str, Any]],
        redis_mgr,
        gemini: GeminiClient,
        telegram_bot=None,
        target_info: Dict[str, Any] = None,
    ):
        self._pool = session_pool
        self._redis = redis_mgr
        self._gemini = gemini
        self._telegram = telegram_bot
        self._target = target_info or {}
        self._metrics: Dict[str, AccountMetrics] = {}
        self._lock = threading.Lock()
        self._stop_evt = threading.Event()

    def start(self):
        pass

    def stop(self):
        self._stop_evt.set()

    def update_target(self, info: Dict[str, Any]):
        self._target = info

    def record(self, account_id: str, label: str, success: bool, latency_ms: float, skip3: bool = False):
        m = self._get_or_create(account_id, label)
        m.record(success, latency_ms, skip3=skip3)

    def adapt_after_request(
        self,
        account_id: str,
        label: str,
        last_req: Dict[str, Any],
        current_delay: float,
        is_random_delay: bool,
        available_vectors: List[Any],
        consec_successes: int,
        consec_fails: int,
    ) -> Dict[str, Any]:
        m = self._get_or_create(account_id, label)
        m.record(
            last_req.get('success', False),
            last_req.get('latency', 0.0),
            skip3=last_req.get('skip3', False),
        )
        return self._gemini.adapt_request_and_pacing(
            target_user=str(self._target.get('username', 'target')),
            target_info=self._target,
            last_req=last_req,
            current_delay=current_delay,
            is_random_delay=is_random_delay,
            available_vectors=available_vectors,
            consecutive_successes=consec_successes,
            consecutive_failures=consec_fails,
        )

    def should_skip(self, account_id: str) -> bool:
        with self._lock:
            m = self._metrics.get(account_id)
        if m is None:
            return False
        if m.is_sleeping() or m.is_in_cooldown():
            return True
        return False

    def get_rate(self, account_id: str) -> int:
        return 15

    def get_recommended_delay(self, default_delay: float) -> float:
        return default_delay

    def get_all_metrics(self) -> List[Dict]:
        with self._lock:
            snapshot = list(self._metrics.values())
        out = []
        for m in snapshot:
            out.append({
                "id": m.identifier,
                "label": m.label,
                "total": m.total,
                "successes": m.successes,
                "success_rate": round(m.success_rate, 1),
                "avg_latency": round(m.avg_latency, 0),
                "consec_fails": m.consec_fails,
                "consec_skip3": m.consec_skip3,
                "total_skip3": m.total_skip3,
                "status": m.status,
                "remaining": m.cooldown_remaining(),
            })
        return out

    def get_panel_data(self) -> Dict[str, Any]:
        return self._gemini.get_panel()

    def _get_or_create(self, aid: str, label: str) -> AccountMetrics:
        with self._lock:
            if aid not in self._metrics:
                self._metrics[aid] = AccountMetrics(aid, label)
            return self._metrics[aid]
