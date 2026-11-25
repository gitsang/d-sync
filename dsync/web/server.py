"""Built-in local web service: serves the dashboard page plus a REST API and live
SSE push.

Binds 127.0.0.1 only and is never exposed externally. Uses the standard-library
http.server to avoid extra dependencies.
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

    # injected by create_server
    app: "WebApp" = None  # type: ignore[assignment]

    # ------------------------------------------------------------------
    def log_message(self, fmt: str, *args) -> None:  # silence the default access log
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
            self._json({"error": f"Unknown action: {action}"}, HTTPStatus.NOT_FOUND)
            return
        try:
            result = handler(payload)
        except Exception as exc:
            logger.exception("Failed to handle action %s", action)
            self._json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return
        self._json({"ok": True, "result": result})

    # ------------------------------------------------------------------
    def _serve_static(self, relative: str) -> None:
        safe = (STATIC_DIR / relative).resolve()
        try:
            safe.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self._json({"error": "Invalid path"}, HTTPStatus.FORBIDDEN)
            return
        if not safe.is_file():
            self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type, _ = mimetypes.guess_type(str(safe))
        if content_type and content_type.startswith("text/"):
            content_type += "; charset=utf-8"
        self._send(HTTPStatus.OK, safe.read_bytes(), content_type or "application/octet-stream")

    def _serve_sse(self) -> None:
        """SSE long-lived connection that keeps pushing events."""
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        subscriber = self.app.bus.subscribe()
        try:
            # first frame: push the current state snapshot to the client
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
                    # heartbeat so proxies do not drop the connection
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.time()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.bus.unsubscribe(subscriber)


class WebApp:
    """Dashboard backend: aggregates state snapshots and exposes the action API."""

    def __init__(self, bus: EventBus, state_provider: Callable[[], dict],
                 actions: dict[str, Callable[[dict], Any]], version: str = "1.1.0") -> None:
        self.bus = bus
        self.state_provider = state_provider
        self.actions = actions
        self.version = version
        self._history: list[dict] = []
        self._lock = threading.Lock()

    def snapshot(self) -> dict:
        state = dict(self.state_provider())
        with self._lock:
            # If the application layer provides a history, prefer it; otherwise use
            # the WebApp's own records
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
    """Probe whether something is already listening on the given port.

    On Windows, bind() combined with SO_REUSEADDR can succeed even when another
    process already listens on that port, which makes "find a free port" pick a
    taken port and sends requests to the old service. So this probes by actively
    connecting instead.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def find_free_port(preferred: int = 8765, host: str = "127.0.0.1") -> int:
    """Prefer the requested port and fall back to scanning upwards when it is taken."""
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
    raise RuntimeError("No free local port could be found")


class WebServer:
    """HTTP service that runs on a background thread."""

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
        # Port 0 lets the OS allocate one; write the real port back for the caller
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        name="web-server", daemon=True)
        self._thread.start()
        logger.info("Dashboard started: %s", self.url)

    def stop(self) -> None:
        if self._server:
            try:
                self._server.shutdown()
                self._server.server_close()
            except Exception:  # pragma: no cover
                pass
