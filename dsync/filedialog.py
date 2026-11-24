"""Native Windows "pick a folder / pick a file" dialogs.

Built on the COM ``IFileDialog`` API (Windows Vista+), which is much closer to the
modern Explorer experience than the old ``SHBrowseForFolder`` tree, and supports
setting the initial directory.

Only two functions are exposed:

* :func:`pick_folder` -- pick a folder
* :func:`pick_file`   -- pick a single file

Both return ``None`` when the user cancels, and raise :class:`PickerUnavailable`
on non-Windows platforms, where the caller should fall back to typing the path.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
import uuid
from ctypes import wintypes
from pathlib import Path

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"


class PickerUnavailable(RuntimeError):
    """The native picker cannot be shown in this environment."""


# ----------------------------------------------------------------------
# COM constants
# ----------------------------------------------------------------------
CLSCTX_INPROC_SERVER = 0x1
COINIT_APARTMENTTHREADED = 0x2

FOS_OVERWRITEPROMPT = 0x00000002
FOS_STRICTFILETYPES = 0x00000004
FOS_NOCHANGEDIR = 0x00000008
FOS_PICKFOLDERS = 0x00000020
FOS_FORCEFILESYSTEM = 0x00000040
FOS_ALLNONSTORAGEITEMS = 0x00000080
FOS_NOVALIDATION = 0x00000100
FOS_ALLOWMULTISELECT = 0x00000200
FOS_PATHMUSTEXIST = 0x00000800
FOS_FILEMUSTEXIST = 0x00001000
FOS_CREATEPROMPT = 0x00002000

SIGDN_FILESYSPATH = 0x80058000
SIGDN_NORMALDISPLAY = 0x00000000

HRESULT_OK = 0
HRESULT_CANCELLED = -2147023673  # 0x800704C7 ERROR_CANCELLED
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106 COM was already initialised with another model

_CLSID_FILE_OPEN_DIALOG = "DC1C5A9B-E848-4B3A-A1B5-4ED1FD32689C"
_IID_FILE_OPEN_DIALOG = "D57C7288-D4AD-4768-BE02-9D969532D960"
_IID_SHELL_ITEM = "43826D1E-E718-42EE-BC55-A1E261C37BFE"


class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", ctypes.c_ulong),
        ("Data2", ctypes.c_ushort),
        ("Data3", ctypes.c_ushort),
        ("Data4", ctypes.c_ubyte * 8),
    ]

    @classmethod
    def from_string(cls, text: str) -> "_GUID":
        raw = uuid.UUID(text).bytes_le
        return cls.from_buffer_copy(raw)


# ----------------------------------------------------------------------
# vtable call helper
# ----------------------------------------------------------------------
def _vfunc(interface: int, index: int, restype, *argtypes):
    """Fetch method number ``index`` from a COM interface vtable."""
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(table[index])


def _com_available() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        ctypes.windll.ole32  # noqa: B018
    except Exception:  # pragma: no cover - only triggered on unusual systems
        return False
    return True


def _file_dialog_class_available() -> bool:
    """Check whether ``CLSID_FileOpenDialog`` is registered.

    Some trimmed-down Windows builds and container images do not register this COM
    class, in which case the code falls back to :func:`_run_dialog_legacy` (plain
    shell32 API, no COM class required).
    """
    if not _com_available():
        return False
    try:
        ole32 = ctypes.windll.ole32
        ole32.CoCreateInstance.restype = ctypes.c_long
        clsid = _GUID.from_string(_CLSID_FILE_OPEN_DIALOG)
        iid = _GUID.from_string(_IID_FILE_OPEN_DIALOG)
        probe = ctypes.c_void_p()
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(iid), ctypes.byref(probe),
        )
        if hr == HRESULT_OK and probe:
            _release(probe.value)
            return True
        return False
    except Exception:  # pragma: no cover
        return False


def _init_com() -> bool:
    """Initialise COM for the current thread; returns whether this call did it."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr == HRESULT_OK:
        return True
    if hr == RPC_E_CHANGED_MODE:
        # The thread was already initialised with another model (e.g. MTA); still usable
        return False
    if hr == HRESULT_CANCELLED:
        raise OSError("CoInitializeEx failed")
    return False


def _release(interface: int) -> None:
    try:
        _vfunc(interface, 2, ctypes.c_ulong)()  # IUnknown::Release
    except Exception:  # pragma: no cover
        logger.debug("Failed to release the COM object", exc_info=True)


def _shell_item_for_path(path: str) -> int:
    """Convert a path into an IShellItem; returns 0 on failure."""
    shell32 = ctypes.windll.shell32
    iid = _GUID.from_string(_IID_SHELL_ITEM)
    item = ctypes.c_void_p()
    hr = shell32.SHCreateItemFromParsingName(
        ctypes.c_wchar_p(path), None, ctypes.byref(iid), ctypes.byref(item)
    )
    if hr != HRESULT_OK or not item:
        return 0
    return item.value


