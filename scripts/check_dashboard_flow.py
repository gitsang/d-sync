"""End-to-end verification of the dashboard's new interaction flow (over real HTTP).

Covers:
  1. the global state API
  2. selecting a device (both with and without a config)
  3. reading and writing the enable flag
  4. editing and saving rules
  5. triggering a manual sync
  6. checking the page structure
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Isolate the app data directory so the user environment is not polluted
_TMP = tempfile.mkdtemp(prefix="dsync_e2e_")
os.environ["SDSYNC_DATA_DIR"] = _TMP

from dsync.app import SyncApplication  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS = "  [PASS]"
FAIL = "  [FAIL]"
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"{PASS if condition else FAIL} {message}")
    if not condition:
        failures.append(message)


def api(base: str, path: str) -> dict:
    with OPENER.open(base + path, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def post(base: str, action: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base + "/api/" + action,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with OPENER.open(req, timeout=30) as resp:
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
    workdir = Path(_TMP)
    app = SyncApplication()
    web = app.start_web(host="127.0.0.1", port=8795)
    base = f"http://127.0.0.1:{web.port}"
    time.sleep(0.6)

    try:
        print("\n[1] Global state API")
        state = api(base, "/api/state")
        for key in ("phase", "devices", "stats", "history", "selected_card"):
            check(key in state, f"state contains {key}")
        check("rules" in state, "state contains the current rules")

        print("\n[2] Selecting a device (with config)")
        card = make_card(workdir, "E", with_config=True)
        result = post(base, "select_card", {"path": str(card)})
        check(result.get("has_config") is True, "config file recognised")
        check(result.get("rules") == 2, f"read 2 rules (got {result.get('rules')})")
        check(result.get("enable") is True, "enable starts as true")
        check(state["selected_card"] == "", "no card selected initially")

        state2 = api(base, "/api/state")
        check(state2["selected_card"] == str(card), "selection recorded in the global snapshot")

        print("\n[3] Loading the config into the editor")
        cfg = post(base, "get_config", {"card_path": str(card)})
        check(cfg.get("exists") is True, "config file exists")
        check(len(cfg["model"]["rules"]) == 2, "edit model has 2 rules")
        check(cfg["model"]["enable"] is True, "edit model has the enable field")

        print("\n[4] Turning enable off")
        res = post(base, "set_enabled", {"path": str(card), "enable": False})
        check(res.get("enable") is False, "flag written successfully")
        yaml_text = (card / "dsync.yml").read_text(encoding="utf-8")
        check("enable: false" in yaml_text, "enable: false written to the YAML")

        reread = post(base, "get_config", {"card_path": str(card)})
        check(reread["model"]["enable"] is False, "re-read confirms persistence")

        print("\n[5] A manual sync must still run when enable=false")
        out_dir = workdir / "OUT_E"
        res = post(base, "sync_now", {"path": str(card)})
        check(res.get("started") is True, "manual sync started")
        copied = 0
        for _ in range(40):
            time.sleep(0.4)
            st = api(base, "/api/state")
            stats = st.get("stats") or {}
            if stats.get("total_files"):
                copied = stats.get("copied_files", 0)
                break
        check(copied == 4, f"copied 4 files (got {copied})")
        check((out_dir / "Photos" / "100CANON").exists(), "photos landed in the Photos directory")

        print("\n[6] A second sync should skip everything")
        post(base, "sync_now", {"path": str(card)})
        skipped = 0
        for _ in range(40):
            time.sleep(0.4)
            st = api(base, "/api/state")
            stats = st.get("stats") or {}
            if stats.get("skipped_files") == 4:
                skipped = stats["skipped_files"]
                break
        check(skipped == 4, f"all duplicate files skipped (got {skipped})")

        print("\n[7] Saving rules from the page (new destination + enable off)")
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
        check(res.get("rules") == 1, "saved 1 rule")
        saved_text = (card / "dsync.yml").read_text(encoding="utf-8")
        check("Pictures" in saved_text, "new destination written to the YAML")
        check("enable: false" in saved_text, "enable=false written to the YAML")
        check("*.tmp" in saved_text, "global exclude written to the YAML")

        reread = post(base, "get_config", {"card_path": str(card)})
        check(len(reread["model"]["rules"]) == 1, "re-read has exactly 1 rule")
        check(reread["model"]["rules"][0]["destination"] == "Pictures", "destination is correct")

        print("\n[8] Selecting a card without a config")
        empty_card = make_card(workdir, "F", with_config=False)
        result = post(base, "select_card", {"path": str(empty_card)})
        check(result.get("has_config") is False, "recognised as having no config")
        check(result.get("selected") == str(empty_card), "switched to that card")

        print("\n[9] Page structure")
        with OPENER.open(base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        check('data-i18n="section.selectDevice"' in html, "contains the device selection area")
        check('data-i18n="action.syncNow"' in html, "contains the manual sync button")
        check('data-i18n="config.autoSync"' in html, "contains the enable toggle")
        check('data-i18n="section.noConfig"' in html, "contains the no-config prompt")
        check('data-i18n="section.log"' in html, "contains the activity log area")
        check('data-i18n="section.history"' in html, "contains the history area")
        check(html.index("section.selectDevice") < html.index("section.config") or "section.config" not in html,
              "the config area comes after the device selection")
        check(html.index("section.log") < html.index("section.history"), "log comes before history")

        print("\n[10] History")
        st = api(base, "/api/state")
        check(len(st.get("history") or []) >= 2, f"history has {len(st.get('history') or [])} entries")

        print("\n[11] Path-picker API shape (mocked, no real dialog)")
        from dsync import app as app_module
        original = app_module.pick_path_for_rule
        app_module.pick_path_for_rule = lambda *a, **k: {
            "cancelled": True, "path": "", "absolute": ""}
        try:
            res = post(base, "pick_path", {"kind": "destination", "current": ""})
            check(res.get("cancelled") is True, "path-picker API returns a structured result")
        finally:
            app_module.pick_path_for_rule = original

    finally:
        web.stop()

    print("\n" + "=" * 56)
    if failures:
        print(f"{len(failures)} checks failed:")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("All checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
