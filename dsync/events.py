"""线程安全的发布/订阅事件总线。

同步引擎、设备检测器、HTTP 服务之间通过它解耦：
设备检测线程产生事件 -> 总线 -> Web 看板（SSE）与托盘订阅者。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)


class EventBus:
    """极简广播总线。每个订阅者拿到自己独立的队列，互不阻塞。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: list[queue.Queue] = []
        self._seq = 0

    # ------------------------------------------------------------------ 订阅
    def subscribe(self, maxsize: int = 1000) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=maxsize)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._subscribers:
                self._subscribers.remove(q)

    # ------------------------------------------------------------------ 发布
    def publish(self, event_type: str, **payload: Any) -> dict:
        """广播一个事件，返回带序号的完整事件字典。

        ``event_type`` 是事件大类（device / phase / plan / file_start ...），
        具体的子事件通过 ``**payload`` 附加，避免与形参名冲突。
        """
        with self._lock:
            self._seq += 1
            message = {"seq": self._seq, "event": event_type, "ts": time.time(), **payload}
            targets = list(self._subscribers)
        for q in targets:
            try:
                q.put_nowait(message)
            except queue.Full:
                # 订阅者消费不过来时丢弃最老的一条，保证不阻塞发布方
                try:
                    q.get_nowait()
                    q.put_nowait(message)
                except (queue.Empty, queue.Full):
                    logger.warning("事件队列已满，丢弃事件 %s", event_type)
        return message


class EventBroadcaster:
    """把总线事件转发给任意回调（用于托盘图标刷新、日志落盘等）。"""

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
            except Exception:  # pragma: no cover - 回调异常不应影响主流程
                logger.exception("事件回调执行失败")

    def stop(self) -> None:
        self._stop.set()
        self._bus.unsubscribe(self._queue)