def _item_display_path(item: int) -> str:
    """Get the filesystem path out of an IShellItem."""
    ole32 = ctypes.windll.ole32
    fn = _vfunc(item, 5, ctypes.c_int, ctypes.c_uint, ctypes.POINTER(ctypes.c_wchar_p))
    buffer = ctypes.c_wchar_p()
    hr = fn(item, SIGDN_FILESYSPATH, ctypes.byref(buffer))
    if hr != HRESULT_OK or not buffer:
        return ""
    try:
        return buffer.value or ""
    finally:
        try:
            ole32.CoTaskMemFree(buffer)
        except Exception:  # pragma: no cover
            pass


def _run_dialog(initial_dir: str, folders_only: bool, file_filter: str | None,
                title: str = "") -> str | None:
    """The core dialog call: returns the chosen path, or None when cancelled."""
    if not _com_available():
        raise PickerUnavailable("This system does not support the native picker")

    ole32 = ctypes.windll.ole32
    shell32 = ctypes.windll.shell32

    owned_init = _init_com()
    dialog = ctypes.c_void_p()
    try:
        clsid = _GUID.from_string(_CLSID_FILE_OPEN_DIALOG)
        iid = _GUID.from_string(_IID_FILE_OPEN_DIALOG)
        hr = ole32.CoCreateInstance(
            ctypes.byref(clsid), None, CLSCTX_INPROC_SERVER,
            ctypes.byref(iid), ctypes.byref(dialog),
        )
        if hr != HRESULT_OK or not dialog:
            raise OSError(f"Failed to create the picker (HRESULT=0x{hr & 0xFFFFFFFF:08X})")

        options = (FOS_FORCEFILESYSTEM | FOS_NOCHANGEDIR
                   | FOS_NOVALIDATION | FOS_ALLNONSTORAGEITEMS)
        if folders_only:
            options |= FOS_PICKFOLDERS
        else:
            options |= FOS_FILEMUSTEXIST

        # IFileDialog vtable:
        #   3=Show 9=SetOptions 12=SetFolder 14=SetFileName 17=SetTitle 20=GetResult
        _vfunc(dialog.value, 9, ctypes.c_int, ctypes.c_uint)(
            dialog.value, options)

        if title:
            try:
                _vfunc(dialog.value, 17, ctypes.c_int, ctypes.c_wchar_p)(
                    dialog.value, title)
            except Exception:  # pragma: no cover - a failed title must not break the flow
                logger.debug("Failed to set the dialog title", exc_info=True)

        # set the initial directory
        start = initial_dir or ""
        if start and not os.path.isdir(start):
            start = os.path.dirname(start) or ""
        if start and os.path.isdir(start):
            item = _shell_item_for_path(start)
            if item:
                try:
                    _vfunc(dialog.value, 12, ctypes.c_int, ctypes.c_void_p)(
                        dialog.value, item)
                finally:
                    _release(item)

        # Show
        hr = _vfunc(dialog.value, 3, ctypes.c_int, wintypes.HWND)(
            dialog.value, None)
        if hr == HRESULT_CANCELLED:
            return None
        if hr != HRESULT_OK:
            raise OSError(f"The picker exited unexpectedly (HRESULT=0x{hr & 0xFFFFFFFF:08X})")

        # GetResult -> IShellItem
        result = ctypes.c_void_p()
        hr = _vfunc(dialog.value, 20, ctypes.c_int, ctypes.POINTER(ctypes.c_void_p))(
            dialog.value, ctypes.byref(result))
        if hr != HRESULT_OK or not result:
            return None
        try:
            return _item_display_path(result.value)
        finally:
            _release(result.value)
    finally:
        if dialog.value:
            _release(dialog.value)
        if owned_init:
            try:
                ole32.CoUninitialize()
            except Exception:  # pragma: no cover
                pass


# ----------------------------------------------------------------------
# Fallback: SHBrowseForFolder
# ----------------------------------------------------------------------
# These are plain shell32 exports with no COM class registration requirement, so
# they still work on trimmed-down systems where CLSID_FileOpenDialog is missing.
BFFM_INITIALIZED = 1
BFFM_SETSELECTIONW = 11

BROWSEINFOF_NONE = 0x00
BROWSEINFOF_NEWDIALOGSTYLE = 0x40

# SHBrowseForFolder callback prototype: int CALLBACK (HWND, UINT, LPARAM, LPARAM)
# Note that HWND and LPARAM are both 64-bit on 64-bit Windows and must be declared
# as c_ssize_t / c_void_p; otherwise ctypes passes them as 32-bit ints and the
# pointers the callback receives are truncated.
_BFFMProc = ctypes.WINFUNCTYPE(
    ctypes.c_int,          # return value: int
    ctypes.c_void_p,       # HWND hwnd
    ctypes.c_uint,         # UINT uMsg
    ctypes.c_ssize_t,      # LPARAM lParam
    ctypes.c_ssize_t,      # LPARAM lpData
)


