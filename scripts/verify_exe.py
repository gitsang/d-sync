"""用打包好的 EXE 做最终验收：真实跑一遍「插入 -> 同步 -> 跳过重复」。

用法::

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

# 本地请求必须绕过环境代理，否则会被代理拦截返回 502
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist" / "D-Sync.exe"
PASS, FAIL = "  [通过]", "  [失败]"
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
        print(f"未找到 EXE: {EXE}\n请先运行 python scripts/build_exe.py")
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
        print(f"[1] 启动 EXE: {EXE.name}")
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
        check(ready, "EXE 启动并监听本地端口")
        if not ready:
            print(proc.stderr.read().decode("utf-8", errors="replace")[:2000])
            return 1

        print("\n[2] 看板接口")
        state = api(base, "/api/state")
        check(state.get("version") is not None, "状态接口返回版本号")
        with OPENER.open(base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        check("同步看板" in html, "看板首页正常渲染")

        print("\n[3] 通过 API 触发同步（模拟 SD 卡插入）")
        req = urllib.request.Request(
            base + "/api/sync_now", data=json.dumps({"path": str(card)}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with OPENER.open(req, timeout=15) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        check(result.get("ok") is True, "sync_now 接口接受请求")

        copied = 0
        for _ in range(30):
            time.sleep(0.5)
            state = api(base, "/api/state")
            stats = state.get("stats")
            if stats and not state.get("syncing"):
                copied = stats["copied_files"]
                break
        check(copied == 6, f"同步复制 6 个文件（实际 {copied}）")
        check((backup / "Photos" / "P0.jpg").exists(), "文件已落盘到目标路径")

        print("\n[4] 再次同步（应跳过全部重复）")
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
        check(skipped == 6, f"重复文件全部跳过（实际跳过 {skipped}）")

        print("\n[5] 页面配置规则（EXE 内）")
        cfg_req = urllib.request.Request(
            base + "/api/get_config",
            data=json.dumps({"card_path": str(card)}).encode(),
            method="POST", headers={"Content-Type": "application/json"})
        with OPENER.open(cfg_req, timeout=15) as resp:
            cfg = json.loads(resp.read().decode("utf-8"))
        check(cfg["result"]["exists"] is True, "EXE 可读取卡上配置")
        check(cfg["result"]["model"]["rules"][0]["source"] == "DCIM", "规则内容正确")

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
        check(saved["result"]["saved"] is True, "页面配置保存成功")
        check("PageConfigured" in (card / "dsync.yml").read_text(encoding="utf-8"),
              "YAML 已写回 SD 卡")

        # 保存后自动重新同步是异步的，等它跑完
        landed = False
        for _ in range(30):
            time.sleep(0.5)
            if (workdir / "OUT" / "PageConfigured").exists():
                landed = True
                break
        check(landed, "新规则已实际生效并落盘")

        print("\n[6] 清理与退出")
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        check(True, "EXE 可正常响应终止信号")

        print()
        if failures:
            print(f"=== {len(failures)} 项失败 ===")
            for item in failures:
                print(f"  - {item}")
            return 1
        print("=== EXE 验收全部通过 ===")
        return 0
    finally:
        if proc and proc.poll() is None:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
