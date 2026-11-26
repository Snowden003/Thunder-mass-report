# -*- coding: utf-8 -*-
"""Minimal event bus so workers never write to the terminal directly.

Workers publish without awaiting; a single consumer renders output. This is
what prevents interleaved rich/console output once several workers run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from .models import Result


@dataclass(slots=True)
class Event:
    kind: str
    result: Result | None = None
    message: str = ""


class EventBus:
    def __init__(self, maxsize: int = 1000) -> None:
        self._queue: asyncio.Queue[Event | None] = asyncio.Queue(maxsize=maxsize)

    def publish(self, event: Event) -> None:
        """Non-blocking publish; drops when the consumer falls behind."""
        try:
            self._queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def result(self, result: Result) -> None:
        self.publish(Event("result", result=result))

    def log(self, message: str) -> None:
        self.publish(Event("log", message=message))

    def close(self) -> None:
        if self._queue.full():
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
        try:
            self._queue.put_nowait(None)
        except asyncio.QueueFull:
            pass

    def __aiter__(self) -> "EventBus":
        return self

    async def __anext__(self) -> Event:
        event = await self._queue.get()
        if event is None:
            raise StopAsyncIteration
        return event
