"""应用主控：串联设备检测、配置加载、同步引擎与 Web 看板。

职责划分：
  * :class:`SyncApplication` 持有全局状态，向看板提供快照
  * 设备插入 -> 读取 SD 卡根目录 YAML -> 执行同步 -> 记录历史
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
    ConfigError,
    SyncConfig,
    config_to_editable,
    editable_to_yaml_text,
    find_config_file,
    load_config_file,
    parse_yaml_text,
    validate_destination,
)
from .devices import DeviceMonitor, list_external_drives, list_removable_drives, volume_label
from .events import EventBus
from .filedialog import PickerUnavailable, pick_path_for_rule
from .logging_setup import app_data_dir
from .syncer import SyncEngine
from .web.server import WebApp, WebServer, find_free_port

logger = logging.getLogger(__name__)

VERSION = "1.0.0"
APP_NAME = "D-Sync"


class SyncApplication:
    """同步守护程序主体。"""

    def __init__(
        self,
        config_names: tuple[str, ...] = ("dsync.yml", "dsync.yaml"),
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
        """页面上当前选中的设备路径（空串表示未选择）。"""
        self._phase = "idle"
        self._message = "等待设备插入…"
        self._stats: dict | None = None
        self._current_file: str | None = None
        self._done_files = 0
        self._total_files = 0
        self._history: list[dict] = []
        self._pending_enable: dict[str, bool] = {}
        """卡上还没有配置文件时，先记下用户设置的自动同步开关，创建配置时生效。"""
        self._engine: SyncEngine | None = None
        self._syncing = False
        self._monitor: DeviceMonitor | None = None
        self._web: WebServer | None = None
        self._stop_event = threading.Event()

    # ============================================================== 状态
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

    # ============================================================== 设备
    def on_device_inserted(self, path: Path) -> None:
        """检测到设备插入。"""
        key = str(path).upper().rstrip("\\")
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
                         message=f"检测到设备插入：{volume_label(path)}（{path}）")
        self._handle_device(path)

    def on_device_removed(self, path: Path) -> None:
        key = str(path).upper().rstrip("\\")
        with self._lock:
            self._devices.pop(key, None)
        self.bus.publish("device", action="removed", path=str(path),
                         message=f"检测到设备移除：{path}")

    def _handle_device(self, path: Path) -> None:
        """读取 SD 卡配置并按 enable 开关决定是否执行同步。

        配置来源只有一处：**SD 卡根目录的配置文件**。
        卡上没有配置文件时不做任何同步，等待用户在页面上创建。

        配置中 ``enable`` 为 False 时只加载规则，不自动同步 —— 需在页面手动点「立即同步」。
        """
        config_path = find_config_file(path, self.config_names)
        config: SyncConfig | None = None
        source_desc = ""

        if config_path is None:
            logger.info("设备 %s 根目录未找到配置文件（%s），等待在页面创建",
                        path, ", ".join(self.config_names))
            with self._lock:
                key = str(path).upper().rstrip("\\")
                if key in self._devices:
                    self._devices[key]["state"] = "detected"
                    self._devices[key]["config"] = ""
            self.bus.publish("device", action="no-config", path=str(path),
                             message=f"检测到 {path}，但卡上没有配置文件，等待在页面创建")
            return

        logger.info("在 %s 找到配置文件 %s", path, config_path.name)
        try:
            config = load_config_file(config_path, card_root=path)
            source_desc = config_path.name
        except ConfigError as exc:
            logger.error("解析配置失败: %s", exc)
            self.bus.publish("error", message=f"解析 {config_path.name} 失败：{exc}")
            with self._lock:
                key = str(path).upper().rstrip("\\")
                if key in self._devices:
                    self._devices[key]["state"] = "error"
                    self._devices[key]["config"] = config_path.name
            self._set_phase("error", f"配置解析失败：{exc}")
            return

        with self._lock:
            key = str(path).upper().rstrip("\\")
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
            logger.info("配置 %s 中 enable=false，仅加载规则不自动同步", source_desc)
            self.bus.publish("device", action="config-loaded", path=str(path),
                             message=f"已加载配置 {source_desc}（自动同步已关闭）")
            self._set_phase("idle", f"已加载 {source_desc}，自动同步已关闭，可手动触发")
            return

        self.bus.publish("device", action="config-loaded", path=str(path),
                         message=f"已加载配置 {source_desc}（{len(config.rules)} 条规则）")

        self.run_sync(path, config)

    # ============================================================== 同步
    def run_sync(self, card_root: Path, config: SyncConfig) -> None:
        """执行一次同步（同步执行于调用线程）。"""
        key = str(card_root).upper().rstrip("\\")
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
                            f"同步完成：复制 {stats.copied_files} 个，跳过 {stats.skipped_files} 个")
        except Exception as exc:
            logger.exception("同步失败")
            with self._lock:
                if key in self._devices:
                    self._devices[key]["state"] = "error"
                    self._devices[key]["started_at"] = 0.0
            self.bus.publish("error", message=f"同步失败：{exc}")
            self._set_phase("error", f"同步失败：{exc}")
        finally:
            with self._lock:
                self._syncing = False
                self._engine = None
                self._current_file = None

    def _make_progress(self, device_key: str):
        """为某个设备生成进度回调，把进度写进该设备自己的状态里。"""

        def _progress(event: str, payload: dict) -> None:
            self._on_progress(event, payload, device_key)

        return _progress

    def _on_progress(self, event: str, payload: dict, device_key: str = "") -> None:
        """同步引擎进度回调 -> 更新设备状态并转发到事件总线。"""
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
                if device_key and device_key in self._devices:
                    self._devices[device_key]["total_files"] = total
                    self._devices[device_key]["done_files"] = 0
            self.bus.publish("plan", **payload)
        elif event == "file_start":
            with self._lock:
                self._current_file = payload.get("source")
                done = max(0, payload.get("index", 1) - 1)
                self._done_files = done
                if device_key and device_key in self._devices:
                    self._devices[device_key]["done_files"] = done
            self.bus.publish("file_start", **payload)
        elif event == "file_done":
            status = payload.get("status")
            with self._lock:
                done = payload.get("index", self._done_files)
                self._done_files = done
                self._current_file = None
                if device_key and device_key in self._devices:
                    device = self._devices[device_key]
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
                if device_key and device_key in self._devices:
                    device = self._devices[device_key]
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

    # ============================================================== 操作
    def action_rescan(self, payload: dict) -> dict:
        """重新扫描可移动设备。"""
        found = list_removable_drives(include_fixed=self.include_fixed)
        with self._lock:
            known = set(self._devices)
        for path in found:
            key = str(path).upper().rstrip("\\")
            if key not in known:
                self.on_device_inserted(path)
        return {"scanned": len(found)}

    def action_list_devices(self, payload: dict) -> dict:
        """列出当前可用的外置设备（U 盘、读卡器等）。"""
        found = list_external_drives()
        with self._lock:
            known = set(self._devices)
        devices = []
        for path in found:
            key = str(path).upper().rstrip("\\")
            devices.append({
                "path": str(path),
                "name": volume_label(path),
                "known": key in known,
            })
        return {"devices": devices}

    def action_open_folder(self, payload: dict) -> dict:
        """用系统文件管理器打开配置根目录。"""
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
        """手动触发某个设备的同步。

        配置来源与自动同步一致：只认 SD 卡根目录的配置文件。
        无论配置中的 ``enable`` 是否开启，手动点击始终会执行。
        """
        path = payload.get("path")
        if not path:
            with self._lock:
                selected = self._selected_card
            path = selected or None
        if not path:
            return {"error": "缺少 path 参数"}

        root = Path(path)
        if not root.exists():
            return {"error": f"路径不存在：{root}"}

        config_path = find_config_file(root, self.config_names)
        if config_path is None:
            return {"error": f"{root} 中未找到配置文件，请先在页面上创建配置文件"}
        try:
            config = load_config_file(config_path, card_root=root)
        except ConfigError as exc:
            return {"error": str(exc)}

        if not config.active_rules():
            return {"error": "配置中没有启用的规则"}

        with self._lock:
            key = str(root).upper().rstrip("\\")
            if key in self._devices:
                self._devices[key]["state"] = "syncing"
        threading.Thread(target=self.run_sync, args=(root, config),
                         name="manual-sync", daemon=True).start()
        return {"started": True, "rules": len(config.active_rules())}

    def action_select_card(self, payload: dict) -> dict:
        """页面选择某张 SD 卡：切换当前卡片并返回它的配置概况。"""
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
                             message=f"已选择 {root}，该卡尚无配置文件")
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
                         message=f"已选择 {root}")
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
        """弹出系统原生窗口选择路径（源路径 / 目标路径）。

        对话框是模态阻塞的，放在独立线程里执行，
        避免占住 HTTP 工作线程影响 SSE 推送。
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
                outcome["error"] = f"无法打开选择窗口：{exc}。请直接手动输入路径。"
            except ValueError as exc:
                outcome["error"] = str(exc)
            except Exception as exc:  # pragma: no cover
                logger.exception("路径选择失败")
                outcome["error"] = f"选择窗口异常：{exc}"

        thread = threading.Thread(target=_worker, name="path-picker", daemon=True)
        thread.start()
        thread.join(timeout=300)

        if thread.is_alive():
            return {"error": "选择窗口超时未响应"}
        if "error" in outcome:
            return {"error": outcome["error"]}
        return outcome.get("result") or {"cancelled": True, "path": "", "absolute": ""}

    def action_set_enabled(self, payload: dict) -> dict:
        """切换某张卡的自动同步开关（enable），并就地写回配置文件。"""
        path = (payload.get("path") or "").strip()
        if not path:
            with self._lock:
                path = self._selected_card
        if not path:
            return {"error": "未指定 SD 卡路径"}

        root = Path(path)
        enabled = bool(payload.get("enable", True))

        config_path = find_config_file(root, self.config_names)
        if config_path is None:
            # 卡上还没有配置文件 —— 开关先记在内存里，创建配置文件时生效
            with self._lock:
                self._pending_enable[str(root)] = enabled
            self.bus.publish("config", action="enabled", path=str(root),
                             message=f"自动同步已{'开启' if enabled else '关闭'}（创建配置文件后生效）")
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
            return {"error": f"写入失败（SD 卡可能为只读）: {exc}"}

        with self._lock:
            key = str(root).upper().rstrip("\\")
            if key in self._devices:
                self._devices[key]["enable"] = config.enabled

        self.bus.publish("config", action="enabled", path=str(config_path),
                         message=f"自动同步已{'开启' if config.enabled else '关闭'}")
        return {"enable": config.enabled, "path": str(config_path)}

    # ============================================================== 配置编辑
    def _current_card(self) -> Path | None:
        """返回当前应当编辑配置的设备（优先页面选中的卡，其次最近插入且带配置的盘）。"""
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
        """读取待编辑的配置（只有 SD 卡上的配置文件）。"""
        card_path = payload.get("card_path") or ""

        # SD 卡配置
        if card_path:
            root = Path(card_path)
        else:
            root = self._current_card()
        if root is None:
            return {"exists": False, "path": "", "model": self._blank_model(),
                    "error": "未检测到 SD 卡"}

        config_path = find_config_file(root, self.config_names)
        if config_path is None:
            return {"exists": False, "path": str(root / self.config_names[0]),
                    "model": self._blank_model(), "card_root": str(root),
                    "hint": "该 SD 卡尚无配置文件，可在此编辑后保存到卡片"}

        try:
            config = load_config_file(config_path, card_root=root)
        except ConfigError as exc:
            return {"exists": True, "path": str(config_path), "error": str(exc),
                    "model": self._blank_model(), "card_root": str(root)}

        return {"exists": True, "path": str(config_path),
                "model": config_to_editable(config), "card_root": str(root)}

    def _blank_model(self) -> dict:
        """空白编辑模型 —— 不含任何预置规则，用户从零开始配置。"""
        return {
            "version": 1,
            "enable": True,
            "destination_root": "",
            "overwrite": False,
            "exclude": [],
            "rules": [],
        }

    def action_validate_config(self, payload: dict) -> dict:
        """校验前端提交的编辑模型，不落盘。"""
        model = payload.get("model") or {}
        try:
            text = editable_to_yaml_text(model)
            config = parse_yaml_text(text)
        except ConfigError as exc:
            return {"valid": False, "error": str(exc)}
        except Exception as exc:
            return {"valid": False, "error": f"配置无效: {exc}"}

        errors: list[str] = []
        card_path = payload.get("card_path") or ""
        card_root = Path(card_path) if card_path else None
        for rule in config.rules:
            error = validate_destination(str(config.resolve_destination(rule)), card_root)
            if error:
                errors.append(f"规则「{rule.source}」：{error}")
        if errors:
            return {"valid": False, "error": "；".join(errors)}
        return {"valid": True, "rules": len(config.rules), "yaml": text}

    def action_save_config(self, payload: dict) -> dict:
        """保存配置：写回 SD 卡的 YAML。"""
        model = payload.get("model") or {}
        card_path = payload.get("card_path") or ""

        try:
            text = editable_to_yaml_text(model)
            config = parse_yaml_text(text)
        except ConfigError as exc:
            return {"saved": False, "error": str(exc)}
        except Exception as exc:
            return {"saved": False, "error": f"配置无效: {exc}"}

        # 目标路径合法性检查
        card_root = Path(card_path) if card_path else None
        errors: list[str] = []
        for rule in config.rules:
            error = validate_destination(str(config.resolve_destination(rule)), card_root)
            if error:
                errors.append(f"规则「{rule.source}」：{error}")
        if errors:
            return {"saved": False, "error": "；".join(errors)}

        if card_root is None:
            return {"saved": False, "error": "未指定 SD 卡路径"}

        existing = find_config_file(card_root, self.config_names)
        path = existing or (card_root / self.config_names[0])
        try:
            path.write_text(text, encoding="utf-8")
        except OSError as exc:
            return {"saved": False, "error": f"写入失败（SD 卡可能为只读）: {exc}"}

        logger.info("配置已保存到 %s（%d 条规则）", path, len(config.rules))
        self.bus.publish("config", action="saved", path=str(path),
                         message=f"配置已保存到 {path.name}（{len(config.rules)} 条规则）")

        # 更新内存中的规则展示
        with self._lock:
            self._rules = [
                {"source": rule.source, "destination": str(config.resolve_destination(rule)),
                 "enabled": rule.enabled}
                for rule in config.rules
            ]
            self._pending_enable.pop(str(card_root), None)

        # 保存到 SD 卡后：enable 开启则立即重新同步，否则等用户手动触发
        if config.enabled and config.active_rules():
            threading.Thread(target=self.run_sync, args=(card_root, config),
                             name="config-save-sync", daemon=True).start()
        else:
            reason = "自动同步已关闭" if not config.enabled else "没有启用的规则"
            self.bus.publish("config", action="saved",
                             message=f"配置已保存（{reason}），可点击「立即同步」执行")
            self._set_phase("idle", f"配置已保存（{reason}）")

        return {"saved": True, "path": str(path), "rules": len(config.rules),
                "enable": config.enabled}

    def action_delete_config(self, payload: dict) -> dict:
        """删除 SD 卡上的配置文件。"""
        card_path = payload.get("card_path") or ""
        if not card_path:
            return {"deleted": False, "error": "未指定 SD 卡路径"}
        root = Path(card_path)
        config_path = find_config_file(root, self.config_names)
        if config_path is None:
            return {"deleted": False, "error": "该 SD 卡上没有配置文件"}
        try:
            config_path.unlink()
        except OSError as exc:
            return {"deleted": False, "error": f"删除失败: {exc}"}
        with self._lock:
            key = str(root).upper().rstrip("\\")
            if key in self._devices:
                self._devices[key]["config"] = ""
                self._devices[key]["state"] = "detected"
            self._rules = []
        self.bus.publish("config", action="deleted", path=str(config_path),
                         message=f"已删除 {config_path.name}")
        return {"deleted": True, "path": str(config_path)}

    def action_preview_config(self, payload: dict) -> dict:
        """把当前配置转成 YAML 文本，供页面预览/复制。"""
        result = self.action_get_config(payload)
        model = result.get("model") or self._blank_model()
        try:
            text = editable_to_yaml_text(model)
        except ConfigError as exc:
            return {"yaml": "", "error": str(exc)}
        return {"yaml": text, "path": result.get("path", "")}

    # ============================================================== 生命周期
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
            # 让系统直接分配一个空闲端口
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
        ignore = [Path(app_data_dir().anchor or "C:\\")]
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
