"""Windows 原生「选择文件夹 / 选择文件」对话框。

基于 COM 的 ``IFileDialog``（Windows Vista+），比 ``SHBrowseForFolder``
的旧式树形窗口更接近现代资源管理器体验，并且支持指定初始目录。

对外只暴露两个函数：

* :func:`pick_folder` —— 选择文件夹
* :func:`pick_file`   —— 选择单个文件

两者在用户取消时返回 ``None``；在非 Windows 平台直接抛
:class:`PickerUnavailable`，调用方应回退到手工输入路径。
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
    """当前环境无法弹出原生选择窗口。"""


# ----------------------------------------------------------------------
# COM 常量
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
RPC_E_CHANGED_MODE = -2147417850  # 0x80010106 COM 已经以其它模型初始化

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
# vtable 调用helper
# ----------------------------------------------------------------------
def _vfunc(interface: int, index: int, restype, *argtypes):
    """取 COM 接口 vtable 上的第 index 个方法。"""
    table = ctypes.cast(interface, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return prototype(table[index])


def _com_available() -> bool:
    if not IS_WINDOWS:
        return False
    try:
        ctypes.windll.ole32  # noqa: B018
    except Exception:  # pragma: no cover - 仅在异常系统上触发
        return False
    return True


def _file_dialog_class_available() -> bool:
    """检查 ``CLSID_FileOpenDialog`` 是否已注册。

    部分精简版 Windows / 容器环境没有注册这个 COM 类，
    此时退回 :func:`_run_dialog_legacy`（纯 shell32 API，无需 COM 类）。
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
    """初始化当前线程的 COM，返回是否由本次调用完成初始化。"""
    ole32 = ctypes.windll.ole32
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoInitializeEx.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
    hr = ole32.CoInitializeEx(None, COINIT_APARTMENTTHREADED)
    if hr == HRESULT_OK:
        return True
    if hr == RPC_E_CHANGED_MODE:
        # 线程已被其它模型初始化（如 MTA），仍可尝试调用
        return False
    if hr == HRESULT_CANCELLED:
        raise OSError("CoInitializeEx 失败")
    return False


def _release(interface: int) -> None:
    try:
        _vfunc(interface, 2, ctypes.c_ulong)()  # IUnknown::Release
    except Exception:  # pragma: no cover
        logger.debug("释放 COM 对象失败", exc_info=True)


def _shell_item_for_path(path: str) -> int:
    """把路径转成 IShellItem，失败返回 0。"""
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
    """从 IShellItem 取文件系统路径。"""
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
    """核心对话框调用，返回选中路径；取消返回 None。"""
    if not _com_available():
        raise PickerUnavailable("当前系统不支持原生选择窗口")

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
            raise OSError(f"创建选择窗口失败 (HRESULT=0x{hr & 0xFFFFFFFF:08X})")

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
            except Exception:  # pragma: no cover - 标题失败不影响主流程
                logger.debug("设置窗口标题失败", exc_info=True)

        # 设定初始目录
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
            raise OSError(f"选择窗口异常退出 (HRESULT=0x{hr & 0xFFFFFFFF:08X})")

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
# 回退方案：SHBrowseForFolder
# ----------------------------------------------------------------------
# 这是一组纯 shell32 导出函数，不依赖 COM 类注册，
# 在未注册 CLSID_FileOpenDialog 的精简系统上依然可用。
BFFM_INITIALIZED = 1
BFFM_SETSELECTIONW = 11

BROWSEINFOF_NONE = 0x00
BROWSEINFOF_NEWDIALOGSTYLE = 0x40

# SHBrowseForFolder 回调原型：int CALLBACK (HWND, UINT, LPARAM, LPARAM)
# 注意 HWND 与 LPARAM 在 64 位下都是 64 位，必须显式用 c_ssize_t / c_void_p，
# 否则 ctypes 默认按 32 位 int 传参，回调里取到的指针会被截断。
_BFFMProc = ctypes.WINFUNCTYPE(
    ctypes.c_int,          # 返回值 int
    ctypes.c_void_p,       # HWND hwnd
    ctypes.c_uint,         # UINT uMsg
    ctypes.c_ssize_t,      # LPARAM lParam
    ctypes.c_ssize_t,      # LPARAM lpData
)


