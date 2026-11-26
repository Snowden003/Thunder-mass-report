# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

### Environment & Dependencies
```bash
python -m pip install -r requirements.txt
```

### Running Applications
```bash
# Terminal CLI interface (Rich TUI)
python report-tiktok.py

# Web Dashboard (Flask + SocketIO on 0.0.0.0:5000 by default)
python scripts/dashboard.py

# With custom host/port or secret
DASHBOARD_HOST=127.0.0.1 DASHBOARD_PORT=8080 python scripts/dashboard.py
```
*Note:* `scripts/run_dashboard.sh` changes directory into `scripts/` before searching for `venv`. Run via root Python directly or activate the root virtualenv beforehand.

### Testing
```bash
# Run full test suite (requires PYTHONPATH set to root src:scripts)
PYTHONPATH=src:scripts python -m unittest discover -s tests

# Run a single test module
PYTHONPATH=src:scripts python -m unittest tests/test_dry_run.py
PYTHONPATH=src:scripts python -m unittest tests/test_async_engine.py

# Run a specific test case or method
PYTHONPATH=src:scripts python -m unittest tests.test_dry_run.DryRunTests.test_do_report_never_calls_network
```
*Test caveats:* Async tests bind a loopback port. Tests involving target-resolution rely on local cache (`data/targets.json`, `data/important_requests.json`) and may conflict with dashboard auth when testing unauthenticated dashboard endpoints.

### Authentication & Secrets Management
```bash
# Set or change the shared master password for dashboard and Telegram bot:
python -c "import sys; sys.path.insert(0,'src'); from core import security; security.set_password('NEW_PASSWORD')"
```

## Architecture & System Design

The repository is modularized into presentation interfaces (Rich CLI and Flask Web Dashboard) driving core execution and network engines.

### 1. Presentation Layers
- **CLI Interface (`report-tiktok.py` & `src/ui/`)**: State machine driven by `Screen` instances (`WelcomeScreen`, `ConfigScreen`, `ExecutionScreen`, `PauseMenu`, `ResultsScreen`) managed by `Renderer` and `AppState`. Drives `ReportEngine` directly.
- **Web Dashboard (`scripts/dashboard.py` & `templates/`)**: Flask + Flask-SocketIO backend with real-time WebSocket state streaming (`stats_update`, `new_request`). Protected by mandatory session auth hooks and optionally exposes controls to a Telegram bot (`TelegramReporterBot`).

### 2. Core Reporting Engines (`src/core/`)
- **`ReportEngine` (`src/core/report_engine.py`)**: Standalone, thread-safe reporting engine decoupled from UI. Exposes `snapshot()` polling, start/stop/pause/resume controls, and supports dry-run mode (`dry_run=True`) without making network requests.
- **`FastReporter` & Async Engine (`src/core/fast_report.py`, `src/core/async_engine/`)**: Concurrent worker pool fanning out requests across latency-probed proxies (filtered under 300ms by default) with concurrency limits and fail-over recycling.
- **Request Signing & Cryptography (`Argus.py`, `Gorgon.py`, `Ladon.py`, `Sm3.py`, `ttencrypt.py`)**: Generates TikTok API request signatures (`x-gorgon`, `x-ladon`, `x-argus`, `x-khronos`) across mobile (`musical_ly`) and web (`tiktok_web`) modes. Query parameters must be included before urlencoding for valid signature checks.
- **Device & Fingerprint Spoofing (`TikTokAg.py`, `web_devices.py`)**: Rotates mobile hardware profiles and web browser user-agents/client-hints. Tracks temporary blocks upon captcha challenges.
- **Session & Account Rotation (`account_rotation.py`, `redis_manager.py`)**: Manages TikTok session cookies (`sessionid`, `sid_tt`), optionally persisting to Redis or `data/saved_sessions.json`.
- **Telemetry & Logging (`project_logger.py`)**: Emits structured JSONL logs per attack session under `logs/` and persists important events/devices under `data/`.

### 3. Authentication & Security Model (`src/core/security.py`)
- Dashboard and Telegram bot share a single master password stored as a salted scrypt hash in `config/auth.json`.
- Login enforces brute-force protection (lockout after 5 failed attempts).
- Session signing key is read from `DASHBOARD_SECRET` env var or generated and cached in `config/.dashboard_secret`.
