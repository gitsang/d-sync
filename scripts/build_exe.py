"""PyInstaller 打包脚本：生成无控制台窗口的 Windows 单文件 EXE。

用法::

    python scripts/build_exe.py
    python scripts/build_exe.py --clean
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD_DIR = ROOT / "build"
DIST_DIR = ROOT / "dist"
NAME = "D-Sync"


def main() -> int:
    parser = argparse.ArgumentParser(description="打包 D-Sync 为 Windows EXE")
    parser.add_argument("--clean", action="store_true", help="打包前清理 build/dist 目录")
    parser.add_argument("--onedir", action="store_true",
                        help="生成目录版而非单文件（启动更快，便于排查问题）")
    args = parser.parse_args()

    if args.clean:
        for path in (BUILD_DIR, DIST_DIR):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        print("已清理 build/ 与 dist/")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", NAME,
        "--onedir" if args.onedir else "--onefile",
        "--windowed",          # 无控制台窗口
        "--hidden-import", "yaml",
        "--hidden-import", "pystray",
        "--hidden-import", "pystray._win32",
        "--collect-submodules", "dsync",
        "--add-data", f"{ROOT / 'dsync' / 'web' / 'static'}{os.pathsep}dsync/web/static",
        "--icon", str(ROOT / "assets" / "dsync.ico"),
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
        str(ROOT / "run_dsync.py"),
    ]

    print("执行:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("打包失败")
        return result.returncode

    exe = DIST_DIR / (f"{NAME}.exe" if not args.onedir else f"{NAME}/{NAME}.exe")
    if exe.exists():
        size_mb = exe.stat().st_size / 1024 / 1024
        print(f"\n打包完成: {exe}  ({size_mb:.1f} MB)")
    else:
        print("\n打包结束，请检查 dist/ 目录")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
