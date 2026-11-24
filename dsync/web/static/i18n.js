/* D-Sync dashboard i18n.
 *
 * English is the default. The choice is persisted in localStorage under
 * "dsync.lang" and applied to elements carrying data-i18n / data-i18n-title /
 * data-i18n-placeholder as well as to any string rendered from JS via t().
 */
(function (global) {
  "use strict";

  var STRINGS = {
    en: {
      "app.title": "D-Sync · Sync Dashboard",
      "app.heading": "D-Sync Sync Dashboard",
      "app.subtitle": "Connecting…",
      "app.footer": "This page is served locally on 127.0.0.1 only",

      "conn.connecting": "Connecting",
      "conn.live": "Live",
      "conn.error": "Disconnected, retrying",

      "action.rescan": "Rescan",
      "action.openFolder": "Open folder",
      "action.refreshList": "Refresh list",
      "action.syncNow": "Sync now",
      "action.cancelSync": "Cancel sync",
      "action.validate": "Validate",
      "action.saveConfig": "Save config",
      "action.addRule": "+ Add rule",
      "action.copy": "Copy",
      "action.createConfig": "Create config file",
      "action.clear": "Clear",
      "action.browse": "Browse…",
      "action.delete": "Delete",

      "section.devices": "Device status",
      "section.selectDevice": "Select device",
      "section.config": "Sync configuration",
      "section.rules": "Rule list",
      "section.yaml": "YAML preview",
      "section.noConfig": "No config file yet",
      "section.log": "Activity log",
      "section.history": "History",

      "col.device": "Device",
      "col.state": "State",
      "col.progress": "Progress",
      "col.elapsed": "Elapsed",
      "col.source": "Source (on device)",
      "col.destination": "Destination",
      "col.exclude": "Exclude",
      "col.enabled": "Enabled",
      "col.overwrite": "Overwrite",
      "col.time": "Time",
      "col.copied": "Copied",
      "col.skipped": "Skipped",

      "devices.empty": "No devices detected",
      "devices.selectHint": "Only external devices (USB sticks, card readers, ...) are listed.",
      "devices.notSelected": "None selected",
      "devices.choose": "— Select a device —",
      "devices.noConfigMark": " (no config)",
      "devices.hasConfigMark": " ✓",

      "state.detected": "Detected",
      "state.syncing": "Syncing",
      "state.synced": "Synced",
      "state.error": "Error",

      "phase.idle": "Idle",
      "phase.planning": "Scanning files",
      "phase.copying": "Syncing",
      "phase.done": "Finished",
      "phase.error": "Error",
      "phase.cancelled": "Cancelled",

      "config.targetTag": "Device config",
      "config.unsavedTag": "Unsaved changes",
      "config.autoSync": "Auto-sync",
      "config.autoSyncHint": "When on, inserting this device starts a sync automatically; when off, press \"Sync now\".",
      "config.global": "Global settings",
      "config.destRoot": "Destination root",
      "config.destRootPlaceholder": "e.g. D:/SD_Backup",
      "config.destRootHint": "Relative destinations in the rules resolve against this directory",
      "config.browseFolder": "Browse for a folder",
      "config.exclude": "Global excludes",
      "config.excludeHint": "Comma separated, applied to every rule",
      "config.overwrite": "Overwrite when content differs",
      "config.overwriteHint": "When off, existing files are kept; identical content is always skipped",
      "config.rulesEmpty": "No rules yet. Press \"Add rule\" to start configuring.",
      "config.yamlEmpty": "# No rules added yet",
      "config.noRules": "Add at least one rule",
      "config.pickDeviceFirst": "Select a device first",
      "config.loading": "Loading configuration…",
      "config.loadFailed": "Load failed",
      "config.noConfigHint": "This device has no config file yet; saving will create one.",
      "config.saving": "Saving…",
      "config.saveFailed": "Save failed: {error}",
      "config.saveFailedNoServer": "Save failed: no response from the server",
      "config.saved": "Saved ({n} rules)",
      "config.validated": "Validation passed ({n} rules)",
      "config.validateFailed": "Validation failed: {error}",
      "config.unknownError": "unknown error",
      "config.copied": "YAML copied to the clipboard",
      "config.copyUnsupported": "This browser cannot copy automatically; select the text and copy it manually",
      "config.pickSourceInDevice": "Choose a source folder on the device",
      "config.pickDestination": "Choose the destination folder",
      "config.enableRule": "Enable this rule",

      "picker.opening": "Opening the picker…",
      "picker.cancelled": "Selection cancelled",
      "picker.selected": "Selected: {path}",

      "log.progress": "{done}/{total} ({skipped} skipped) {pct}%",
      "log.requestedRescan": "Requested a device rescan",
      "log.refreshedList": "Device list refreshed",
      "log.pickDeviceFirst": "Select a device first",
      "log.syncFailed": "Sync failed: {error}",
      "log.manualSync": "Manually triggered a sync: {path}",
      "log.cancelRequested": "Requested sync cancellation",
      "log.toggleFailed": "Could not change the setting: {error}",
      "log.autoSyncCreated": "Auto-sync {state} (takes effect once a config file exists)",
      "log.autoSync": "Auto-sync {state}",
      "log.enabled": "enabled",
      "log.disabled": "disabled",
      "log.createHint": "Fill in the source and destination paths; saving writes dsync.yml at the device root",
      "log.requestFailed": "Request failed: {error}",
      "log.actionFailed": "Action failed",
      "log.scanned": "Scanned {files} files, {bytes} total",
      "log.copiedTo": "Copied → {dest}",
      "log.skippedDup": "Skipped (duplicate) → {dest}",
      "log.failed": "Failed: {what}",
      "log.unknownError": "Unknown error",
      "log.syncCancelled": "Sync cancelled",
      "log.syncDone": "Sync finished: {copied} copied, {skipped} skipped",
      "log.deviceEvent": "Device event: {action}",
      "log.configUpdated": "Configuration updated",
      "log.configSaved": "Config saved: {path}",

      "history.empty": "No history yet",
      "history.ok": "OK",
      "history.cancelled": "Cancelled",
      "history.failed": "Failed",

      "logfilter.all": "All",
      "logfilter.autoscroll": "Auto-scroll",

      "empty.title": "{path} has no config file yet",
      "empty.desc": "Create a <code>dsync.yml</code> with the source and destination paths and it will sync automatically. Saving writes it to the root of this device.",

      "time.minutesSeconds": "{m} min {s} s",
      "lang.label": "Language"
    },
    zh: {
      "app.title": "D-Sync · 同步看板",
      "app.heading": "D-Sync 同步看板",
      "app.subtitle": "正在连接…",
      "app.footer": "本页仅在本机 127.0.0.1 提供服务",

      "conn.connecting": "连接中",
      "conn.live": "实时连接",
      "conn.error": "连接中断，重试中",

      "action.rescan": "重新扫描",
      "action.openFolder": "打开目录",
      "action.refreshList": "刷新列表",
      "action.syncNow": "立即同步",
      "action.cancelSync": "取消同步",
      "action.validate": "校验",
      "action.saveConfig": "保存配置",
      "action.addRule": "+ 添加规则",
      "action.copy": "复制",
      "action.createConfig": "创建配置文件",
      "action.clear": "清空",
      "action.browse": "浏览…",
      "action.delete": "删除",

      "section.devices": "设备状态",
      "section.selectDevice": "选择设备",
      "section.config": "同步配置",
      "section.rules": "规则列表",
      "section.yaml": "YAML 预览",
      "section.noConfig": "尚未创建配置文件",
      "section.log": "运行日志",
      "section.history": "历史记录",

      "col.device": "设备名",
      "col.state": "当前状态",
      "col.progress": "同步进度",
      "col.elapsed": "本次同步耗时",
      "col.source": "源路径（设备内）",
      "col.destination": "目标路径",
      "col.exclude": "排除",
      "col.enabled": "启用",
      "col.overwrite": "覆盖",
      "col.time": "时间",
      "col.copied": "复制",
      "col.skipped": "跳过",

      "devices.empty": "未检测到设备",
      "devices.selectHint": "仅列出外置设备（U 盘、读卡器等）。",
      "devices.notSelected": "未选择",
      "devices.choose": "— 请选择设备 —",
      "devices.noConfigMark": "（无配置）",
      "devices.hasConfigMark": " ✓",

      "state.detected": "已检测",
      "state.syncing": "同步中",
      "state.synced": "已同步",
      "state.error": "错误",

      "phase.idle": "空闲等待",
      "phase.planning": "正在扫描文件",
      "phase.copying": "正在同步",
      "phase.done": "已完成",
      "phase.error": "出错",
      "phase.cancelled": "已取消",

      "config.targetTag": "设备配置",
      "config.unsavedTag": "未保存的修改",
      "config.autoSync": "自动同步",
      "config.autoSyncHint": "开启后，插入该设备将自动开始同步；关闭后需手动点「立即同步」",
      "config.global": "全局设置",
      "config.destRoot": "目标根目录",
      "config.destRootPlaceholder": "例如 D:/SD_Backup",
      "config.destRootHint": "规则中的相对目标路径基于此目录解析",
      "config.browseFolder": "浏览文件夹",
      "config.exclude": "全局排除",
      "config.excludeHint": "逗号分隔，对所有规则生效",
      "config.overwrite": "内容不同时覆盖",
      "config.overwriteHint": "关闭时保留已有文件；内容完全一致始终跳过",
      "config.rulesEmpty": "还没有规则，点击「添加规则」开始配置。",
      "config.yamlEmpty": "# 尚未添加规则",
      "config.noRules": "请至少添加一条规则",
      "config.pickDeviceFirst": "请先选择一台设备",
      "config.loading": "正在载入配置…",
      "config.loadFailed": "载入失败",
      "config.noConfigHint": "该设备尚无配置文件，保存后将创建。",
      "config.saving": "保存中…",
      "config.saveFailed": "保存失败：{error}",
      "config.saveFailedNoServer": "保存失败：服务器无响应",
      "config.saved": "已保存（{n} 条规则）",
      "config.validated": "校验通过（{n} 条规则）",
      "config.validateFailed": "校验失败：{error}",
      "config.unknownError": "未知错误",
      "config.copied": "YAML 已复制到剪贴板",
      "config.copyUnsupported": "当前浏览器不支持自动复制，请手动选择文本复制",
      "config.pickSourceInDevice": "在设备内选择源文件夹",
      "config.pickDestination": "选择目标文件夹",
      "config.enableRule": "启用该规则",

      "picker.opening": "正在打开选择窗口…",
      "picker.cancelled": "已取消选择",
      "picker.selected": "已选择：{path}",

      "log.progress": "{done}/{total}（已跳过 {skipped}）{pct}%",
      "log.requestedRescan": "已请求重新扫描设备",
      "log.refreshedList": "已刷新设备列表",
      "log.pickDeviceFirst": "请先选择一台设备",
      "log.syncFailed": "同步失败：{error}",
      "log.manualSync": "已手动触发同步：{path}",
      "log.cancelRequested": "已请求取消同步",
      "log.toggleFailed": "开关设置失败：{error}",
      "log.autoSyncCreated": "自动同步已{state}（创建配置文件后生效）",
      "log.autoSync": "自动同步已{state}",
      "log.enabled": "开启",
      "log.disabled": "关闭",
      "log.createHint": "请填写源路径与目标路径，保存后将写入设备根目录的 dsync.yml",
      "log.requestFailed": "请求失败：{error}",
      "log.actionFailed": "操作失败",
      "log.scanned": "扫描到 {files} 个文件，共 {bytes}",
      "log.copiedTo": "已复制 → {dest}",
      "log.skippedDup": "已跳过（重复）→ {dest}",
      "log.failed": "失败：{what}",
      "log.unknownError": "未知错误",
      "log.syncCancelled": "同步已取消",
      "log.syncDone": "同步完成：复制 {copied} 个，跳过 {skipped} 个",
      "log.deviceEvent": "设备事件：{action}",
      "log.configUpdated": "配置已更新",
      "log.configSaved": "配置已保存：{path}",

      "history.empty": "暂无历史记录",
      "history.ok": "成功",
      "history.cancelled": "已取消",
      "history.failed": "失败",

      "logfilter.all": "全部",
      "logfilter.autoscroll": "自动滚动",

      "empty.title": "{path} 还没有配置文件",
      "empty.desc": "创建一个 <code>dsync.yml</code>，写好源路径与目标路径后即可同步。保存后会自动写入这台设备的根目录。",

      "time.minutesSeconds": "{m} 分 {s} 秒",
      "lang.label": "语言"
    }
  };

  var lang = "en";
  try {
    var saved = global.localStorage && global.localStorage.getItem("dsync.lang");
    if (saved === "zh" || saved === "en") lang = saved;
  } catch (e) { /* storage unavailable */ }

  function t(key, params) {
    var pack = STRINGS[lang] || STRINGS.en;
    var text = pack[key];
    if (text === undefined) text = STRINGS.en[key];
    if (text === undefined) return key;
    if (params) {
      Object.keys(params).forEach(function (k) {
        text = text.split("{" + k + "}").join(String(params[k]));
      });
    }
    return text;
  }

  function apply(root) {
    var scope = root || global.document;
    if (!scope || !scope.querySelectorAll) return;
    scope.querySelectorAll("[data-i18n]").forEach(function (el) {
      el.textContent = t(el.getAttribute("data-i18n"));
    });
    scope.querySelectorAll("[data-i18n-html]").forEach(function (el) {
      el.innerHTML = t(el.getAttribute("data-i18n-html"));
    });
    scope.querySelectorAll("[data-i18n-title]").forEach(function (el) {
      el.title = t(el.getAttribute("data-i18n-title"));
    });
    scope.querySelectorAll("[data-i18n-placeholder]").forEach(function (el) {
      el.placeholder = t(el.getAttribute("data-i18n-placeholder"));
    });
    scope.querySelectorAll("[data-i18n-aria]").forEach(function (el) {
      el.setAttribute("aria-label", t(el.getAttribute("data-i18n-aria")));
    });
    if (global.document) {
      global.document.documentElement.lang = lang === "zh" ? "zh-CN" : "en";
      global.document.title = t("app.title");
    }
  }

  function getLang() { return lang; }

  function setLang(next) {
    lang = next === "zh" ? "zh" : "en";
    try {
      if (global.localStorage) global.localStorage.setItem("dsync.lang", lang);
    } catch (e) { /* ignore */ }
    apply();
    if (typeof global.onLangChange === "function") global.onLangChange(lang);
  }

  global.DSyncI18n = { t: t, apply: apply, setLang: setLang, getLang: getLang, STRINGS: STRINGS };
  global.t = t;
})(typeof window !== "undefined" ? window : this);
