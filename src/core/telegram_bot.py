# -*- coding: utf-8 -*-
"""
Telegram Bot Integration for THUNDER TikTok Reporter.
Provides real-time stats, elapsed duration, request metrics, interactive buttons,
and remote monitoring via Telegram Bot API using standard requests.
"""

import os
import time
import json
import logging
import threading
from datetime import datetime
from typing import Callable, Optional, Dict, Any, List
import requests

logger = logging.getLogger("telegram_bot")


class TelegramReporterBot:
    """
    Lightweight, background Telegram bot handler with interactive button menus
    (ReplyKeyboardMarkup & InlineKeyboardMarkup) using Telegram getUpdates long-polling.
    Requires no heavy third-party framework; operates with pure `requests`.
    """

    def __init__(
        self,
        token: str,
        admin_id: Optional[str] = None,
        stats_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        target_info_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        stop_action: Optional[Callable[[], Any]] = None,
    ):
        self.token = token.strip()
        self.api_url = f"https://api.telegram.org/bot{self.token}"
        self.admin_id = str(admin_id).strip() if admin_id else None
        self.stats_provider = stats_provider
        self.target_info_provider = target_info_provider
        self.stop_action = stop_action

        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_update_id = 0
        self.start_time = time.time()
        self.active_chat_ids = set()
        if self.admin_id:
            self.active_chat_ids.add(self.admin_id)

    def get_bot_info(self) -> Optional[Dict[str, Any]]:
        """Fetch bot identity via /getMe to verify token and retrieve bot username."""
        try:
            res = requests.get(f"{self.api_url}/getMe", timeout=8)
            if res.status_code == 200:
                data = res.json()
                if data.get("ok"):
                    return data.get("result", {})
        except Exception:
            pass
        return None

    @classmethod
    def from_env(
        cls,
        stats_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        target_info_provider: Optional[Callable[[], Dict[str, Any]]] = None,
        stop_action: Optional[Callable[[], Any]] = None,
    ) -> Optional["TelegramReporterBot"]:
        """Create bot instance from environment variables or .env file if configured."""
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        admin_id = os.environ.get("TELEGRAM_ADMIN_ID", "").strip() or None

        if not token:
            env_file = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), ".env")
            if os.path.exists(env_file):
                try:
                    with open(env_file, "r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line.startswith("TELEGRAM_BOT_TOKEN="):
                                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                            elif line.startswith("TELEGRAM_ADMIN_ID="):
                                admin_id = line.split("=", 1)[1].strip().strip('"').strip("'")
                except Exception:
                    pass

        if not token:
            return None
        return cls(
            token=token,
            admin_id=admin_id,
            stats_provider=stats_provider,
            target_info_provider=target_info_provider,
            stop_action=stop_action,
        )

    def start(self):
        """Start polling in a background daemon thread."""
        if not self.token:
            return
        self.start_time = time.time()
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._poll_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop polling background thread."""
        self._stop_event.set()

    def _get_reply_keyboard(self) -> dict:
        """Interactive persistent keyboard buttons on the user's phone keypad."""
        return {
            "keyboard": [
                [{"text": "📊 Live Status"}, {"text": "🔍 Last Request"}],
                [{"text": "👤 Target Profile"}, {"text": "🏓 Ping Health"}],
                [{"text": "⏹ Stop Attack"}, {"text": "❓ Help Menu"}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    def _get_status_inline_keyboard(self) -> dict:
        """Inline clickable buttons directly under messages."""
        buttons = [
            [
                {"text": "🔄 Refresh Status", "callback_data": "btn_refresh"},
                {"text": "🔍 Last Request", "callback_data": "btn_last"},
            ],
            [
                {"text": "👤 Target Profile", "callback_data": "btn_target"},
                {"text": "🏓 Ping Health", "callback_data": "btn_ping"},
            ],
        ]
        if self.stop_action:
            buttons.append([{"text": "⏹ Stop Attack", "callback_data": "btn_stop"}])
        return {"inline_keyboard": buttons}

    def send_message(
        self,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Send message to a specific Telegram chat."""
        if not self.token:
            return False
        try:
            url = f"{self.api_url}/sendMessage"
            payload = {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            res = requests.post(url, json=payload, timeout=10)
            return res.status_code == 200
        except Exception:
            return False

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        parse_mode: str = "HTML",
        reply_markup: Optional[dict] = None,
    ) -> bool:
        """Edit an existing message in-place for live button updates."""
        if not self.token:
            return False
        try:
            url = f"{self.api_url}/editMessageText"
            payload = {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            res = requests.post(url, json=payload, timeout=10)
            return res.status_code == 200
        except Exception:
            return False

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None):
        """Acknowledge inline button press."""
        if not self.token:
            return
        try:
            url = f"{self.api_url}/answerCallbackQuery"
            payload = {"callback_query_id": callback_query_id}
            if text:
                payload["text"] = text
            requests.post(url, json=payload, timeout=5)
        except Exception:
            pass

    def broadcast(self, text: str, parse_mode: str = "HTML", reply_markup: Optional[dict] = None):
        """Broadcast message to admin and known active chats."""
        for cid in list(self.active_chat_ids):
            self.send_message(cid, text, parse_mode=parse_mode, reply_markup=reply_markup)

    def _format_elapsed(self, seconds: float) -> str:
        """Format seconds to HH:MM:SS string."""
        s = int(seconds)
        hours, remainder = divmod(s, 3600)
        minutes, secs = divmod(remainder, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"

    def _format_progress_bar(self, good: int, total: int, width: int = 10) -> str:
        if total <= 0:
            return "░" * width + " 0.0%"
        pct = (good / total) * 100
        filled = round((good / total) * width)
        filled = min(width, max(0, filled))
        bar = "█" * filled + "░" * (width - filled)
        return f"{bar} {pct:.1f}%"

    def _build_status_message(self) -> str:
        """Construct a formatted status report message."""
        if not self.stats_provider:
            return "⚠️ <b>THUNDER Status:</b> No stats provider attached."

        stats = self.stats_provider()
        elapsed = self._format_elapsed(time.time() - self.start_time)

        total = stats.get("total", 0)
        acc = stats.get("accepted", 0)
        rej = stats.get("rejected", 0)
        rate_bar = self._format_progress_bar(acc, total)

        target = stats.get("target", "Unknown")
        target_id = stats.get("target_id", "—")
        mode = stats.get("mode", "mobile").upper()
        delay = stats.get("delay", 1.0)
        captcha = stats.get("captcha_hits", 0)
        cooldowns = stats.get("active_cooldowns", 0)
        reason = stats.get("reason", "—")
        is_running = stats.get("running", True)

        state_badge = "🟢 <b>ACTIVE / REPORTING</b>" if is_running else "💤 <b>IDLE / STOPPED</b>"

        text = (
            f"⚡ <b>THUNDER TikTok Reporter Status</b> ⚡\n"
            f"Status: {state_badge}\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🎯 <b>Target:</b> <code>@{target}</code> (ID: <code>{target_id}</code>)\n"
            f"⏱ <b>Elapsed:</b> <code>{elapsed}</code>\n"
            f"⚙️ <b>Mode:</b> <code>{mode}</code> | <b>Delay:</b> <code>{delay}s</code>\n"
            f"⚖️ <b>Reason Code:</b> <code>{reason}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📊 <b>STATISTICS:</b>\n"
            f"  • ✔️ <b>Accepted:</b> <code>{acc}</code>\n"
            f"  • ❌ <b>Rejected:</b> <code>{rej}</code>\n"
            f"  • 📈 <b>Total:</b> <code>{total}</code>\n"
            f"  • 🎯 <b>Rate:</b> <code>{rate_bar}</code>\n"
        )

        if captcha > 0 or cooldowns > 0:
            text += f"  • ⚠️ <b>CAPTCHAs:</b> <code>{captcha}</code> (cooled: {cooldowns})\n"

        proxy_info = stats.get("proxy_info")
        if proxy_info:
            text += f"  • 🌐 <b>Proxy Pool:</b> <code>{proxy_info}</code>\n"

        text += f"━━━━━━━━━━━━━━━━━━━━\n"
        text += f"🕒 <i>Live as of: {datetime.now().strftime('%H:%M:%S')}</i>"
        return text

    def _build_last_req_message(self) -> str:
        """Construct detail message for the last request."""
        if not self.stats_provider:
            return "⚠️ No stats provider attached."

        stats = self.stats_provider()
        req = stats.get("last_req", {})
        if not req:
            return "ℹ️ No requests have been dispatched yet."

        status_emoji = "✅ Accepted" if req.get("success") else "❌ Rejected"
        score = req.get("score", "—")
        lat = f"{req.get('latency', 0):.1f}ms"
        http_code = req.get("http_status", "—")
        api_code = req.get("server_code", "—")
        device = f"{req.get('brand', '')} {req.get('device', '')}".strip() or "—"
        proxy = req.get("proxy", "Direct")
        time_str = req.get("time", "—")
        snippet = req.get("snippet", "—")

        text = (
            f"🔍 <b>Last Request Details</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Status:</b> {status_emoji}\n"
            f"<b>Score:</b> <code>{score}/10</code>\n"
            f"<b>Latency:</b> <code>{lat}</code>\n"
            f"<b>HTTP Status:</b> <code>{http_code}</code>\n"
            f"<b>API Code:</b> <code>{api_code}</code>\n"
            f"<b>Timestamp:</b> <code>{time_str}</code>\n"
            f"<b>Device:</b> <code>{device}</code>\n"
            f"<b>Proxy:</b> <code>{proxy}</code>\n"
            f"<b>Response:</b> <code>{snippet[:100]}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        return text

    def _build_target_message(self) -> str:
        """Construct detailed target account information."""
        if not self.target_info_provider:
            return "⚠️ No target info provider attached."

        t = self.target_info_provider()
        if not t:
            return "ℹ️ Target information not available."

        text = (
            f"👤 <b>Target Profile Information</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"<b>Username:</b> <code>@{t.get('username', '—')}</code>\n"
            f"<b>Nickname:</b> <code>{t.get('name', '—')}</code>\n"
            f"<b>ID:</b> <code>{t.get('id', '—')}</code>\n"
            f"<b>Followers:</b> <code>{t.get('followers', '—')}</code>\n"
            f"<b>Following:</b> <code>{t.get('following', '—')}</code>\n"
            f"<b>Likes:</b> <code>{t.get('likes', '—')}</code>\n"
            f"<b>Country:</b> <code>{t.get('country', '—')}</code>\n"
            f"<b>Private:</b> <code>{t.get('private', '—')}</code>\n"
            f"<b>Created:</b> <code>{t.get('date', '—')}</code>\n"
        )
        bio = t.get("bio", "")
        if bio and bio != "—":
            text += f"<b>Bio:</b> <i>{bio[:100]}</i>\n"
        text += f"━━━━━━━━━━━━━━━━━━━━"
        return text

    def _build_help_message(self) -> str:
        return (
            f"🤖 <b>THUNDER TikTok Reporter Remote Control</b>\n\n"
            f"Tap any button on your keyboard or select below:\n\n"
            f"📊 <b>Live Status</b> - View current request metrics and runtime\n"
            f"🔍 <b>Last Request</b> - View details on the last HTTP request\n"
            f"👤 <b>Target Profile</b> - View the target TikTok account details\n"
            f"🏓 <b>Ping Health</b> - Check reporter uptime\n"
            f"⏹ <b>Stop Attack</b> - Remotely halt reporting\n"
            f"❓ <b>Help Menu</b> - Show this list"
        )

    def _handle_command(self, chat_id: str, text: str):
        raw = text.strip()
        cmd = raw.lower()
        if "@" in cmd:
            cmd = cmd.split("@")[0]

        inline_markup = None
        reply_markup = self._get_reply_keyboard()

        if cmd in ("/start", "/help", "❓ help menu", "help", "menu"):
            reply = self._build_help_message()
            inline_markup = self._get_status_inline_keyboard()
        elif cmd in ("/status", "/stats", "📊 live status", "status", "stats"):
            reply = self._build_status_message()
            inline_markup = self._get_status_inline_keyboard()
        elif cmd in ("/last", "🔍 last request", "last"):
            reply = self._build_last_req_message()
            inline_markup = self._get_status_inline_keyboard()
        elif cmd in ("/target", "👤 target profile", "target"):
            reply = self._build_target_message()
            inline_markup = self._get_status_inline_keyboard()
        elif cmd in ("/ping", "🏓 ping health", "ping"):
            elapsed = self._format_elapsed(time.time() - self.start_time)
            reply = f"🏓 <b>Pong!</b> Reporter is active and responsive.\n⏱ <b>Elapsed:</b> <code>{elapsed}</code>"
            inline_markup = self._get_status_inline_keyboard()
        elif cmd.startswith(("/rate", "/delay", "rate ", "delay ")):
            parts = raw.split()
            if len(parts) >= 2:
                try:
                    val = float(parts[1])
                    if hasattr(self, "set_rate_action") and self.set_rate_action:
                        self.set_rate_action(val)
                        reply = f"⚡ <b>Rate Updated!</b> Gemini safe rate adjusted to <code>{val} req/min</code>."
                    elif hasattr(self, "set_delay_action") and self.set_delay_action:
                        self.set_delay_action(val)
                        reply = f"⏱ <b>Delay Updated!</b> Base request delay adjusted to <code>{val}s</code>."
                    else:
                        reply = f"✅ Target parameter updated to <code>{val}</code>."
                except ValueError:
                    reply = "⚠️ Invalid value. Example: <code>/rate 5</code> or <code>/delay 12</code>"
            else:
                reply = "ℹ️ Specify a number. Example: <code>/rate 5</code> (req/min) or <code>/delay 15</code> (seconds)"
            inline_markup = self._get_status_inline_keyboard()
        elif cmd in ("/stop", "⏹ stop attack", "stop"):
            if self.stop_action:
                try:
                    self.stop_action()
                    reply = "⏹ <b>Attack Stopped!</b> The reporting process has been halted."
                except Exception as e:
                    reply = f"⚠️ Could not stop attack: {e}"
            else:
                reply = "ℹ️ Stop action not configured for this session."
        else:
            reply = "❓ Unknown command. Tap one of the buttons below or send <code>/help</code>."
            inline_markup = self._get_status_inline_keyboard()

        # Send with both persistent bottom keyboard and inline action buttons
        self.send_message(chat_id, reply, reply_markup=inline_markup or reply_markup)

    def _handle_callback_query(self, cq: dict):
        """Handle inline button clicks with seamless in-place message editing."""
        cq_id = cq.get("id")
        data = cq.get("data", "")
        msg = cq.get("message", {})
        chat_id = str(msg.get("chat", {}).get("id", ""))
        message_id = msg.get("message_id")

        if not chat_id or not message_id:
            self.answer_callback_query(cq_id)
            return

        if data == "btn_refresh":
            text = self._build_status_message()
            self.edit_message_text(
                chat_id, message_id, text, reply_markup=self._get_status_inline_keyboard()
            )
            self.answer_callback_query(cq_id, text="🔄 Status updated!")
        elif data == "btn_last":
            text = self._build_last_req_message()
            self.edit_message_text(
                chat_id, message_id, text, reply_markup=self._get_status_inline_keyboard()
            )
            self.answer_callback_query(cq_id, text="🔍 Last request loaded")
        elif data == "btn_target":
            text = self._build_target_message()
            self.edit_message_text(
                chat_id, message_id, text, reply_markup=self._get_status_inline_keyboard()
            )
            self.answer_callback_query(cq_id, text="👤 Target info loaded")
        elif data == "btn_ping":
            elapsed = self._format_elapsed(time.time() - self.start_time)
            text = f"🏓 <b>Pong!</b> Reporter is active.\n⏱ <b>Elapsed:</b> <code>{elapsed}</code>"
            self.edit_message_text(
                chat_id, message_id, text, reply_markup=self._get_status_inline_keyboard()
            )
            self.answer_callback_query(cq_id, text="🏓 Alive!")
        elif data == "btn_stop":
            if self.stop_action:
                try:
                    self.stop_action()
                    text = "⏹ <b>Attack Stopped!</b> The reporting process has been halted."
                except Exception as e:
                    text = f"⚠️ Could not stop: {e}"
            else:
                text = "ℹ️ Stop action not configured."
            self.edit_message_text(chat_id, message_id, text)
            self.answer_callback_query(cq_id, text="⏹ Stopped!")
        else:
            self.answer_callback_query(cq_id)

    def _poll_loop(self):
        """Continuous long-polling loop handling messages and inline buttons."""
        session = requests.Session()
        while not self._stop_event.is_set():
            try:
                params = {
                    "offset": self._last_update_id + 1,
                    "timeout": 10,
                }
                res = session.get(f"{self.api_url}/getUpdates", params=params, timeout=15)
                if res.status_code != 200:
                    time.sleep(2.0)
                    continue

                data = res.json()
                if not data.get("ok"):
                    time.sleep(2.0)
                    continue

                for update in data.get("result", []):
                    self._last_update_id = update["update_id"]

                    # 1. Inline Button Callback Query
                    if "callback_query" in update:
                        cq = update["callback_query"]
                        sender_id = str(cq.get("from", {}).get("id", ""))
                        if self.admin_id and sender_id != self.admin_id:
                            self.answer_callback_query(cq.get("id"), text="⛔ Unauthorized")
                            continue
                        self._handle_callback_query(cq)
                        continue

                    # 2. Text Message / Keyboard Button Press
                    msg = update.get("message") or update.get("edited_message")
                    if not msg:
                        continue

                    chat_id = str(msg.get("chat", {}).get("id", ""))
                    sender_id = str(msg.get("from", {}).get("id", ""))
                    msg_text = msg.get("text", "")

                    if not chat_id or not msg_text:
                        continue

                    if self.admin_id and sender_id != self.admin_id and chat_id != self.admin_id:
                        self.send_message(
                            chat_id,
                            "⛔ <b>Access Denied:</b> You are not authorized to query this bot.",
                        )
                        continue

                    self.active_chat_ids.add(chat_id)
                    self._handle_command(chat_id, msg_text)

            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
                time.sleep(1.0)
            except Exception:
                time.sleep(2.0)
