"""Windows SD 卡 / 可移动设备检测。

两条检测路径并行工作，互为冗余：

1. ``WM_DEVICECHANGE`` 消息监听 —— 插拔的瞬间立刻收到通知，响应最快。
   通过 ctypes 注册一个隐藏窗口，监听 ``DBT_DEVICEARRIVAL`` / 
   ``DBT_DEVICEREMOVECOMPLETE``，收到后立即触发一次重新扫描。

2. 轮询扫描 —— 每隔若干秒枚举一次逻辑盘。相比事件方式，它能兜住
   休眠唤醒、驱动异常、虚拟机挂载等事件丢失的情况。

只有「盘符下存在配置 YAML 文件」的盘才会被认定为有效的 SD 卡。
"""

from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

# ----------------------------------------------------------------------
# Win32 常量
# ----------------------------------------------------------------------
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

DBT_DEVICEARRIVAL = 0x8000
DBT_DEVICEREMOVECOMPLETE = 0x8004
DBT_DEVNODES_CHANGED = 0x0007
WM_DEVICECHANGE = 0x0219
WM_CLOSE = 0x0010

DRIVE_REMOVABLE_TYPES = (DRIVE_REMOVABLE, DRIVE_CDROM, DRIVE_RAMDISK)

EXTERNAL_DRIVE_TYPES = (DRIVE_REMOVABLE,)
"""外置设备类型（U 盘 / 读卡器）。光驱与内存盘不计入。"""


def _enum_drives() -> list[str]:
    """返回所有存在的盘符列表（形如 'E:\\'）。"""
    try:
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except (AttributeError, OSError):  # pragma: no cover - 非 Windows 环境
        return []
    drives: list[str] = []
    for index in range(26):
        if bitmask & (1 << index):
            drives.append(f"{chr(ord('A') + index)}:\\")
    return drives


def _drive_type(root: str) -> int:
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
    except (AttributeError, OSError, ValueError):  # pragma: no cover
        return 0


def volume_label(path: Path | str) -> str:
    """读取卷标（设备名）。读不到时回退为盘符。"""
    root = str(path)
    if not root.endswith("\\"):
        root += "\\"
    try:
        kernel32 = ctypes.windll.kernel32
        buf = ctypes.create_unicode_buffer(261)
        fs_name = ctypes.create_unicode_buffer(261)
        serial = ctypes.c_uint()
        max_comp = ctypes.c_uint()
        flags = ctypes.c_uint()
        kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root), buf, 260,
            ctypes.byref(serial), ctypes.byref(max_comp), ctypes.byref(flags),
            fs_name, 260,
        )
        label = buf.value.strip()
        if label:
            return label
    except (AttributeError, OSError, ValueError):  # pragma: no cover
        pass
    return root.rstrip("\\")


def is_external(path: Path | str) -> bool:
    """判断是否为外置设备（U 盘 / 读卡器）。固定磁盘、光驱、内存盘都不算。"""
    root = str(path)
    if not root.endswith("\\"):
        root += "\\"
    return _drive_type(root) in EXTERNAL_DRIVE_TYPES


def list_external_drives() -> list[Path]:
    """列出外置设备（U 盘、读卡器等）。"""
    result: list[Path] = []
    for root in _enum_drives():
        if _drive_type(root) not in EXTERNAL_DRIVE_TYPES:
            continue
        try:
            if os.path.isdir(root):
                result.append(Path(root))
        except OSError:
            continue
    return result


def list_removable_drives(include_fixed: bool = False) -> list[Path]:
    """列出可移动磁盘（读卡器、U 盘、光驱等）。

    部分读卡器会把 SD 卡挂载成"固定磁盘"，此时可通过
    ``include_fixed=True`` 一并纳入候选。
    """
    result: list[Path] = []
    for root in _enum_drives():
        drive_type = _drive_type(root)
        if drive_type in DRIVE_REMOVABLE_TYPES or (include_fixed and drive_type == DRIVE_FIXED):
            try:
                if os.path.isdir(root):
                    result.append(Path(root))
            except OSError:
                continue
    return result


def list_all_drives() -> list[Path]:
    """列出所有可访问的盘符。"""
    result: list[Path] = []
    for root in _enum_drives():
        try:
            if os.path.isdir(root):
                result.append(Path(root))
        except OSError:
            continue
    return result


def is_removable(path: Path) -> bool:
    """判断某个盘是否为可移动设备。"""
    root = str(path)
    if not root.endswith("\\"):
        root += "\\"
    return _drive_type(root) in DRIVE_REMOVABLE_TYPES


# ----------------------------------------------------------------------
# Win32 回调类型
# ----------------------------------------------------------------------
WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_long,
    wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
)


class WndClass(ctypes.Structure):
    """WNDCLASS 结构体。"""

    _fields_ = [
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
    ]


