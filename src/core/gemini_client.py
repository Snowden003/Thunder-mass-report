# -*- coding: utf-8 -*-
"""
Gemini AI Client + Live Session Monitor for THUNDER TikTok Reporter.

Features
────────
• Calls Gemini 1.5 Flash via generateContent REST to make live decisions.
• SessionMonitor background thread:
    – every 30 s checks each account's fail-rate.
    – skip_reason=3 → Gemini analyses the account and decides sleep duration.
    – consecutive fails >= FAIL_THRESHOLD → 5-min cooldown.
    – auto-restores sessions after cooldown.
    – Telegram notifications for every status change.
• estimate_sleep_for_skip3(): Gemini looks at account stats to decide
  how long to pause after a skip_reason=3 duplicate-ticket signal.
• Gemini panel data (get_panel()) ready for display_report().
"""

import os
import re
import json
import time
import threading
import logging
from typing import Any, Dict, List, Optional, Tuple

import requests as _req

log = logging.getLogger("GeminiMonitor")
if not log.handlers:
    logging.basicConfig(
        level=logging.INFO,
        format="[%(asctime)s] %(name)s %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_env():
    """Load configuration from the repository-root .env file if available."""
    env_file = os.path.join(_REPO_ROOT, '.env')
    if os.path.exists(env_file):
        try:
            with open(env_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith('#') and '=' in line:
                        k, v = line.split('=', 1)
                        k = k.strip()
                        v = v.strip().strip('"').strip("'")
                        if k and k not in os.environ:
                            os.environ[k] = v
        except Exception:
            pass


def _load_settings_model() -> Optional[str]:
    """Read ``ai.model`` from config/settings.yaml without requiring PyYAML.

    This keeps the settings file authoritative for the model even when PyYAML
    is not installed, while ``TOOKEN_MODEL`` still wins as an explicit override.
    """
    path = os.path.join(_REPO_ROOT, 'config', 'settings.yaml')
    try:
        with open(path, 'r', encoding='utf-8') as f:
            in_ai_block = False
            for raw in f:
                stripped = raw.split('#', 1)[0].rstrip()
                if not stripped.strip():
                    continue
                if not raw[:1].isspace():
                    in_ai_block = stripped.strip() == 'ai:'
                    continue
                if not in_ai_block:
                    continue
                m = re.match(r"\s+model:\s*['\"]?([^'\"\s]+)['\"]?", stripped)
                if m:
                    return m.group(1)
    except OSError:
        return None
    return None


_load_env()

def _extract_json_dict(text: str) -> Optional[dict]:
    """Robustly extract and parse a JSON dictionary from LLM responses."""
    if not text:
        return None
    text_clean = text.strip()
    try:
        res = json.loads(text_clean)
        if isinstance(res, dict):
            return res
    except Exception:
        pass

    # Try code block ```json ... ```
    m = re.search(r'```(?:json)?\s*([\s\S]*?)\s*```', text_clean)
    if m:
        try:
            res = json.loads(m.group(1).strip())
            if isinstance(res, dict):
                return res
        except Exception:
            pass

    # Try outer brackets {...}
    m = re.search(r'(\{[\s\S]*\})', text_clean)
    if m:
        try:
            res = json.loads(m.group(1).strip())
            if isinstance(res, dict):
                return res
        except Exception:
            pass

    return None

# ── Configuration ──────────────────────────────────────────────────────────────
# Token Club (primary, reliable) — Gemini disabled due to instability
_DEFAULT_GEMINI_KEY      = ""
_DEFAULT_GEMINI_ENDPOINT = ""
_DEFAULT_GEMINI_MODEL    = ""

# Google Gemini kept as disabled fallback only
_GOOGLE_FALLBACK_MODELS  = ("")

FAIL_THRESHOLD       = 5     # consecutive failures before cooldown
COOLDOWN_SECONDS     = 300   # 5 minutes
CHECK_INTERVAL       = 30    # health-check loop interval (seconds)
SKIP3_MIN_SLEEP      = 10    # minimum Gemini-decided sleep after skip_reason=3
SKIP3_MAX_SLEEP      = 120   # maximum sleep cap (seconds)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Dual AI Engine Client (Gemini REST + Tooken.club API)
# ══════════════════════════════════════════════════════════════════════════════
class GeminiClient:
    """Multi-Engine Neural Sentinel Client supporting Google Gemini REST and Tooken.club API."""

    def __init__(self):
        _load_env()
        # Google Gemini Native
        self.api_key         = os.getenv("GEMINI_API_KEY", _DEFAULT_GEMINI_KEY)
        self.endpoint        = os.getenv("GEMINI_ENDPOINT", _DEFAULT_GEMINI_ENDPOINT).rstrip("/")
        self.model           = os.getenv("GEMINI_MODEL", _DEFAULT_GEMINI_MODEL)


        # Provider strategy: "tooken" (primary, stable), or "gemini" (disabled fallback)
        self.provider        = os.getenv("AI_PROVIDER", "tooken").lower()
        self.active_backend  = "Tooken/Gemini"
        self._lock           = threading.Lock()

        # Panel state (updated by SessionMonitor, read by display_report)
        self.panel: Dict[str, Any] = {
            "status":           "Connecting…",
            "last_decision":    "—",
            "last_sleep":       0,
            "total_calls":      0,
            "active_sleeps":    {},        # account_id → (sleep_seconds, wake_at)
            "active_engine":    "Dual (Tooken+Gemini)",
            # ── startup analysis results ──────────────────────────────────────
            "startup_done":     False,
            "risk_level":       "—",       # Low / Medium / High
            "safe_rate":        "—",       # req/min
            "est_requests":     "—",       # estimated reports needed
            "est_duration":     "—",       # estimated time to finish
            "strategy":         "—",       # AI recommended strategy
            "account_summary":  "—",       # one-line account evaluation
            "started_at":       time.strftime("%H:%M:%S"),
        }

    # ─── internal providers ───────────────────────────────────────────────────
    def _url(self) -> str:
        return f"{self.endpoint}/models/{self.model}:generateContent?key={self.api_key}"

    def _tooken_models(self) -> List[str]:
        seen = set()
        return [m for m in (self.tooken_model)
                if m and not (m in seen or seen.add(m))]

    def _google_models(self) -> List[str]:
        seen = set()
        return [m for m in (*_GOOGLE_FALLBACK_MODELS, self.model)
                if m and not (m in seen or seen.add(m))]

    def _ask_tooken(self, prompt: str, max_tokens: int = 1200) -> Optional[str]:
        """Query Tooken.club API (/chat/completions)."""
        if not self.tooken_api_key:
            return None
        url = f"{self.tooken_endpoint}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.tooken_api_key}",
            "Content-Type": "application/json",
        }
        models_to_try = self._tooken_models()

        for model_cand in models_to_try:
            payload = {
                "model": model_cand,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            }
            try:
                self.panel["total_calls"] += 1
                with self._lock:
                    r = _req.post(url, json=payload, headers=headers, timeout=20)
                if r.status_code == 200:
                    data = r.json()
                    choices = data.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "").strip()
                        if content:
                            self.active_backend = f"Tooken ({model_cand})"
                            self.panel["active_engine"] = f"Tooken ({model_cand})"
                            self.panel["status"] = f"OK (tooken.club/{model_cand})"
                            return content
                else:
                    log.warning("Tooken %s HTTP %s: %s", model_cand, r.status_code, r.text[:120])
            except Exception as exc:
                log.warning("Tooken %s call error: %s", model_cand, exc)
                continue
        return None

    def _ask_vision(
        self,
        prompt: str,
        image_data_uri: str,
        max_tokens: int = 800,
    ) -> Optional[str]:
        """
        Send a text prompt plus one image to the AI for vision analysis.

        Uses Token Club's OpenAI-compatible chat/completions endpoint with
        image_url content. Falls back to text-only if the provider does not
        support vision.
        """
        if not self.tooken_api_key:
            return None
        url = f"{self.tooken_endpoint}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.tooken_api_key}",
            "Content-Type": "application/json",
        }
        models_to_try = self._tooken_models()

        for model_cand in models_to_try:
            payload = {
                "model": model_cand,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": prompt},
                            {"type": "image_url", "image_url": {"url": image_data_uri}},
                        ],
                    }
                ],
                "max_tokens": max_tokens,
                "temperature": 0.2,
            }
            try:
                self.panel["total_calls"] += 1
                with self._lock:
                    r = _req.post(url, json=payload, headers=headers, timeout=25)
                if r.status_code == 200:
                    data = r.json()
                    choices = data.get("choices", [])
                    if choices:
                        content = choices[0].get("message", {}).get("content", "").strip()
                        if content:
                            self.active_backend = f"Tooken ({model_cand}) [vision]"
                            self.panel["active_engine"] = f"Tooken ({model_cand}) [vision]"
                            self.panel["status"] = f"OK (tooken.club/{model_cand} vision)"
                            return content
                else:
                    log.warning("Tooken vision %s HTTP %s: %s", model_cand, r.status_code, r.text[:120])
            except Exception as exc:
                log.warning("Tooken vision %s call error: %s", model_cand, exc)
                continue
        return None

    def _ask_google(self, prompt: str, max_tokens: int = 1200) -> Optional[str]:
        """Query Google Gemini REST generateContent API."""
        if not self.api_key:
            return None
        payload = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "maxOutputTokens": max_tokens,
                "temperature": 0.2,
                "thinkingConfig": {"thinkingBudget": 0}
            },
        }
        models_to_try = self._google_models()

        for model_cand in models_to_try:
            url = f"{self.endpoint}/models/{model_cand}:generateContent?key={self.api_key}"
            try:
                self.panel["total_calls"] += 1
                with self._lock:
                    r = _req.post(url, json=payload, timeout=15)
                if r.status_code == 200:
                    data = r.json()
                    parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                    text = "".join(p.get("text", "") for p in parts).strip()
                    if text:
                        self.active_backend = f"Google ({model_cand})"
                        self.panel["active_engine"] = f"Google ({model_cand})"
                        self.panel["status"] = f"OK (Google/{model_cand})"
                        return text
                elif r.status_code in (429, 500, 502, 503, 504):
                    log.warning("Google Gemini %s HTTP %s. Trying next fallback...", model_cand, r.status_code)
                    continue
                else:
                    log.warning("Google Gemini %s HTTP %s: %s", model_cand, r.status_code, r.text[:120])
                    continue
            except Exception as exc:
                log.warning("Google Gemini %s call failed: %s", model_cand, exc)
                continue
        return None

    def _ask(self, prompt: str, max_tokens: int = 1200) -> Optional[str]:
        """Query AI using active provider strategy with stable model selection.

        The selected model is locked for the duration of the request — no
        mid-request model switching, ensuring stable latency and consistent
        behavior.
        """
        if self.provider == "tooken":
            # Token Club only — no Gemini fallback (avoids instability)
            return self._ask_tooken(prompt, max_tokens=max_tokens)
        elif self.provider == "gemini":
            # Gemini only — no Token Club fallback
            return self._ask_google(prompt, max_tokens=max_tokens)
        else:
            # "auto": Token Club primary, Gemini secondary (legacy)
            ans = self._ask_tooken(prompt, max_tokens=max_tokens)
            if ans:
                return ans
            return self._ask_google(prompt, max_tokens=max_tokens)

    # ─── Model Preflight ──────────────────────────────────────────────────────
    def check_models(self, timeout: float = 15.0, on_result=None) -> List[Dict[str, Any]]:
        """Probe configured AI provider models for availability.

        When provider="tooken", only Token Club models are probed — Gemini
        endpoints are skipped entirely to avoid instability and stale latency.
        """
        providers = []
        if self.provider == "tooken":
            providers = [("Tooken", self._tooken_models(), self._check_tooken_model)]
        elif self.provider == "gemini":
            providers = [("Google", self._google_models(), self._check_google_model)]
        else:  # "auto": both, Tooken first
            providers = [
                ("Tooken", self._tooken_models(), self._check_tooken_model),
                ("Google", self._google_models(), self._check_google_model),
            ]

        results: List[Dict[str, Any]] = []
        for provider, models, checker in providers:
            for model in models:
                started = time.perf_counter()
                try:
                    ok, status = checker(model, timeout)
                except Exception as exc:
                    ok, status = False, f"Error: {type(exc).__name__}"
                latency_ms = int((time.perf_counter() - started) * 1000)
                result = {
                    "provider": provider,
                    "model": model,
                    "ok": ok,
                    "status": status,
                    "latency_ms": latency_ms,
                }
                results.append(result)
                if on_result:
                    try:
                        on_result(result)
                    except Exception:
                        pass

        online = [r for r in results if r["ok"]]
        self.panel["model_check"] = results
        self.panel["models_online"] = len(online)
        self.panel["models_total"] = len(results)
        if online:
            first = online[0]
            self.active_backend = f"{first['provider']} ({first['model']})"
            self.panel["active_engine"] = self.active_backend
            self.panel["status"] = f"OK ({first['provider']}/{first['model']})"
        else:
            self.panel["status"] = "No AI Model Available"
        return results

    def _check_tooken_model(self, model: str, timeout: float = 15.0) -> Tuple[bool, str]:
        if not self.tooken_api_key:
            return False, "No API key"
        url = f"{self.tooken_endpoint}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.tooken_api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            "max_tokens": 8,
            "temperature": 0,
        }
        with self._lock:
            r = _req.post(url, json=payload, headers=headers, timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        choices = r.json().get("choices", [])
        content = choices[0].get("message", {}).get("content", "").strip() if choices else ""
        if not content:
            return False, "Empty response"
        return True, "OK"

    def _check_google_model(self, model: str, timeout: float = 15.0) -> Tuple[bool, str]:
        if not self.api_key:
            return False, "No API key"
        url = f"{self.endpoint}/models/{model}:generateContent?key={self.api_key}"
        payload = {
            "contents": [{"parts": [{"text": "Reply with exactly: OK"}]}],
            "generationConfig": {
                "maxOutputTokens": 8,
                "temperature": 0,
                "thinkingConfig": {"thinkingBudget": 0},
            },
        }
        with self._lock:
            r = _req.post(url, json=payload, timeout=timeout)
        if r.status_code != 200:
            return False, f"HTTP {r.status_code}"
        parts = r.json().get("candidates", [{}])[0].get("content", {}).get("parts", [])
        text = "".join(p.get("text", "") for p in parts).strip()
        if not text:
            return False, "Empty response"
        return True, "OK"

    # ─── Startup Analysis ─────────────────────────────────────────────────────
    def analyze_target_account(
        self,
        username: str,
        followers: int,
        following: int,
        likes: int,
        country: str,
        private: str,
        bio: str,
        joined: str,
        num_sessions: int,
        run_mode: str,
        chosen_reason: str,
    ) -> Dict[str, Any]:
        """
        Called ONCE at startup. Gemini analyses the target account and
        returns a full strategy: risk level, safe rate, estimated requests
        needed, estimated duration, and a plain-English plan.

        Returns a dict with keys:
          risk_level, safe_rate, est_requests, est_duration, strategy, account_summary
        """
        self.panel["status"] = "Analysing…"

        def _fmt_num(v):
            try:
                return f"{int(v):,}"
            except (ValueError, TypeError):
                return str(v or "—")

        prompt = (
            "You are an AI assistant helping a TikTok moderation reporting system.\n"
            "Analyse the following TikTok account and reporting session, then produce "
            "a structured JSON response with EXACTLY these keys:\n\n"
            "  risk_level    : \"Low\", \"Medium\", or \"High\"  (risk of TikTok rate-limiting this session)\n"
            "  safe_rate     : integer 1–30  (safe reports per minute, considering account size)\n"
            "  est_requests  : integer  (estimated total report requests needed to trigger moderation review)\n"
            "  est_duration  : string   (human-readable estimated total time, e.g. \"~8 minutes\")\n"
            "  strategy      : string   (1-2 sentences: best approach to avoid blocks and maximise accepted reports)\n"
            "  account_summary: string  (1 sentence: plain-English evaluation of the account)\n\n"
            "--- TARGET ACCOUNT ---\n"
            f"  Username  : @{username}\n"
            f"  Followers : {_fmt_num(followers)}\n"
            f"  Following : {_fmt_num(following)}\n"
            f"  Likes     : {_fmt_num(likes)}\n"
            f"  Country   : {country}\n"
            f"  Private   : {private}\n"
            f"  Bio       : {bio[:120] if bio else '—'}\n"
            f"  Joined    : {joined}\n\n"
            "--- REPORTING SESSION ---\n"
            f"  Sessions (accounts)  : {num_sessions}\n"
            f"  Run mode             : {run_mode}\n"
            f"  Report reason        : {chosen_reason}\n\n"
            "Reply ONLY with valid JSON. No markdown, no code fences, no explanation."
        )
        raw = self._ask(prompt, max_tokens=600)
        result = {
            "risk_level":      "Medium",
            "safe_rate":       10,
            "est_requests":    50,
            "est_duration":    "—",
            "strategy":        "No response from AI engine.",
            "account_summary": "—",
        }
        if raw:
            parsed = _extract_json_dict(raw)
            if parsed and isinstance(parsed, dict):
                result.update({
                    "risk_level":      str(parsed.get("risk_level",      result["risk_level"])),
                    "safe_rate":       int(parsed.get("safe_rate",       result["safe_rate"])),
                    "est_requests":    int(parsed.get("est_requests",    result["est_requests"])),
                    "est_duration":    str(parsed.get("est_duration",    result["est_duration"])),
                    "strategy":        str(parsed.get("strategy",        result["strategy"])),
                    "account_summary": str(parsed.get("account_summary", result["account_summary"])),
                })
            else:
                result["strategy"] = raw.strip()

        # Push into panel for live display
        self.panel.update({
            "startup_done":    True,
            "risk_level":      result["risk_level"],
            "safe_rate":       str(result["safe_rate"]),
            "est_requests":    str(result["est_requests"]),
            "est_duration":    result["est_duration"],
            "strategy":        result["strategy"],
            "account_summary": result["account_summary"],
            "last_decision":   f"Startup: {result['risk_level']} risk, {result['safe_rate']} req/min",
        })
        log.info(
            "[Gemini startup] risk=%s rate=%s est=%s dur=%s",
            result["risk_level"], result["safe_rate"],
            result["est_requests"], result["est_duration"],
        )
        return result

    # ─── public API ───────────────────────────────────────────────────────────
    def estimate_sleep_for_skip3(
        self,
        account: str,
        followers: int,
        following: int,
        likes: int,
        country: str,
        private: str,
        total_reports: int,
        success_reports: int,
        avg_latency_ms: float,
        consecutive_skip3: int,
    ) -> int:
        """
        Ask Gemini how many seconds to sleep after skip_reason=3.
        skip_reason=3 means TikTok detected a duplicate ticket; we need to
        back off to avoid triggering rate-limiting or account suspension.

        Returns an integer number of seconds (clamped to SKIP3_MIN_SLEEP..SKIP3_MAX_SLEEP).
        """
        success_rate = (success_reports / total_reports * 100) if total_reports else 0.0

        def _fmt_num(v):
            try:
                return f"{int(v):,}"
            except (ValueError, TypeError):
                return str(v or "—")

        prompt = (
            "You are an AI assistant helping manage a TikTok automated moderation system.\n\n"
            f"Account details:\n"
            f"  Username         : {account}\n"
            f"  Followers        : {_fmt_num(followers)}\n"
            f"  Following        : {_fmt_num(following)}\n"
            f"  Likes            : {_fmt_num(likes)}\n"
            f"  Country          : {country}\n"
            f"  Private account  : {private}\n\n"
            f"Session performance:\n"
            f"  Total reports sent      : {total_reports}\n"
            f"  Accepted reports        : {success_reports} ({success_rate:.1f}%)\n"
            f"  Avg request latency     : {avg_latency_ms:.0f} ms\n"
            f"  Consecutive skip_reason=3 hits : {consecutive_skip3}\n\n"
            "skip_reason=3 means TikTok already has a moderation ticket for this account "
            "and is de-duplicating. Sleeping before retrying avoids rate-limits.\n\n"
            "Based on the above, reply with ONLY a single integer: "
            f"the number of seconds to sleep (between {SKIP3_MIN_SLEEP} and {SKIP3_MAX_SLEEP}). "
            "No explanation, just the number."
        )
        reply = self._ask(prompt)
        if reply:
            for tok in reply.split():
                try:
                    val = int(tok.replace(",", "").replace(".", ""))
                    clamped = max(SKIP3_MIN_SLEEP, min(SKIP3_MAX_SLEEP, val))
                    self.panel["last_sleep"]    = clamped
                    self.panel["last_decision"] = (
                        f"skip3→sleep {clamped}s for @{account}"
                    )
                    return clamped
                except ValueError:
                    continue
        # Heuristic fallback
        fallback = min(SKIP3_MAX_SLEEP, SKIP3_MIN_SLEEP + consecutive_skip3 * 8)
        self.panel["last_sleep"]    = fallback
        self.panel["last_decision"] = f"skip3→sleep {fallback}s (heuristic)"
        return fallback

    def analyze_session(
        self,
        account: str,
        total: int,
        success: int,
        avg_latency_ms: float,
        consec_fails: int,
    ) -> str:
        """Return a one-sentence Gemini health verdict for a session."""
        success_rate = (success / total * 100) if total else 0.0
        prompt = (
            "You are monitoring a TikTok automation session.\n"
            f"Account      : {account}\n"
            f"Total reqs   : {total}\n"
            f"Success rate : {success_rate:.1f}%\n"
            f"Avg latency  : {avg_latency_ms:.0f} ms\n"
            f"Consec fails : {consec_fails}\n\n"
            "In ONE short sentence say whether the session is healthy, "
            "at risk, or dead, and what to do next."
        )
        reply = self._ask(prompt)
        if reply:
            self.panel["last_decision"] = reply.strip()
        return reply or "No Gemini response."

    def estimate_safe_rate(
        self,
        account: str,
        avg_latency_ms: float,
        success_rate_pct: float,
    ) -> int:
        """Return a safe request-per-minute budget for an account."""
        prompt = (
            f"TikTok session account='{account}', "
            f"avg_latency={avg_latency_ms:.0f}ms, "
            f"success_rate={success_rate_pct:.1f}%. "
            "Reply with ONLY a number: the safe requests-per-minute budget "
            "(integer, 1–60, conservative). No explanation."
        )
        reply = self._ask(prompt)
        if reply:
            for tok in reply.split():
                try:
                    rate = int(tok.replace(",", "").replace(".", ""))
                    return max(1, min(60, rate))
                except ValueError:
                    continue
        if avg_latency_ms > 0:
            return max(1, min(30, int((60_000 / avg_latency_ms) * 0.7)))
        return 10

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
        """
        Closed-loop Neural Adaptation Engine:
        Inspects the exact request and TikTok response, then dynamically computes:
        1. "The Time" (Pacing delay & sleep duration for next request)
        2. "The Request" (Vector mutation, device fingerprint rotation, session rotation)
        """
        success = bool(last_req.get('success', False))
        http_status = str(last_req.get('http_status', '—'))
        server_code = str(last_req.get('server_code', '—'))
        latency_ms = float(last_req.get('latency', 0.0))
        skip3 = bool(last_req.get('skip3', False))
        snippet = str(last_req.get('snippet', ''))[:120]
        cur_vector = str(last_req.get('vector', ''))
        cur_reason = str(last_req.get('reason', ''))

        # ── 1. Local Neural Rule Engine (Immediate Fallback / Baseline) ───────
        if skip3:
            # TikTok duplicate ticket detected: pivot vector & back off
            rec_delay = min(45.0, max(12.0, current_delay + 6.0))
            mut_vec = True
            mut_dev = False
            rot_sess = True
            directive = f"TikTok duplicate ticket (skip3). Pivoting vector + added +{round(rec_delay - current_delay, 1)}s delay buffer."
            action = "Pivoted vector & increased delay"
        elif http_status in ('429', '403') or server_code in ('1000', '1001'):
            # Rate limit or firewall challenge
            rec_delay = min(60.0, max(18.0, current_delay * 1.6 + 4.0))
            mut_vec = True
            mut_dev = True
            rot_sess = True
            directive = f"Anti-bot friction ({http_status}/code {server_code}). Re-generating device ID & backing off to {rec_delay:.1f}s."
            action = "Device fingerprint regenerated & throttled"
        elif latency_ms > 2400 or http_status in ('502', '503', '504'):
            # Node congestion or gateway lag
            rec_delay = min(30.0, max(6.0, current_delay + 3.5))
            mut_vec = False
            mut_dev = False
            rot_sess = False
            directive = f"High latency ({latency_ms:.0f}ms). Applied defensive buffer (+3.5s)."
            action = "Applied latency buffer"
        elif success and consecutive_successes >= 3:
            # Stable infiltration: maintain optimal safe rate
            try:
                safe_rate_num = float(str(self.panel.get("safe_rate", "12")).split()[0])
                target_del = max(1.5, 60.0 / safe_rate_num)
            except Exception:
                target_del = max(2.5, current_delay * 0.95)
            rec_delay = max(2.0, min(current_delay, target_del))
            mut_vec = (len(available_vectors) > 1 and consecutive_successes % 3 == 0)
            mut_dev = False
            rot_sess = False
            directive = f"Nominal flow ({consecutive_successes} consecutive hits). Rate locked at {rec_delay:.1f}s."
            action = "Pacing optimized for throughput"
        elif not success and consecutive_failures >= 2:
            # Repetitive deflection: adapt vector and pacing
            rec_delay = min(30.0, max(5.0, current_delay + 2.5))
            mut_vec = (len(available_vectors) > 1)
            mut_dev = (consecutive_failures >= 3)
            rot_sess = False
            directive = f"Repeated deflection ({consecutive_failures}x). Adapting vector & pacing to {rec_delay:.1f}s."
            action = "Shifted attack angle & expanded jitter"
        else:
            rec_delay = max(1.0, current_delay)
            mut_vec = (len(available_vectors) > 1)
            mut_dev = False
            rot_sess = False
            directive = "Pacing balanced. Standard rotation."
            action = "Standard rotation"

        alt_vectors = [v for v in available_vectors if str(v[0]) != str(cur_reason)]
        rec_code = str(alt_vectors[0][0]) if alt_vectors else "auto"

        result = {
            "recommended_delay": round(rec_delay, 2),
            "mutate_vector": mut_vec,
            "recommended_vector_code": rec_code if mut_vec else "auto",
            "mutate_device": mut_dev,
            "rotate_session": rot_sess,
            "ai_directive": directive,
            "next_action": action,
        }

        # ── 2. Live Gemini LLM Query ─────────────────────────────────────────
        # Build ultra-compact prompt for Gemini Flash
        vector_labels = [f"code:{v[0]} ({v[1]})" for v in available_vectors]
        prompt = (
            "You are an AI Sentinel controlling an automated TikTok moderation testing tool.\n"
            "Analyze the latest request and TikTok server response, then return ONLY a JSON object to optimize the operation:\n\n"
            "LATEST REQUEST & RESPONSE:\n"
            f"  Target: @{target_user} (Followers: {target_info.get('followers', '—')}, Region: {target_info.get('country', '—')})\n"
            f"  Vector Used: {cur_vector} (code: {cur_reason})\n"
            f"  HTTP Status: {http_status} | Server Status Code: {server_code}\n"
            f"  Latency: {latency_ms:.0f} ms | Success: {success} | De-duped (skip3): {skip3}\n"
            f"  Response Snippet: {snippet}\n"
            f"  Consecutive Fails: {consecutive_failures} | Consecutive Successes: {consecutive_successes}\n"
            f"  Current Delay: {current_delay}s\n"
            f"  Available Vectors: {', '.join(vector_labels)}\n\n"
            "REQUIREMENTS (Reply ONLY with valid JSON with EXACTLY these keys):\n"
            "{\n"
            '  "recommended_delay": <float between 1.0 and 45.0, optimal sleep seconds before next request>,\n'
            '  "mutate_vector": <boolean, true if next request should switch to another vector>,\n'
            '  "recommended_vector_code": <string code of best next vector, or "auto">,\n'
            '  "mutate_device": <boolean, true if device fingerprint/ID should be regenerated>,\n'
            '  "rotate_identity": <boolean, true if multi-session account should rotate>,\n'
            '  "ai_directive": <1 concise sentence explaining the timing & request adaptation>\n'
            "}"
        )

        raw = self._ask(prompt, max_tokens=250)
        if raw:
            parsed = _extract_json_dict(raw)
            if parsed and isinstance(parsed, dict):
                if "recommended_delay" in parsed:
                    try:
                        gem_delay = float(parsed["recommended_delay"])
                        result["recommended_delay"] = round(max(1.0, min(60.0, gem_delay)), 2)
                    except (ValueError, TypeError):
                        pass
                if "mutate_vector" in parsed:
                    result["mutate_vector"] = bool(parsed["mutate_vector"])
                if "recommended_vector_code" in parsed:
                    result["recommended_vector_code"] = str(parsed["recommended_vector_code"])
                if "mutate_device" in parsed:
                    result["mutate_device"] = bool(parsed["mutate_device"])
                if "rotate_identity" in parsed:
                    result["rotate_session"] = bool(parsed["rotate_identity"])
                if "ai_directive" in parsed and parsed["ai_directive"]:
                    result["ai_directive"] = str(parsed["ai_directive"])
                    result["next_action"] = f"Adaptive Mutation ({self.active_backend})"

        # Update Sentinel HUD Panel
        self.panel["last_decision"] = result["ai_directive"]
        self.panel["last_directive"] = result["ai_directive"]
        self.panel["next_action"] = result["next_action"]
        self.panel["last_sleep"] = result["recommended_delay"]
        if result["recommended_delay"] > 0:
            self.panel["safe_rate"] = str(round(60.0 / result["recommended_delay"], 1))

        return result

    def get_panel(self) -> Dict[str, Any]:
        """Snapshot of the panel dict for live display."""
        with self._lock:
            snap = dict(self.panel)
            snap["active_sleeps"] = dict(self.panel["active_sleeps"])
        return snap


# ══════════════════════════════════════════════════════════════════════════════
# 2. Per-Account Metrics
# ══════════════════════════════════════════════════════════════════════════════
class AccountMetrics:
    """Thread-safe sliding window of metrics for one account."""

    def __init__(self, identifier: str, label: str):
        self.identifier       = identifier
        self.label            = label
        self.total            = 0
        self.successes        = 0
        self.total_lat        = 0.0
        self.consec_fails     = 0
        self.consec_skip3     = 0     # consecutive skip_reason=3 hits
        self.total_skip3      = 0     # lifetime count
        self.cooldown_until   = 0.0
        self.status           = "active"   # active | cooldown | sleeping
        self.sleep_until      = 0.0        # epoch when skip3-sleep ends
        self._lock            = threading.Lock()

    def record(self, success: bool, latency_ms: float, skip3: bool = False):
        with self._lock:
            self.total     += 1
            self.total_lat += latency_ms
            if skip3:
                self.consec_skip3 += 1
                self.total_skip3  += 1
            if success:
                self.successes     += 1
                self.consec_fails   = 0
                if not skip3:
                    self.consec_skip3 = 0
            else:
                self.consec_fails  += 1

    @property
    def success_rate(self) -> float:
        return (self.successes / self.total * 100) if self.total else 100.0

    @property
    def avg_latency(self) -> float:
        return (self.total_lat / self.total) if self.total else 0.0

    def enter_cooldown(self, seconds: int = COOLDOWN_SECONDS):
        with self._lock:
            self.status        = "cooldown"
            self.cooldown_until = time.time() + seconds
            self.consec_fails  = 0

    def enter_sleep(self, seconds: int):
        with self._lock:
            self.status     = "sleeping"
            self.sleep_until = time.time() + seconds

    def restore(self):
        with self._lock:
            self.status       = "active"
            self.cooldown_until = 0.0
            self.sleep_until    = 0.0

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


# ══════════════════════════════════════════════════════════════════════════════
# 3. Live Session Monitor
# ══════════════════════════════════════════════════════════════════════════════
class SessionMonitor:
    """
    Background thread that watches every account in session_pool.

    Quick reference
    ───────────────
    monitor.record(account_id, label, success, latency_ms, skip3=False)
        → call from Report() after every request

    monitor.should_skip(account_id) → bool
        → call at the TOP of Report(); if True, skip this account this cycle

    monitor.get_all_metrics() → list of dicts (for panel display)
    monitor.get_panel_data()  → GeminiClient.panel dict (for display)
    """

    def __init__(
        self,
        session_pool: List[Dict[str, Any]],
        redis_mgr,
        gemini: GeminiClient,
        telegram_bot=None,
        target_info: Dict[str, Any] = None,
    ):
        self._pool        = session_pool
        self._redis       = redis_mgr
        self._gemini      = gemini
        self._telegram    = telegram_bot
        self._target      = target_info or {}   # followers, likes, etc.
        self._metrics: Dict[str, AccountMetrics] = {}
        self._lock        = threading.Lock()
        self._stop_evt    = threading.Event()
        self._thread      = threading.Thread(
            target=self._loop, daemon=True, name="SessionMonitor"
        )

    # ─── lifecycle ────────────────────────────────────────────────────────────
    def start(self):
        self._thread.start()
        log.info(
            "SessionMonitor started (interval=%ds, fail_threshold=%d)",
            CHECK_INTERVAL,
            FAIL_THRESHOLD,
        )
        # Run initial Gemini account analysis asynchronously right away
        threading.Thread(target=self.run_startup_analysis, daemon=True, name="GeminiStartupAnalysis").start()

    def run_startup_analysis(self):
        """Immediately trigger Gemini to inspect target account and set safe rate + deadline."""
        def _to_clean_val(v):
            if v in (None, "", "—"):
                return 0
            try:
                return int(v)
            except Exception:
                return 0

        try:
            res = self._gemini.analyze_target_account(
                username=self._target.get("username", "target"),
                followers=_to_clean_val(self._target.get("followers")),
                following=_to_clean_val(self._target.get("following")),
                likes=_to_clean_val(self._target.get("likes")),
                country=str(self._target.get("country", "—")),
                private=str(self._target.get("private", "false")),
                bio=str(self._target.get("bio", "—")),
                joined=str(self._target.get("date", "—")),
                num_sessions=max(1, len(self._pool)),
                run_mode=str(self._target.get("mode", "web")),
                chosen_reason=str(self._target.get("reason", "default")),
            )
            # Broadcast to Telegram if configured
            self._notify(
                f"🧠 <b>Gemini Live Target Analysis</b>\n"
                f"🎯 <b>Target:</b> <code>@{self._target.get('username')}</code>\n"
                f"📊 <b>Risk Level:</b> <code>{res.get('risk_level')}</code>\n"
                f"⚡ <b>Safe Rate:</b> <code>{res.get('safe_rate')} req/min</code>\n"
                f"🎯 <b>Est. Goal:</b> <code>{res.get('est_requests')} reqs</code> ({res.get('est_duration')})\n"
                f"💡 <b>Strategy:</b> <i>{res.get('strategy')}</i>"
            )
        except Exception as e:
            log.warning("Startup analysis error: %s", e)
            self._gemini.panel["status"] = f"Error: {e}"
            self._gemini.panel["last_decision"] = f"Startup failed: {e}"

    def stop(self):
        self._stop_evt.set()

    def update_target(self, info: Dict[str, Any]):
        """Call after target account info is fetched."""
        self._target = info

    # ─── called from Report() ─────────────────────────────────────────────────
    def record(
        self,
        account_id: str,
        label: str,
        success: bool,
        latency_ms: float,
        skip3: bool = False,
    ):
        """Feed one request result into the metrics engine."""
        m = self._get_or_create(account_id, label)
        m.record(success, latency_ms, skip3=skip3)

        # Immediate reaction: skip_reason=3 → ask Gemini how long to sleep
        if skip3 and m.status == "active":
            sleep_sec = self._gemini.estimate_sleep_for_skip3(
                account        = label,
                followers      = self._target.get("followers", 0),
                following      = self._target.get("following", 0),
                likes          = self._target.get("likes", 0),
                country        = self._target.get("country", "—"),
                private        = str(self._target.get("private", "false")),
                total_reports  = m.total,
                success_reports= m.successes,
                avg_latency_ms = m.avg_latency,
                consecutive_skip3 = m.consec_skip3,
            )
            m.enter_sleep(sleep_sec)
            # Update panel active-sleeps map
            self._gemini.panel["active_sleeps"][account_id] = (
                sleep_sec,
                time.time() + sleep_sec,
            )
            log.info(
                "[skip3] @%s → Gemini says sleep %ds", label, sleep_sec
            )
            self._notify(
                f"😴 <b>Session sleeping (skip_reason=3)</b>\n"
                f"👤 Account: <code>{label}</code>\n"
                f"🤖 Gemini decision: sleep <code>{sleep_sec}s</code>\n"
                f"⏳ Wakes at: <code>"
                + time.strftime("%H:%M:%S", time.localtime(time.time() + sleep_sec))
                + "</code>"
            )

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
        """
        Evaluates the latest request and TikTok server response, records metrics,
        and dynamically computes adaptations for:
        1. "The Time" (pacing delay)
        2. "The Request" (vector mutation, device fingerprint regeneration, session rotation)
        """
        m = self._get_or_create(account_id, label)
        m.record(
            last_req.get('success', False),
            last_req.get('latency', 0.0),
            skip3=last_req.get('skip3', False),
        )

        decision = self._gemini.adapt_request_and_pacing(
            target_user=str(self._target.get('username', 'target')),
            target_info=self._target,
            last_req=last_req,
            current_delay=current_delay,
            is_random_delay=is_random_delay,
            available_vectors=available_vectors,
            consecutive_successes=consec_successes,
            consecutive_failures=consec_fails,
        )

        # If skip3 was triggered, set sleeping period for this account
        if last_req.get('skip3'):
            sleep_sec = int(decision.get('recommended_delay', 15))
            m.enter_sleep(sleep_sec)
            self._gemini.panel["active_sleeps"][account_id] = (
                sleep_sec,
                time.time() + sleep_sec,
            )

        return decision

    def should_skip(self, account_id: str) -> bool:
        """
        Return True if this account is sleeping or in cooldown.
        Call at the top of Report() before sending any request.
        """
        with self._lock:
            m = self._metrics.get(account_id)
        if m is None:
            return False
        if m.is_sleeping():
            return True
        if m.is_in_cooldown():
            return True
        # Auto-restore if sleep/cooldown expired
        if m.status in ("sleeping", "cooldown") and not m.is_sleeping() and not m.is_in_cooldown():
            m.restore()
            # Remove from panel active-sleeps
            self._gemini.panel["active_sleeps"].pop(account_id, None)
            log.info("[restore] @%s is active again.", m.label)
            self._notify(
                f"✅ <b>Session Restored</b>\n"
                f"👤 Account: <code>{m.label}</code> is active again."
            )
        return False

    # ─── query ────────────────────────────────────────────────────────────────
    def get_rate(self, account_id: str) -> int:
        with self._lock:
            m = self._metrics.get(account_id)
        if m is None:
            return 10
        return self._gemini.estimate_safe_rate(m.label, m.avg_latency, m.success_rate)

    def get_recommended_delay(self, default_delay: float) -> float:
        """Calculate dynamic sleep delay based on Gemini's safe_rate."""
        p = self.get_panel_data()
        raw_rate = p.get("safe_rate")
        try:
            if raw_rate and str(raw_rate) not in ("—", "None"):
                rate = float(raw_rate)
                if rate > 0:
                    # 60 seconds / rate (e.g. 2 req/min -> 30s)
                    return max(1.0, min(120.0, 60.0 / rate))
        except (ValueError, TypeError):
            pass
        return default_delay

    def get_all_metrics(self) -> List[Dict]:
        with self._lock:
            snapshot = list(self._metrics.values())
        out = []
        for m in snapshot:
            out.append({
                "id":           m.identifier,
                "label":        m.label,
                "total":        m.total,
                "successes":    m.successes,
                "success_rate": round(m.success_rate, 1),
                "avg_latency":  round(m.avg_latency, 0),
                "consec_fails": m.consec_fails,
                "consec_skip3": m.consec_skip3,
                "total_skip3":  m.total_skip3,
                "status":       m.status,
                "remaining":    m.cooldown_remaining(),
            })
        return out

    def get_panel_data(self) -> Dict[str, Any]:
        return self._gemini.get_panel()

    # ─── background loop ──────────────────────────────────────────────────────
    def _loop(self):
        while not self._stop_evt.is_set():
            time.sleep(CHECK_INTERVAL)
            self._check_all()

    def _check_all(self):
        with self._lock:
            items = list(self._metrics.items())

        for account_id, m in items:
            # Auto-restore expired sleeps/cooldowns
            if m.status in ("sleeping", "cooldown"):
                if not m.is_sleeping() and not m.is_in_cooldown():
                    m.restore()
                    self._gemini.panel["active_sleeps"].pop(account_id, None)
                    log.info("[restore] @%s active.", m.label)
                    self._notify(
                        f"✅ <b>Session Restored</b>\n"
                        f"👤 Account: <code>{m.label}</code> is active again."
                    )
                continue   # don't run fail-checks while sleeping/cooling

            # Check consecutive fail threshold
            if m.consec_fails >= FAIL_THRESHOLD:
                verdict = self._gemini.analyze_session(
                    account       = m.label,
                    total         = m.total,
                    success       = m.successes,
                    avg_latency_ms= m.avg_latency,
                    consec_fails  = m.consec_fails,
                )
                log.warning(
                    "[Monitor] @%s → %d consecutive fails. Gemini: %s",
                    m.label, m.consec_fails, verdict,
                )
                m.enter_cooldown(COOLDOWN_SECONDS)
                try:
                    self._redis.mark_expired(account_id)
                except Exception:
                    pass
                self._notify(
                    f"⚠️ <b>Session Dead – 5m Cooldown</b>\n"
                    f"👤 Account: <code>{m.label}</code>\n"
                    f"❌ Consecutive fails: <code>{m.consec_fails}</code>\n"
                    f"🤖 Gemini: <i>{verdict}</i>"
                )
                threading.Thread(
                    target=self._restore_storage,
                    args=(account_id, m, COOLDOWN_SECONDS),
                    daemon=True,
                ).start()

            # Keep monitor purely observational
            pass

    def _restore_storage(self, account_id: str, m: AccountMetrics, delay: int):
        time.sleep(delay)
        try:
            sessions = self._redis._load_local_sessions()
            for s in sessions:
                if s.get("identifier") == account_id:
                    s["status"] = "active"
            self._redis._save_local_sessions(sessions)
            if self._redis.is_connected:
                key = f"{self._redis.SESSION_PREFIX}{account_id}"
                self._redis.client.hset(key, "status", "active")
                self._redis.client.sadd(self._redis.ACTIVE_SET, account_id)
        except Exception as exc:
            log.debug("restore storage failed: %s", exc)

    def _get_or_create(self, account_id: str, label: str) -> AccountMetrics:
        with self._lock:
            if account_id not in self._metrics:
                self._metrics[account_id] = AccountMetrics(account_id, label)
            return self._metrics[account_id]

    def _notify(self, html_msg: str):
        if self._telegram:
            try:
                self._telegram.broadcast(html_msg)
            except Exception as exc:
                log.debug("Telegram notify failed: %s", exc)
