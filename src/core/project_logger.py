# -*- coding: utf-8 -*-
"""Per-project request/response logging with split JSONL files.

Each project gets its own directory under ``logs/projects`` containing:

    meta.json
    requests.part0001.jsonl
    responses.part0001.jsonl
    events.part0001.jsonl

Every stream rotates independently once it reaches ``max_bytes`` so long
projects never produce a single huge file. Requests and responses share a
``request_id`` so they can be correlated.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any, Optional

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_ROOT = os.path.join(REPO_ROOT, "logs", "projects")
DEFAULT_MAX_BYTES = 5 * 1024 * 1024
STREAMS = ("requests", "responses", "events")


def _slug(value: Any, fallback: str = "project") -> str:
    text = str(value or "").strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "_", text)
    text = text.strip("._-")
    return text[:80] or fallback


def _jsonable(value: Any) -> Any:
    """Best-effort conversion so any payload can be written as JSON."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    try:
        return _jsonable(dict(value))
    except Exception:
        return str(value)


class ProjectLogger:
    """Thread-safe logger that writes request/response/event streams per project."""

    def __init__(
        self,
        name: str,
        root: Optional[str] = None,
        max_bytes: int = DEFAULT_MAX_BYTES,
        enabled: bool = True,
    ):
        self.name = str(name or "project")
        self.root = root or DEFAULT_ROOT
        self.max_bytes = max(1024, int(max_bytes))
        self.enabled = bool(enabled)
        self.closed = False
        self._lock = threading.RLock()
        self._parts = {stream: 1 for stream in STREAMS}
        self._sizes = {stream: 0 for stream in STREAMS}
        self._counts = {stream: 0 for stream in STREAMS}
        self._started_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
        self._started_ts = time.time()
        self._ended_at: Optional[str] = None
        self._paths = {stream: [] for stream in STREAMS}

        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        base = f"{_slug(self.name)}_{stamp}"
        self.dir = os.path.join(self.root, base)
        if os.path.exists(self.dir):
            self.dir = os.path.join(self.root, f"{base}_{uuid.uuid4().hex[:6]}")

        if self.enabled:
            os.makedirs(self.dir, exist_ok=True)
            for stream in STREAMS:
                path = self._part_path(stream)
                self._paths[stream].append(os.path.basename(path))
                self._sizes[stream] = os.path.getsize(path) if os.path.exists(path) else 0
            self._write_meta()

    # ── Public API ──────────────────────────────────────────────────────────

    @property
    def path(self) -> str:
        return self.dir

    def new_request_id(self) -> str:
        return uuid.uuid4().hex

    def log_request(
        self,
        request_id: str,
        method: str,
        url: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        cookies: Optional[dict] = None,
        body: Any = None,
        **extra: Any,
    ) -> None:
        entry = {
            "ts": time.time(),
            "iso": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "kind": "request",
            "request_id": request_id,
            "method": method,
            "url": url,
            "params": params or {},
            "headers": headers or {},
            "cookies": cookies or {},
            "body": body,
        }
        entry.update(extra)
        self._append("requests", entry)

    def log_response(
        self,
        request_id: str,
        status_code: Any = None,
        headers: Optional[dict] = None,
        body: Any = None,
        latency_ms: Optional[float] = None,
        error: Optional[str] = None,
        **extra: Any,
    ) -> None:
        entry = {
            "ts": time.time(),
            "iso": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "kind": "response",
            "request_id": request_id,
            "status_code": status_code,
            "headers": headers or {},
            "body": body,
            "latency_ms": round(latency_ms, 1) if latency_ms is not None else None,
            "error": error,
        }
        entry.update(extra)
        self._append("responses", entry)

    def log_event(self, event: str, **fields: Any) -> None:
        entry = {
            "ts": time.time(),
            "iso": datetime.now().astimezone().isoformat(timespec="milliseconds"),
            "kind": "event",
            "event": event,
        }
        entry.update(fields)
        self._append("events", entry)

    def close(self, **fields: Any) -> None:
        with self._lock:
            if self.closed:
                return
            self.closed = True
            self._ended_at = datetime.now().astimezone().isoformat(timespec="milliseconds")
            if self.enabled:
                self._append(
                    "events",
                    {
                        "ts": time.time(),
                        "iso": self._ended_at,
                        "kind": "event",
                        "event": "project_closed",
                        "counts": dict(self._counts),
                        **fields,
                    },
                    force=True,
                )
                self._write_meta(extra=fields)

    def __enter__(self) -> "ProjectLogger":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close(error=str(exc) if exc else None)

    # ── Internals ───────────────────────────────────────────────────────────

    def _part_path(self, stream: str) -> str:
        return os.path.join(self.dir, f"{stream}.part{self._parts[stream]:04d}.jsonl")

    def _append(self, stream: str, entry: dict, force: bool = False) -> None:
        if not self.enabled or (self.closed and not force):
            return
        line = json.dumps(_jsonable(entry), ensure_ascii=False) + "\n"
        data = line.encode("utf-8")
        with self._lock:
            if self.closed and not force:
                return
            if self._sizes[stream] and self._sizes[stream] + len(data) > self.max_bytes:
                self._rotate(stream)
            path = self._part_path(stream)
            try:
                with open(path, "ab") as handle:
                    handle.write(data)
                self._sizes[stream] += len(data)
                self._counts[stream] += 1
                if self._counts[stream] % 100 == 0:
                    self._write_meta()
            except OSError:
                pass

    def _rotate(self, stream: str) -> None:
        self._parts[stream] += 1
        self._sizes[stream] = 0
        name = os.path.basename(self._part_path(stream))
        self._paths[stream].append(name)
        self._write_meta()

    def _write_meta(self, extra: Optional[dict] = None) -> None:
        meta = {
            "project": self.name,
            "directory": self.dir,
            "started_at": self._started_at,
            "started_ts": self._started_ts,
            "ended_at": self._ended_at,
            "max_bytes": self.max_bytes,
            "streams": {
                stream: {
                    "files": list(self._paths[stream]),
                    "records": self._counts[stream],
                    "bytes": self._sizes[stream],
                }
                for stream in STREAMS
            },
        }
        if extra:
            meta.update(_jsonable(extra))
        path = os.path.join(self.dir, "meta.json")
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(_jsonable(meta), handle, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except OSError:
            pass
