"""内置的本地 Web 服务：提供看板页面 + REST API + SSE 实时推送。

只监听 127.0.0.1，不对外暴露。使用标准库 http.server，避免额外依赖。
"""

from __future__ import annotations

import json
import logging
import mimetypes
import queue
import socket
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

from ..events import EventBus

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"


class _Handler(BaseHTTPRequestHandler):
    server_version = "D-Sync/1.0"
    protocol_version = "HTTP/1.1"

    # 由 create_server 注入
    app: "WebApp" = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # 降噪
        logger.debug("HTTP %s - %s", self.address_string(), fmt % args)

    def _send(self, status: int, body: bytes, content_type: str,
              extra_headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    # ------------------------------------------------------------------
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self._serve_static("index.html")
        elif path == "/api/state":
            self._json(self.app.snapshot())
        elif path == "/api/history":
            self._json({"history": self.app.history()})
        elif path == "/api/events":
            self._serve_sse()
        elif path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
        elif path == "/health":
            self._json({"ok": True, "version": self.app.version})
        else:
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (ValueError, UnicodeDecodeError):
            payload = {}

        action = parsed.path.rsplit("/", 1)[-1]
        handler = self.app.actions.get(action)
        if handler is None:
            self._json({"error": f"未知操作: {action}"}, HTTPStatus.NOT_FOUND)
            return
        try:
            result = handler(payload)
        except Exception as exc:
            logger.exception("处理操作 %s 失败", action)
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._json({"ok": True, "result": result})

    # ------------------------------------------------------------------
    def _serve_static(self, relative: str) -> None:
        safe = (STATIC_DIR / relative).resolve()
        try:
            safe.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json({"error": "非法路径"}, HTTPStatus.FORBIDDEN)
            return
        if not safe.is_file():
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type, _ = mimetypes.guess_type(str(safe))
        if content_type and content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, safe.read_bytes(), content_type or "application/octet-stream")

    def _serve_sse(self) -> None:
        """SSE 长连接，持续推送事件。"""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        subscriber = self.app.bus.subscribe()
        try:
            # 首帧：把当前状态快照推给客户端
            snapshot = json.dumps(
                {"event": "snapshot", "seq": 0, "ts": time.time(), "state": self.app.snapshot()},
                ensure_ascii=False, default=str,
            )
            self.wfile.write(f"data: {snapshot}\n\n".encode("utf-8"))
            self.wfile.flush()

            last_beat = time.time()
            while True:
                try:
                    message = subscriber.get(timeout=1.0)
                except queue.Empty:
                    message = None

                if message is not None:
                    payload = json.dumps(message, ensure_ascii=False, default=str)
                    self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))
                    self.wfile.flush()
                elif time.time() - last_beat > 15:
                    # 心跳，防止代理断开
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.bus.unsubscribe(subscriber)


class WebApp:
    """看板后端：聚合状态快照与操作接口。"""

    def __init__(self, bus: EventBus, state_provider: Callable[[], dict],
                 actions: dict[str, Callable[[dict], Any]], version: str = "1.0.0") -> None:
        self.bus = bus
        self.state_provider = state_provider
        self.actions = actions
        self.version = version
        self._history: list[dict] = []
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        state = dict(self.state_provider())
        with self._lock:
            # 应用层若已提供 history 则以其为准，否则用 WebApp 自己的记录
            if not state.get("history"):
                state["history"] = list(self._history[-20:])
        return state

    def history(self) -> list[dict]:
        with self._lock:
            return list(self._history)

    def record_history(self, entry: dict) -> None:
        with self._lock:
            self._history.append(entry)
            del self._history[:-100]


def _port_is_listening(host: str, port: int) -> bool:
    """探测指定端口上是否已经有服务在监听。

    Windows 上 bind() 配合 SO_REUSEADDR 时，即使该端口已被别的进程监听
    也可能绑定成功，导致「找空闲端口」误判、请求被发到旧服务上。
    因此这里改用「主动连接探测」来判断端口是否真的空闲。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def find_free_port(preferred: int = 8765, host: str = "127.0.0.1") -> int:
    """优先使用 preferred 端口，被占用则自动向后寻找。"""
    for offset in range(0, 50):
        candidate = preferred + offset
        if _port_is_listening(host, candidate):
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((host, candidate))
            except OSError:
                continue
            return candidate
    raise RuntimeError("找不到可用的本地端口")


class WebServer:
    """在后台线程中运行的 HTTP 服务。"""

    def __init__(self, app: WebApp, host: str = "127.0.0.1", port: int = 8765) -> None:
        self.app = app
        self.host = host
        self.port = port
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/"

    def start(self) -> None:
        handler = type("BoundHandler", (_Handler,), {"app": self.app})
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._server.daemon_threads = True
        # 端口传 0 时由系统分配，这里回填真实端口，供调用方访问
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="web-server", daemon=True)
        self._thread.start()
        logger.info("看板已启动: %s", self.url)

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # pragma: no cover
                pass
