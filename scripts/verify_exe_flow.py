"""Full acceptance of the packaged EXE: verify the new interaction flow over real HTTP.

Covers: select card -> load rules -> enable flag -> manual sync -> dedup -> page save -> card without config
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "D-Sync.exe"

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PASS, FAIL = "  [PASS]", "  [FAIL]"
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"{PASS if condition else FAIL} {message}")
    if not condition:
        failures.append(message)


def api(base: str, path: str) -> dict:
    with OPENER.open(base + path, timeout=20) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post(base: str, action: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base + "/api/" + action,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with OPENER.open(req, timeout=60) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("result", body)


def make_card(root: Path, name: str, with_config: bool) -> Path:
    card = root / name
    (card / "DCIM" / "100CANON").mkdir(parents=True, exist_ok=True)
    for i in range(3):
        (card / "DCIM" / "100CANON" / f"IMG_{i:04d}.jpg").write_bytes(bytes([i]) * 1024)
    (card / "Documents").mkdir(exist_ok=True)
    (card / "Documents" / "note.txt").write_bytes(b"hello" * 100)
    if with_config:
        out = root / f"OUT_{name}"
        (card / "dsync.yml").write_text(
            "version: 1\n"
            "enable: true\n"
            f"destination_root: '{out.as_posix()}'\n"
            "rules:\n"
            "  - source: DCIM\n"
            "    destination: Photos\n"
            "  - source: Documents\n"
            "    destination: Docs\n",
            encoding="utf-8",
        )
    return card


def main() -> int:
    if not EXE.exists():
        print("EXE not found; run scripts/build_exe.py first")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="dsync_exe_"))
    data_dir = workdir / "_appdata"
    port = 8798
    base = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env["SDSYNC_DATA_DIR"] = str(data_dir)

    print("[0] Starting the EXE")
    proc = subprocess.Popen(
        [str(EXE), "--no-tray", "--no-browser", "--port", str(port)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        ready = False
        for _ in range(40):
            time.sleep(0.6)
            try:
                api(base, "/api/state")
                ready = True
                break
            except Exception:
                continue
        check(ready, "EXE started and the dashboard is reachable")
        if not ready:
            return 1

        print("\n[1] Page structure")
        with OPENER.open(base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        for label in ("section.devices", "section.selectDevice", "action.syncNow", "config.autoSync",
                      "col.progress", "col.elapsed",
                      "section.noConfig", "section.log", "section.history"):
            check(label in html, f"page contains \"{label}\"")
        check("section.global" not in html, "global status section removed")
        check("action.saveTemplate" not in html, "local template entry removed")
        check(html.index("section.devices") < html.index("section.selectDevice"),
              "device status comes before the selection area")
        check(html.index("section.selectDevice") < html.index("section.log"),
              "selection area comes before the log")
        check(html.index("section.log") < html.index("section.history"),
              "log comes before history")

        print("\n[2] Selecting a card with a config")
        card = make_card(workdir, "E", with_config=True)
        result = post(base, "select_card", {"path": str(card)})
        check(result.get("has_config") is True, "config file recognised")
        check(result.get("rules") == 2, f"read 2 rules (got {result.get('rules')})")

        state = api(base, "/api/state")
        check(state["selected_card"] == str(card), "global state recorded the selected card")

        print("\n[3] Loading the rules into the editor")
        cfg = post(base, "get_config", {"card_path": str(card)})
        check(cfg.get("exists") is True, "config file exists")
        check(len(cfg["model"]["rules"]) == 2, "edit model has 2 rules")
        check(cfg["model"]["enable"] is True, "edit model has the enable field")

        print("\n[4] The enable flag")
        res = post(base, "set_enabled", {"path": str(card), "enable": False})
        check(res.get("enable") is False, "flag written successfully")
        text = (card / "dsync.yml").read_text(encoding="utf-8")
        check("enable: false" in text, "enable: false written to the YAML")

        reread = post(base, "get_config", {"card_path": str(card)})
        check(reread["model"]["enable"] is False, "re-read confirms persistence")

        print("\n[5] A manual sync must still run when enable=false")
        out_dir = workdir / "OUT_E"
        res = post(base, "sync_now", {"path": str(card)})
        check(res.get("started") is True, "manual sync started")
        copied = 0
        for _ in range(50):
            time.sleep(0.4)
            stats = api(base, "/api/state").get("stats") or {}
            if stats.get("total_files"):
                copied = stats.get("copied_files", 0)
                break
        check(copied == 4, f"copied 4 files (got {copied})")
        check((out_dir / "Photos" / "100CANON").exists(), "photos landed in the Photos directory")
        check((out_dir / "Docs" / "note.txt").exists(), "document landed in the Docs directory")

        print("\n[6] A second sync should skip everything")
        post(base, "sync_now", {"path": str(card)})
        skipped = 0
        for _ in range(50):
            time.sleep(0.4)
            stats = api(base, "/api/state").get("stats") or {}
            if stats.get("skipped_files") == 4:
                skipped = stats["skipped_files"]
                break
        check(skipped == 4, f"all duplicate files skipped (got {skipped})")

        print("\n[7] Saving rules from the page")
        model = {
            "version": 1,
            "enable": False,
            "destination_root": (workdir / "OUT_NEW").as_posix(),
            "overwrite": False,
            "exclude": ["*.tmp"],
            "rules": [
                {"source": "DCIM", "destination": "Pictures",
                 "exclude": [], "enabled": True, "overwrite": None},
            ],
        }
        res = post(base, "save_config",
                   {"model": model, "card_path": str(card)})
        check(res.get("saved") is True, "saved successfully")
        saved = (card / "dsync.yml").read_text(encoding="utf-8")
        check("Pictures" in saved, "new destination written")
        check("enable: false" in saved, "enable written")

        reread = post(base, "get_config", {"card_path": str(card)})
        check(len(reread["model"]["rules"]) == 1, "re-read has exactly 1 rule")

        print("\n[8] Selecting a card without a config")
        empty = make_card(workdir, "F", with_config=False)
        result = post(base, "select_card", {"path": str(empty)})
        check(result.get("has_config") is False, "recognised as having no config")
        check(result.get("selected") == str(empty), "switched to that card")

        print("\n[9] History")
        state = api(base, "/api/state")
        check(len(state.get("history") or []) >= 2,
              f"history has {len(state.get('history') or [])} entries")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 56)
    if failures:
        print(f"{len(failures)} checks failed:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("EXE acceptance passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
