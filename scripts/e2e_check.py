"""端到端验证脚本：模拟一张 SD 卡，跑通「插入 -> 读配置 -> 同步 -> 重复跳过 -> 看板」全链路。

用法::

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

# 本地请求绕过环境代理
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dsync.app import SyncApplication  # noqa: E402
from dsync.config import load_config_file  # noqa: E402

PASS, FAIL = "  [通过]", "  [失败]"
failures: list[str] = []


def check(condition: bool, message: str) -> None:
    if condition:
        print(f"{PASS} {message}")
    else:
        print(f"{FAIL} {message}")
        failures.append(message)


def build_fake_card(root: Path) -> Path:
    """构造一张模拟 SD 卡。"""
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
        print(f"临时工作目录: {workdir}\n")
        card = build_fake_card(workdir)
        backup = workdir / "BACKUP"
        print("[1] 模拟 SD 卡已就绪")
        check((card / "dsync.yml").is_file(), "SD 卡根目录存在 dsync.yml")

        # ---------------------------------------------------------- 配置解析
        print("\n[2] 解析 SD 卡配置")
        config = load_config_file(card / "dsync.yml", card_root=card)
        check(len(config.rules) == 2, f"解析出 2 条规则（实际 {len(config.rules)}）")
        check(config.destination_root == str(backup), "destination_root 正确")

        # ---------------------------------------------------------- 首次同步
        print("\n[3] 首次同步")
        app = SyncApplication()
        app.run_sync(card, config)
        snap = app.snapshot()
        stats = snap["stats"]
        check(stats["copied_files"] == 3, f"复制 3 个文件（实际 {stats['copied_files']}）")
        check(stats["skipped_files"] == 0, f"跳过 0 个（实际 {stats['skipped_files']}）")
        check((backup / "Photos" / "100CANON" / "IMG_001.jpg").is_file(),
              "照片已复制到 Photos/100CANON/")
        check((backup / "Docs" / "report.pdf").is_file(), "文档已复制到 Docs/")
        check(not (backup / "Docs" / "temp.bak").exists(),
              "被 exclude 排除的 temp.bak 未被复制")

        # ---------------------------------------------------------- 重复同步
        print("\n[4] 再次同步（应全部跳过重复）")
        app2 = SyncApplication()
        app2.run_sync(card, config)
        stats2 = app2.snapshot()["stats"]
        check(stats2["copied_files"] == 0, f"未重复复制（实际复制 {stats2['copied_files']}）")
        check(stats2["skipped_files"] == 3, f"跳过 3 个重复文件（实际 {stats2['skipped_files']}）")

        # ---------------------------------------------------------- 内容变更
        print("\n[5] SD 卡上文件内容变更后重新同步")
        (card / "Documents" / "report.pdf").write_bytes(b"%PDF-1.4 modified content " * 200)
        app3 = SyncApplication()
        app3.run_sync(card, config)
        stats3 = app3.snapshot()["stats"]
        check(stats3["copied_files"] == 0,
              f"内容不同时默认不覆盖，跳过（实际复制 {stats3['copied_files']}）")

        config_ow = load_config_file(card / "dsync.yml", card_root=card)
        config_ow.overwrite = True
        app4 = SyncApplication()
        app4.run_sync(card, config_ow)
        stats4 = app4.snapshot()["stats"]
        check(stats4["copied_files"] == 1,
              f"开启 overwrite 后复制 1 个变更文件（实际 {stats4['copied_files']}）")
        check((backup / "Docs" / "report.pdf").read_bytes().startswith(b"%PDF-1.4 modified"),
              "目标文件已被更新")

        # ---------------------------------------------------------- Web 看板
        print("\n[6] 启动 Web 看板并验证接口")
        app5 = SyncApplication()
        web = app5.start_web(port=8791)
        time.sleep(0.6)
        try:
            with OPENER.open(f"{web.url}api/state", timeout=5) as resp:
                state = json.loads(resp.read().decode("utf-8"))
            check(state.get("version") is not None, "/api/state 返回状态快照")
            check("devices" in state and "rules" in state, "快照包含设备与规则字段")

            with OPENER.open(f"{web.url}", timeout=5) as resp:
                html = resp.read().decode("utf-8")
            check("D-Sync" in html, "首页 HTML 正常返回")
            check("/static/app.js" in html, "首页引用了前端脚本")

            with OPENER.open(f"{web.url}static/app.js", timeout=5) as resp:
                js = resp.read().decode("utf-8")
            check("EventSource" in js, "前端使用 SSE 接收实时进度")

            req = urllib.request.Request(
                f"{web.url}api/rescan", data=b"{}", method="POST",
                headers={"Content-Type": "application/json"})
            with OPENER.open(req, timeout=5) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            check(result.get("ok") is True, "POST /api/rescan 调用成功")
        except Exception as exc:
            check(False, f"看板接口异常: {exc}")
        finally:
            web.stop()

        # ---------------------------------------------------------- 历史记录
        print("\n[7] 历史记录")
        app6 = SyncApplication()
        app6.run_sync(card, config)
        history = app6.snapshot()["history"]
        check(len(history) == 1, f"产生 1 条历史记录（实际 {len(history)}）")
        if history:
            check(history[0]["copied"] == 0 and history[0]["skipped"] == 3,
                  "历史记录统计正确")

        print()
        if failures:
            print(f"=== {len(failures)} 项失败 ===")
            for item in failures:
                print(f"  - {item}")
            return 1
        print("=== 全部检查通过 ===")
        return 0
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