class BROWSEINFO(ctypes.Structure):
    """The ``SHBrowseForFolder`` parameter structure."""

    _fields_ = [
        ("hwndOwner", wintypes.HWND),
        ("pidlRoot", ctypes.c_void_p),
        ("pszDisplayName", ctypes.c_wchar_p),
        ("lpszTitle", ctypes.c_wchar_p),
        ("ulFlags", wintypes.UINT),
        ("lpfn", ctypes.c_void_p),
        ("lParam", ctypes.c_long),
        ("iImage", ctypes.c_int),
    ]


def _run_dialog_legacy(initial_dir: str, title: str) -> str | None:
    """Open the "browse for folder" dialog via ``SHBrowseForFolder``.

    This is a plain shell32 export call with no COM class registration requirement,
    so it works on systems without ``CLSID_FileOpenDialog``.
    """
    # Use WinDLL rather than windll: windll caches function objects globally, so
    # another module setting different argtypes for the same name would interfere.
    shell32 = ctypes.WinDLL("shell32")
    user32 = ctypes.WinDLL("user32")

    # Declare the prototypes explicitly:
    #  * SendMessageW's lParam is a pointer; without a declaration it is passed as int and truncated
    #  * the PIDL returned by SHBrowseForFolderW is a pointer, so restype must be
    #    c_void_p; otherwise it is truncated to int on 64-bit and the following
    #    SHGetPathFromIDListW reads out of bounds and crashes
    user32.SendMessageW.restype = ctypes.c_long
    user32.SendMessageW.argtypes = [
        ctypes.c_void_p, ctypes.c_uint, ctypes.c_long, ctypes.c_void_p,
    ]
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [
        ctypes.POINTER(BROWSEINFO), ctypes.c_void_p, ctypes.c_uint,
        ctypes.c_wchar_p, ctypes.c_void_p, ctypes.c_long,
    ]
    shell32.SHGetPathFromIDListW.restype = ctypes.c_int
    shell32.SHGetPathFromIDListW.argtypes = [
        ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int,
    ]

    buffer = ctypes.create_unicode_buffer(260)
    info = BROWSEINFO()
    info.hwndOwner = None
    info.pidlRoot = None
    info.pszDisplayName = ctypes.cast(buffer, ctypes.c_wchar_p)
    info.lpszTitle = title or "Select a folder"
    info.ulFlags = BROWSEINFOF_NEWDIALOGSTYLE | BROWSEINFOF_NONE

    start = initial_dir or ""
    if start and not os.path.isdir(start):
        start = os.path.dirname(start) or ""

    def _callback(hwnd, msg, param, data):
        try:
            if msg == BFFM_INITIALIZED and start:
                # point the dialog at the desired initial directory
                user32.SendMessageW(
                    hwnd, BFFM_SETSELECTIONW, 0,
                    ctypes.cast(ctypes.c_wchar_p(start), ctypes.c_void_p),
                )
        except Exception:  # pragma: no cover
            logger.debug("Failed to set the initial directory", exc_info=True)
        return 0

    proc = _BFFMProc(_callback)
    info.lpfn = ctypes.cast(proc, ctypes.c_void_p)

    pidl = shell32.SHBrowseForFolderW(
        ctypes.pointer(info), None, 0, info.lpszTitle, None, 0)
    if not pidl:
        # The user cancelled, or the dialog could not open
        return None

    try:
        path_buffer = ctypes.create_unicode_buffer(260)
        ok = shell32.SHGetPathFromIDListW(pidl, path_buffer, 260)
        if ok and path_buffer.value:
            logger.info("User selected folder: %s", path_buffer.value)
            return path_buffer.value
        return None
    finally:
        try:
            ole32 = ctypes.windll.ole32
            ole32.CoTaskMemFree.restype = ctypes.c_long
            ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]
            ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
        except Exception:  # pragma: no cover
            pass


