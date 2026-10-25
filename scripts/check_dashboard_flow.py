"""看板新交互流程的端到端验证（走真实 HTTP 接口）。

覆盖：
  1. 全局状态接口
  2. 选择设备（有配置 / 无配置两种情况）
  3. enable 开关的读写
  4. 规则编辑与保存
  5. 手动同步触发
  6. 页面结构检查
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

# 隔离应用数据目录，避免污染用户环境
_TMP = tempfile.mkdtemp(prefix="dsync_e2e_")
os.environ["SDSYNC_DATA_DIR"] = _TMP

from dsync.app import SyncApplication  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

PASS = "  [通过]"
FAIL = "  [失败]"
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
        print("\n[1] 全局状态接口")
        state = api(base, "/api/state")
        for key in ("phase", "devices", "stats", "history", "selected_card"):
            check(key in state, f"状态包含 {key}")
        check("rules" in state, "状态包含当前规则")

        print("\n[2] 选择设备（带配置）")
        card = make_card(workdir, "E", with_config=True)
        result = post(base, "select_card", {"path": str(card)})
        check(result.get("has_config") is True, "识别到配置文件")
        check(result.get("rules") == 2, f"读到 2 条规则（实际 {result.get('rules')}）")
        check(result.get("enable") is True, "enable 初始为 true")
        check(state["selected_card"] == "", "初始未选中卡")

        state2 = api(base, "/api/state")
        check(state2["selected_card"] == str(card), "选中状态已记录到全局快照")

        print("\n[3] 加载配置到编辑器")
        cfg = post(base, "get_config", {"card_path": str(card)})
        check(cfg.get("exists") is True, "配置文件存在")
        check(len(cfg["model"]["rules"]) == 2, "编辑模型含 2 条规则")
        check(cfg["model"]["enable"] is True, "编辑模型含 enable 字段")

        print("\n[4] enable 开关关闭")
        res = post(base, "set_enabled", {"path": str(card), "enable": False})
        check(res.get("enable") is False, "开关写入成功")
        yaml_text = (card / "dsync.yml").read_text(encoding="utf-8")
        check("enable: false" in yaml_text, "YAML 中已写入 enable: false")

        reread = post(base, "get_config", {"card_path": str(card)})
        check(reread["model"]["enable"] is False, "重新读取确认已持久化")

        print("\n[5] enable=false 时手动同步仍应执行")
        out_dir = workdir / "OUT_E"
        res = post(base, "sync_now", {"path": str(card)})
        check(res.get("started") is True, "手动同步已启动")
        copied = 0
        for _ in range(40):
            time.sleep(0.4)
            st = api(base, "/api/state")
            stats = st.get("stats") or {}
            if stats.get("total_files"):
                copied = stats.get("copied_files", 0)
                break
        check(copied == 4, f"复制了 4 个文件（实际 {copied}）")
        check((out_dir / "Photos" / "100CANON").exists(), "照片落到 Photos 目录")

        print("\n[6] 再次同步应全部跳过")
        post(base, "sync_now", {"path": str(card)})
        skipped = 0
        for _ in range(40):
            time.sleep(0.4)
            st = api(base, "/api/state")
            stats = st.get("stats") or {}
            if stats.get("skipped_files") == 4:
                skipped = stats["skipped_files"]
                break
        check(skipped == 4, f"重复文件全部跳过（实际 {skipped}）")

        print("\n[7] 页面保存规则（改目标目录 + 关闭 enable）")
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
        check(res.get("rules") == 1, "保存了 1 条规则")
        saved_text = (card / "dsync.yml").read_text(encoding="utf-8")
        check("Pictures" in saved_text, "新目标路径已写入 YAML")
        check("enable: false" in saved_text, "enable=false 已写入 YAML")
        check("*.tmp" in saved_text, "全局排除已写入 YAML")

        reread = post(base, "get_config", {"card_path": str(card)})
        check(len(reread["model"]["rules"]) == 1, "重新读取只剩 1 条规则")
        check(reread["model"]["rules"][0]["destination"] == "Pictures", "目标路径正确")

        print("\n[8] 选择没有配置的卡")
        empty_card = make_card(workdir, "F", with_config=False)
        result = post(base, "select_card", {"path": str(empty_card)})
        check(result.get("has_config") is False, "识别为无配置")
        check(result.get("selected") == str(empty_card), "已切换到该卡")

        print("\n[9] 页面结构")
        with OPENER.open(base + "/", timeout=10) as resp:
            html = resp.read().decode("utf-8")
        check("选择设备" in html, "包含设备选择区")
        check("立即同步" in html, "包含手动同步按钮")
        check("自动同步" in html, "包含 enable 开关")
        check("尚未创建配置文件" in html, "包含无配置提示区")
        check("运行日志" in html, "包含运行日志区")
        check("历史记录" in html, "包含历史记录区")
        check(html.index("选择设备") < html.index("同步配置") or "同步配置" not in html,
              "配置区排在设备选择区之后")
        check(html.index("运行日志") < html.index("历史记录"), "日志在历史之前")

        print("\n[10] 历史记录")
        st = api(base, "/api/state")
        check(len(st.get("history") or []) >= 2, f"历史记录有 {len(st.get('history') or [])} 条")

        print("\n[11] 路径选择接口结构（mock，不实际弹窗）")
        from dsync import app as app_module
        original = app_module.pick_path_for_rule
        app_module.pick_path_for_rule = lambda *a, **k: {
            "cancelled": True, "path": "", "absolute": ""}
        try:
            res = post(base, "pick_path", {"kind": "destination", "current": ""})
            check(res.get("cancelled") is True, "路径选择接口返回结构化结果")
        finally:
            app_module.pick_path_for_rule = original

    finally:
        web.stop()

    print("\n" + "=" * 56)
    if failures:
        print(f"失败 {len(failures)} 项：")
        for item in failures:
            print(f"  - {item}")
        return 1
    print("全部检查通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