class BROWSEINFO(ctypes.Structure):
    """``SHBrowseForFolder`` 的参数结构。"""

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
    """用 ``SHBrowseForFolder`` 打开「浏览文件夹」窗口。

    这是纯 shell32 导出函数调用，不依赖 COM 类注册，
    可在未安装 ``CLSID_FileOpenDialog`` 的系统上正常工作。
    """
    # 用 WinDLL 而非 windll：windll 的函数对象是全局共享缓存，
    # 其它模块若给同名函数设了不同 argtypes 会互相干扰。
    shell32 = ctypes.WinDLL("shell32")
    user32 = ctypes.WinDLL("user32")

    # 显式声明原型：
    #  * SendMessageW 的 lParam 是指针，不声明时按 int 传会截断
    #  * SHBrowseForFolderW 返回的 PIDL 是指针，restype 必须是 c_void_p，
    #    否则 64 位下指针被截断成 int，后续 SHGetPathFromIDListW 读越界崩溃
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
    info.lpszTitle = title or "选择文件夹"
    info.ulFlags = BROWSEINFOF_NEWDIALOGSTYLE | BROWSEINFOF_NONE

    start = initial_dir or ""
    if start and not os.path.isdir(start):
        start = os.path.dirname(start) or ""

    def _callback(hwnd, msg, param, data):
        try:
            if msg == BFFM_INITIALIZED and start:
                # 把对话框定位到期望的初始目录
                user32.SendMessageW(
                    hwnd, BFFM_SETSELECTIONW, 0,
                    ctypes.cast(ctypes.c_wchar_p(start), ctypes.c_void_p),
                )
        except Exception:  # pragma: no cover
            logger.debug("设置初始目录失败", exc_info=True)
        return 0

    proc = _BFFMProc(_callback)
    info.lpfn = ctypes.cast(proc, ctypes.c_void_p)

    pidl = shell32.SHBrowseForFolderW(
        ctypes.pointer(info), None, 0, info.lpszTitle, None, 0)
    if not pidl:
        # 用户取消，或窗口未能打开
        return None

    try:
        path_buffer = ctypes.create_unicode_buffer(260)
        ok = shell32.SHGetPathFromIDListW(pidl, path_buffer, 260)
        if ok and path_buffer.value:
            logger.info("用户选择目录: %s", path_buffer.value)
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
    """``GetOpenFileName`` 的参数结构。"""

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
    """用 ``GetOpenFileName`` 打开「选择文件」窗口。"""
    comdlg32 = ctypes.windll.comdlg32

    buffer = ctypes.create_unicode_buffer(1024)
    info = OPENFILENAME()
    info.lStructSize = ctypes.sizeof(OPENFILENAME)
    info.hwndOwner = None
    info.lpstrFilter = file_filter or "所有文件 (*.*)|*.*"
    info.nFilterIndex = 1
    info.lpstrFile = ctypes.cast(buffer, ctypes.c_wchar_p)
    info.nMaxFile = 1024
    info.lpstrTitle = title or "选择文件"

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
# 对外接口
# ----------------------------------------------------------------------
def pick_folder(initial_dir: str | Path | None = None, title: str = "") -> str | None:
    """弹出「选择文件夹」窗口，返回选中目录；用户取消返回 ``None``。"""
    start = str(initial_dir) if initial_dir else ""
    if start and not os.path.isdir(start):
        start = os.path.dirname(start) or ""

    if _file_dialog_class_available():
        result = _run_dialog(start, folders_only=True, file_filter=None, title=title)
    else:
        logger.info("CLSID_FileOpenDialog 未注册，改用 SHBrowseForFolder")
        result = _run_dialog_legacy(start, title or "选择文件夹")

    if result:
        logger.info("用户选择目录: %s", result)
    return result or None


def pick_file(initial_dir: str | Path | None = None,
              file_filter: str = "所有文件 (*.*)|*.*",
              title: str = "") -> str | None:
    """弹出「选择文件」窗口，返回选中文件；用户取消返回 ``None``。"""
    start = str(initial_dir) if initial_dir else ""
    if start and not os.path.isdir(start):
        start = os.path.dirname(start) or ""

    if _file_dialog_class_available():
        result = _run_dialog(start, folders_only=False, file_filter=file_filter,
                             title=title)
    else:
        logger.info("CLSID_FileOpenDialog 未注册，改用 GetOpenFileName")
        result = _run_file_dialog_legacy(start, file_filter, title or "选择文件")

    if result:
        logger.info("用户选择文件: %s", result)
    return result or None


def pick_path_for_rule(kind: str, card_root: str | Path | None,
                       current: str = "") -> dict:
    """为规则编辑器的路径输入框选择路径。

    ``kind`` 取值：

    * ``source`` —— 在 SD 卡内浏览，选中后换算成相对卡根目录的路径
    * ``destination`` —— 浏览本机任意目录，返回绝对路径

    返回字典：``{"path": ..., "absolute": ..., "cancelled": bool}``。
    """
    card = Path(card_root) if card_root else None
    current_text = (current or "").strip()

    if kind == "source":
        # 源路径在 SD 卡内：优先从当前填写的相对路径定位起始目录
        start = str(card) if card else ""
        if card and current_text:
            candidate = card / current_text
            if candidate.is_dir():
                start = str(candidate)
        chosen = pick_folder(start, title="选择 SD 卡内的源文件夹")
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
                raise ValueError("源路径必须位于 SD 卡内部")
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
    chosen = pick_folder(start, title="选择目标文件夹")
    if not chosen:
        return {"cancelled": True, "path": "", "absolute": ""}
    return {"cancelled": False, "path": chosen, "absolute": chosen}
