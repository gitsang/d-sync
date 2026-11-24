"""End-to-end script: simulate an SD card and run the whole chain
"insert -> read config -> sync -> skip duplicates -> dashboard".

Usage::

    python scripts/e2e_check.py
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# Bypass environment proxies for local requests
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsync.app import SyncApplication  # noqa: E402
from dsync.config import load_config_file  # noqa: E402

PASS, FAIL = "  [PASS]", "  [FAIL]"
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"{PASS} {message}")
    else:
        print(f"{FAIL} {message}")
        failures.append(message)


def build_fake_card(root: Path) -> Path:
    """Build a simulated SD card."""
    card = root / "FAKECARD"
    (card / "DCIM" / "100CANON").mkdir(parents=True)
    (card / "DCIM" / "100CANON" / "IMG_001.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"photo-one" * 500)
    (card / "DCIM" / "100CANON" / "IMG_002.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"photo-two" * 500)
    (card / "Documents").mkdir()
    (card / "Documents" / "report.pdf").write_bytes(b"%PDF-1.4 test doc " * 200)
    (card / "Documents" / "temp.bak").write_bytes(b"backup")
    (card / "dsync.yml").write_text(
        "version: 1\n"
        f"destination_root: '{root / 'BACKUP'}'\n"
        "rules:\n"
        "  - source: DCIM\n"
        "    destination: Photos\n"
        "  - source: Documents\n"
        "    destination: Docs\n"
        "    exclude: ['*.bak']\n",
        encoding="utf-8",
    )
    return card


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="dsync_e2e_"))
    try:
        print(f"Temp work directory: {workdir}\n")
        card = build_fake_card(workdir)
        backup = workdir / "BACKUP"
        print("[1] Simulated SD card is ready")
        check((card / "dsync.yml").is_file(), "dsync.yml exists at the card root")

        # ---------------------------------------------------------- config parsing
        print("\n[2] Parsing the SD card config")
        config = load_config_file(card / "dsync.yml", card_root=card)
        check(len(config.rules) == 2, f"parsed 2 rules (got {len(config.rules)})")
        check(config.destination_root == str(backup), "destination_root is correct")

        # ---------------------------------------------------------- first sync
        print("\n[3] First sync")
        app = SyncApplication()
        app.run_sync(card, config)
        snap = app.snapshot()
        stats = snap["stats"]
        check(stats["copied_files"] == 3, f"copied 3 files (got {stats['copied_files']})")
        check(stats["skipped_files"] == 0, f"skipped 0 (got {stats['skipped_files']})")
        check((backup / "Photos" / "100CANON" / "IMG_001.jpg").is_file(),
              "photos copied to Photos/100CANON/")
        check((backup / "Docs" / "report.pdf").is_file(), "document copied to Docs/")
        check(not (backup / "Docs" / "temp.bak").exists(),
              "temp.bak excluded by the exclude rule was not copied")

        # ---------------------------------------------------------- duplicate sync
        print("\n[4] Syncing again (all duplicates should be skipped)")
        app2 = SyncApplication()
        app2.run_sync(card, config)
        stats2 = app2.snapshot()["stats"]
        check(stats2["copied_files"] == 0, f"nothing copied twice (copied {stats2['copied_files']})")
        check(stats2["skipped_files"] == 3, f"skipped 3 duplicate files (got {stats2['skipped_files']})")

        # ---------------------------------------------------------- content change
        print("\n[5] Re-syncing after a file changed on the card")
        (card / "Documents" / "report.pdf").write_bytes(b"%PDF-1.4 modified content " * 200)
        app3 = SyncApplication()
        app3.run_sync(card, config)
        stats3 = app3.snapshot()["stats"]
        check(stats3["copied_files"] == 0,
              f"with different content and overwrite off, skipped by default (copied {stats3['copied_files']})")

        config_ow = load_config_file(card / "dsync.yml", card_root=card)
        config_ow.overwrite = True
        app4 = SyncApplication()
        app4.run_sync(card, config_ow)
        stats4 = app4.snapshot()["stats"]
        check(stats4["copied_files"] == 1,
              f"with overwrite on, 1 changed file was copied (got {stats4['copied_files']})")
        check((backup / "Docs" / "report.pdf").read_bytes().startswith(b"%PDF-1.4 modified"),
              "the destination file was updated")

        # ---------------------------------------------------------- web dashboard
        print("\n[6] Starting the web dashboard and verifying the API")
        app5 = SyncApplication()
        web = app5.start_web(port=8791)
        time.sleep(0.6)
        try:
            with OPENER.open(f"{web.url}api/state", timeout=5) as resp:
                state = json.loads(resp.read().decode("utf-8"))
            check(state.get("version") is not None, "/api/state returns a state snapshot")
            check("devices" in state and "rules" in state, "the snapshot contains device and rule fields")

            with OPENER.open(f"{web.url}", timeout=5) as resp:
                html = resp.read().decode("utf-8")
            check("D-Sync" in html, "the index HTML is served")
            check("/static/app.js" in html, "the index references the front-end script")

            with OPENER.open(f"{web.url}static/app.js", timeout=5) as resp:
                js = resp.read().decode("utf-8")
            check("EventSource" in js, "the front end uses SSE for live progress")

            req = urllib.request.Request(
                f"{web.url}api/rescan", data=b"{}", method="POST",
                headers={"Content-Type": "application/json"})
            with OPENER.open(req, timeout=5) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            check(result.get("ok") is True, "POST /api/rescan succeeded")
        except Exception as exc:
            check(False, f"dashboard API error: {exc}")
        finally:
            web.stop()

        # ---------------------------------------------------------- history
        print("\n[7] History")
        app6 = SyncApplication()
        app6.run_sync(card, config)
        history = app6.snapshot()["history"]
        check(len(history) == 1, f"produced 1 history entry (got {len(history)})")
        if history:
            check(history[0]["copied"] == 0 and history[0]["skipped"] == 3,
                  "history statistics are correct")

        print()
        if failures:
            print(f"=== {len(failures)} checks failed ===")
            for item in failures:
                print(f"  - {item}")
            return 1
        print("=== All checks passed ===")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
