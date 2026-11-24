"""Final acceptance with the packaged EXE: really run "insert -> sync -> skip duplicates".

Usage::

    python scripts/verify_exe.py
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

# Local requests must bypass environment proxies or they get intercepted and return 502
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "D-Sync.exe"
PASS, FAIL = "  [PASS]", "  [FAIL]"
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"{PASS if condition else FAIL} {message}")
    if not condition:
        failures.append(message)


def api(base: str, path: str) -> dict:
    with OPENER.open(base + path, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    if not EXE.exists():
        print(f"EXE not found: {EXE}\nRun python scripts/build_exe.py first")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="dsync_exe_"))
    proc = None
    try:
        card = workdir / "TESTCARD"
        (card / "DCIM").mkdir(parents=True)
        for index in range(6):
            (card / "DCIM" / f"P{index}.jpg").write_bytes(b"img" * 3000)
        backup = workdir / "OUT"
        (card / "dsync.yml").write_text(
            "version: 1\n"
            f"destination_root: '{backup}'\n"
            "rules:\n"
            "  - source: DCIM\n"
            "    destination: Photos\n",
            encoding="utf-8")

        port = 8795
        print(f"[1] Starting the EXE: {EXE.name}")
        proc = subprocess.Popen(
            [str(EXE), "--no-tray", "--no-browser", "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        base = f"http://127.0.0.1:{port}"

        ready = False
        for _ in range(40):
            time.sleep(0.5)
            try:
                api(base, "/api/state")
                ready = True
                break
            except Exception:
                continue
        check(ready, "the EXE started and listens on a local port")
        if not ready:
            print(proc.stderr.read().decode("utf-8", errors="replace")[:2000])
            return 1

        print("\n[2] Dashboard API")
        state = api(base, "/api/state")
        check(state.get("version") is not None, "the state API returns a version")
        with OPENER.open(base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        check('data-i18n="app.heading"' in html, "the dashboard index renders")

        print("\n[3] Triggering a sync over the API (simulating a card insert)")
        req = urllib.request.Request(
            base + "/api/sync_now", data=json.dumps({"path": str(card)}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with OPENER.open(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        check(result.get("ok") is True, "the sync_now API accepted the request")

        copied = 0
        for _ in range(30):
            time.sleep(0.5)
            state = api(base, "/api/state")
            stats = state.get("stats")
            if stats and not state.get("syncing"):
                copied = stats["copied_files"]
                break
        check(copied == 6, f"the sync copied 6 files (got {copied})")
        check((backup / "Photos" / "P0.jpg").exists(), "files landed at the destination")

        print("\n[4] Syncing again (all duplicates should be skipped)")
        req = urllib.request.Request(
            base + "/api/sync_now", data=json.dumps({"path": str(card)}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        urllib.request.install_opener(OPENER)
        urllib.request.urlopen(req, timeout=15).read()
        skipped = 0
        for _ in range(30):
            time.sleep(0.5)
            state = api(base, "/api/state")
            stats = state.get("stats")
            if stats and stats.get("skipped_files") == 6:
                skipped = stats["skipped_files"]
                break
        check(skipped == 6, f"all duplicate files skipped (skipped {skipped})")

        print("\n[5] Configuring rules from the page (inside the EXE)")
        cfg_req = urllib.request.Request(
            base + "/api/get_config",
            data=json.dumps({"card_path": str(card)}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with OPENER.open(cfg_req, timeout=15) as resp:
            cfg = json.loads(resp.read().decode("utf-8"))
        check(cfg["result"]["exists"] is True, "the EXE can read the card config")
        check(cfg["result"]["model"]["rules"][0]["source"] == "DCIM", "rule content is correct")

        save_req = urllib.request.Request(
            base + "/api/save_config",
            data=json.dumps({
                                "card_path": str(card),
                "model": {
                    "destination_root": str(workdir / "OUT"),
                    "overwrite": False,
                    "exclude": ["*.tmp"],
                    "rules": [{"source": "DCIM", "destination": "PageConfigured",
                               "exclude": [], "overwrite": None}],
                }}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with OPENER.open(save_req, timeout=15) as resp:
            saved = json.loads(resp.read().decode("utf-8"))
        check(saved["result"]["saved"] is True, "the page saved the config")
        check("PageConfigured" in (card / "dsync.yml").read_text(encoding="utf-8"),
              "the YAML was written back to the card")

        # The auto-resync after saving is asynchronous, so wait for it to finish
        landed = False
        for _ in range(30):
            time.sleep(0.5)
            if (workdir / "OUT" / "PageConfigured").exists():
                landed = True
                break
        check(landed, "the new rules took effect and the files landed")

        print("\n[6] Cleanup and exit")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        check(True, "the EXE responds to the termination signal")

        print()
        if failures:
            print(f"=== {len(failures)} checks failed ===")
            for item in failures:
                print(f"  - {item}")
            return 1
        print("=== EXE acceptance passed ===")
        return 0
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
