"""看板预览服务：启动本地看板并注入模拟同步数据，用于展示界面效果。

用法::

    python scripts/preview_dashboard.py --port 8765
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsync.app import SyncApplication  # noqa: E402
from dsync.config import load_config_file  # noqa: E402


def build_fake_state(app: SyncApplication, workdir: Path) -> None:
    """构造一份逼真的同步现场状态。"""
    card = workdir / "FAKECARD"
    (card / "DCIM" / "100CANON").mkdir(parents=True, exist_ok=True)
    for index in range(48):
        (card / "DCIM" / "100CANON" / f"IMG_{index:04d}.jpg").write_bytes(b"x" * 4096)
    (card / "Documents").mkdir(exist_ok=True)
    (card / "Documents" / "report.pdf").write_bytes(b"y" * 2048)

    backup = workdir / "BACKUP"
    config_path = card / "dsync.yml"
    config_path.write_text(
        "version: 1\n"
        "enable: true\n"
        f"destination_root: '{backup}'\n"
        "overwrite: false\n"
        "exclude:\n"
        "  - '*.tmp'\n"
        "  - 'Thumbs.db'\n"
        "rules:\n"
        "  - source: 'DCIM'\n"
        "    destination: 'Photos'\n"
        "  - source: 'Documents'\n"
        "    destination: 'Docs'\n"
        "    exclude:\n"
        "      - '*.bak'\n"
        "  - source: 'Data'\n"
        "    destination: 'E:/Archive/Data'\n",
        encoding="utf-8")

    with app._lock:
        # 模拟一张已插入的外置设备，指向真实的临时目录
        app._devices[str(card).upper()] = {
            "path": str(card), "name": "CANON (E:)", "state": "syncing",
            "config": "dsync.yml", "enable": True,
            "total_files": 49, "done_files": 31, "skipped_files": 3,
            "copied_files": 28, "elapsed": 0.0,
        }
        app._devices["F:\\"] = {
            "path": "F:\\", "name": "USB Flash Disk", "state": "detected",
            "config": "", "enable": True,
            "total_files": 0, "done_files": 0, "skipped_files": 0,
            "copied_files": 0, "elapsed": 0.0,
        }
        app._devices["G:\\"] = {
            "path": "G:\\", "name": "Kingston DataTraveler", "state": "synced",
            "config": "dsync.yml", "enable": True,
            "total_files": 120, "done_files": 120, "skipped_files": 12,
            "copied_files": 108, "elapsed": 18.72,
        }
        app._selected_card = str(card)
        app._rules = [
            {"source": "DCIM", "destination": str(backup / "Photos"),
             "enabled": True},
            {"source": "Documents", "destination": str(backup / "Docs"),
             "enabled": True},
            {"source": "Data", "destination": "E:/Archive/Data",
             "enabled": True},
        ]
        app._phase = "copying"
        app._message = "正在同步 49 个文件…"
        app._total_files = 49
        app._done_files = 31
        app._current_file = str(card / "DCIM" / "100CANON" / "IMG_0031.jpg")
        app._stats = {
            "total_files": 49, "copied_files": 28, "skipped_files": 3, "failed_files": 0,
            "total_bytes": 196608, "copied_bytes": 114688, "skipped_bytes": 12288,
            "elapsed": 2.41, "finished_at": None,
        }
        app._history = [
            {"ts": time.time() - 3600, "device": "Kingston DataTraveler",
             "path": "G:\\", "status": "ok",
             "copied": 128, "skipped": 46, "failed": 0, "elapsed": 18.72},
            {"ts": time.time() - 7200, "device": "CANON (E:)", "path": str(card),
             "status": "ok",
             "copied": 0, "skipped": 174, "failed": 0, "elapsed": 6.31},
            {"ts": time.time() - 86400, "device": "CANON (E:)", "path": str(card),
             "status": "cancelled",
             "copied": 55, "skipped": 12, "failed": 0, "elapsed": 9.04},
        ]

    # 推一条日志到总线，让前端页面有内容
    for message, level in [
        ("已复制 → D:/SD_Backup/Photos/100CANON/IMG_0029.jpg", "ok"),
        ("已复制 → D:/SD_Backup/Photos/100CANON/IMG_0030.jpg", "ok"),
        ("已跳过（重复）→ D:/SD_Backup/Docs/report.pdf", "warn"),
    ]:
        app.bus.publish("log", message=message, level=level)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    import tempfile
    workdir = Path(tempfile.mkdtemp(prefix="dsync_preview_"))
    app = SyncApplication()
    web = app.start_web(port=args.port)
    build_fake_state(app, workdir)
    print(f"看板预览: {web.url}")
    print("按 Ctrl+C 结束")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        web.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
