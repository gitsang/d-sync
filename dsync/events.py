"""Thread-safe publish/subscribe event bus.

It decouples the sync engine, the device monitor and the HTTP server:
the device monitor thread emits events -> bus -> web dashboard (SSE) and tray subscribers.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventBus:
    """Minimal broadcast bus. Every subscriber gets its own queue, so nobody blocks."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._seq = 0

    # -------------------------------------------------------------- subscribe
    def subscribe(self, maxsize: int = 1000) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # ---------------------------------------------------------------- publish
    def publish(self, event_type: str, **payload: Any) -> dict:
        """Broadcast an event and return the complete message including its sequence number.

        ``event_type`` is the event category (device / phase / plan / file_start ...);
        sub-event details are attached through ``**payload`` so they cannot clash
        with parameter names.
        """
        with self._lock:
            self._seq += 1
            message = {"seq": self._seq, "event": event_type, "ts": time.time(), **payload}
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(message)
            except queue.Full:
                # Drop the oldest event when a subscriber cannot keep up, so publishers never block.
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except (queue.Empty, queue.Full):
                    logger.warning("Event queue is full, dropping event %s", event_type)
        return message


class EventBroadcaster:
    """Forwards bus events to an arbitrary callback (tray refresh, file logging, ...)."""

    def __init__(self, bus: EventBus, callback: Callable[[dict], None]) -> None:
        self._bus = bus
        self._callback = callback
        self._queue = bus.subscribe()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="event-broadcaster", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                message = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._callback(message)
            except Exception:  # pragma: no cover - a callback error must not break the main flow
                logger.exception("Event callback failed")

    def stop(self) -> None:
        self._stop.set()
        self._bus.unsubscribe(self._queue)
