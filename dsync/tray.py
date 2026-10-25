"""Windows 系统托盘图标。

后台运行时，程序只以托盘图标形式存在：
  * 左键/双击 -> 打开看板页面
  * 右键菜单 -> 打开看板 / 立即扫描 / 取消同步 / 退出

若 pystray 不可用（例如无图形环境），自动降级为纯控制台模式。
"""

from __future__ import annotations

import logging
import threading
import webbrowser
from typing import Callable

logger = logging.getLogger(__name__)


def _make_icon_image():
    """用 Pillow 动态绘制托盘图标，避免依赖外部图片文件。"""
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
    """托盘图标封装。"""

    def __init__(
        self,
        on_open: Callable[[], None],
        on_rescan: Callable[[], None],
        on_cancel: Callable[[], None],
        on_quit: Callable[[], None],
        title: str = "D-Sync 同步服务",
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
        """启动托盘图标。返回 False 表示不可用（调用方应降级）。"""
        try:
            import pystray
            from pystray import Menu, MenuItem
        except ImportError:
            logger.info("未安装 pystray，跳过托盘图标")
            return False

        image = _make_icon_image()
        if image is None:
            logger.info("缺少 Pillow，跳过托盘图标")
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
            MenuItem("打开看板", _open, default=True),
            MenuItem("立即扫描设备", _rescan),
            MenuItem("取消当前同步", _cancel),
            Menu.SEPARATOR,
            MenuItem("退出", _quit),
        )

        try:
            self._icon = pystray.Icon("dsync", image, self.title, menu)
        except Exception as exc:  # pragma: no cover
            logger.warning("创建托盘图标失败: %s", exc)
            return False

        self._thread = threading.Thread(target=self._icon.run, name="tray-icon", daemon=True)
        self._thread.start()
        logger.info("托盘图标已启动")
        return True

    def notify(self, message: str, title: str | None = None) -> None:
        """弹出气泡通知（失败不影响主流程）。"""
        if not self._icon:
            return
        try:
            self._icon.notify(message, title or self.title)
        except Exception:  # pragma: no cover
            logger.debug("通知失败", exc_info=True)

    def stop(self) -> None:
        if self._icon:
            try:
                self._icon.stop()
            except Exception:  # pragma: no cover
                pass


def open_url(url: str) -> None:
    """在默认浏览器中打开地址。"""
    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover
        logger.exception("打开浏览器失败: %s", url)
