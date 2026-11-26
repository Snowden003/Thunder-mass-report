# -*- coding: utf-8 -*-
"""Shared password authentication for the dashboard and the Telegram bot.

One master password controls both surfaces. Only a salted scrypt hash is ever
stored on disk (``config/auth.json``); the plaintext password is never written
anywhere. Failed attempts are tracked with a temporary lockout, and short-lived
session tokens are used for the browser and the bot.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from typing import Dict, Optional, Tuple

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
AUTH_FILE = os.path.join(_REPO_ROOT, "config", "auth.json")

# Password seeded once on first run so the shared password works immediately.
INITIAL_PASSWORD = "admin"

# scrypt parameters (interactive-grade; ~50-100ms per verification).
_SCRYPT_N = 2 ** 14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_DKLEN = 32
_SALT_BYTES = 16

# Lockout policy.
MAX_FAILED_ATTEMPTS = 5
LOCKOUT_SECONDS = 300

# Session token lifetime for browser + bot logins.
TOKEN_TTL_SECONDS = 1800


# ── Password hashing ────────────────────────────────────────────────────────

def _hash_with_salt(password: str, salt: bytes) -> str:
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
    )
    return digest.hex()


def hash_password(password: str) -> Dict[str, str]:
    """Return a storable ``{salt, hash}`` record for *password*."""
    salt = secrets.token_bytes(_SALT_BYTES)
    return {"salt": salt.hex(), "hash": _hash_with_salt(password, salt)}


def _read_auth() -> Optional[Dict[str, str]]:
    try:
        with open(AUTH_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if data.get("salt") and data.get("hash"):
            return data
    except (FileNotFoundError, ValueError, OSError):
        pass
    return None


def _write_auth(record: Dict[str, str]) -> None:
    os.makedirs(os.path.dirname(AUTH_FILE), exist_ok=True)
    tmp = AUTH_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2)
    os.replace(tmp, AUTH_FILE)
    try:
        os.chmod(AUTH_FILE, 0o600)
    except OSError:
        pass


def ensure_initial_password(password: str = INITIAL_PASSWORD) -> None:
    """Create the auth file with *password* if none exists yet."""
    if _read_auth() is None:
        _write_auth(hash_password(password))


def set_password(password: str) -> None:
    """Overwrite the stored password hash."""
    _write_auth(hash_password(password))


def verify_password(password: str) -> bool:
    """Constant-time check of *password* against the stored hash."""
    record = _read_auth()
    if not record or not password:
        return False
    try:
        salt = bytes.fromhex(record["salt"])
    except (KeyError, ValueError):
        return False
    candidate = _hash_with_salt(password, salt)
    return hmac.compare_digest(candidate, record.get("hash", ""))


# ── Brute-force lockout ─────────────────────────────────────────────────────

class _AttemptTracker:
    def __init__(self) -> None:
        self._failures: Dict[str, int] = {}
        self._locked_until: Dict[str, float] = {}

    def is_locked(self, key: str) -> Tuple[bool, int]:
        until = self._locked_until.get(key, 0.0)
        remaining = int(until - time.time())
        if remaining > 0:
            return True, remaining
        return False, 0

    def register_failure(self, key: str) -> int:
        count = self._failures.get(key, 0) + 1
        self._failures[key] = count
        if count >= MAX_FAILED_ATTEMPTS:
            self._locked_until[key] = time.time() + LOCKOUT_SECONDS
            self._failures[key] = 0
        return count

    def reset(self, key: str) -> None:
        self._failures.pop(key, None)
        self._locked_until.pop(key, None)


_attempts = _AttemptTracker()


def check_login(key: str, password: str) -> Tuple[bool, str]:
    """Validate a login attempt for *key* (a user id or client IP).

    Returns ``(ok, message)``. Applies a temporary lockout after repeated
    failures so credentials cannot be brute-forced.
    """
    locked, remaining = _attempts.is_locked(key)
    if locked:
        return False, f"Too many failed attempts. Try again in {remaining}s."
    if verify_password(password):
        _attempts.reset(key)
        return True, "ok"
    count = _attempts.register_failure(key)
    left = MAX_FAILED_ATTEMPTS - count
    if left <= 0:
        return False, f"Locked out for {LOCKOUT_SECONDS}s after too many failures."
    return False, f"Incorrect password ({left} attempts left)."


# ── Session tokens ──────────────────────────────────────────────────────────

_tokens: Dict[str, float] = {}


def issue_token() -> str:
    token = secrets.token_urlsafe(32)
    _tokens[token] = time.time() + TOKEN_TTL_SECONDS
    return token


def validate_token(token: Optional[str]) -> bool:
    if not token:
        return False
    expires = _tokens.get(token)
    if expires is None:
        return False
    if time.time() > expires:
        _tokens.pop(token, None)
        return False
    return True


def revoke_token(token: Optional[str]) -> None:
    if token:
        _tokens.pop(token, None)
