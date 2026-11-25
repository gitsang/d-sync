"""Application controller: wires together device detection, config loading, the
sync engine and the web dashboard.

Responsibilities:
  * :class:`SyncApplication` holds the global state and serves snapshots to the dashboard
  * device inserted -> read the YAML at the card root -> run the sync -> record history
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from .config import (
    DEFAULT_CONFIG_NAMES,
    LEGACY_CONFIG_NAME,
    ConfigError,
    SyncConfig,
    config_to_editable,
    editable_to_yaml_text,
    find_config_file,
    load_config_file,
    parse_yaml_text,
    validate_destination,
)
from .devices import (
    DeviceMonitor,
    device_key,
    list_external_drives,
    list_removable_drives,
    volume_label,
)
from .events import EventBus
from .filedialog import PickerUnavailable, pick_path_for_rule
from .logging_setup import app_data_dir
from .syncer import SyncEngine
from .web.server import WebApp, WebServer, find_free_port

logger = logging.getLogger(__name__)

VERSION = "1.1.0"
APP_NAME = "D-Sync"


class SyncApplication:
    """The sync daemon itself."""

    def __init__(
        self,
        config_names: tuple[str, ...] = DEFAULT_CONFIG_NAMES,
        poll_interval: float = 3.0,
        include_fixed: bool = False,
        open_browser: bool = False,
    ) -> None:
        self.bus = EventBus()
        self.config_names = config_names
        self.poll_interval = poll_interval
        self.include_fixed = include_fixed
        self.open_browser = open_browser

        self._lock = threading.RLock()
        self._devices: dict[str, dict] = {}
        self._rules: list[dict] = []
        self._selected_card: str = ""
        """The device path currently selected on the page (empty when nothing is selected)."""
        self._phase = "idle"
        self._message = "Waiting for a device..."
        self._stats: dict | None = None
        self._current_file: str | None = None
        self._done_files = 0
        self._total_files = 0
        self._history: list[dict] = []
        self._pending_enable: dict[str, bool] = {}
        """When the card has no config file yet, remember the auto-sync toggle the user
        sets and apply it once the config is created."""
        self._engine: SyncEngine | None = None
        self._syncing = False
        self._monitor: DeviceMonitor | None = None
        self._web: WebServer | None = None
        self._stop_event = threading.Event()

    # ============================================================== state
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "phase": self._phase,
                "message": self._message,
                "devices": list(self._devices.values()),
                "rules": list(self._rules),
                "selected_card": self._selected_card,
                "stats": self._stats,
                "current_file": self._current_file,
                "total_files": self._total_files,
                "done_files": self._done_files,
                "history": list(self._history[-20:]),
                "version": VERSION,
                "app_name": APP_NAME,
                "syncing": self._syncing,
            }

    def _set_phase(self, phase: str, message: str = "") -> None:
        with self._lock:
            self._phase = phase
            if message:
                self._message = message
        self.bus.publish("state", state=self.snapshot())

    # ============================================================== devices
    def on_device_inserted(self, path: Path) -> None:
        """A device was inserted."""
        key = device_key(path)
        with self._lock:
            self._devices[key] = {
                "path": str(path),
                "name": volume_label(path),
                "state": "detected",
                "config": "",
                "enable": True,
                "total_files": 0,
                "done_files": 0,
                "skipped_files": 0,
                "copied_files": 0,
                "elapsed": 0.0,
                "started_at": 0.0,
            }
        self.bus.publish("device", action="inserted", path=str(path),
                         message=f"Device inserted: {volume_label(path)} ({path})")
        self._handle_device(path)

    def on_device_removed(self, path: Path) -> None:
        key = device_key(path)
        with self._lock:
            self._devices.pop(key, None)
        self.bus.publish("device", action="removed", path=str(path),
                         message=f"Device removed: {path}")

    def _handle_device(self, path: Path) -> None:
        """Read the card's config and decide from the enable flag whether to sync.

        There is exactly one source of configuration: **the config file at the card
        root**. When the card has no config file nothing is synced; the tool waits for
        the user to create one on the page.

        When ``enable`` is False the rules are only loaded and no automatic sync runs --
        the user has to press "Sync now" on the page.
        """
        config_path = find_config_file(path, self.config_names)
        config: SyncConfig | None = None
        source_desc = ""

        if config_path is None:
            logger.info("No config file at the root of device %s (%s); waiting for one to be created on the page",
                        path, ", ".join(self.config_names))
            with self._lock:
                key = device_key(path)
                if key in self._devices:
                    self._devices[key]["state"] = "detected"
                    self._devices[key]["config"] = ""
            self.bus.publish("device", action="no-config", path=str(path),
                             message=f"Detected {path}, but the card has no config file; create one on the page")
            return

        logger.info("Found config file %s in %s", config_path.name, path)
        try:
            config = load_config_file(config_path, card_root=path)
            source_desc = config_path.name
        except ConfigError as exc:
            logger.error("Failed to parse the config: %s", exc)
            self.bus.publish("error", message=f"Failed to parse {config_path.name}: {exc}")
            with self._lock:
                key = device_key(path)
                if key in self._devices:
                    self._devices[key]["state"] = "error"
                    self._devices[key]["config"] = config_path.name
            self._set_phase("error", f"Config parsing failed: {exc}")
            return

        with self._lock:
            key = device_key(path)
            if key in self._devices:
                self._devices[key]["config"] = source_desc
                self._devices[key]["enable"] = config.enabled
                self._devices[key]["state"] = "detected" if not config.enabled else "syncing"
            self._rules = [
                {"source": rule.source, "destination": str(config.resolve_destination(rule)),
                 "enabled": rule.enabled}
                for rule in config.rules
            ]

        if not config.enabled:
            logger.info("enable=false in config %s; loading rules without auto-sync", source_desc)
            self.bus.publish("device", action="config-loaded", path=str(path),
                             message=f"Loaded config {source_desc} (auto-sync is off)")
            self._set_phase("idle", f"Loaded {source_desc}; auto-sync is off, sync can be triggered manually")
            return

        self.bus.publish("device", action="config-loaded", path=str(path),
                         message=f"Loaded config {source_desc} ({len(config.rules)} rules)")

        self.run_sync(path, config)

    # ============================================================== sync
    def run_sync(self, card_root: Path, config: SyncConfig) -> None:
        """Run one sync (executed synchronously on the calling thread)."""
        key = device_key(card_root)
        engine = SyncEngine(config, progress=self._make_progress(key))
        started = time.time()
        with self._lock:
            self._engine = engine
            self._syncing = True
            self._stats = None
            self._done_files = 0
            self._total_files = 0
            self._current_file = None
            if key in self._devices:
                device = self._devices[key]
                device["total_files"] = 0
                device["done_files"] = 0
                device["skipped_files"] = 0
                device["copied_files"] = 0
                device["elapsed"] = 0.0
                device["started_at"] = started
                device["state"] = "syncing"

        try:
            stats = engine.run(card_root)
            status = "cancelled" if engine.cancelled else "ok"
            with self._lock:
                self._stats = stats.to_dict()
                if key in self._devices:
                    device = self._devices[key]
                    device["state"] = "synced" if status == "ok" else "detected"
                    device["total_files"] = stats.total_files
                    device["done_files"] = stats.copied_files + stats.skipped_files + stats.failed_files
                    device["skipped_files"] = stats.skipped_files
                    device["copied_files"] = stats.copied_files
                    device["elapsed"] = round(stats.elapsed, 2)
                    device["started_at"] = 0.0
                self._history.append({
                    "ts": started,
                    "device": volume_label(card_root),
                    "path": str(card_root),
                    "status": status,
                    "copied": stats.copied_files,
                    "skipped": stats.skipped_files,
                    "failed": stats.failed_files,
                    "elapsed": round(stats.elapsed, 2),
                })
                del self._history[:-100]
            self._set_phase("done",
                            f"Sync finished: {stats.copied_files} copied, {stats.skipped_files} skipped")
        except Exception as exc:
            logger.exception("Sync failed")
            with self._lock:
                if key in self._devices:
                    self._devices[key]["state"] = "error"
                    self._devices[key]["started_at"] = 0.0
            self.bus.publish("error", message=f"Sync failed: {exc}")
            self._set_phase("error", f"Sync failed: {exc}")
        finally:
            with self._lock:
                self._syncing = False
                self._engine = None
                self._current_file = None

    def _make_progress(self, dev_key: str):
        """Build a progress callback for one device that writes progress into its own state."""

        def _progress(event: str, payload: dict) -> None:
            self._on_progress(event, payload, dev_key)

        return _progress

    def _on_progress(self, event: str, payload: dict, dev_key: str = "") -> None:
        """Sync engine progress callback -> update the device state and republish on the bus."""
        if event == "phase":
            phase = payload.get("phase", "copying")
            with self._lock:
                self._phase = "copying" if phase == "copying" else self._phase
                self._message = payload.get("message", "")
            self.bus.publish("phase", phase=phase, message=payload.get("message", ""))
        elif event == "plan":
            total = payload.get("total_files", 0)
            with self._lock:
                self._total_files = total
                self._done_files = 0
                if dev_key and dev_key in self._devices:
                    self._devices[dev_key]["total_files"] = total
                    self._devices[dev_key]["done_files"] = 0
            self.bus.publish("plan", **payload)
        elif event == "file_start":
            with self._lock:
                self._current_file = payload.get("source")
                done = max(0, payload.get("index", 1) - 1)
                self._done_files = done
                if dev_key and dev_key in self._devices:
                    self._devices[dev_key]["done_files"] = done
            self.bus.publish("file_start", **payload)
        elif event == "file_done":
            status = payload.get("status")
            with self._lock:
                done = payload.get("index", self._done_files)
                self._done_files = done
                self._current_file = None
                if dev_key and dev_key in self._devices:
                    device = self._devices[dev_key]
                    device["done_files"] = done
                    if status == "skipped":
                        device["skipped_files"] = device.get("skipped_files", 0) + 1
                    elif status == "copied":
                        device["copied_files"] = device.get("copied_files", 0) + 1
            self.bus.publish("file_done", **payload)
        elif event == "finished":
            stats = payload.get("stats") or {}
            with self._lock:
                self._stats = stats
                if dev_key and dev_key in self._devices:
                    device = self._devices[dev_key]
                    device["total_files"] = stats.get("total_files", device.get("total_files", 0))
                    device["done_files"] = (stats.get("copied_files", 0)
                                           + stats.get("skipped_files", 0)
                                           + stats.get("failed_files", 0))
                    device["skipped_files"] = stats.get("skipped_files", device.get("skipped_files", 0))
                    device["copied_files"] = stats.get("copied_files", device.get("copied_files", 0))
                    device["elapsed"] = stats.get("elapsed", device.get("elapsed", 0.0))
            self.bus.publish("finished", **payload)
        elif event == "error":
            self.bus.publish("error", **payload)
        elif event == "cancelled":
            self.bus.publish("cancelled", **payload)

    # ============================================================== actions
    def action_rescan(self, payload: dict) -> dict:
        """Rescan for removable devices."""
        found = list_removable_drives(include_fixed=self.include_fixed)
        with self._lock:
            known = set(self._devices)
        for path in found:
            key = device_key(path)
            if key not in known:
                self.on_device_inserted(path)
        return {"scanned": len(found)}

    def action_list_devices(self, payload: dict) -> dict:
        """List the external devices currently available (USB sticks, card readers, ...)."""
        found = list_external_drives()
        with self._lock:
            known = set(self._devices)
        devices = []
        for path in found:
            key = device_key(path)
            devices.append({
                "path": str(path),
                "name": volume_label(path),
                "known": key in known,
            })
        return {"devices": devices}

    def action_open_folder(self, payload: dict) -> dict:
        """Open the config root directory in the system file manager."""
        target = payload.get("path") or ""
        if not target:
            with self._lock:
                devices = list(self._devices.values())
            target = devices[0]["path"] if devices else str(app_data_dir())
        try:
            if sys.platform == "win32":
                os.startfile(target)  # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", target])
            else:
                subprocess.Popen(["xdg-open", target])
            return {"opened": target}
        except Exception as exc:
            return {"error": str(exc)}

    def action_cancel(self, payload: dict) -> dict:
        with self._lock:
            engine = self._engine
        if engine:
            engine.cancel()
            return {"cancelled": True}
        return {"cancelled": False}

    def action_sync_now(self, payload: dict) -> dict:
        """Manually trigger a sync for one device.

        The config source is the same as for auto-sync: only the config file at the card
        root is used. A manual click always runs, regardless of ``enable``.
        """
        path = payload.get("path")
        if not path:
            with self._lock:
                selected = self._selected_card
            path = selected or None
        if not path:
            return {"error": "Missing the path parameter"}

        root = Path(path)
        if not root.exists():
            return {"error": f"Path does not exist: {root}"}

        config_path = find_config_file(root, self.config_names)
        if config_path is None:
            return {"error": f"No config file found in {root}; create one on the page first"}
        try:
            config = load_config_file(config_path, card_root=root)
        except ConfigError as exc:
            return {"error": str(exc)}

        if not config.active_rules():
            return {"error": "The config has no enabled rules"}

        with self._lock:
            key = device_key(root)
            if key in self._devices:
                self._devices[key]["state"] = "syncing"
        threading.Thread(target=self.run_sync, args=(root, config),
                         name="manual-sync", daemon=True).start()
        return {"started": True, "rules": len(config.active_rules())}

    def action_select_card(self, payload: dict) -> dict:
        """Select a card on the page: switch the current card and return its config summary."""
        path = (payload.get("path") or "").strip()
        if not path:
            with self._lock:
                self._selected_card = ""
            return {"selected": ""}

        root = Path(path)
        with self._lock:
            self._selected_card = str(root)

        config_path = find_config_file(root, self.config_names)
        if config_path is None:
            with self._lock:
                pending = self._pending_enable.get(str(root), True)
            self.bus.publish("device", action="selected", path=str(root),
                             message=f"Selected {root}; the card has no config file yet")
            return {"selected": str(root), "has_config": False, "source": "",
                    "enable": pending}

        source = "card"
        try:
            config = load_config_file(config_path, card_root=root)
        except ConfigError as exc:
            return {"selected": str(root), "has_config": True, "source": source,
                    "error": str(exc)}

        with self._lock:
            self._rules = [
                {"source": rule.source, "destination": str(config.resolve_destination(rule)),
                 "enabled": rule.enabled}
                for rule in config.rules
            ]

        self.bus.publish("device", action="selected", path=str(root),
                         message=f"Selected {root}")
        return {
            "selected": str(root),
            "has_config": True,
            "source": source,
            "enable": config.enabled,
            "rules": len(config.rules),
            "active_rules": len(config.active_rules()),
            "config_path": str(config_path),
        }

    def action_pick_path(self, payload: dict) -> dict:
        """Open a native path picker (source path / destination path).

        The dialog is modal and blocks, so it runs on its own thread to avoid tying up
        the HTTP worker thread and stalling SSE delivery.
        """
        kind = payload.get("kind") or "destination"
        card_path = payload.get("card_path") or ""
        current = payload.get("current") or ""

        if not card_path:
            with self._lock:
                card_path = self._selected_card

        outcome: dict = {}

        def _worker() -> None:
            try:
                outcome["result"] = pick_path_for_rule(
                    kind, card_path or None, current)
            except PickerUnavailable as exc:
                outcome["error"] = f"Could not open the picker: {exc}. Enter the path manually instead."
            except ValueError as exc:
                outcome["error"] = str(exc)
            except Exception as exc:  # pragma: no cover
                logger.exception("Path selection failed")
                outcome["error"] = f"Path picker error: {exc}"

        thread = threading.Thread(target=_worker, name="path-picker", daemon=True)
        thread.start()
        thread.join(timeout=300)

        if thread.is_alive():
            return {"error": "The path picker timed out"}
        if "error" in outcome:
            return {"error": outcome["error"]}
        return outcome.get("result") or {"cancelled": True, "path": "", "absolute": ""}

    def action_set_enabled(self, payload: dict) -> dict:
        """Toggle a card's auto-sync flag (enable) and write it back to the config in place."""
        path = (payload.get("path") or "").strip()
        if not path:
            with self._lock:
                path = self._selected_card
        if not path:
            return {"error": "No SD card path given"}

        root = Path(path)
        enabled = bool(payload.get("enable", True))

        config_path = find_config_file(root, self.config_names)
        if config_path is None:
            # The card has no config file yet -- remember the toggle in memory and
            # apply it when the config is created
            with self._lock:
                self._pending_enable[str(root)] = enabled
            self.bus.publish("config", action="enabled", path=str(root),
                             message=f"Auto-sync {"enabled" if enabled else "disabled"} (takes effect once a config file exists)")
            return {"enable": enabled, "path": str(root), "pending": True}

        try:
            config = load_config_file(config_path, card_root=root)
        except ConfigError as exc:
            return {"error": str(exc)}

        config.enabled = enabled
        try:
            text = editable_to_yaml_text(config_to_editable(config))
            config_path.write_text(text, encoding="utf-8")
        except OSError as exc:
            return {"error": f"Write failed (the SD card may be read-only): {exc}"}

        with self._lock:
            key = device_key(root)
            if key in self._devices:
                self._devices[key]["enable"] = config.enabled

        self.bus.publish("config", action="enabled", path=str(config_path),
                         message=f"Auto-sync {"enabled" if config.enabled else "disabled"}")
        return {"enable": config.enabled, "path": str(config_path)}

    # ============================================================== config editing
    def _current_card(self) -> Path | None:
        """Return the device whose config should be edited (the card selected on the page
        first, otherwise the most recently inserted drive that has a config)."""
        with self._lock:
            if self._selected_card:
                return Path(self._selected_card)
            for device in reversed(list(self._devices.values())):
                if device.get("config"):
                    return Path(device["path"])
            for device in reversed(list(self._devices.values())):
                return Path(device["path"])
        return None

    def action_get_config(self, payload: dict) -> dict:
        """Load the config to be edited (only a config file on the card)."""
        card_path = payload.get("card_path") or ""

        # config on the SD card
        if card_path:
            root = Path(card_path)
        else:
            root = self._current_card()
        if root is None:
            return {"exists": False, "path": "", "model": self._blank_model(),
                    "error": "No SD card detected"}

        config_path = find_config_file(root, self.config_names)
        if config_path is None:
            return {"exists": False, "path": str(root / self.config_names[0]),
                    "model": self._blank_model(), "card_root": str(root),
                    "hint": "This SD card has no config file yet; edit one here and save it to the card"}

        try:
            config = load_config_file(config_path, card_root=root)
        except ConfigError as exc:
            return {"exists": True, "path": str(config_path), "error": str(exc),
                    "model": self._blank_model(), "card_root": str(root)}

        return {"exists": True, "path": str(config_path),
                "model": config_to_editable(config), "card_root": str(root)}

    def _blank_model(self) -> dict:
        """A blank edit model -- no preset rules, the user starts from scratch."""
        return {
            "version": 1,
            "enable": True,
            "destination_root": "",
            "overwrite": False,
            "exclude": [],
            "rules": [],
        }

    def action_validate_config(self, payload: dict) -> dict:
        """Validate the edit model submitted by the front end without writing it."""
        model = payload.get("model") or {}
        try:
            text = editable_to_yaml_text(model)
            config = parse_yaml_text(text)
        except ConfigError as exc:
            return {"valid": False, "error": str(exc)}
        except Exception as exc:
            return {"valid": False, "error": f"Invalid config: {exc}"}

        errors: list[str] = []
        card_path = payload.get("card_path") or ""
        card_root = Path(card_path) if card_path else None
        for rule in config.rules:
            error = validate_destination(str(config.resolve_destination(rule)), card_root)
            if error:
                errors.append(f"Rule \"{rule.source}\": {error}")
        if errors:
            return {"valid": False, "error": "; ".join(errors)}
        return {"valid": True, "rules": len(config.rules), "yaml": text}

    def action_save_config(self, payload: dict) -> dict:
        """Save the config: write the YAML back to the SD card."""
        model = payload.get("model") or {}
        card_path = payload.get("card_path") or ""

        try:
            text = editable_to_yaml_text(model)
            config = parse_yaml_text(text)
        except ConfigError as exc:
            return {"saved": False, "error": str(exc)}
        except Exception as exc:
            return {"saved": False, "error": f"Invalid config: {exc}"}

        # Validate the destination paths
        card_root = Path(card_path) if card_path else None
        errors: list[str] = []
        for rule in config.rules:
            error = validate_destination(str(config.resolve_destination(rule)), card_root)
            if error:
                errors.append(f"Rule \"{rule.source}\": {error}")
        if errors:
            return {"saved": False, "error": "; ".join(errors)}

        if card_root is None:
            return {"saved": False, "error": "No SD card path given"}

        existing = find_config_file(card_root, self.config_names)
        migrated_from: str = ""
        if existing is not None and existing.name == LEGACY_CONFIG_NAME:
            # The dashboard edits YAML. Writing YAML back into the legacy key=value file
            # would make it unparseable on the next insert, so save as YAML and retire the
            # old file instead.
            migrated_from = existing.name
            path = card_root / self.config_names[0]
        else:
            path = existing or (card_root / self.config_names[0])
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            return {"saved": False, "error": f"Write failed (the SD card may be read-only): {exc}"}

        if migrated_from:
            try:
                (card_root / migrated_from).unlink()
                logger.info("Migrated the legacy %s to %s", migrated_from, path.name)
                self.bus.publish("config", action="migrated",
                                 message=f"Migrated {migrated_from} to {path.name}")
            except OSError as exc:
                logger.warning("Could not remove the legacy %s: %s", migrated_from, exc)

        logger.info("Config saved to %s (%d rules)", path, len(config.rules))
        self.bus.publish("config", action="saved", path=str(path),
                         message=f"Config saved to {path.name} ({len(config.rules)} rules)")

        # Refresh the in-memory rule view
        with self._lock:
            self._rules = [
                {"source": rule.source, "destination": str(config.resolve_destination(rule)),
                 "enabled": rule.enabled}
                for rule in config.rules
            ]
            self._pending_enable.pop(str(card_root), None)

        # After saving to the card: resync immediately when enable is on, otherwise wait
        # for the user to trigger it
        if config.enabled and config.active_rules():
            threading.Thread(target=self.run_sync, args=(card_root, config),
                             name="config-save-sync", daemon=True).start()
        else:
            reason = "auto-sync is off" if not config.enabled else "no enabled rules"
            self.bus.publish("config", action="saved",
                             message=f"Config saved ({reason}); press \"Sync now\" to run one")
            self._set_phase("idle", f"Config saved ({reason})")

        return {"saved": True, "path": str(path), "rules": len(config.rules),
                "enable": config.enabled}

    def action_delete_config(self, payload: dict) -> dict:
        """Delete the config file on the SD card."""
        card_path = payload.get("card_path") or ""
        if not card_path:
            return {"deleted": False, "error": "No SD card path given"}
        root = Path(card_path)
        config_path = find_config_file(root, self.config_names)
        if config_path is None:
            return {"deleted": False, "error": "There is no config file on this SD card"}
        try:
            config_path.unlink()
        except OSError as exc:
            return {"deleted": False, "error": f"Delete failed: {exc}"}
        with self._lock:
            key = device_key(root)
            if key in self._devices:
                self._devices[key]["config"] = ""
                self._devices[key]["state"] = "detected"
            self._rules = []
        self.bus.publish("config", action="deleted", path=str(config_path),
                         message=f"Deleted {config_path.name}")
        return {"deleted": True, "path": str(config_path)}

    def action_preview_config(self, payload: dict) -> dict:
        """Render the current config as YAML text for preview/copy on the page."""
        result = self.action_get_config(payload)
        model = result.get("model") or self._blank_model()
        try:
            text = editable_to_yaml_text(model)
        except ConfigError as exc:
            return {"yaml": "", "error": str(exc)}
        return {"yaml": text, "path": result.get("path", "")}

    # ============================================================== lifecycle
    def start_web(self, host: str = "127.0.0.1", port: int = 8765) -> WebServer:
        actions = {
            "rescan": self.action_rescan,
            "list_devices": self.action_list_devices,
            "open_folder": self.action_open_folder,
            "cancel": self.action_cancel,
            "sync_now": self.action_sync_now,
            "select_card": self.action_select_card,
            "pick_path": self.action_pick_path,
            "set_enabled": self.action_set_enabled,
            "get_config": self.action_get_config,
            "validate_config": self.action_validate_config,
            "save_config": self.action_save_config,
            "delete_config": self.action_delete_config,
            "preview_config": self.action_preview_config,
        }
        web_app = WebApp(self.bus, self.snapshot, actions, VERSION)
        if port == 0:
            # Let the OS pick a free port
            actual_port = 0
        else:
            try:
                actual_port = find_free_port(port, host)
            except RuntimeError:
                actual_port = port
        self._web = WebServer(web_app, host, actual_port)
        self._web.start()
        return self._web

    def start_monitor(self) -> None:
        # Never treat the volume holding the app's own data dir as a card. On POSIX the
        # anchor of that path is "/", i.e. the root filesystem -- correct, because the
        # root is never a removable card. On Windows it is the system drive ("C:\\").
        ignore = [Path(app_data_dir().anchor or os.sep)]
        self._monitor = DeviceMonitor(
            on_inserted=self.on_device_inserted,
            on_removed=self.on_device_removed,
            poll_interval=self.poll_interval,
            ignore=ignore,
            include_fixed=self.include_fixed,
        )
        self._monitor.start()

    def stop(self) -> None:
        self._stop_event.set()
        with self._lock:
            engine = self._engine
        if engine:
            engine.cancel()
        if self._monitor:
            self._monitor.stop()
        if self._web:
            self._web.stop()
