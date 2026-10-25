"""打包后 EXE 的完整验收：走真实 HTTP 接口验证新交互流程。

覆盖：选卡 -> 加载规则 -> enable 开关 -> 手动同步 -> 去重 -> 页面保存 -> 无配置卡
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
PASS, FAIL = "  [通过]", "  [失败]"
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
        print("EXE 不存在，先运行 scripts/build_exe.py")
        return 1

    workdir = Path(tempfile.mkdtemp(prefix="dsync_exe_"))
    data_dir = workdir / "_appdata"
    port = 8798
    base = f"http://127.0.0.1:{port}"

    env = dict(os.environ)
    env["SDSYNC_DATA_DIR"] = str(data_dir)

    print("[0] 启动 EXE")
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
        check(ready, "EXE 启动且看板可访问")
        if not ready:
            return 1

        print("\n[1] 页面结构")
        with OPENER.open(base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        for label in ("设备状态", "选择设备", "立即同步", "自动同步",
                      "同步进度", "本次同步耗时",
                      "尚未创建配置文件", "运行日志", "历史记录"):
            check(label in html, f"页面包含「{label}」")
        check("全局状态" not in html, "全局状态区已移除")
        check("存为模板" not in html, "本地模板入口已移除")
        check(html.index("设备状态") < html.index("选择设备"),
              "设备状态区在选择区之前")
        check(html.index("选择设备") < html.index("运行日志"),
              "选择区在日志之前")
        check(html.index("运行日志") < html.index("历史记录"),
              "日志在历史之前")

        print("\n[2] 选择带配置的卡")
        card = make_card(workdir, "E", with_config=True)
        result = post(base, "select_card", {"path": str(card)})
        check(result.get("has_config") is True, "识别到配置文件")
        check(result.get("rules") == 2, f"读到 2 条规则（实际 {result.get('rules')}）")

        state = api(base, "/api/state")
        check(state["selected_card"] == str(card), "全局状态记录了选中卡")

        print("\n[3] 加载规则到编辑器")
        cfg = post(base, "get_config", {"card_path": str(card)})
        check(cfg.get("exists") is True, "配置文件存在")
        check(len(cfg["model"]["rules"]) == 2, "编辑模型含 2 条规则")
        check(cfg["model"]["enable"] is True, "编辑模型含 enable 字段")

        print("\n[4] enable 开关")
        res = post(base, "set_enabled", {"path": str(card), "enable": False})
        check(res.get("enable") is False, "开关写入成功")
        text = (card / "dsync.yml").read_text(encoding="utf-8")
        check("enable: false" in text, "YAML 已写入 enable: false")

        reread = post(base, "get_config", {"card_path": str(card)})
        check(reread["model"]["enable"] is False, "重新读取确认已持久化")

        print("\n[5] enable=false 时手动同步仍应执行")
        out_dir = workdir / "OUT_E"
        res = post(base, "sync_now", {"path": str(card)})
        check(res.get("started") is True, "手动同步已启动")
        copied = 0
        for _ in range(50):
            time.sleep(0.4)
            stats = api(base, "/api/state").get("stats") or {}
            if stats.get("total_files"):
                copied = stats.get("copied_files", 0)
                break
        check(copied == 4, f"复制 4 个文件（实际 {copied}）")
        check((out_dir / "Photos" / "100CANON").exists(), "照片落到 Photos 目录")
        check((out_dir / "Docs" / "note.txt").exists(), "文档落到 Docs 目录")

        print("\n[6] 再次同步应全部跳过")
        post(base, "sync_now", {"path": str(card)})
        skipped = 0
        for _ in range(50):
            time.sleep(0.4)
            stats = api(base, "/api/state").get("stats") or {}
            if stats.get("skipped_files") == 4:
                skipped = stats["skipped_files"]
                break
        check(skipped == 4, f"重复文件全部跳过（实际 {skipped}）")

        print("\n[7] 页面保存规则")
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
        check(res.get("saved") is True, "保存成功")
        saved = (card / "dsync.yml").read_text(encoding="utf-8")
        check("Pictures" in saved, "新目标路径已写入")
        check("enable: false" in saved, "enable 已写入")

        reread = post(base, "get_config", {"card_path": str(card)})
        check(len(reread["model"]["rules"]) == 1, "重新读取只剩 1 条规则")

        print("\n[8] 选择没有配置的卡")
        empty = make_card(workdir, "F", with_config=False)
        result = post(base, "select_card", {"path": str(empty)})
        check(result.get("has_config") is False, "识别为无配置")
        check(result.get("selected") == str(empty), "已切换到该卡")

        print("\n[9] 历史记录")
        state = api(base, "/api/state")
        check(len(state.get("history") or []) >= 2,
              f"历史记录 {len(state.get('history') or [])} 条")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(workdir, ignore_errors=True)

    print("\n" + "=" * 56)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("EXE 验收全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
