<div align="center">

# ⚡ THUNDER – Advanced TikTok Mass Reporting & Automation Toolkit

[![Python](https://img.shields.io/badge/Python-3.10%2B-blue.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/Flask-Web%20Dashboard-green.svg)](https://flask.palletsprojects.com/)
[![License](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Status](https://img.shields.io/badge/Status-Active-success.svg)]()

*A modular, high-performance, asynchronous reporting and security automation engine tailored for TikTok platforms, featuring real-time telemetry, cryptographic signature generators, multi-session rotation, and an interactive web dashboard.*

</div>

---

## 🚀 Key Features

### 1. Presentation & Control Layers
- **Rich TUI Terminal Interface (`report-tiktok.py`)**: State-machine-driven screens (`Welcome`, `Config`, `Execution`, `PauseMenu`, `Results`) managed via Rich UI.
- **Web Dashboard (`scripts/dashboard.py`)**: Flask + Flask-SocketIO backend providing real-time WebSocket state streaming, live graphs, proxy health inspection, session management, and remote monitoring.
- **Telegram Bot Integration (`TelegramReporterBot`)**: Lightweight background bot supporting long-polling, interactive inline keyboards, live metrics, and remote stop/status triggers.

### 2. Core Reporting & Async Engines (`src/core/`)
- **`ReportEngine`**: Standalone, thread-safe reporting core decoupled from UI, supporting snapshot polling, dry-run mode, and robust error recovery.
- **Async Concurrency Pool (`src/core/async_engine/`)**: Concurrent worker pool fanning out requests across latency-probed proxies (filtered under 300ms) with fail-over recycling.
- **Request Cryptography & Signing (`src/mobile/`)**: Generates valid TikTok mobile API request signatures (`x-gorgon`, `x-ladon`, `x-argus`, `x-khronos`) across mobile (`musical_ly`) and web modes.
- **Device & Fingerprint Spoofing**: Rotates mobile hardware profiles (`TikTokAg.py`) and web browser user-agents/client-hints (`web_devices.py`) to bypass anti-bot challenges.
- **Session & Account Rotation (`account_rotation.py`, `redis_manager.py`)**: Manages and validates session cookies (`sessionid`, `sid_tt`), supporting Redis persistence and automatic fallback.

### 3. Security & Authentication (`src/core/security.py`)
- **Shared Master Password**: Dashboard and Telegram bot protected by a single master password stored as a salted `scrypt` hash in `config/auth.json`.
- **Brute-Force Protection**: Temporary IP/user lockout after 5 consecutive failed login attempts.

---

## 🛠️ Project Structure

```text
thunder/
├── report-tiktok.py         # Terminal CLI entry point (Rich TUI)
├── thunder.py               # Main launcher / bootstrap utility
├── requirements.txt         # Python package dependencies
├── config/                  # Configuration files (settings.yaml)
├── data/                    # Local caches, proxy lists, saved sessions
├── logs/                    # Structured JSONL session audit logs
├── scripts/
│   ├── dashboard.py         # Flask + SocketIO Web Dashboard backend
│   └── extract_browser_cookies.py # Browser cookie extractor helper
├── templates/               # Dashboard HTML templates (index.html, login.html)
└── src/
    ├── core/                # Core engines, proxy manager, AI client, bot
    ├── mobile/              # TikTok mobile cryptographic signers (Gorgon, Argus, Ladon)
    ├── ui/                  # Rich TUI screens and components
    └── web/                 # Web device profiles and fingerprint spoofing
```

---

## 📦 Installation & Setup

### 1. Clone the Repository
```bash
git clone https://github.com/Snowden003/Thunder-mass-report.git
cd Thunder-mass-report
```

### 2. Install Dependencies
```bash
python -m pip install -r requirements.txt
```

### 3. Configure Authentication
Set or update the shared master password for the dashboard and Telegram bot:
```bash
python -c "import sys; sys.path.insert(0,'src'); from core import security; security.set_password('YOUR_SECURE_PASSWORD')"
```

---

## 🚀 Running Applications

### Terminal CLI Interface (Rich TUI)
```bash
python report-tiktok.py
```

### Web Dashboard (Flask + SocketIO)
```bash
python scripts/dashboard.py
```
*(By default, accessible at `http://0.0.0.0:5000`)*

With custom host/port:
```bash
DASHBOARD_HOST=127.0.0.1 DASHBOARD_PORT=8080 python scripts/dashboard.py
```

---

## 🧪 Testing

Run the full test suite with root PYTHONPATH:
```bash
PYTHONPATH=src:scripts python -m unittest discover -s tests
```

Run a specific test module:
```bash
PYTHONPATH=src:scripts python -m unittest tests/test_dry_run.py
```

---

## ⚠️ Disclaimer
This tool is designed strictly for educational purposes, security research, and API auditing. Users are responsible for complying with local laws and platform terms of service.
