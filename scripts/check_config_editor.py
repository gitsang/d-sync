"""HTTP-level verification of the config-editing API: a real server simulating the page flow."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsync.app import SyncApplication  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
PASS, FAIL = "  [PASS]", "  [FAIL]"
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    print(f"{PASS if condition else FAIL} {message}")
    if not condition:
        failures.append(message)


def post(base: str, action: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base + "/api/" + action,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"})
    with OPENER.open(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def get(base: str, path: str) -> dict:
    with OPENER.open(base + path, timeout=10) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main() -> int:
    workdir = Path(tempfile.mkdtemp(prefix="dsync_cfg_"))
    try:
        card = workdir / "TESTCARD"
        (card / "DCIM").mkdir(parents=True)
        for index in range(4):
            (card / "DCIM" / f"P{index}.jpg").write_bytes(b"pic" * 500)
        (card / "dsync.yml").write_text(
            "version: 1\n"
            f"destination_root: '{workdir / 'OUT'}'\n"
            "rules:\n"
            "  - source: DCIM\n"
            "    destination: Photos\n",
            encoding="utf-8")

        app = SyncApplication()
        web = app.start_web(port=8796)
        time.sleep(0.6)
        base = web.url
        print(f"Dashboard: {base}\n")

        try:
            print("[1] Reading the existing config")
            resp = post(base, "get_config", {"card_path": str(card)})
            assert resp.get("ok"), resp
            model = resp["result"]["model"]
            check(resp["result"]["exists"] is True, "exists flag is correct")
            check(model["rules"][0]["source"] == "DCIM", "read source path DCIM")
            check(model["rules"][0]["destination"] == "Photos", "read destination path Photos")
            check(model["destination_root"] == str(workdir / "OUT"), "read destination root")

            print("\n[2] Validating a valid config")
            resp = post(base, "validate_config", {
                "model": {"rules": [{"source": "DCIM", "destination": "Photos"}]},
                "card_path": str(card)})
            check(resp["result"]["valid"] is True, "valid config passed validation")

            print("\n[3] Validating an invalid config (destination inside the card)")
            resp = post(base, "validate_config", {
                "model": {"rules": [{"source": "DCIM", "destination": str(card / "x")}]},
                "card_path": str(card)})
            check(resp["result"]["valid"] is False, "destination inside the card rejected")
            check("SD card" in resp["result"].get("error", ""), "error message explains the reason")

            print("\n[4] Editing and saving the config")
            new_model = {
                "destination_root": str(workdir / "OUT"),
                "overwrite": True,
                "exclude": ["*.tmp"],
                "rules": [
                    {"source": "DCIM", "destination": "Photos2", "exclude": [],
                     "overwrite": None},
                    {"source": "DCIM", "destination": "Photos3", "exclude": [],
                     "overwrite": True},
                ],
            }
            resp = post(base, "save_config", {
                "model": new_model, "card_path": str(card)})
            check(resp.get("ok"), f"save API returned success: {resp}")
            check(resp["result"]["saved"] is True, "config saved")
            check(resp["result"]["rules"] == 2, "2 rules saved")

            yaml_text = (card / "dsync.yml").read_text(encoding="utf-8")
            check("Photos2" in yaml_text, "YAML file updated")
            check("Photos3" in yaml_text, "second rule written")
            check("overwrite: true" in yaml_text, "overwrite setting written")

            print("\n[5] Re-reading to confirm persistence")
            resp = post(base, "get_config", {"card_path": str(card)})
            model = resp["result"]["model"]
            check(len(model["rules"]) == 2, "re-read 2 rules")
            check(model["rules"][0]["destination"] == "Photos2", "rule content read back correctly")
            check(model["overwrite"] is True, "global overwrite setting read back correctly")

            print("\n[6] A sync is triggered automatically after saving")
            time.sleep(2.5)
            state = get(base, "/api/state")
            stats = state.get("stats")
            check(stats is not None, "a sync statistic was produced after saving")
            if stats:
                # The two rules share a source but differ in destination (Photos2 / Photos3),
                # so each file is copied once per rule -> 4 files x 2 = 8
                check(stats["copied_files"] == 8,
                      f"the new rules copied 8 files (got {stats['copied_files']})")
            check((workdir / "OUT" / "Photos2").exists(),
                  "files landed in Photos2/ per the new rules")
            check((workdir / "OUT" / "Photos3").exists(),
                  "files landed in Photos3/ per the new rules")

            print("\n[7] YAML preview")
            resp = post(base, "preview_config", {"card_path": str(card)})
            check("source" in resp["result"]["yaml"], "preview returns YAML text")

            print("\n[8] Page static assets contain the editor")
            with OPENER.open(base + "/", timeout=10) as resp:
                html = resp.read().decode("utf-8")
            check('data-i18n="section.config"' in html, "the page has the rule editor area")
            check('id="cfg-rules"' in html, "the page has the rules table")
            check('id="card-select"' in html, "the page has the SD card selector")
            check('id="auto-enable"' in html, "the page has the auto-sync toggle")
            with OPENER.open(base + "/static/app.js", timeout=10) as resp:
                js = resp.read().decode("utf-8")
            check("save_config" in js, "the front end calls the save API")
            check("get_config" in js, "the front end calls the load API")
            check("pick_path" in js, "the front end calls the path-picker API")
            with OPENER.open(base + "/static/style.css", timeout=10) as resp:
                css = resp.read().decode("utf-8")
            check(".rule-table" in css, "the stylesheet contains the rule-table styles")
        finally:
            web.stop()

        print()
        if failures:
            print(f"=== {len(failures)} checks failed ===")
            for item in failures:
                print(f"  - {item}")
            return 1
        print("=== All config-editing API checks passed ===")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
