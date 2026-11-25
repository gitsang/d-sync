"""Detect external devices (SD cards, USB sticks, card readers) and watch for
inserts and removals.

Two platform backends share one public API:

* **Windows** — drives are letters, enumerated with ``GetLogicalDrives`` and
  classified with ``GetDriveTypeW``. Instant notification comes from a hidden
  ``WM_DEVICECHANGE`` window.
* **POSIX (Linux, macOS)** — devices are mount points, read from
  ``/proc/mounts`` (Linux) or ``mount(8)`` (macOS) and classified through
  ``/sys/block``. Instant notification comes from ``pyudev`` when it is
  installed, otherwise polling alone is used.

Both backends fall back to polling, which also covers the events the kernel
does not deliver: resume from sleep, driver glitches, VM-mounted volumes.

A volume only counts as a sync candidate when the sync configuration file sits
in its root directory; that decision is made by the caller, not here.

Nothing in this module touches a Win32 symbol until a Windows-only code path
actually runs, so importing it on Linux or macOS is safe.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable

logger = logging.getLogger(__name__)

IS_WINDOWS = os.name == "nt"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")

# ----------------------------------------------------------------------
# Win32 constants
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
"""External device types (USB sticks / card readers). Optical and RAM disks are not included."""

# Filesystems that are never a real external volume on POSIX. Network shares are
# excluded too: syncing onto one is a legitimate use case, but treating it as a
# "card" would make the app rescan it forever.
PSEUDO_FILESYSTEMS = frozenset({
    "proc", "sysfs", "devtmpfs", "devpts", "tmpfs", "ramfs", "cgroup", "cgroup2",
    "pstore", "securityfs", "debugfs", "tracefs", "configfs", "fusectl", "bpf",
    "mqueue", "hugetlbfs", "autofs", "binfmt_misc", "efivarfs", "squashfs",
    "overlay", "aufs", "nsfs", "rpc_pipefs", "selinuxfs", "systemd-1",
    "fuse.gvfsd-fuse", "fuse.portal", "fuse.snapfuse",
})

NETWORK_FILESYSTEMS = frozenset({
    "nfs", "nfs4", "cifs", "smbfs", "smb3", "sshfs", "fuse.sshfs",
    "davfs", "davfs2", "ncpfs", "afs", "9p", "ceph", "glusterfs",
})


# ======================================================================
# Shared helpers
# ======================================================================
def device_key(path: Path | str) -> str:
    """Return the identity used to track a device across polls.

    Windows paths are case-insensitive, so they are folded and stripped of their
    trailing separator. POSIX paths are case-*sensitive*: folding them would make
    ``/media/card`` and ``/media/CARD`` collide, which are genuinely different
    volumes. ``resolve()`` instead guards against the same volume being reported
    under two spellings (a symlinked mount point, a trailing slash).
    """
    if IS_WINDOWS:
        return str(path).upper().rstrip("\\")
    try:
        return str(Path(path).resolve())
    except OSError:  # pragma: no cover - unresolvable path
        return str(path).rstrip("/")


# ======================================================================
# Windows backend
# ======================================================================
def _win_ctypes():
    """Import ctypes only on Windows. Raises ImportError/AttributeError elsewhere."""
    import ctypes

    ctypes.windll  # noqa: B018 - probes that Win32 is actually available
    return ctypes


def _win_enum_drives() -> list[str]:
    """Return every existing drive letter (formatted like 'E:\\')."""
    try:
        ctypes = _win_ctypes()
        bitmask = ctypes.windll.kernel32.GetLogicalDrives()
    except (ImportError, AttributeError, OSError):  # pragma: no cover - non-Windows
        return []
    drives: list[str] = []
    for index in range(26):
        if bitmask & (1 << index):
            drives.append(f"{chr(ord('A') + index)}:\\")
    return drives


def _win_drive_type(root: str) -> int:
    try:
        ctypes = _win_ctypes()
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
    except (ImportError, AttributeError, OSError, ValueError):  # pragma: no cover
        return 0


def _win_volume_label(path: Path | str) -> str:
    root = str(path)
    if not root.endswith("\\"):
        root += "\\"
    try:
        ctypes = _win_ctypes()
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
    except (ImportError, AttributeError, OSError, ValueError):  # pragma: no cover
        pass
    return root.rstrip("\\")


# ======================================================================
# POSIX backend
# ======================================================================
_MOUNT_ESCAPE = re.compile(r"\\([0-7]{3})")


def _unescape_mount_field(value: str) -> str:
    r"""Decode the octal escapes the kernel uses in /proc/mounts.

    Spaces, tabs, newlines and backslashes are written as ``\040``, ``\011``,
    ``\012`` and ``\134`` so that the file stays one-entry-per-line.
    """
    return _MOUNT_ESCAPE.sub(lambda m: chr(int(m.group(1), 8)), value)


def _read_proc_mounts() -> list[tuple[str, str, str]]:
    """Return (device, mountpoint, fstype) for every mounted filesystem."""
    entries: list[tuple[str, str, str]] = []
    try:
        with open("/proc/mounts", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                parts = line.split()
                if len(parts) < 3:
                    continue
                entries.append((
                    _unescape_mount_field(parts[0]),
                    _unescape_mount_field(parts[1]),
                    parts[2],
                ))
    except OSError:  # pragma: no cover - /proc unavailable
        return []
    return entries


def _read_macos_mounts() -> list[tuple[str, str, str]]:
    """Return (device, mountpoint, fstype) using mount(8) on macOS."""
    try:
        output = subprocess.run(
            ["mount"], capture_output=True, text=True, timeout=10, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return []
    entries: list[tuple[str, str, str]] = []
    # "/dev/disk2s1 on /Volumes/CARD (msdos, local, nodev, nosuid, noowners)"
    for line in output.splitlines():
        match = re.match(r"^(.+?) on (.+?) \(([^,)]+)", line)
        if match:
            entries.append((match.group(1), match.group(2), match.group(3)))
    return entries


def _posix_mounts() -> list[tuple[str, str, str]]:
    if IS_LINUX:
        return _read_proc_mounts()
    if IS_MACOS:
        return _read_macos_mounts()
    return []


def _sysfs_block_dir(device_path: str) -> Path | None:
    """Resolve the /sys/block entry backing a mounted device."""
    try:
        stat = os.stat(device_path)
        major, minor = os.major(stat.st_rdev), os.minor(stat.st_rdev)
    except (OSError, AttributeError):
        return None
    link = Path("/sys/dev/block") / f"{major}:{minor}"
    try:
        return link.resolve()
    except OSError:  # pragma: no cover
        return None


def _posix_flags(device_path: str, mountpoint: str) -> tuple[bool, bool]:
    """Return (removable, external-usb) for a mount.

    ``removable`` mirrors the kernel's ``/sys/block/<dev>/removable`` flag, or the
    presence of ``/sys/dev/block/<maj>:<min>/../../usb`` for the many card readers
    that advertise themselves as fixed disks. ``external`` is narrowed further:
    only USB/MMC-backed devices count, so an internal SATA disk never matches.
    """
    resolved = _sysfs_block_dir(device_path)
    if resolved is None:
        # No /sys entry: many FUSE mounts, bind mounts and network shares land here.
        # Fall back to the conventional /media, /mnt, /run/media and /Volumes roots.
        conventional = ("/media/", "/mnt/", "/run/media/", "/Volumes/")
        external = any(mountpoint.startswith(root) for root in conventional)
        return external, external

    removable = False
    external_usb = False
    # Walk up from the partition to the whole disk to find the transport.
    for ancestor in list(resolved.parents)[:4]:
        flag = ancestor / "removable"
        try:
            if flag.read_text(encoding="utf-8").strip() == "1":
                removable = True
        except OSError:
            pass
        if (ancestor / "usb").exists() or (ancestor / "device" / "usb").exists():
            external_usb = True
        if ancestor.name.startswith(("mmcblk", "sd", "vd", "nvme")):
            break

    # Card readers in USB enclosures report removable=0 on the *disk* but carry a
    # usb ancestor; treat either signal as removability.
    return (removable or external_usb), external_usb


def _posix_label(device_path: str, mountpoint: str) -> str:
    """Best-effort volume label, without shelling out to blkid (needs root)."""
    for base in ("/dev/disk/by-label", "/dev/disk/by-partlabel"):
        try:
            for link in Path(base).iterdir():
                try:
                    if os.path.realpath(link) == os.path.realpath(device_path):
                        return link.name.replace("\\x20", " ")
                except OSError:  # pragma: no cover
                    continue
        except OSError:
            continue
    return Path(mountpoint).name or mountpoint


def _posix_candidates() -> list[tuple[Path, bool, bool]]:
    """Return (mountpoint, removable, external) for every real mounted filesystem."""
    result: list[tuple[Path, bool, bool]] = []
    seen: set[str] = set()
    for device, mountpoint, fstype in _posix_mounts():
        if fstype in PSEUDO_FILESYSTEMS or fstype in NETWORK_FILESYSTEMS:
            continue
        if not device.startswith("/dev/"):
            # tmpfs, overlay, snap loop mounts and similar have no backing device.
            continue
        if mountpoint in seen:
            continue
        seen.add(mountpoint)
        try:
            if not os.path.isdir(mountpoint):
                continue
        except OSError:
            continue
        removable, external = _posix_flags(device, mountpoint)
        result.append((Path(mountpoint), removable, external))
    return result


# ======================================================================
# Public API (dispatch by platform)
# ======================================================================
def volume_label(path: Path | str) -> str:
    """Read the volume label (device name). Falls back to the path itself."""
    if IS_WINDOWS:
        return _win_volume_label(path)
    device_path = next(
        (dev for dev, mp, _ in _posix_mounts() if mp == str(path)),
        "",
    )
    return _posix_label(device_path, str(path)) if device_path else Path(path).name


def is_external(path: Path | str) -> bool:
    """Return whether this is an external device (USB stick / card reader).

    Fixed disks, optical drives, RAM disks and network shares do not count.
    """
    if IS_WINDOWS:
        root = str(path)
        if not root.endswith("\\"):
            root += "\\"
        return _win_drive_type(root) in EXTERNAL_DRIVE_TYPES
    target = str(Path(path).resolve())
    for mountpoint, _removable, external in _posix_candidates():
        if str(mountpoint) == target:
            return external
    return False


def is_removable(path: Path | str) -> bool:
    """Return whether the volume is removable, including optical and RAM disks."""
    if IS_WINDOWS:
        root = str(path)
        if not root.endswith("\\"):
            root += "\\"
        return _win_drive_type(root) in DRIVE_REMOVABLE_TYPES
    target = str(Path(path).resolve())
    for mountpoint, removable, _external in _posix_candidates():
        if str(mountpoint) == target:
            return removable
    return False


def list_external_drives() -> list[Path]:
    """List external devices (USB sticks, card readers, ...)."""
    if IS_WINDOWS:
        result: list[Path] = []
        for root in _win_enum_drives():
            if _win_drive_type(root) not in EXTERNAL_DRIVE_TYPES:
                continue
            try:
                if os.path.isdir(root):
                    result.append(Path(root))
            except OSError:
                continue
        return result
    return [mountpoint for mountpoint, _removable, external in _posix_candidates() if external]


def list_removable_drives(include_fixed: bool = False) -> list[Path]:
    """List removable drives (card readers, USB sticks, optical drives, ...).

    Some card readers mount the SD card as a "fixed disk"; pass
    ``include_fixed=True`` to include those as candidates too.
    """
    if IS_WINDOWS:
        result: list[Path] = []
        for root in _win_enum_drives():
            drive_type = _win_drive_type(root)
            if drive_type in DRIVE_REMOVABLE_TYPES or (include_fixed and drive_type == DRIVE_FIXED):
                try:
                    if os.path.isdir(root):
                        result.append(Path(root))
                except OSError:
                    continue
        return result
    return [
        mountpoint
        for mountpoint, removable, _external in _posix_candidates()
        if removable or include_fixed
    ]


def list_all_drives() -> list[Path]:
    """List every accessible volume."""
    if IS_WINDOWS:
        result: list[Path] = []
        for root in _win_enum_drives():
            try:
                if os.path.isdir(root):
                    result.append(Path(root))
            except OSError:
                continue
        return result
    return [mountpoint for mountpoint, _removable, _external in _posix_candidates()]


# ======================================================================
# Change notification
# ======================================================================
class DeviceChangeListener:
    """Notify the caller the moment a device appears or disappears.

    Windows registers a hidden window and listens for ``WM_DEVICECHANGE``.
    POSIX uses ``pyudev`` when available (instant), and silently degrades to
    polling when it is not (``pyudev`` is an optional dependency).
    """

    def __init__(self, on_change: Callable[[], None]) -> None:
        self._on_change = on_change
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._wndproc = None
        self._hwnd = None
        self._ready = threading.Event()
        self._stop = threading.Event()

    # ------------------------------------------------------------------
    def start(self) -> None:
        if IS_WINDOWS:
            self._thread = threading.Thread(target=self._run_windows,
                                            name="devchange-listener", daemon=True)
            self._thread.start()
            self._ready.wait(timeout=3.0)
            return
        if not IS_LINUX:
            logger.info("No instant device notification on this platform; polling only")
            return
        try:
            import pyudev  # noqa: F401
        except ImportError:
            logger.info("pyudev is not installed; polling only (sudo apt install python3-pyudev)")
            return
        self._thread = threading.Thread(target=self._run_udev,
                                        name="udev-listener", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is None:
            return
        if IS_WINDOWS and self._hwnd:
            try:
                ctypes = _win_ctypes()
                ctypes.windll.user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
            except Exception:  # pragma: no cover
                pass
        self._thread.join(timeout=3.0)

    # ------------------------------------------------------------------
    def _notify(self) -> None:
        try:
            self._on_change()
        except Exception:  # pragma: no cover
            logger.exception("Device change callback failed")

    def _run_udev(self) -> None:
        try:
            import pyudev
        except ImportError:  # pragma: no cover - checked in start()
            return
        try:
            context = pyudev.Context()
            monitor = pyudev.Monitor.from_netlink(context)
            monitor.filter_by(subsystem="block")
        except Exception as exc:  # pragma: no cover - no netlink permission
            logger.info("Could not open a udev monitor (%s); polling only", exc)
            return
        logger.info("Listening for udev block events")
        while not self._stop.is_set():
            device = monitor.poll(timeout=1.0)
            if device is None:
                continue
            if device.action in ("add", "remove", "change"):
                self._notify()

    def _run_windows(self) -> None:
        ctypes = _win_ctypes()
        from ctypes import wintypes

        wndproc_type = ctypes.WINFUNCTYPE(
            ctypes.c_long,
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        )

        class _WndClass(ctypes.Structure):
            _fields_ = [
                ("style", wintypes.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE),
                ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE),
                ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR),
            ]

        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32

        # Declare prototypes explicitly so pointers are not truncated on 64-bit builds
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
                    self._notify()
            return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

        self._wndproc = wndproc_type(_wndproc)

        hinstance = kernel32.GetModuleHandleW(None)
        class_name = "DSyncDeviceListener"
        wndclass = _WndClass()
        wndclass.lpfnWndProc = self._wndproc
        wndclass.hInstance = hinstance
        wndclass.lpszClassName = class_name

        if not user32.RegisterClassW(ctypes.byref(wndclass)):
            logger.debug("RegisterClassW failed: %s", ctypes.get_last_error())
            self._ready.set()
            return

        hwnd = user32.CreateWindowExW(
            0, class_name, "D-Sync Device Listener", 0,
            0, 0, 0, 0, wintypes.HWND(0), wintypes.HMENU(0),
            hinstance, None,
        )
        if not hwnd:
            logger.debug("CreateWindowExW failed: %s", ctypes.get_last_error())
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


# ======================================================================
# Monitor
# ======================================================================
class DeviceMonitor:
    """Combine the event listener and polling into insert/remove events."""

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
        self._ignore = {device_key(p) for p in ignore}
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
        logger.info("Detected drives: %s", ", ".join(sorted(self._known)) or "none")
        self._thread = threading.Thread(target=self._loop, name="device-monitor", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._listener.stop()
        if self._thread:
            self._thread.join(timeout=5.0)

    def _trigger_scan(self) -> None:
        """Event driven: scan immediately (without blocking the event thread for long)."""
        try:
            self._scan()
        except Exception:  # pragma: no cover
            logger.exception("Event-triggered scan failed")

    def _loop(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self._poll_interval)
            if self._stop.is_set():
                break
            try:
                self._scan()
            except Exception:  # pragma: no cover
                logger.exception("Polling scan failed")

    # ------------------------------------------------------------------
    def _current_drives(self) -> dict[str, Path]:
        drives = list_removable_drives(include_fixed=self._include_fixed)
        result: dict[str, Path] = {}
        for drive in drives:
            key = device_key(drive)
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
                logger.info("Device inserted: %s", path)
                try:
                    self._on_inserted(path)
                except Exception:  # pragma: no cover
                    logger.exception("Failed to handle device insertion")

        for key, path in previous.items():
            if key not in current:
                logger.info("Device removed: %s", path)
                try:
                    self._on_removed(path)
                except Exception:  # pragma: no cover
                    logger.exception("Failed to handle device removal")
