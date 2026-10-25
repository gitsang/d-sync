"""D-Sync 可执行入口。

PyInstaller 以本文件作为打包入口。此处必须使用绝对导入
（``python -m dsync`` 走包内相对导入，本文件则是独立脚本）。
"""

from __future__ import annotations

import multiprocessing
import sys
from pathlib import Path

# 允许直接运行本文件
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dsync.cli import main  # noqa: E402

if __name__ == "__main__":
    # 打包为窗口程序时，允许多进程重新启动
    multiprocessing.freeze_support()
    raise SystemExit(main())
