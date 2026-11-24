/* D-Sync dashboard front-end logic */
(function () {
  "use strict";

  /* Local alias for the i18n helper provided by i18n.js (loaded before this file). */
  var t = function (key, params) {
    return window.DSyncI18n ? window.DSyncI18n.t(key, params) : key;
  };

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

  /* ---------------- config editor state ---------------- */
  var editor = {
    model: null,        // {version, enable, destination_root, overwrite, exclude, rules}
    loaded: false,
    dirty: false,
    cardPath: "",
    hasConfig: false
  };

  var $ = function (id) { return document.getElementById(id); };

  // ------------------------------------------------------------ utilities
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
    return t("time.minutesSeconds", { m: m, s: String(s).padStart(2, "0") });
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

  // ------------------------------------------------------------ logging
  var LOG_LEVELS = ["debug", "info", "warn", "error"];
  var logLevelEnabled = { debug: true, info: true, warn: true, error: true };

  /* legacy level names -> current levels */
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

  /* filter log lines by the checked levels */
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

  // ============================================================ config editor
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

      // source path + browse button
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
      btnSrc.textContent = t("action.browse");
      btnSrc.title = t("config.pickSourceInDevice");
      btnSrc.addEventListener("click", function () {
        pickPath("source", this.closest("tr").querySelector(".r-source").value);
      });
      srcWrap.appendChild(inSource);
      srcWrap.appendChild(btnSrc);
      tdSource.appendChild(srcWrap);

      // destination path + browse button
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
      btnDest.textContent = t("action.browse");
      btnDest.title = t("config.pickDestination");
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
      chkEnabled.title = t("config.enableRule");
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
      btnDel.textContent = t("action.delete");
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
    tag.textContent = t("config.unsavedTag");
    tag.className = "tag warn";
  }

  function markClean() {
    editor.dirty = false;
    var tag = $("config-target-tag");
    tag.textContent = t("config.targetTag");
    tag.className = "tag accent";
  }

  function updateYamlPreview() {
    var model = readModelFromForm();
    if (!model.rules.length) {
      $("yaml-preview").textContent = t("config.yamlEmpty");
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

  // ------------------------------------------------------------ path picking
  function pickPath(kind, current) {
    setConfigHint(t("picker.opening"), "");
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
        setConfigHint(t("picker.cancelled"), "");
        return;
      }
      // write into the matching input
      var input = null;
      if (kind === "source") {
        input = $("cfg-rules").querySelector(".r-source");
      } else if (kind === "destination") {
        input = $("cfg-rules").querySelector(".r-dest");
      }
      if (input) { input.value = data.path; }
      setConfigHint(t("picker.selected", { path: data.path }), "ok");
      readModelFromForm();
      updateYamlPreview();
      markDirty();
    });
  }

  // ------------------------------------------------------------ device list
  function renderDevices() {
    var body = $("devices");
    body.innerHTML = "";
    var devices = state.devices || [];
    $("device-count").textContent = devices.length;

    if (!devices.length) {
      var empty = document.createElement("tr");
      empty.innerHTML = '<td colspan="4" class="muted">' + escapeHtml(t('devices.empty')) + '</td>';
      body.appendChild(empty);
      return;
    }

    devices.forEach(function (dev) {
      var tr = document.createElement("tr");
      var cls = dev.state === "synced" ? "ok" : dev.state === "syncing" ? "accent"
        : dev.state === "error" ? "err" : "";
      var label = t("state." + dev.state, null);
      if (label === "state." + dev.state) label = dev.state;

      // device name: show only the path line
      var tdName = document.createElement("td");
      var nameBox = document.createElement("div");
      nameBox.className = "dev-name";
      nameBox.textContent = dev.path || dev.name || "";
      nameBox.title = dev.name && dev.name !== dev.path ? dev.name : "";
      tdName.appendChild(nameBox);

      // current state: keep a single status pill
      var tdState = document.createElement("td");
      var tag = document.createElement("span");
      tag.className = "tag " + cls;
      tag.textContent = label;
      tdState.appendChild(tag);

      // progress x/y (z skipped) 99%
      var tdProgress = document.createElement("td");
      var total = dev.total_files || 0;
      var done = dev.done_files || 0;
      var skipped = dev.skipped_files || 0;
      var pct = total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0;
      if (dev.state === "synced" && total > 0) pct = 100;
      var progressText = document.createElement("span");
      progressText.className = "progress-text";
      progressText.textContent = t("log.progress", { done: done, total: total, skipped: skipped, pct: pct });
      tdProgress.appendChild(progressText);
      var bar = document.createElement("div");
      bar.className = "bar mini";
      var fill = document.createElement("div");
      fill.className = "bar-fill" + (dev.state === "synced" ? " done" : "") +
        (dev.state === "error" ? " error" : "");
      fill.style.width = pct + "%";
      bar.appendChild(fill);
      tdProgress.appendChild(bar);

      // elapsed: while syncing, computed live as "now - start time"
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

  /* Elapsed text for a device: while syncing, now minus the start time */
  function elapsedText(dev) {
    var started = dev.started_at || 0;
    if (dev.state === "syncing" && started > 0) {
      return fmtDuration(Math.max(0, Date.now() / 1000 - started));
    }
    if (dev.elapsed) { return fmtDuration(dev.elapsed); }
    return "—";
  }

  /* Refresh the elapsed column every second so a running timer ticks */
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

  // ------------------------------------------------------------ device selection flow
  function refreshCardSelect() {
    var select = $("card-select");
    var current = select.value || state.selectedCard;
    select.innerHTML = "";
    var devices = state.devices || [];

    var emptyOpt = document.createElement("option");
    emptyOpt.value = "";
    emptyOpt.textContent = devices.length ? t("devices.choose") : t("devices.empty");
    select.appendChild(emptyOpt);

    devices.forEach(function (dev) {
      var opt = document.createElement("option");
      opt.value = dev.path;
      var mark = dev.config ? t("devices.hasConfigMark") : t("devices.noConfigMark");
      opt.textContent = (dev.path || dev.name || "") + mark;
      select.appendChild(opt);
    });

    if (current) select.value = current;
    if (select.value !== (current || "")) {
      // the target is not in the list (the device was unplugged)
      select.value = "";
    }
  }

  function selectCard(path) {
    editor.cardPath = path || "";
    state.selectedCard = path || "";

    if (!path) {
      // nothing selected: hide the config area and show the empty state
      editor.hasConfig = false;
      editor.model = emptyModel();
      $("editor-card").hidden = true;
      $("empty-card").hidden = true;
      $("selected-hint").textContent = t("devices.notSelected");
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
        // no config file -> hide the content below and prompt to create one
        editor.hasConfig = false;
        editor.model = emptyModel();
        $("editor-card").hidden = true;
        $("empty-card").hidden = false;
        $("empty-title").textContent = t("empty.title", { path: path });
        $("empty-desc").innerHTML =
          t("empty.desc");
        $("create-hint").textContent = "";
        if (typeof data.enable === "boolean") {
          $("auto-enable").checked = data.enable;
        }
        render();
        return;
      }

      // has a config file -> load the rules and show the config area
      editor.hasConfig = true;
      $("empty-card").hidden = true;
      $("editor-card").hidden = false;
      loadConfig(path);
    });
  }

  function loadConfig(cardPath) {
    setConfigHint(t("config.loading"), "");
    post("get_config", { card_path: cardPath })
      .then(function (data) {
        if (!data || data.error) {
          setConfigHint((data && data.error) || t("config.loadFailed"), "err");
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
          hint = hint || t("config.noConfigHint");
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
      setConfigHint(t("config.noRules"), "err");
      $("save-hint").textContent = t("config.noRules");
      $("save-hint").className = "hint err";
      return;
    }
    if (!editor.cardPath) {
      setConfigHint(t("config.pickDeviceFirst"), "err");
      return;
    }
    $("save-hint").textContent = t("config.saving");
    $("save-hint").className = "hint";
    post("save_config", {
      model: model,
      card_path: editor.cardPath
    }).then(function (data) {
      if (!data) {
        $("save-hint").textContent = t("config.saveFailedNoServer");
        $("save-hint").className = "hint err";
        return;
      }
      if (data.error) {
        $("save-hint").textContent = t("config.saveFailed", { error: data.error });
        $("save-hint").className = "hint err";
        setConfigHint(data.error, "err");
        return;
      }
      $("save-hint").textContent = t("config.saved", { n: data.rules });
      $("save-hint").className = "hint ok";
      markClean();
      editor.hasConfig = true;
      $("empty-card").hidden = true;
      $("editor-card").hidden = false;
      addLog(t("log.configSaved", { path: data.path || editor.cardPath }), "ok");
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
            $("save-hint").textContent = t("config.validated", { n: data.rules });
            $("save-hint").className = "hint ok";
          } else {
            $("save-hint").textContent = t("config.validateFailed", { error: (data && data.error) || t("config.unknownError") });
            $("save-hint").className = "hint err";
          }
        });
    });

    $("btn-save-config").addEventListener("click", saveConfig);

    $("btn-copy-yaml").addEventListener("click", function () {
      var text = $("yaml-preview").textContent;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () {
          $("save-hint").textContent = t("config.copied");
          $("save-hint").className = "hint ok";
        });
      } else {
        setConfigHint(t("config.copyUnsupported"), "warn");
      }
    });

    $("btn-pick-dest-root").addEventListener("click", function () {
      setConfigHint(t("picker.opening"), "");
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
        if (data.cancelled) { setConfigHint(t("picker.cancelled"), ""); return; }
        $("cfg-dest-root").value = data.path;
        setConfigHint(t("picker.selected", { path: data.path }), "ok");
        readModelFromForm();
        updateYamlPreview();
        markDirty();
      });
    });

    // Any form change -> mark unsaved + refresh the preview
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

  // ------------------------------------------------------------ rendering
  var PHASE_TEXT = {
    idle: t("phase.idle"),
    planning: t("phase.planning"),
    copying: t("phase.copying"),
    done: t("phase.done"),
    error: t("phase.error"),
    cancelled: t("phase.cancelled")
  };

  function render() {
    var stats = state.stats || {};

    // sync button state
    $("btn-cancel-sync").disabled = !state.syncing;
    if (!state.syncing) { $("btn-sync-now").disabled = !state.selectedCard; }

    renderDevices();

    // history
    var histBox = $("history");
    histBox.innerHTML = "";
    var history = (window.__history || []);
    $("history-count").textContent = history.length;
    if (!history.length) {
      histBox.innerHTML = '<tr><td colspan="6" class="muted">' + escapeHtml(t('history.empty')) + '</td></tr>';
    }
    history.forEach(function (item) {
      var tr = document.createElement("tr");
      var status = item.status === "ok" ? '<span class="tag ok">' + escapeHtml(t("history.ok")) + '</span>'
        : item.status === "cancelled" ? '<span class="tag warn">' + escapeHtml(t("history.cancelled")) + '</span>'
        : '<span class="tag err">' + escapeHtml(t("history.failed")) + '</span>';
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

    // Keep the selected card when the device list refreshes
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

    // First load: auto-select the first device that has a config
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
      // A device was inserted -- auto-select it when nothing is selected yet
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
      setConn("live", t("conn.live"));
    };

    source.onerror = function () {
      setConn("error", t("conn.error"));
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
          addLog(t("log.scanned", { files: msg.total_files, bytes: fmtBytes(msg.total_bytes) }), "info");
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
            addLog(t("log.copiedTo", { dest: msg.destination }), "info");
          } else if (msg.status === "skipped") {
            addLog(t("log.skippedDup", { dest: msg.destination }), "debug");
          } else {
            addLog(t("log.failed", { what: msg.message || msg.source }), "error");
          }
          render();
          break;
        case "error":
          addLog(msg.message || t("log.unknownError"), "error");
          break;
        case "cancelled":
          addLog(msg.message || t("log.syncCancelled"), "warn");
          break;
        case "finished":
          state.phase = "done";
          state.currentFile = null;
          if (msg.stats) { state.stats = msg.stats; }
          addLog(t("log.syncDone", {
            copied: state.stats ? state.stats.copied_files : 0,
            skipped: state.stats ? state.stats.skipped_files : 0
          }), "ok");
          render();
          fetchState();
          break;
        case "device":
          addLog(msg.message || t("log.deviceEvent", { action: msg.action }), "info");
          fetchState();
          break;
        case "log":
          addLog(msg.message || "", msg.level || "info");
          break;
        case "config":
          addLog(msg.message || t("log.configUpdated"), "ok");
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

  // ------------------------------------------------------------ interactions
  $("btn-rescan").addEventListener("click", function () {
    post("rescan", {});
    addLog(t("log.requestedRescan"), "info");
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
    addLog(t("log.refreshedList"), "info");
  });

  $("btn-sync-now").addEventListener("click", function () {
    if (!state.selectedCard) {
      addLog(t("log.pickDeviceFirst"), "warn");
      return;
    }
    post("sync_now", { path: state.selectedCard }).then(function (data) {
      if (data && data.error) {
        addLog(t("log.syncFailed", { error: data.error }), "error");
        setConfigHint(data.error, "err");
      } else {
        addLog(t("log.manualSync", { path: state.selectedCard }), "info");
      }
    });
  });

  $("btn-cancel-sync").addEventListener("click", function () {
    post("cancel", {});
    addLog(t("log.cancelRequested"), "warn");
  });

  $("auto-enable").addEventListener("change", function () {
    var enabled = $("auto-enable").checked;
    readModelFromForm();
    updateYamlPreview();
    markDirty();
    if (!state.selectedCard) {
      addLog(t("log.pickDeviceFirst"), "warn");
      return;
    }
    post("set_enabled", { path: state.selectedCard, enable: enabled })
      .then(function (data) {
        if (data && data.error) {
          addLog(t("log.toggleFailed", { error: data.error }), "error");
          $("auto-enable").checked = !enabled;
          return;
        }
        if (data && data.pending) {
          addLog(t("log.autoSyncCreated", { state: t(enabled ? "log.enabled" : "log.disabled") }), "info");
          return;
        }
        addLog(t("log.autoSync", { state: t(enabled ? "log.enabled" : "log.disabled") }), "ok");
        fetchState();
      });
  });

  $("btn-create-config").addEventListener("click", function () {
    // Enter create mode: show the editor with one blank rule
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
    setConfigHint(t("log.createHint"), "warn");
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
        // The backend wraps everything as {ok, result}; unwrap result for the caller
        if (data && data.ok === false) {
          return { error: data.error || t("log.actionFailed") };
        }
        if (data && Object.prototype.hasOwnProperty.call(data, "result")) {
          return data.result;
        }
        return data;
      })
      .catch(function (e) {
        addLog(t("log.requestFailed", { error: e }), "error");
        return { error: String(e) };
      });
  }

  /* ---------------- language switching ---------------- */
  function bindLanguage() {
    var sel = $("lang-select");
    if (!sel) return;
    sel.value = window.DSyncI18n.getLang();
    sel.addEventListener("change", function () {
      window.DSyncI18n.setLang(sel.value);
    });
  }

  /* Re-render everything whose text comes from JS after a language change. */
  window.onLangChange = function () {
    var sel = $("lang-select");
    if (sel) sel.value = window.DSyncI18n.getLang();
    render();
    renderDevices();
    refreshCardSelect();
    renderRuleRows();
    updateYamlPreview();
    if (state.selectedCard) selectCard(state.selectedCard);
    fetchState();
  };

  bindEditor();
  bindLogFilters();
  bindLanguage();
  window.DSyncI18n.apply();
  render();
  connect();
  fetchState();
  setInterval(fetchState, 5000);
  // Refresh the elapsed column every second: while syncing show "now - start"
  setInterval(refreshElapsed, 1000);
})();
