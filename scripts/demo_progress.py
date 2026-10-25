"""同步过程实时演示：启动看板 -> 触发同步 -> 抓取 SSE 进度流。

用于验证「页面能展示同步进度」这一核心诉求。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

# 本地请求绕过环境代理
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsync.app import SyncApplication  # noqa: E402


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="dsync_demo_"))
    try:
        card = workdir / "DEMOCARD"
        (card / "DCIM" / "100CANON").mkdir(parents=True)
        for index in range(12):
            (card / "DCIM" / "100CANON" / f"IMG_{index:03d}.jpg").write_bytes(
                b"\xff\xd8\xff\xe0" + f"photo-{index}".encode() * 4000)
        backup = workdir / "BACKUP"

        app = SyncApplication()
        web = app.start_web(port=8792)
        time.sleep(0.5)
        print(f"看板: {web.url}")

        events: list[dict] = []

        def listen():
            try:
                with OPENER.open(f"{web.url}api/events", timeout=30) as resp:
                    for raw in resp:
                        line = raw.decode("utf-8").strip()
                        if line.startswith("data: "):
                            events.append(json.loads(line[6:]))
            except Exception:
                pass

        thread = threading.Thread(target=listen, daemon=True)
        thread.start()
        time.sleep(1.0)

        # 手动触发一次同步
        from dsync.config import load_config_file
        config_path = card / "dsync.yml"
        config_path.write_text(
            "version: 1\n"
            f"destination_root: '{backup}'\n"
            "rules:\n"
            "  - source: DCIM\n"
            "    destination: Photos\n",
            encoding="utf-8")

        config = load_config_file(config_path, card_root=card)
        app.run_sync(card, config)
        time.sleep(1.5)

        print("\n=== SSE 收到的事件流 ===")
        for evt in events:
            name = evt.get("event")
            if name == "file_start":
                print(f"  file_start   {Path(evt.get('source','')).name}")
            elif name == "file_done":
                print(f"  file_done    {Path(evt.get('source','')).name} -> {evt.get('status')}")
            elif name == "plan":
                print(f"  plan         {evt.get('total_files')} 个文件 / {evt.get('total_bytes')} 字节")
            elif name == "finished":
                st = evt.get("stats", {})
                print(f"  finished     复制 {st.get('copied_files')} 跳过 {st.get('skipped_files')} "
                      f"耗时 {st.get('elapsed')}s")
            elif name == "phase":
                print(f"  phase        {evt.get('phase')}: {evt.get('message')}")

        # 第二次同步，验证跳过
        events.clear()
        app2 = SyncApplication()
        app2.run_sync(card, config)
        st = app2.snapshot()["stats"]
        print("\n=== 第二次同步（重复文件）===")
        print(f"  复制 {st['copied_files']} 个，跳过 {st['skipped_files']} 个")

        web.stop()
        print("\n演示完成。")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
