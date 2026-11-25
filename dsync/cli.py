"""Command-line entry point.

Usage examples::

    python -m dsync                    # run in the background (tray + dashboard)
    python -m dsync --no-tray          # do not show the tray icon
    python -m dsync --port 9000        # use a specific dashboard port
    python -m dsync --once             # scan once and exit (good for scheduled tasks)
    python -m dsync --sync E:\\        # sync the given drive now and exit
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
        description="D-Sync external-device sync daemon: detects USB sticks / card readers, reads the YAML config at the card root and syncs files.",
    )
    parser.add_argument("--version", action="version", version=f"D-Sync {VERSION}")
    parser.add_argument("--host", default="127.0.0.1", help="Dashboard bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="Dashboard port (default 8765; moves to the next free port when taken)")
    parser.add_argument("--poll-interval", type=float, default=3.0, help="Device polling interval in seconds (default 3)")
    parser.add_argument("--config-name", action="append", default=None,
                        help="Config file name to look for on the device; may be repeated (default %s)" % ", ".join(DEFAULT_CONFIG_NAMES))
    parser.add_argument("--no-tray", action="store_true", help="Do not show the system tray icon")
    parser.add_argument("--no-browser", action="store_true", help="Do not open the browser automatically on startup")
    parser.add_argument("--include-fixed", action="store_true",
                        help="Also scan removable devices that the OS reports as fixed disks (some card readers need this)")
    parser.add_argument("--once", action="store_true", help="Scan the devices once and exit")
    parser.add_argument("--sync", metavar="DRIVE", help="Sync the given drive immediately and exit, e.g. E:\\")
    parser.add_argument("--identify", action="store_true",
                        help="List the removable devices and their mount points / labels, then exit")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable debug logging")
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

    # ------------------------------------------------------------ identify mode
    if args.identify:
        return _identify(include_fixed=args.include_fixed)

    # ------------------------------------------------------------ one-shot sync mode
    if args.sync:
        root = Path(args.sync)
        if not root.exists():
            print(f"Path does not exist: {root}", file=sys.stderr)
            return 2
        config_path = find_config_file(root, config_names)
        if config_path is None:
            print(f"No config file at the root of {root} (tried: {', '.join(config_names)})", file=sys.stderr)
            return 2
        try:
            config = load_config_file(config_path, card_root=root)
        except Exception as exc:
            print(f"Failed to parse the config: {exc}", file=sys.stderr)
            return 2

        engine = SyncEngine(config, progress=_console_progress)
        stats = engine.run(root)
        print()
        print(f"Done: {stats.copied_files} copied, {stats.skipped_files} skipped, "
              f"{stats.failed_files} failed, took {stats.elapsed:.2f}s")
        return 0 if stats.failed_files == 0 else 1

    # ------------------------------------------------------------ background mode
    web = app.start_web(host=args.host, port=args.port)
    url = web.url
    logger.info("Dashboard URL: %s", url)

    if not args.once:
        app.start_monitor()

    if not args.no_browser:
        threading.Timer(1.0, lambda: open_url(url)).start()

    quit_event = threading.Event()

    def _handle_signal(signum, frame):  # pragma: no cover
        logger.info("Received signal %s, shutting down", signum)
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
            logger.info("Tray unavailable; running in console-only mode")

    print(f"D-Sync v{VERSION} started")
    print(f"Dashboard URL: {url}")
    print("Press Ctrl+C to quit")

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
        logger.info("Shutting down...")
        if tray:
            tray.stop()
        app.stop()
    return 0


def _identify(include_fixed: bool = False) -> int:
    """Print the devices the daemon would consider, so a card can be found by eye.

    Answers the question the dashboard cannot: which mount point is this card, and
    which config file name will it be looked up under.
    """
    from .devices import (
        is_external,
        is_removable,
        list_removable_drives,
        volume_label,
    )

    drives = list_removable_drives(include_fixed=include_fixed)
    if not drives:
        print("No removable devices found.")
        if not include_fixed:
            print("If your card reader reports the card as a fixed disk, retry with --include-fixed.")
        return 0

    print(f"{'MOUNT POINT':<40} {'LABEL':<20} {'CONFIG':<12} EXT REMOVABLE")
    for path in sorted(drives, key=lambda p: str(p)):
        config = "-"
        for name in DEFAULT_CONFIG_NAMES:
            try:
                if (path / name).is_file():
                    config = name
                    break
            except OSError:
                # An unreadable mount point (often /boot/efi) must not abort the listing.
                config = "?"
                break
        print(
            f"{str(path):<40} {volume_label(path):<20} {config:<12} "
            f"{'yes' if is_external(path) else 'no':<3} "
            f"{'yes' if is_removable(path) else 'no'}"
        )
    print()
    print("A device is synced only when CONFIG names a file that exists in its root.")
    return 0


def _console_progress(event: str, payload: dict) -> None:
    """Simple progress output for the command-line mode."""
    if event == "plan":
        print(f"Scanned {payload.get('total_files', 0)} files")
    elif event == "file_done":
        status = payload.get("status")
        mark = {"copied": "✓", "skipped": "=", "failed": "✗"}.get(status, "?")
        print(f"  {mark} {payload.get('source')}")
    elif event == "error":
        print(f"  ! {payload.get('message')}")


if __name__ == "__main__":
    raise SystemExit(main())
