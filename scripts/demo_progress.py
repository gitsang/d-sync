"""Live sync demo: start the dashboard -> trigger a sync -> capture the SSE progress stream.

Used to verify the core requirement that the page can show sync progress.
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

# Bypass environment proxies for local requests
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
        print(f"Dashboard: {web.url}")

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

        # Trigger a sync manually
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

        print("\n=== Events received over SSE ===")
        for evt in events:
            name = evt.get("event")
            if name == "file_start":
                print(f"  file_start   {Path(evt.get('source','')).name}")
            elif name == "file_done":
                print(f"  file_done    {Path(evt.get('source','')).name} -> {evt.get('status')}")
            elif name == "plan":
                print(f"  plan         {evt.get('total_files')} files / {evt.get('total_bytes')} bytes")
            elif name == "finished":
                st = evt.get("stats", {})
                print(f"  finished     copied {st.get('copied_files')} skipped {st.get('skipped_files')} "
                      f"took {st.get('elapsed')}s")
            elif name == "phase":
                print(f"  phase        {evt.get('phase')}: {evt.get('message')}")

        # Second sync, to verify skipping
        events.clear()
        app2 = SyncApplication()
        app2.run_sync(card, config)
        st = app2.snapshot()["stats"]
        print("\n=== Second sync (duplicate files) ===")
        print(f"  copied {st['copied_files']}, skipped {st['skipped_files']}")

        web.stop()
        print("\nDemo finished.")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
