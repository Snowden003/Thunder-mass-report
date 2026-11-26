#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Auto-restart dashboard whenever src/ or scripts/*.py files change."""

import os
import sys
import time
import signal
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BASE_DIR)

WATCH_DIRS = [
    os.path.join(REPO_ROOT, 'src'),
    BASE_DIR,
]
ENV = {**os.environ, 'PYTHONPATH': f'{REPO_ROOT}/src:{REPO_ROOT}/scripts'}


def _collect_files() -> set[str]:
    paths: set[str] = set()
    for d in WATCH_DIRS:
        if not os.path.isdir(d):
            continue
        for root, _, files in os.walk(d):
            for f in files:
                if f.endswith('.py'):
                    paths.add(os.path.join(root, f))
    return paths


def _mtime_map(files: set[str]) -> dict[str, float]:
    return {p: os.path.getmtime(p) for p in files}


def _changed(prev: dict[str, float]) -> bool:
    curr = _mtime_map(_collect_files())
    return curr != prev


def main() -> int:
    proc: subprocess.Popen | None = None
    prev: dict[str, float] = {}

    def _stop(sig, _frame):
        if proc is not None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
        sys.exit(0)

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    poll_interval = 1.0

    while True:
        if proc is None or proc.poll() is not None:
            cmd = [sys.executable, os.path.join(BASE_DIR, 'dashboard.py')]
            print(f'\n[watcher] Starting dashboard (PID {os.getpid()}) ...', flush=True)
            proc = subprocess.Popen(cmd, cwd=BASE_DIR, env=ENV)
            prev = _mtime_map(_collect_files())

        time.sleep(poll_interval)
        if _changed(prev):
            print('[watcher] Source changed — restarting dashboard ...', flush=True)
            if proc and proc.poll() is None:
                proc.send_signal(signal.SIGINT)
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            proc = None
            prev = _mtime_map(_collect_files())


if __name__ == '__main__':
    raise SystemExit(main())