class OPENFILENAME(ctypes.Structure):
    """The ``GetOpenFileName`` parameter structure."""

    _fields_ = [
        ("lStructSize", wintypes.DWORD),
        ("hwndOwner", wintypes.HWND),
        ("hInstance", wintypes.HINSTANCE),
        ("lpstrFilter", ctypes.c_wchar_p),
        ("lpstrCustomFilter", ctypes.c_wchar_p),
        ("nMaxCustFilter", wintypes.DWORD),
        ("nFilterIndex", wintypes.DWORD),
        ("lpstrFile", ctypes.c_wchar_p),
        ("nMaxFile", wintypes.DWORD),
        ("lpstrFileTitle", ctypes.c_wchar_p),
        ("nMaxFileTitle", wintypes.DWORD),
        ("lpstrInitialDir", ctypes.c_wchar_p),
        ("lpstrTitle", ctypes.c_wchar_p),
        ("Flags", wintypes.DWORD),
        ("nFileOffset", wintypes.WORD),
        ("nFileExtension", wintypes.WORD),
        ("lpstrDefExt", ctypes.c_wchar_p),
        ("lCustData", ctypes.c_long),
        ("lpfnHook", ctypes.c_void_p),
        ("lpTemplateName", ctypes.c_wchar_p),
        ("pvReserved", ctypes.c_void_p),
        ("dwReserved", wintypes.DWORD),
        ("FlagsEx", wintypes.DWORD),
    ]


def _run_file_dialog_legacy(initial_dir: str, file_filter: str,
                             title: str) -> str | None:
    """Open the "select file" dialog via ``GetOpenFileName``."""
    comdlg32 = ctypes.windll.comdlg32

    buffer = ctypes.create_unicode_buffer(1024)
    info = OPENFILENAME()
    info.lStructSize = ctypes.sizeof(OPENFILENAME)
    info.hwndOwner = None
    info.lpstrFilter = file_filter or "All files (*.*)|*.*"
    info.nFilterIndex = 1
    info.lpstrFile = ctypes.cast(buffer, ctypes.c_wchar_p)
    info.nMaxFile = 1024
    info.lpstrTitle = title or "Select a file"

    start = initial_dir or ""
    if start and not os.path.isdir(start):
        start = os.path.dirname(start) or ""
    if start:
        info.lpstrInitialDir = start

    ok = comdlg32.GetOpenFileNameW(ctypes.byref(info))
    if not ok:
        return None
    return buffer.value or None


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------
def pick_folder(initial_dir: str | Path | None = None, title: str = "") -> str | None:
    """Show the "select folder" dialog and return the chosen directory, or ``None``
    when the user cancels."""
    start = str(initial_dir) if initial_dir else ""
    if start and not os.path.isdir(start):
        start = os.path.dirname(start) or ""

    if _file_dialog_class_available():
        result = _run_dialog(start, folders_only=True, file_filter=None, title=title)
    else:
        logger.info("CLSID_FileOpenDialog is not registered; falling back to SHBrowseForFolder")
        result = _run_dialog_legacy(start, title or "Select a folder")

    if result:
        logger.info("User selected folder: %s", result)
    return result or None


def pick_file(initial_dir: str | Path | None = None,
              file_filter: str = "All files (*.*)|*.*",
              title: str = "") -> str | None:
    """Show the "select file" dialog and return the chosen file, or ``None`` when the
    user cancels."""
    start = str(initial_dir) if initial_dir else ""
    if start and not os.path.isdir(start):
        start = os.path.dirname(start) or ""

    if _file_dialog_class_available():
        result = _run_dialog(start, folders_only=False, file_filter=file_filter,
                             title=title)
    else:
        logger.info("CLSID_FileOpenDialog is not registered; falling back to GetOpenFileName")
        result = _run_file_dialog_legacy(start, file_filter, title or "Select a file")

    if result:
        logger.info("User selected file: %s", result)
    return result or None


def pick_path_for_rule(kind: str, card_root: str | Path | None,
                       current: str = "") -> dict:
    """Pick a path for a rule editor's path input.

    ``kind`` is one of:

    * ``source`` -- browse inside the SD card and convert the choice into a path
      relative to the card root
    * ``destination`` -- browse any local directory and return an absolute path

    Returns a dict: ``{"path": ..., "absolute": ..., "cancelled": bool}``.
    """
    card = Path(card_root) if card_root else None
    current_text = (current or "").strip()

    if kind == "source":
        # Source path inside the SD card: start from the currently entered relative path
        start = str(card) if card else ""
        if card and current_text:
            candidate = card / current_text
            if candidate.is_dir():
                start = str(candidate)
        chosen = pick_folder(start, title="Select a source folder on the SD card")
        if not chosen:
            return {"cancelled": True, "path": "", "absolute": ""}
        if card:
            try:
                relative = os.path.relpath(chosen, str(card)).replace("\\", "/")
            except ValueError:
                relative = ""
            if relative == ".":
                relative = ""
            if relative.startswith(".."):
                raise ValueError("The source path must be inside the SD card")
            return {
                "cancelled": False,
                "path": relative,
                "absolute": chosen,
            }
        return {"cancelled": False, "path": chosen, "absolute": chosen}

    # destination
    start = current_text
    if start and not os.path.isdir(start):
        start = os.path.dirname(start) or ""
    chosen = pick_folder(start, title="Select the destination folder")
    if not chosen:
        return {"cancelled": True, "path": "", "absolute": ""}
    return {"cancelled": False, "path": chosen, "absolute": chosen}
