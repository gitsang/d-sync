"""Windows system tray icon.

While running in the background the program exists only as a tray icon:
  * left-click / double-click -> open the dashboard page
  * right-click menu -> open dashboard / rescan now / cancel sync / quit

When pystray is unavailable (e.g. no graphical environment) it degrades
gracefully to plain console mode.
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from typing import Callable

logger = logging.getLogger(__name__)


def _make_icon_image():
    """Draw the tray icon with Pillow so no external image file is needed."""
    try:
        from PIL import Image, ImageDraw
    except ImportError:  # pragma: no cover
        return None

    size = 64
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    draw.rounded_rectangle((6, 12, 58, 50), radius=8, fill=(47, 107, 255, 255))
    draw.rounded_rectangle((22, 22, 42, 32), radius=3, fill=(255, 255, 255, 255))
    draw.line((32, 34, 32, 44), fill=(255, 255, 255, 255), width=4)
    draw.line((26, 39, 32, 45, 38, 39), fill=(255, 255, 255, 255), width=4)
    return image


class TrayIcon:
    """Tray icon wrapper."""

    def __init__(
        self,
        on_open: Callable[[], None],
        on_rescan: Callable[[], None],
        on_cancel: Callable[[], None],
        on_quit: Callable[[], None],
        title: str = "D-Sync Sync Service",
    ) -> None:
        self.on_open = on_open
        self.on_rescan = on_rescan
        self.on_cancel = on_cancel
        self.on_quit = on_quit
        self.title = title
        self._icon = None
        self._thread: threading.Thread | None = None

    def available(self) -> bool:
        try:
            import pystray  # noqa: F401
            return True
        except ImportError:
            return False

    def start(self) -> bool:
        """Start the tray icon. Returning False means it is unavailable (the caller
        should degrade gracefully)."""
        try:
            import pystray
            from pystray import Menu, MenuItem
        except ImportError:
            logger.info("pystray is not installed; skipping the tray icon")
            return False

        image = _make_icon_image()
        if image is None:
            logger.info("Pillow is missing; skipping the tray icon")
            return False

        def _open(icon, item):
            self.on_open()

        def _rescan(icon, item):
            self.on_rescan()

        def _cancel(icon, item):
            self.on_cancel()

        def _quit(icon, item):
            self.on_quit()
            icon.stop()

        menu = Menu(
            MenuItem("Open dashboard", _open, default=True),
            MenuItem("Rescan devices now", _rescan),
            MenuItem("Cancel current sync", _cancel),
            Menu.SEPARATOR,
            MenuItem("Quit", _quit),
        )

        try:
            self._icon = pystray.Icon("dsync", image, self.title, menu)
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to create the tray icon: %s", exc)
            return False

        self._thread = threading.Thread(target=self._icon.run, name="tray-icon", daemon=True)
        self._thread.start()
        logger.info("Tray icon started")
        return True

    def notify(self, message: str, title: str | None = None) -> None:
        """Show a balloon notification (failure does not affect the main flow)."""
        if not self._icon:
            return
        try:
            self._icon.notify(message, title or self.title)
        except Exception:  # pragma: no cover
            logger.debug("Notification failed", exc_info=True)

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:  # pragma: no cover
                pass


def open_url(url: str) -> None:
    """Open the URL in the default browser."""
    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover
        logger.exception("Failed to open the browser: %s", url)