class DeviceChangeListener:
    """在独立线程中运行一个隐藏窗口，监听 WM_DEVICECHANGE。

    仅在 Windows 上有效；其它平台会安全降级（线程直接退出）。
    """

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._wndproc = None
        self._hwnd = None
        self._ready = threading.Event()
        self._stop = threading.Event()

    def start(self) -> None:
        if os.name != "nt":
            logger.info("非 Windows 平台，跳过 WM_DEVICECHANGE 监听")
            return
        self._thread = threading.Thread(target=self._run, name="devchange-listener", daemon=True)
        self._thread.start()
        self._ready.wait(timeout=3.0)

    def stop(self) -> None:
        self._stop.set()
        if os.name != "nt" or self._thread is None:
            return
        if self._hwnd:
            try:
                ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:  # pragma: no cover
                pass
        self._thread.join(timeout=3.0)

    # ------------------------------------------------------------------
    def _run(self) -> None:
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # 显式声明原型，避免 64 位下指针被截断
        user32.DefWindowProcW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.DefWindowProcW.restype = ctypes.c_long
        user32.PostMessageW.argtypes = [
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        ]
        user32.PostMessageW.restype = wintypes.BOOL
        user32.CreateWindowExW.restype = wintypes.HWND
        user32.GetMessageW.argtypes = [
            ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
        ]
        user32.GetMessageW.restype = ctypes.c_int

        def _wndproc(hwnd, msg, wparam, lparam):
            if msg == WM_DEVICECHANGE:
                if wparam in (DBT_DEVICEARRIVAL, DBT_DEVICEREMOVECOMPLETE, DBT_DEVNODES_CHANGED):
                    try:
                        self._on_change()
                    except Exception:  # pragma: no cover
                        logger.exception("设备变更回调失败")
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = WNDPROC(_wndproc)

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "DSyncDeviceListener"
        wndclass = WndClass()
        wndclass.lpfnWndProc = self._wndproc
        wndclass.hInstance = hinstance
        wndclass.lpszClassName = class_name

        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            logger.debug("RegisterClassW 失败: %s", ctypes.get_last_error())
            self._ready.set()
            return

        hwnd = user32.CreateWindowExW(
            0, class_name, "D-Sync Device Listener", 0,
            0, 0, 0, 0, wintypes.HWND(0), wintypes.HMENU(0),
            hinstance, None,
        )
        if not hwnd:
            logger.debug("CreateWindowExW 失败: %s", ctypes.get_last_error())
            self._ready.set()
            return

        self._hwnd = hwnd
        self._thread_id = kernel32.GetCurrentThreadId()
        self._ready.set()

        msg = wintypes.MSG()
        while not self._stop.is_set():
            result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if result in (0, -1):
                break
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

# ----------------------------------------------------------------------
# 监控器
# ----------------------------------------------------------------------
class DeviceMonitor:
    """组合事件监听与轮询，产出盘符的插入/移除事件。"""

    def __init__(
        self,
        on_inserted: Callable[[Path], None],
        on_removed: Callable[[Path], None],
        poll_interval: float = 3.0,
        ignore: Iterable[Path] = (),
        include_fixed: bool = False,
    ) -> None:
        self._on_inserted = on_inserted
        self._on_removed = on_removed
        self._poll_interval = max(0.5, poll_interval)
        self._ignore = {str(Path(p)).upper().rstrip("\\") for p in ignore}
        self._include_fixed = include_fixed
        self._known: dict[str, Path] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener = DeviceChangeListener(self._trigger_scan)

    # ------------------------------------------------------------------
    def start(self) -> None:
        self._listener.start()
        self._known = self._current_drives()
        logger.info("已识别当前磁盘: %s", ", ".join(sorted(self._known)) or "无")
        self._thread = threading.Thread(target=self._loop, name="device-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._listener.stop()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _trigger_scan(self) -> None:
        """事件驱动：立刻做一次扫描（不阻塞消息线程太久）。"""
        try:
            self._scan()
        except Exception:  # pragma: no cover
            logger.exception("事件触发扫描失败")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._poll_interval)
            if self._stop.is_set():
                break
            try:
                self._scan()
            except Exception:  # pragma: no cover
                logger.exception("轮询扫描失败")

    # ------------------------------------------------------------------
    def _current_drives(self) -> dict[str, Path]:
        drives = list_removable_drives(include_fixed=self._include_fixed)
        result: dict[str, Path] = {}
        for drive in drives:
            key = str(drive).upper().rstrip("\\")
            if key in self._ignore:
                continue
            result[key] = drive
        return result

    def _scan(self) -> None:
        current = self._current_drives()
        with self._lock:
            previous = dict(self._known)
            self._known = current

        for key, path in current.items():
            if key not in previous:
                logger.info("检测到设备插入: %s", path)
                try:
                    self._on_inserted(path)
                except Exception:  # pragma: no cover
                    logger.exception("处理设备插入失败")

        for key, path in previous.items():
            if key not in current:
                logger.info("检测到设备移除: %s", path)
                try:
                    self._on_removed(path)
                except Exception:  # pragma: no cover
                    logger.exception("处理设备移除失败")
