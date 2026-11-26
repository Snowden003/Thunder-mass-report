from __future__ import annotations

import os
import sys
from typing import Optional

# ── Path Setup ──────────────────────────────────────────────────────────────

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(BASE_DIR, "src")
for _d in (BASE_DIR, SRC_DIR):
    if _d not in sys.path:
        sys.path.insert(0, _d)

# ── Core Imports (Business Logic) ───────────────────────────────────────────

# from core import (
#     ProxyManager,
#     RedisSessionManager,
#     TelegramReporterBot,
#     run_preflight_analysis,
#     run_consultation_cli,
#     PreflightConsultation,
#     make_request_with_proxy,
#     pick_web_device,
#     generate_web_device_id,
#     UserAgent,
#     Gorgon,
#     Ladon,
#     Argus,
# )

from core.proxy_manager import load_proxy_file, validate_proxies

# ── UI Imports (Presentation Layer) ─────────────────────────────────────────

from ui import (
    create_console,
    Renderer,
    AppState,
    Screen,
    WelcomeScreen,
    ConfigScreen,
    ExecutionScreen,
    PauseMenu,
    ResultsScreen,
)

# ── Application Entry Point ─────────────────────────────────────────────────

def main() -> int:
    """Application entry point."""

    # Initialize UI
    console = create_console()
    renderer = Renderer(console)
    state = AppState()

    # ── Target persistence ──────────────────────────────────────────────
    last_target_file = os.path.join(BASE_DIR, "data", "last_target.txt")
    try:
        with open(last_target_file, "r", encoding="utf-8") as f:
            state.target = f.read().strip().lstrip("@")
    except FileNotFoundError:
        pass
    except OSError as exc:
        renderer.print(f"[warning]Could not load last target: {exc}[/warning]")

    def _save_last_target(target: str) -> None:
        """Persist the normalized target after it is entered."""
        try:
            os.makedirs(os.path.dirname(last_target_file), exist_ok=True)
            with open(last_target_file, "w", encoding="utf-8") as f:
                f.write(target)
        except OSError as exc:
            renderer.print(f"[warning]Could not save last target: {exc}[/warning]")

    # ── Screen State Machine ────────────────────────────────────────────
    screen: Optional[Screen] = WelcomeScreen(
        on_target_entered=lambda t: _save_last_target(t)  # Save target to file
    )

    while screen is not None:
        try:
            screen.render(renderer, state)
            next_screen = screen.handle_input(renderer, state)
            screen = next_screen
        except KeyboardInterrupt:
            renderer.print("\n[warning]Interrupted by user.[/warning]")
            state.is_running_attack = False
            break
        except Exception as exc:  # noqa: BLE001
            renderer.print(f"[error]Error: {exc}[/error]")
            renderer.print("[muted]Press Enter to continue...[/muted]")
            renderer.console.input()
            break

    renderer.print("\n[success]Session terminated cleanly.[/success]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())