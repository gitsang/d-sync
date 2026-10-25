"""配置编辑接口的 HTTP 层验证：走真实服务器，模拟页面操作流程。"""

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
PASS, FAIL = "  [通过]", "  [失败]"
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
        print(f"看板: {base}\n")

        try:
            print("[1] 读取现有配置")
            resp = post(base, "get_config", {"card_path": str(card)})
            assert resp.get("ok"), resp
            model = resp["result"]["model"]
            check(resp["result"]["exists"] is True, "配置存在标记正确")
            check(model["rules"][0]["source"] == "DCIM", "读到源路径 DCIM")
            check(model["rules"][0]["destination"] == "Photos", "读到目标路径 Photos")
            check(model["destination_root"] == str(workdir / "OUT"), "读到目标根目录")

            print("\n[2] 校验合法配置")
            resp = post(base, "validate_config", {
                "model": {"rules": [{"source": "DCIM", "destination": "Photos"}]},
                "card_path": str(card)})
            check(resp["result"]["valid"] is True, "合法配置校验通过")

            print("\n[3] 校验非法配置（目标在卡内）")
            resp = post(base, "validate_config", {
                "model": {"rules": [{"source": "DCIM", "destination": str(card / "x")}]},
                "card_path": str(card)})
            check(resp["result"]["valid"] is False, "目标位于卡内被拒绝")
            check("SD 卡" in resp["result"].get("error", ""), "错误提示包含原因")

            print("\n[4] 编辑并保存配置")
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
            check(resp.get("ok"), f"保存接口返回成功: {resp}")
            check(resp["result"]["saved"] is True, "配置保存成功")
            check(resp["result"]["rules"] == 2, "保存了 2 条规则")

            yaml_text = (card / "dsync.yml").read_text(encoding="utf-8")
            check("Photos2" in yaml_text, "YAML 文件已更新")
            check("Photos3" in yaml_text, "第二条规则已写入")
            check("overwrite: true" in yaml_text, "覆盖设置已写入")

            print("\n[5] 重新读取，确认持久化")
            resp = post(base, "get_config", {"card_path": str(card)})
            model = resp["result"]["model"]
            check(len(model["rules"]) == 2, "重新读取得到 2 条规则")
            check(model["rules"][0]["destination"] == "Photos2", "规则内容正确回读")
            check(model["overwrite"] is True, "全局覆盖设置回读正确")

            print("\n[6] 保存后自动触发同步")
            time.sleep(2.5)
            state = get(base, "/api/state")
            stats = state.get("stats")
            check(stats is not None, "保存后产生了同步统计")
            if stats:
                # 两条规则源相同但目标不同（Photos2 / Photos3），
                # 每个文件应各复制一份 -> 4 个文件 x 2 = 8
                check(stats["copied_files"] == 8,
                      f"新规则同步复制 8 个文件（实际 {stats['copied_files']}）")
            check((workdir / "OUT" / "Photos2").exists(),
                  "文件按新规则落到 Photos2/")
            check((workdir / "OUT" / "Photos3").exists(),
                  "文件按新规则落到 Photos3/")

            print("\n[7] YAML 预览")
            resp = post(base, "preview_config", {"card_path": str(card)})
            check("source" in resp["result"]["yaml"], "预览返回 YAML 文本")

            print("\n[8] 页面静态资源包含编辑器")
            with OPENER.open(base + "/", timeout=10) as resp:
                html = resp.read().decode("utf-8")
            check("同步配置" in html, "页面存在规则编辑区")
            check('id="cfg-rules"' in html, "页面存在规则表格")
            check('id="card-select"' in html, "页面存在 SD 卡选择器")
            check('id="auto-enable"' in html, "页面存在自动同步开关")
            with OPENER.open(base + "/static/app.js", timeout=10) as resp:
                js = resp.read().decode("utf-8")
            check("save_config" in js, "前端调用了保存接口")
            check("get_config" in js, "前端调用了读取接口")
            check("pick_path" in js, "前端调用了路径选择接口")
            with OPENER.open(base + "/static/style.css", timeout=10) as resp:
                css = resp.read().decode("utf-8")
            check(".rule-table" in css, "样式包含规则表格样式")
        finally:
            web.stop()

        print()
        if failures:
            print(f"=== {len(failures)} 项失败 ===")
            for item in failures:
                print(f"  - {item}")
            return 1
        print("=== 配置编辑接口全部通过 ===")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
