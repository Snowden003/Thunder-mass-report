# -*- coding: utf-8 -*-
"""Launch the THUNDER web dashboard in the background from the CLI.

When a CLI attack starts we bring the web console (Flask + Telegram bot) online
so the same operation can be monitored and controlled from a browser and from
Telegram. The dashboard runs as a child process so its module-level state and
Flask/SocketIO event loop stay fully isolated from the CLI process.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_DASHBOARD = os.path.join(_REPO_ROOT, "scripts", "dashboard.py")


def _lan_ip() -> str:
    """Best-effort detection of the primary LAN/public IP of this host."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
        finally:
            s.close()
    except OSError:
        try:
            return socket.gethostbyname(socket.gethostname())
        except OSError:
            return "127.0.0.1"


def _local_ipv4s() -> list:
    """All local IPv4 addresses worth showing to the user (LAN/VPS)."""
    ips = []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if ip and not ip.startswith("127.") and ip not in ips:
                ips.append(ip)
    except OSError:
        pass
    return ips


def public_ip() -> str:
    """Best-effort public IP lookup; empty string when offline."""
    for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                ip = resp.read().decode().strip()
                if ip:
                    return ip
        except Exception:  # noqa: BLE001
            continue
    return ""


def resolve_dashboard_url(port: int = 5000) -> str:
    """Build the dashboard URL, honouring an explicit override.

    Priority: DASHBOARD_URL env var -> DASHBOARD_PUBLIC_IP -> detected LAN IP
    -> public IP lookup -> loopback.
    """
    override = os.environ.get("DASHBOARD_URL", "").strip()
    if override:
        return override.rstrip("/")

    host = os.environ.get("DASHBOARD_PUBLIC_IP", "").strip()
    if host:
        return f"http://{host}:{port}"

    lan_ip = _lan_ip()
    if lan_ip and not lan_ip.startswith("127."):
        return f"http://{lan_ip}:{port}"

    pub = public_ip()
    if pub:
        return f"http://{pub}:{port}"

    return f"http://127.0.0.1:{port}"


def dashboard_bot_username() -> str:
    """Resolve the Telegram bot @username from env/.env if present."""
    name = os.environ.get("TELEGRAM_BOT_USERNAME", "").strip()
    if name:
        return name if name.startswith("@") else f"@{name}"
    env_file = os.path.join(_REPO_ROOT, ".env")
    try:
        with open(env_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_USERNAME="):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val if val.startswith("@") else f"@{val}"
    except OSError:
        pass
    return "@q7nullveil_bot"


def announce_dashboard(telegram_bot=None, port: int = 5000, timeout: float = 15.0) -> dict:
    """Wait for the running dashboard, then push its URL to Telegram.

    Called by the CLI right after launching the dashboard so the user receives
    the same address in the terminal and in the Telegram bot chat.
    """
    local_url = f"http://127.0.0.1:{port}"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _port_open("127.0.0.1", port) or _responds(local_url + "/login"):
            break
        time.sleep(0.25)

    url = resolve_dashboard_url(port)
    bot_name = dashboard_bot_username()

    if telegram_bot is not None:
        text = (
            "🌐 <b>THUNDER Dashboard is online</b>\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 <b>URL:</b> {url}\n"
            "🔐 <b>Login:</b> your master password\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            f"🤖 Bot: <code>{bot_name}</code> (<code>/login</code> then "
            "<code>/startattack</code>, <code>/stop</code>, <code>/set</code>)"
        )
        try:
            telegram_bot.broadcast(text)
        except Exception:  # noqa: BLE001
            pass

    return {
        "url": url,
        "local_url": local_url,
        "port": port,
        "bot": bot_name,
        "lan_ips": _local_ipv4s(),
    }


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _responds(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status in (200, 302, 401)
    except Exception:  # noqa: BLE001
        return False


def launch_dashboard(port: int = 5000, wait: float = 8.0):
    """Start the dashboard if it is not already running.

    Returns ``(info_dict, process)`` where ``info_dict`` contains the URL the
    user should open and the login hint, and ``process`` is the child process
    (or ``None`` if the dashboard was already running).
    """
    lan_ip = _lan_ip()

    if _port_open("127.0.0.1", port):
        info = {
            "url": f"http://{lan_ip}:{port}",
            "local_url": f"http://127.0.0.1:{port}",
            "port": port,
            "already_running": True,
        }
        return info, None

    env = dict(os.environ)
    env.setdefault("DASHBOARD_HOST", "0.0.0.0")
    env["DASHBOARD_PORT"] = str(port)
    env["PYTHONUNBUFFERED"] = "1"

    creationflags = 0
    if os.name == "nt":  # pragma: no cover - Windows only
        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

    process = subprocess.Popen(
        [sys.executable, _DASHBOARD],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creationflags,
    )

    # Wait for the HTTP server to answer so the printed URL is actually live.
    deadline = time.time() + wait
    local_url = f"http://127.0.0.1:{port}"
    while time.time() < deadline:
        if process.poll() is not None:
            break
        if _port_open("127.0.0.1", port) or _responds(local_url + "/login"):
            break
        time.sleep(0.25)

    info = {
        "url": f"http://{lan_ip}:{port}",
        "local_url": local_url,
        "port": port,
        "already_running": False,
    }
    return info, process
