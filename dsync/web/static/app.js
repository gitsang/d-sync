/* D-Sync 看板前端逻辑 */
(function () {
  "use strict";

  var state = {
    phase: "idle",
    devices: [],
    rules: [],
    stats: null,
    currentFile: null,
    totalFiles: 0,
    doneFiles: 0,
    selectedCard: "",
    syncing: false
  };

  /* ---------------- 配置编辑器状态 ---------------- */
  var editor = {
    model: null,        // {version, enable, destination_root, overwrite, exclude, rules}
    loaded: false,
    dirty: false,
    cardPath: "",
    hasConfig: false
  };

  var $ = function (id) { return document.getElementById(id); };

  // ------------------------------------------------------------ 工具
  function fmtBytes(bytes) {
    if (!bytes) return "0 B";
    var units = ["B", "KB", "MB", "GB", "TB"];
    var i = 0;
    var value = bytes;
    while (value >= 1024 && i < units.length - 1) { value /= 1024; i++; }
    return (value >= 100 || i === 0 ? value.toFixed(0) : value.toFixed(1)) + " " + units[i];
  }

  function fmtDuration(seconds) {
    if (!seconds && seconds !== 0) return "—";
    if (seconds < 1) return seconds.toFixed(2) + " s";
    if (seconds < 60) return seconds.toFixed(1) + " s";
    var m = Math.floor(seconds / 60);
    var s = Math.floor(seconds % 60);
    return m + " 分 " + String(s).padStart(2, "0") + " 秒";
    }

  function fmtTime(ts) {
    var d = new Date((ts || Date.now() / 1000) * 1000);
    function p(n) { return String(n).padStart(2, "0"); }
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  function fmtDateTime(ts) {
    var d = new Date((ts || Date.now() / 1000) * 1000);
    function p(n) { return String(n).padStart(2, "0"); }
    return (d.getMonth() + 1) + "/" + d.getDate() + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }

  // ------------------------------------------------------------ 日志
  var LOG_LEVELS = ["debug", "info", "warn", "error"];
  var logLevelEnabled = { debug: true, info: true, warn: true, error: true };

  /* 旧版 level 名称 -> 新等级 */
  function normalizeLevel(level) {
    var lv = String(level || "info").toLowerCase();
    if (lv === "ok" || lv === "success") return "info";
    if (lv === "warning") return "warn";
    if (LOG_LEVELS.indexOf(lv) >= 0) return lv;
    return "info";
  }

  function levelClass(level) {
    return "log-" + normalizeLevel(level);
  }

  function addLog(message, level) {
    var lv = normalizeLevel(level);
    var box = $("log");
    var line = document.createElement("div");
    line.className = "log-line";
    line.setAttribute("data-level", lv);
    line.innerHTML = '<span class="log-time">' + fmtTime() + "</span>" +
      '<span class="log-level">' + lv.toUpperCase() + "</span>" +
      '<span class="' + levelClass(lv) + '">' + escapeHtml(message) + "</span>";
    box.appendChild(line);
    while (box.childElementCount > 800) { box.removeChild(box.firstChild); }
    applyLogFilter();
    if ($("autoscroll").checked) { box.scrollTop = box.scrollHeight; }
  }

  /* 按勾选的等级过滤日志行 */
  function applyLogFilter() {
    var lines = $("log").children;
    for (var i = 0; i < lines.length; i++) {
      var lv = lines[i].getAttribute("data-level") || "info";
      lines[i].style.display = logLevelEnabled[lv] === false ? "none" : "";
    }
  }

  function bindLogFilters() {
    var box = $("log-filters");
    if (!box) return;
    box.addEventListener("change", function (event) {
      var target = event.target;
      if (!target || !target.getAttribute) return;
      var lv = target.getAttribute("data-log-level");
      if (!lv) return;
      logLevelEnabled[lv] = target.checked;
      applyLogFilter();
    });
  }

  // ============================================================ 配置编辑器
  function emptyModel() {
    return {
      version: 1,
      enable: true,
      destination_root: "",
      overwrite: false,
      exclude: [],
      rules: []
    };
  }

  function splitList(text) {
    if (!text) return [];
    return String(text)
      .split(/[,;，；\n]/)
      .map(function (s) { return s.trim(); })
      .filter(function (s) { return s.length > 0; });
  }

  function readModelFromForm() {
    if (!editor.model) editor.model = emptyModel();
    editor.model.destination_root = $("cfg-dest-root").value.trim();
    editor.model.exclude = splitList($("cfg-exclude").value);
    editor.model.overwrite = $("cfg-overwrite").checked;
    editor.model.enable = $("auto-enable").checked;

    var rows = $("cfg-rules").querySelectorAll("tr");
    var rules = [];
    for (var i = 0; i < rows.length; i++) {
      var row = rows[i];
      var source = row.querySelector(".r-source").value.trim();
      var destination = row.querySelector(".r-dest").value.trim();
      if (!source && !destination) continue;
      var overwriteCell = row.querySelector(".r-overwrite");
      var enabledCell = row.querySelector(".r-enabled");
      rules.push({
        source: source,
        destination: destination,
        exclude: splitList(row.querySelector(".r-exclude").value),
        enabled: enabledCell ? enabledCell.checked : true,
        overwrite: overwriteCell.checked ? true : (overwriteCell.dataset.set === "1" ? false : null)
      });
    }
    editor.model.rules = rules;
    return editor.model;
  }

  function writeModelToForm(model) {
    editor.model = model || emptyModel();
    $("cfg-dest-root").value = editor.model.destination_root || "";
    $("cfg-exclude").value = (editor.model.exclude || []).join(", ");
    $("cfg-overwrite").checked = !!editor.model.overwrite;
    if (editor.model.enable !== undefined) {
      $("auto-enable").checked = !!editor.model.enable;
    }
    renderRuleRows();
    updateYamlPreview();
  }

  function renderRuleRows() {
    var body = $("cfg-rules");
    body.innerHTML = "";
    var rules = (editor.model && editor.model.rules) || [];

    for (var i = 0; i < rules.length; i++) {
      var rule = rules[i];
      var tr = document.createElement("tr");

      // 源路径 + 浏览按钮
      var tdSource = document.createElement("td");
      var srcWrap = document.createElement("span");
      srcWrap.className = "input-with-btn";
      var inSource = document.createElement("input");
      inSource.type = "text";
      inSource.className = "r-source";
      inSource.value = rule.source || "";
      inSource.placeholder = "DCIM";
      var btnSrc = document.createElement("button");
      btnSrc.type = "button";
      btnSrc.className = "btn tiny";
      btnSrc.textContent = "浏览…";
      btnSrc.title = "在设备内选择源文件夹";
      btnSrc.addEventListener("click", function () {
        pickPath("source", this.closest("tr").querySelector(".r-source").value);
      });
      srcWrap.appendChild(inSource);
      srcWrap.appendChild(btnSrc);
      tdSource.appendChild(srcWrap);

      // 目标路径 + 浏览按钮
      var tdDest = document.createElement("td");
      var destWrap = document.createElement("span");
      destWrap.className = "input-with-btn";
      var inDest = document.createElement("input");
      inDest.type = "text";
      inDest.className = "r-dest";
      inDest.value = rule.destination || "";
      inDest.placeholder = "Photos";
      var btnDest = document.createElement("button");
      btnDest.type = "button";
      btnDest.className = "btn tiny";
      btnDest.textContent = "浏览…";
      btnDest.title = "选择目标文件夹";
      btnDest.addEventListener("click", function () {
        pickPath("destination", this.closest("tr").querySelector(".r-dest").value);
      });
      destWrap.appendChild(inDest);
      destWrap.appendChild(btnDest);
      tdDest.appendChild(destWrap);

      var tdExclude = document.createElement("td");
      var inExclude = document.createElement("input");
      inExclude.type = "text";
      inExclude.className = "r-exclude";
      inExclude.value = (rule.exclude || []).join(", ");
      inExclude.placeholder = "*.bak";
      tdExclude.appendChild(inExclude);

      var tdEnabled = document.createElement("td");
      tdEnabled.className = "center";
      var chkEnabled = document.createElement("input");
      chkEnabled.type = "checkbox";
      chkEnabled.className = "r-enabled";
      chkEnabled.checked = rule.enabled !== false;
      chkEnabled.title = "启用该规则";
      tdEnabled.appendChild(chkEnabled);

      var tdOverwrite = document.createElement("td");
      tdOverwrite.className = "center";
      var chk = document.createElement("input");
      chk.type = "checkbox";
      chk.className = "r-overwrite";
      if (rule.overwrite === true) { chk.checked = true; chk.dataset.set = "1"; }
      else if (rule.overwrite === false) { chk.checked = false; chk.dataset.set = "1"; }
      tdOverwrite.appendChild(chk);

      var tdActions = document.createElement("td");
      tdActions.className = "center";
      var btnDel = document.createElement("button");
      btnDel.type = "button";
      btnDel.className = "btn small danger";
      btnDel.textContent = "删除";
      btnDel.addEventListener("click", function () {
        readModelFromForm();
        editor.model.rules.splice(indexOfRow(this), 1);
        renderRuleRows();
        updateYamlPreview();
        markDirty();
      });
      tdActions.appendChild(btnDel);

      tr.appendChild(tdSource);
      tr.appendChild(tdDest);
      tr.appendChild(tdExclude);
      tr.appendChild(tdEnabled);
      tr.appendChild(tdOverwrite);
      tr.appendChild(tdActions);
      body.appendChild(tr);
    }

    $("rules-empty").style.display = rules.length ? "none" : "block";
  }

  function indexOfRow(btn) {
    var rows = $("cfg-rules").querySelectorAll("tr");
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].contains(btn)) return i;
    }
    return -1;
  }

  function markDirty() {
    editor.dirty = true;
    var tag = $("config-target-tag");
    tag.textContent = "未保存的修改";
    tag.className = "tag warn";
  }

  function markClean() {
    editor.dirty = false;
    var tag = $("config-target-tag");
    tag.textContent = "设备配置";
    tag.className = "tag accent";
  }

  function updateYamlPreview() {
    var model = readModelFromForm();
    if (!model.rules.length) {
      $("yaml-preview").textContent = "# 尚未添加规则";
      return;
    }
    var lines = ["version: 1", "enable: " + (model.enable ? "true" : "false")];
    if (model.destination_root) lines.push("destination_root: " + JSON.stringify(model.destination_root));
    lines.push("overwrite: " + (model.overwrite ? "true" : "false"));
    if (model.exclude.length) lines.push("exclude: [" + model.exclude.map(function (e) { return JSON.stringify(e); }).join(", ") + "]");
    lines.push("rules:");
    model.rules.forEach(function (rule) {
      lines.push("  - source: " + JSON.stringify(rule.source));
      lines.push("    destination: " + JSON.stringify(rule.destination));
      if (rule.exclude && rule.exclude.length) {
        lines.push("    exclude: [" + rule.exclude.map(function (e) { return JSON.stringify(e); }).join(", ") + "]");
      }
      if (rule.enabled === false) lines.push("    enabled: false");
      if (rule.overwrite !== null && rule.overwrite !== undefined) {
        lines.push("    overwrite: " + (rule.overwrite ? "true" : "false"));
      }
    });
    $("yaml-preview").textContent = lines.join("\n");
  }

  // ------------------------------------------------------------ 路径选择
  function pickPath(kind, current) {
    setConfigHint("正在打开选择窗口…", "");
    post("pick_path", {
      kind: kind,
      card_path: editor.cardPath || state.selectedCard || "",
      current: current || ""
    }).then(function (data) {
      if (!data) return;
      if (data.error) {
        setConfigHint(data.error, "err");
        $("save-hint").textContent = data.error;
        $("save-hint").className = "hint err";
        return;
      }
      if (data.cancelled) {
        setConfigHint("已取消选择", "");
        return;
      }
      // 写入对应输入框
      var input = null;
      if (kind === "source") {
        input = $("cfg-rules").querySelector(".r-source");
      } else if (kind === "destination") {
        input = $("cfg-rules").querySelector(".r-dest");
      }
      if (input) { input.value = data.path; }
      setConfigHint("已选择：" + data.path, "ok");
      readModelFromForm();
      updateYamlPreview();
      markDirty();
    });
  }

  // ------------------------------------------------------------ 设备列表
  function renderDevices() {
    var body = $("devices");
    body.innerHTML = "";
    var devices = state.devices || [];
    $("device-count").textContent = devices.length;

    if (!devices.length) {
      var empty = document.createElement("tr");
      empty.innerHTML = '<td colspan="4" class="muted">未检测到设备</td>';
      body.appendChild(empty);
      return;
    }

    devices.forEach(function (dev) {
      var tr = document.createElement("tr");
      var cls = dev.state === "synced" ? "ok" : dev.state === "syncing" ? "accent"
        : dev.state === "error" ? "err" : "";
      var label = { detected: "已检测", syncing: "同步中", synced: "已同步", error: "错误" }[dev.state] || dev.state;

      // 设备名：只显示路径一行
      var tdName = document.createElement("td");
      var nameBox = document.createElement("div");
      nameBox.className = "dev-name";
      nameBox.textContent = dev.path || dev.name || "";
      nameBox.title = dev.name && dev.name !== dev.path ? dev.name : "";
      tdName.appendChild(nameBox);

      // 当前状态：只保留一个状态胶囊
      var tdState = document.createElement("td");
      var tag = document.createElement("span");
      tag.className = "tag " + cls;
      tag.textContent = label;
      tdState.appendChild(tag);

      // 同步进度 x/y(已跳过 z) 99%
      var tdProgress = document.createElement("td");
      var total = dev.total_files || 0;
      var done = dev.done_files || 0;
      var skipped = dev.skipped_files || 0;
      var pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
      if (dev.state === "synced" && total > 0) pct = 100;
      var progressText = document.createElement("span");
      progressText.className = "progress-text";
      progressText.textContent = done + "/" + total + " (已跳过 " + skipped + ") " + pct + "%";
      tdProgress.appendChild(progressText);
      var bar = document.createElement("div");
      bar.className = "bar mini";
      var fill = document.createElement("div");
      fill.className = "bar-fill" + (dev.state === "synced" ? " done" : "") +
        (dev.state === "error" ? " error" : "");
      fill.style.width = pct + "%";
      bar.appendChild(fill);
      tdProgress.appendChild(bar);

      // 本次同步耗时：同步中按「当前时间 - 开始时间」实时计算
      var tdElapsed = document.createElement("td");
      tdElapsed.className = "num";
      tdElapsed.setAttribute("data-elapsed-path", dev.path || "");
      tdElapsed.textContent = elapsedText(dev);

      tr.appendChild(tdName);
      tr.appendChild(tdState);
      tr.appendChild(tdProgress);
      tr.appendChild(tdElapsed);
      body.appendChild(tr);
    });
  }

  /* 计算某设备本次同步的耗时文本：同步中用当前时间减开始时间 */
  function elapsedText(dev) {
    var started = dev.started_at || 0;
    if (dev.state === "syncing" && started > 0) {
      return fmtDuration(Math.max(0, Date.now() / 1000 - started));
    }
    if (dev.elapsed) { return fmtDuration(dev.elapsed); }
    return "—";
  }

  /* 每秒刷新一次耗时列，让同步中的计时实时走动 */
  function refreshElapsed() {
    var devices = state.devices || [];
    var cells = $("devices").querySelectorAll("[data-elapsed-path]");
    for (var i = 0; i < cells.length; i++) {
      var path = cells[i].getAttribute("data-elapsed-path");
      var dev = null;
      for (var j = 0; j < devices.length; j++) {
        if (devices[j].path === path) { dev = devices[j]; break; }
      }
      if (dev) { cells[i].textContent = elapsedText(dev); }
    }
  }

  // ------------------------------------------------------------ 选设备流程
  function refreshCardSelect() {
    var select = $("card-select");
    var current = select.value || state.selectedCard;
    select.innerHTML = "";
    var devices = state.devices || [];

    var emptyOpt = document.createElement("option");
    emptyOpt.value = "";
    emptyOpt.textContent = devices.length ? "— 请选择设备 —" : "未检测到设备";
    select.appendChild(emptyOpt);

    devices.forEach(function (dev) {
      var opt = document.createElement("option");
      opt.value = dev.path;
      var mark = dev.config ? " ✓" : "（无配置）";
      opt.textContent = (dev.path || dev.name || "") + mark;
      select.appendChild(opt);
    });

    if (current) select.value = current;
    if (select.value !== (current || "")) {
      // 目标不在列表里（设备已被拔出）
      select.value = "";
    }
  }

  function selectCard(path) {
    editor.cardPath = path || "";
    state.selectedCard = path || "";

    if (!path) {
      // 未选择：隐藏配置区，显示空状态提示
      editor.hasConfig = false;
      editor.model = emptyModel();
      $("editor-card").hidden = true;
      $("empty-card").hidden = true;
      $("selected-hint").textContent = "未选择";
      $("btn-sync-now").disabled = true;
      render();
      return;
    }

    $("selected-hint").textContent = path;
    $("btn-sync-now").disabled = false;

    post("select_card", { path: path }).then(function (data) {
      if (!data) return;
      if (data.error) {
        setConfigHint(data.error, "err");
        return;
      }
      if (!data.has_config) {
        // 没有配置文件 -> 隐藏下方内容，提示创建
        editor.hasConfig = false;
        editor.model = emptyModel();
        $("editor-card").hidden = true;
        $("empty-card").hidden = false;
        $("empty-title").textContent = path + " 还没有配置文件";
        $("empty-desc").innerHTML =
          "创建一个 <code>dsync.yml</code>，写好源路径与目标路径后即可同步。保存后会自动写入这台设备的根目录。";
        $("create-hint").textContent = "";
        if (typeof data.enable === "boolean") {
          $("auto-enable").checked = data.enable;
        }
        render();
        return;
      }

      // 有配置文件 -> 加载规则，显示配置区
      editor.hasConfig = true;
      $("empty-card").hidden = true;
      $("editor-card").hidden = false;
      loadConfig(path);
    });
  }

  function loadConfig(cardPath) {
    setConfigHint("正在载入配置…", "");
    post("get_config", { card_path: cardPath })
      .then(function (data) {
        if (!data || data.error) {
          setConfigHint((data && data.error) || "载入失败", "err");
          if (data && !data.exists) {
            $("editor-card").hidden = true;
            $("empty-card").hidden = false;
          }
          return;
        }
        writeModelToForm(data.model);
        markClean();
        var hint = data.hint || "";
        if (!data.exists) {
          hint = hint || "该设备尚无配置文件，保存后将创建。";
        }
        if (data.error) hint = data.error + (hint ? " " + hint : "");
        setConfigHint(hint, data.error ? "err" : (data.exists ? "" : "warn"));
      });
  }

  function setConfigHint(text, level) {
    var el = $("config-hint");
    el.textContent = text || "";
    el.className = "hint" + (level ? " " + level : "");
  }

  function saveConfig() {
    var model = readModelFromForm();
    if (!model.rules.length) {
      setConfigHint("请至少添加一条规则", "err");
      $("save-hint").textContent = "请至少添加一条规则";
      $("save-hint").className = "hint err";
      return;
    }
    if (!editor.cardPath) {
      setConfigHint("请先选择一台设备", "err");
      return;
    }
    $("save-hint").textContent = "保存中…";
    $("save-hint").className = "hint";
    post("save_config", {
      model: model,
      card_path: editor.cardPath
    }).then(function (data) {
      if (!data) {
        $("save-hint").textContent = "保存失败：服务器无响应";
        $("save-hint").className = "hint err";
        return;
      }
      if (data.error) {
        $("save-hint").textContent = "保存失败：" + data.error;
        $("save-hint").className = "hint err";
        setConfigHint(data.error, "err");
        return;
      }
      $("save-hint").textContent = "已保存（" + data.rules + " 条规则）";
      $("save-hint").className = "hint ok";
      markClean();
      editor.hasConfig = true;
      $("empty-card").hidden = true;
      $("editor-card").hidden = false;
      addLog("配置已保存：" + (data.path || editor.cardPath), "ok");
      fetchState();
    });
  }

  function bindEditor() {
    $("btn-add-rule").addEventListener("click", function () {
      readModelFromForm();
      editor.model.rules.push({
        source: "", destination: "", exclude: [],
        enabled: true, overwrite: null
      });
      renderRuleRows();
      updateYamlPreview();
      markDirty();
      var inputs = $("cfg-rules").querySelectorAll(".r-source");
      if (inputs.length) inputs[inputs.length - 1].focus();
    });

    $("btn-validate").addEventListener("click", function () {
      var model = readModelFromForm();
      post("validate_config", { model: model, card_path: editor.cardPath })
        .then(function (data) {
          if (data && data.valid) {
            $("save-hint").textContent = "校验通过（" + data.rules + " 条规则）";
            $("save-hint").className = "hint ok";
          } else {
            $("save-hint").textContent = "校验失败：" + ((data && data.error) || "未知错误");
            $("save-hint").className = "hint err";
          }
        });
    });

    $("btn-save-config").addEventListener("click", saveConfig);

    $("btn-copy-yaml").addEventListener("click", function () {
      var text = $("yaml-preview").textContent;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          $("save-hint").textContent = "YAML 已复制到剪贴板";
          $("save-hint").className = "hint ok";
        });
      } else {
        setConfigHint("当前浏览器不支持自动复制，请手动选择文本复制", "warn");
      }
    });

    $("btn-pick-dest-root").addEventListener("click", function () {
      setConfigHint("正在打开选择窗口…", "");
      post("pick_path", {
        kind: "destination",
        card_path: editor.cardPath || state.selectedCard || "",
        current: $("cfg-dest-root").value.trim()
      }).then(function (data) {
        if (!data) return;
        if (data.error) {
          setConfigHint(data.error, "err");
          return;
        }
        if (data.cancelled) { setConfigHint("已取消选择", ""); return; }
        $("cfg-dest-root").value = data.path;
        setConfigHint("已选择：" + data.path, "ok");
        readModelFromForm();
        updateYamlPreview();
        markDirty();
      });
    });

    // 表单任意变化 -> 标记未保存 + 刷新预览
    var editorCard = $("editor-card");
    editorCard.addEventListener("input", function (event) {
      readModelFromForm();
      updateYamlPreview();
      markDirty();
    });
    editorCard.addEventListener("change", function (event) {
      if (event.target.className === "r-overwrite") {
        event.target.dataset.set = "1";
      }
      readModelFromForm();
      updateYamlPreview();
      markDirty();
    });
  }

  // ------------------------------------------------------------ 渲染
  var PHASE_TEXT = {
    idle: "空闲等待",
    planning: "正在扫描文件",
    copying: "正在同步",
    done: "已完成",
    error: "出错",
    cancelled: "已取消"
  };

  function render() {
    var stats = state.stats || {};

    // 同步按钮状态
    $("btn-cancel-sync").disabled = !state.syncing;
    if (!state.syncing) { $("btn-sync-now").disabled = !state.selectedCard; }

    renderDevices();

    // 历史
    var histBox = $("history");
    histBox.innerHTML = "";
    var history = (window.__history || []);
    $("history-count").textContent = history.length;
    if (!history.length) {
      histBox.innerHTML = '<tr><td colspan="6" class="muted">暂无历史记录</td></tr>';
    }
    history.forEach(function (item) {
      var tr = document.createElement("tr");
      var status = item.status === "ok" ? '<span class="tag ok">成功</span>'
        : item.status === "cancelled" ? '<span class="tag warn">已取消</span>'
        : '<span class="tag err">失败</span>';
      tr.innerHTML =
        "<td>" + escapeHtml(fmtDateTime(item.ts)) + "</td>" +
        "<td>" + escapeHtml(item.device || item.path || "") + "</td>" +
        "<td>" + status + "</td>" +
        '<td class="num">' + (item.copied || 0) + "</td>" +
        '<td class="num">' + (item.skipped || 0) + "</td>" +
        '<td class="num">' + escapeHtml(fmtDuration(item.elapsed)) + "</td>";
      histBox.appendChild(tr);
    });
  }

  function applySnapshot(data) {
    if (!data) return;
    var hadDevices = (state.devices || []).length;
    state.phase = data.phase || "idle";
    state.devices = data.devices || [];
    state.rules = data.rules || [];
    state.stats = data.stats || null;
    state.totalFiles = (data.stats && data.stats.total_files) || data.total_files || 0;
    state.doneFiles = data.done_files || 0;
    state.currentFile = data.current_file || null;
    state.syncing = !!data.syncing;
    window.__history = data.history || window.__history || [];
    if (data.message) { $("subtitle").textContent = data.message; }

    // 设备列表刷新时保持当前选卡
    var prevSelected = state.selectedCard;
    var backendSelected = data.selected_card || "";
    state.selectedCard = backendSelected || prevSelected;
    render();

    refreshCardSelect();
    if (backendSelected) {
      $("card-select").value = backendSelected;
      $("selected-hint").textContent = backendSelected;
      $("btn-sync-now").disabled = false;
    }

    // 首次加载：自动选中第一台带配置的设备
    if (!editor.loaded) {
      editor.loaded = true;
      var devices = state.devices || [];
      if (!state.selectedCard && devices.length) {
        var target = "";
        for (var i = 0; i < devices.length; i++) {
          if (devices[i].config) { target = devices[i].path; break; }
        }
        if (!target) target = devices[0].path;
        $("card-select").value = target;
        selectCard(target);
      }
    } else if (hadDevices !== (state.devices || []).length && !editor.dirty) {
      // 新设备插入 —— 若尚未选卡则自动选上
      if (!state.selectedCard && state.devices.length) {
        var newest = state.devices[state.devices.length - 1].path;
        $("card-select").value = newest;
        selectCard(newest);
      }
    }
  }

  // ------------------------------------------------------------ SSE
  function connect() {
    var source = new EventSource("/api/events");

    source.onopen = function () {
      setConn("live", "实时连接");
    };

    source.onerror = function () {
      setConn("error", "连接中断，重试中");
    };

    source.onmessage = function (event) {
      var msg;
      try { msg = JSON.parse(event.data); } catch (e) { return; }

      switch (msg.event) {
        case "snapshot":
          applySnapshot(msg.state);
          break;
        case "phase":
          state.phase = msg.phase || "idle";
          if (msg.message) { $("subtitle").textContent = msg.message; addLog(msg.message, "info"); }
          render();
          break;
        case "plan":
          state.totalFiles = msg.total_files || 0;
          state.doneFiles = 0;
          addLog("扫描到 " + msg.total_files + " 个文件，共 " + fmtBytes(msg.total_bytes), "info");
          render();
          break;
        case "file_start":
          state.currentFile = msg.source;
          state.doneFiles = (msg.index || 1) - 1;
          render();
          break;
        case "file_done":
          state.doneFiles = msg.index || state.doneFiles;
          state.currentFile = null;
          if (msg.status === "copied") {
            addLog("已复制 → " + msg.destination, "info");
          } else if (msg.status === "skipped") {
            addLog("已跳过（重复）→ " + msg.destination, "debug");
          } else {
            addLog("失败：" + (msg.message || msg.source), "error");
          }
          render();
          break;
        case "error":
          addLog(msg.message || "未知错误", "error");
          break;
        case "cancelled":
          addLog(msg.message || "同步已取消", "warn");
          break;
        case "finished":
          state.phase = "done";
          state.currentFile = null;
          if (msg.stats) { state.stats = msg.stats; }
          addLog("同步完成：复制 " + (state.stats ? state.stats.copied_files : 0) +
            " 个，跳过 " + (state.stats ? state.stats.skipped_files : 0) + " 个", "ok");
          render();
          fetchState();
          break;
        case "device":
          addLog(msg.message || ("设备事件：" + msg.action), "info");
          fetchState();
          break;
        case "log":
          addLog(msg.message || "", msg.level || "info");
          break;
        case "config":
          addLog(msg.message || "配置已更新", "ok");
          fetchState();
          break;
        case "state":
          applySnapshot(msg.state);
          break;
      }
    };
  }

  function setConn(stateName, text) {
    $("conn-pill").setAttribute("data-state", stateName);
    $("conn-text").textContent = text;
  }

  function fetchState() {
    fetch("/api/state").then(function (r) { return r.json(); })
      .then(applySnapshot).catch(function () {});
  }

  // ------------------------------------------------------------ 交互
  $("btn-rescan").addEventListener("click", function () {
    post("rescan", {});
    addLog("已请求重新扫描设备", "info");
  });

  $("btn-open-folder").addEventListener("click", function () {
    post("open_folder", { path: state.selectedCard || "" });
  });

  $("btn-clear-log").addEventListener("click", function () {
    $("log").innerHTML = "";
  });

  $("card-select").addEventListener("change", function () {
    var path = $("card-select").value;
    if (!path) {
      selectCard("");
      return;
    }
    selectCard(path);
  });

  $("btn-refresh-cards").addEventListener("click", function () {
    post("rescan", {});
    fetchState();
    addLog("已刷新设备列表", "info");
  });

  $("btn-sync-now").addEventListener("click", function () {
    if (!state.selectedCard) {
      addLog("请先选择一台设备", "warn");
      return;
    }
    post("sync_now", { path: state.selectedCard }).then(function (data) {
      if (data && data.error) {
        addLog("同步失败：" + data.error, "error");
        setConfigHint(data.error, "err");
      } else {
        addLog("已手动触发同步：" + state.selectedCard, "info");
      }
    });
  });

  $("btn-cancel-sync").addEventListener("click", function () {
    post("cancel", {});
    addLog("已请求取消同步", "warn");
  });

  $("auto-enable").addEventListener("change", function () {
    var enabled = $("auto-enable").checked;
    readModelFromForm();
    updateYamlPreview();
    markDirty();
    if (!state.selectedCard) {
      addLog("请先选择一台设备", "warn");
      return;
    }
    post("set_enabled", { path: state.selectedCard, enable: enabled })
      .then(function (data) {
        if (data && data.error) {
          addLog("开关设置失败：" + data.error, "error");
          $("auto-enable").checked = !enabled;
          return;
        }
        if (data && data.pending) {
          addLog("自动同步已" + (enabled ? "开启" : "关闭") + "（创建配置文件后生效）", "info");
          return;
        }
        addLog("自动同步已" + (enabled ? "开启" : "关闭"), "ok");
        fetchState();
      });
  });

  $("btn-create-config").addEventListener("click", function () {
    // 进入创建模式：显示编辑器，给一条空规则
    $("empty-card").hidden = true;
    $("editor-card").hidden = false;
    if (!editor.model || !editor.model.rules.length) {
      writeModelToForm({
        version: 1,
        enable: $("auto-enable").checked,
        destination_root: "",
        overwrite: false,
        exclude: [],
        rules: [{ source: "", destination: "", exclude: [], enabled: true, overwrite: null }]
      });
    }
    setConfigHint("请填写源路径与目标路径，保存后将写入设备根目录的 dsync.yml", "warn");
    $("create-hint").textContent = "";
    markDirty();
    var inputs = $("cfg-rules").querySelectorAll(".r-source");
    if (inputs.length) inputs[0].focus();
  });

  function post(action, payload) {
    return fetch("/api/" + action, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload || {})
    }).then(function (r) { return r.json(); })
      .then(function (data) {
        // 后端统一包装为 {ok, result}，此处拆出 result 供调用方使用
        if (data && data.ok === false) {
          return { error: data.error || "操作失败" };
        }
        if (data && Object.prototype.hasOwnProperty.call(data, "result")) {
          return data.result;
        }
        return data;
      })
      .catch(function (e) {
        addLog("请求失败：" + e, "error");
        return { error: String(e) };
      });
  }

  bindEditor();
  bindLogFilters();
  render();
  connect();
  fetchState();
  setInterval(fetchState, 5000);
  // 耗时列每秒刷新：同步中实时显示「当前时间 - 开始时间」
  setInterval(refreshElapsed, 1000);
})();
