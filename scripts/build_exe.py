"""PyInstaller packaging script: build a console-less single-file Windows EXE.

Usage::

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
    parser = argparse.ArgumentParser(description="Package D-Sync as a Windows EXE")
    parser.add_argument("--clean", action="store_true", help="Clean the build/dist directories before packaging")
    parser.add_argument("--onedir", action="store_true",
                        help="Produce a directory build instead of a single file (faster startup, easier to debug)")
    args = parser.parse_args()

    if args.clean:
        for path in (BUILD_DIR, DIST_DIR):
            if path.exists():
                shutil.rmtree(path, ignore_errors=True)
        print("Cleaned build/ and dist/")

    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        "--name", NAME,
        "--onedir" if args.onedir else "--onefile",
        "--windowed",          # no console window
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

    print("Running:", " ".join(cmd))
    result = subprocess.run(cmd, cwd=str(ROOT))
    if result.returncode != 0:
        print("Packaging failed")
        return result.returncode

    exe = DIST_DIR / (f"{NAME}.exe" if not args.onedir else f"{NAME}/{NAME}.exe")
    if exe.exists():
        size_mb = exe.stat().st_size / 1024 / 1024
        print(f"\nPackaging finished: {exe}  ({size_mb:.1f} MB)")
    else:
        print("\nPackaging done; check the dist/ directory")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
