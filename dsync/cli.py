"""命令行入口。

用法示例::

    python -m dsync                    # 后台运行（托盘 + 看板）
    python -m dsync --no-tray          # 不显示托盘图标
    python -m dsync --port 9000        # 指定看板端口
    python -m dsync --once             # 扫描一次后退出（适合计划任务）
    python -m dsync --sync E:\\        # 立即同步指定盘并退出
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
import time
from pathlib import Path

from .app import VERSION, SyncApplication
from .config import DEFAULT_CONFIG_NAMES, find_config_file, load_config_file
from .logging_setup import setup_logging
from .syncer import SyncEngine
from .tray import TrayIcon, open_url

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dsync",
        description="D-Sync 外置设备自动同步守护程序：检测 U 盘/读卡器，读取根目录 YAML 配置并同步文件。",
    )
    parser.add_argument("--version", action="version", version=f"D-Sync {VERSION}")
    parser.add_argument("--host", default="127.0.0.1", help="看板监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="看板监听端口（默认 8765，被占用时自动顺延）")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="设备轮询间隔秒数（默认 3）")
    parser.add_argument("--config-name", action="append", default=None,
                        help="设备上的配置文件名，可重复指定（默认 %s）" % ", ".join(DEFAULT_CONFIG_NAMES))
    parser.add_argument("--no-tray", action="store_true", help="不显示系统托盘图标")
    parser.add_argument("--no-browser", action="store_true", help="启动时不自动打开浏览器")
    parser.add_argument("--include-fixed", action="store_true",
                        help="同时扫描被识别为固定磁盘的可移动设备（部分读卡器需要）")
    parser.add_argument("--once", action="store_true", help="扫描一次设备后退出")
    parser.add_argument("--sync", metavar="DRIVE", help="立即同步指定盘符并退出，如 E:\\")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose, console=not args.no_tray)

    config_names = tuple(args.config_name) if args.config_name else DEFAULT_CONFIG_NAMES
    app = SyncApplication(
        config_names=config_names,
        poll_interval=args.poll_interval,
        include_fixed=args.include_fixed,
    )

    # ------------------------------------------------------------ 单次同步模式
    if args.sync:
        root = Path(args.sync)
        if not root.exists():
            print(f"路径不存在: {root}", file=sys.stderr)
            return 2
        config_path = find_config_file(root, config_names)
        if config_path is None:
            print(f"{root} 根目录未找到配置文件（尝试过: {', '.join(config_names)}）", file=sys.stderr)
            return 2
        try:
            config = load_config_file(config_path, card_root=root)
        except Exception as exc:
            print(f"配置解析失败: {exc}", file=sys.stderr)
            return 2

        engine = SyncEngine(config, progress=_console_progress)
        stats = engine.run(root)
        print()
        print(f"完成：复制 {stats.copied_files} 个，跳过 {stats.skipped_files} 个，"
              f"失败 {stats.failed_files} 个，耗时 {stats.elapsed:.2f}s")
        return 0 if stats.failed_files == 0 else 1

    # ------------------------------------------------------------ 后台模式
    web = app.start_web(host=args.host, port=args.port)
    url = web.url
    logger.info("看板地址: %s", url)

    if not args.once:
        app.start_monitor()

    if not args.no_browser:
        threading.Timer(1.0, lambda: open_url(url)).start()

    quit_event = threading.Event()

    def _handle_signal(signum, frame):  # pragma: no cover
        logger.info("收到信号 %s，准备退出", signum)
        quit_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _handle_signal)
        except (ValueError, OSError):  # pragma: no cover
            pass

    tray = None
    if not args.no_tray and not args.once:
        tray = TrayIcon(
            on_open=lambda: open_url(url),
            on_rescan=lambda: app.action_rescan({}),
            on_cancel=lambda: app.action_cancel({}),
            on_quit=lambda: quit_event.set(),
        )
        if not tray.start():
            logger.info("托盘不可用，进入纯控制台模式")

    print(f"D-Sync v{VERSION} 已启动")
    print(f"看板地址: {url}")
    print("按 Ctrl+C 退出")

    if args.once:
        time.sleep(1.0)
        app.stop()
        return 0

    try:
        while not quit_event.is_set():
            quit_event.wait(0.5)
    except KeyboardInterrupt:  # pragma: no cover
        pass
    finally:
        logger.info("正在退出…")
        if tray:
            tray.stop()
        app.stop()
    return 0


def _console_progress(event: str, payload: dict) -> None:
    """命令行模式下的简易进度输出。"""
    if event == "plan":
        print(f"扫描到 {payload.get('total_files', 0)} 个文件")
    elif event == "file_done":
        status = payload.get("status")
        mark = {"copied": "✓", "skipped": "=", "failed": "✗"}.get(status, "?")
        print(f"  {mark} {payload.get('source')}")
    elif event == "error":
        print(f"  ! {payload.get('message')}")


if __name__ == "__main__":
    raise SystemExit(main())
