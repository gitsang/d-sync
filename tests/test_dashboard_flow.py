"""看板交互流程的测试：选卡、enable 开关、路径选择、手动同步。"""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dsync.app import SyncApplication  # noqa: E402
from dsync.config import parse_config  # noqa: E402

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _make_card(tmp_path: Path, *, with_config: bool = True,
               enable: bool = True) -> Path:
    """造一个模拟 SD 卡。"""
    card = tmp_path / "E"
    (card / "DCIM" / "100CANON").mkdir(parents=True, exist_ok=True)
    (card / "DCIM" / "100CANON" / "IMG_0001.jpg").write_bytes(b"a" * 1024)
    (card / "DCIM" / "100CANON" / "IMG_0002.jpg").write_bytes(b"b" * 2048)
    (card / "Documents").mkdir(exist_ok=True)
    (card / "Documents" / "note.txt").write_bytes(b"c" * 512)

    if with_config:
        dest = tmp_path / "OUT"
        (card / "dsync.yml").write_text(
            "version: 1\n"
            f"enable: {'true' if enable else 'false'}\n"
            f"destination_root: '{dest.as_posix()}'\n"
            "rules:\n"
            "  - source: DCIM\n"
            "    destination: Photos\n"
            "  - source: Documents\n"
            "    destination: Docs\n",
            encoding="utf-8",
        )
    return card


# ====================================================================== 配置
def test_config_enable_parses():
    cfg = parse_config({"enable": False, "rules": [
        {"source": "DCIM", "destination": "P"}]})
    assert cfg.enabled is False


def test_config_enable_defaults_true():
    cfg = parse_config({"rules": [{"source": "DCIM", "destination": "P"}]})
    assert cfg.enabled is True


def test_config_enable_in_defaults_block():
    cfg = parse_config({"defaults": {"enable": False}, "rules": [
        {"source": "DCIM", "destination": "P"}]})
    assert cfg.enabled is False


def test_rule_enabled_false_is_kept_but_excluded():
    cfg = parse_config({"rules": [
        {"source": "DCIM", "destination": "P", "enabled": False},
        {"source": "Docs", "destination": "D"}]})
    assert len(cfg.rules) == 2
    assert len(cfg.active_rules()) == 1
    assert cfg.active_rules()[0].source == "Docs"


def test_editable_model_roundtrip_keeps_enable():
    from dsync.config import config_to_editable, editable_to_yaml_text, parse_yaml_text

    cfg = parse_config({"enable": False, "destination_root": "D:/B",
                        "rules": [{"source": "DCIM", "destination": "P"}]})
    model = config_to_editable(cfg)
    assert model["enable"] is False
    text = editable_to_yaml_text(model)
    assert "enable: false" in text
    again = parse_yaml_text(text)
    assert again.enabled is False


def test_editable_model_roundtrip_keeps_rule_enabled():
    from dsync.config import config_to_editable, editable_to_yaml_text, parse_yaml_text

    cfg = parse_config({"rules": [
        {"source": "DCIM", "destination": "P", "enabled": False}]})
    model = config_to_editable(cfg)
    assert model["rules"][0]["enabled"] is False
    again = parse_yaml_text(editable_to_yaml_text(model))
    assert again.rules[0].enabled is False


# ====================================================================== 应用
def test_select_card_without_config_reports_no_config(tmp_path):
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=False)
    result = app.action_select_card({"path": str(card)})
    assert result["has_config"] is False
    assert result["selected"] == str(card)


def test_select_card_with_config_reports_rules(tmp_path):
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=True)
    result = app.action_select_card({"path": str(card)})
    assert result["has_config"] is True
    assert result["rules"] == 2
    assert result["active_rules"] == 2
    assert result["enable"] is True


def test_select_card_with_enable_false(tmp_path):
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=True, enable=False)
    result = app.action_select_card({"path": str(card)})
    assert result["enable"] is False


def test_select_card_updates_snapshot_selected(tmp_path):
    app = SyncApplication()
    card = _make_card(tmp_path)
    app.action_select_card({"path": str(card)})
    assert app.snapshot()["selected_card"] == str(card)


def test_handle_device_does_not_autosync_when_enable_false(tmp_path, monkeypatch):
    """enable=false 时插入设备不应自动同步。"""
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=True, enable=False)
    out = tmp_path / "OUT"

    called = []
    monkeypatch.setattr(app, "run_sync",
                        lambda *a, **k: called.append(a))

    app.on_device_inserted(card)
    assert called == [], "enable=false 时不应自动同步"
    assert not out.exists(), "不应产生任何输出"


def test_handle_device_autosyncs_when_enable_true(tmp_path, monkeypatch):
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=True, enable=True)
    called = []
    monkeypatch.setattr(app, "run_sync", lambda *a, **k: called.append(a))
    app.on_device_inserted(card)
    assert len(called) == 1, "enable=true 时应自动同步"


def test_set_enabled_writes_back_to_yaml(tmp_path):
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=True, enable=True)
    result = app.action_set_enabled({"path": str(card), "enable": False})
    assert result["enable"] is False
    text = (card / "dsync.yml").read_text(encoding="utf-8")
    assert "enable: false" in text


def test_set_enabled_can_turn_back_on(tmp_path):
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=True, enable=False)
    result = app.action_set_enabled({"path": str(card), "enable": True})
    assert result["enable"] is True
    assert "enable: true" in (card / "dsync.yml").read_text(encoding="utf-8")


def test_action_sync_now_rejects_when_no_config(tmp_path, monkeypatch):
    """卡上没有配置文件时，手动同步应报错且不启动任何任务。"""
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=False)

    started = []
    monkeypatch.setattr(app, "run_sync",
                        lambda root, config: started.append((root, config)))
    result = app.action_sync_now({"path": str(card)})
    assert "error" in result
    assert started == []


def test_no_local_template_mechanism(tmp_path):
    """本地模板机制已移除：应用层不应再暴露 _template_path。"""
    app = SyncApplication()
    assert not hasattr(app, "_template_path")


def test_action_sync_now_ignores_enable_false(tmp_path, monkeypatch):
    """enable=false 时手动点击仍应执行同步。"""
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=True, enable=False)
    started = []
    monkeypatch.setattr(app, "run_sync",
                        lambda root, config: started.append((root, config)))
    result = app.action_sync_now({"path": str(card)})
    assert result.get("started") is True
    assert len(started) == 1


def test_action_pick_path_reports_unavailable_gracefully(monkeypatch):
    """路径选择不可用时不应抛异常，而是返回可读错误。"""
    from dsync import app as app_module

    def _boom(*args, **kwargs):
        raise app_module.PickerUnavailable("测试环境不支持")

    monkeypatch.setattr(app_module, "pick_path_for_rule", _boom)
    app = SyncApplication()
    result = app.action_pick_path({"kind": "destination", "current": ""})
    assert "error" in result
    assert "选择窗口" in result["error"]


def test_action_pick_path_returns_cancelled(monkeypatch):
    """用户取消时返回 cancelled 标记。"""
    from dsync import app as app_module

    monkeypatch.setattr(
        app_module, "pick_path_for_rule",
        lambda *a, **k: {"cancelled": True, "path": "", "absolute": ""})
    app = SyncApplication()
    result = app.action_pick_path({"kind": "destination", "current": ""})
    assert result["cancelled"] is True


def test_action_pick_path_passes_selected_card(monkeypatch):
    """未显式指定卡时，应使用页面当前选中的卡。"""
    from dsync import app as app_module

    seen = {}

    def _capture(kind, card_root, current):
        seen["kind"] = kind
        seen["card_root"] = card_root
        return {"cancelled": True, "path": "", "absolute": ""}

    monkeypatch.setattr(app_module, "pick_path_for_rule", _capture)
    app = SyncApplication()
    app._selected_card = "E:\\"
    app.action_pick_path({"kind": "source", "current": ""})
    assert seen["kind"] == "source"
    assert seen["card_root"] == "E:\\"


# ====================================================================== 路径选择
def test_pick_path_for_source_converts_to_relative(monkeypatch, tmp_path):
    """源路径选择结果应换算成相对卡根目录的路径。"""
    from dsync import filedialog

    card = tmp_path / "E"
    (card / "DCIM" / "100CANON").mkdir(parents=True)

    monkeypatch.setattr(filedialog, "pick_folder",
                        lambda start=None, title="": str(card / "DCIM" / "100CANON"))
    result = filedialog.pick_path_for_rule("source", card, "")
    assert result["cancelled"] is False
    assert result["path"] == "DCIM/100CANON"
    assert result["absolute"] == str(card / "DCIM" / "100CANON")


def test_pick_path_for_source_rejects_outside_card(monkeypatch, tmp_path):
    """选到卡外的目录应报错。"""
    from dsync import filedialog

    card = tmp_path / "E"
    card.mkdir()
    outside = tmp_path / "C"
    outside.mkdir()

    monkeypatch.setattr(filedialog, "pick_folder", lambda start=None, title="": str(outside))
    with pytest.raises(ValueError):
        filedialog.pick_path_for_rule("source", card, "")


def test_pick_path_for_destination_returns_absolute(monkeypatch, tmp_path):
    from dsync import filedialog

    target = tmp_path / "OUT"
    target.mkdir()
    monkeypatch.setattr(filedialog, "pick_folder", lambda start=None, title="": str(target))
    result = filedialog.pick_path_for_rule("destination", None, "")
    assert result["cancelled"] is False
    assert result["path"] == str(target)


def test_pick_path_cancelled(monkeypatch):
    from dsync import filedialog

    monkeypatch.setattr(filedialog, "pick_folder", lambda start=None, title="": None)
    result = filedialog.pick_path_for_rule("destination", None, "")
    assert result["cancelled"] is True


def test_filedialog_module_imports_cleanly():
    """模块必须能在无 COM 环境下正常导入。"""
    from dsync import filedialog
    assert hasattr(filedialog, "pick_folder")
    assert hasattr(filedialog, "pick_path_for_rule")


# ====================================================================== HTTP
@pytest.fixture()
def live_app(tmp_path):
    """启动一个真实的后端 + HTTP 服务。

    端口用 0 让系统分配，避免与残留进程或并发测试抢端口。
    """
    app = SyncApplication()
    web = app.start_web(host="127.0.0.1", port=0)
    base = f"http://127.0.0.1:{web.port}"
    yield app, base
    web.stop()


def _post(base: str, action: str, payload: dict) -> dict:
    req = urllib.request.Request(
        base + "/api/" + action,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with OPENER.open(req, timeout=20) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body.get("result", body)


def test_http_select_card_flow(live_app, tmp_path):
    app, base = live_app
    card = _make_card(tmp_path, with_config=True)

    result = _post(base, "select_card", {"path": str(card)})
    assert result["has_config"] is True
    assert result["rules"] == 2

    cfg = _post(base, "get_config", {"card_path": str(card)})
    assert cfg["exists"] is True
    assert len(cfg["model"]["rules"]) == 2
    assert cfg["model"]["enable"] is True


def test_http_select_card_without_config(live_app, tmp_path):
    app, base = live_app
    card = _make_card(tmp_path, with_config=False)
    result = _post(base, "select_card", {"path": str(card)})
    assert result["has_config"] is False


def test_http_set_enabled(live_app, tmp_path):
    app, base = live_app
    card = _make_card(tmp_path, with_config=True, enable=True)
    result = _post(base, "set_enabled", {"path": str(card), "enable": False})
    assert result["enable"] is False
    assert "enable: false" in (card / "dsync.yml").read_text(encoding="utf-8")


def test_http_save_config_persists_enable(live_app, tmp_path):
    app, base = live_app
    card = _make_card(tmp_path, with_config=True, enable=True)
    model = {
        "version": 1,
        "enable": False,
        "destination_root": (tmp_path / "OUT").as_posix(),
        "overwrite": False,
        "exclude": [],
        "rules": [{"source": "DCIM", "destination": "Photos",
                   "exclude": [], "enabled": True,
                   "overwrite": None}],
    }
    result = _post(base, "save_config",
                   {"model": model, "card_path": str(card)})
    assert result["saved"] is True
    assert result["enable"] is False

    text = (card / "dsync.yml").read_text(encoding="utf-8")
    assert "enable: false" in text

    reread = _post(base, "get_config", {"card_path": str(card)})
    assert reread["model"]["enable"] is False


def test_http_save_config_does_not_autosync_when_disabled(live_app, tmp_path, monkeypatch):
    app, base = live_app
    card = _make_card(tmp_path, with_config=True, enable=True)
    called = []
    monkeypatch.setattr(app, "run_sync", lambda *a, **k: called.append(a))

    model = {
        "version": 1, "enable": False,
        "destination_root": (tmp_path / "OUT").as_posix(),
        "overwrite": False, "exclude": [],
        "rules": [{"source": "DCIM", "destination": "Photos", "exclude": [],
                   "enabled": True, "overwrite": None}],
    }
    _post(base, "save_config",
          {"model": model, "card_path": str(card)})
    import time
    time.sleep(0.4)
    assert called == [], "enable=false 时保存配置不应触发同步"


def test_http_index_contains_select_device_section(live_app):
    app, base = live_app
    with OPENER.open(base + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert "选择设备" in html
    assert "立即同步" in html
    assert "自动同步" in html
    assert "尚未创建配置文件" in html
    # 全局状态区已移除，改为每设备一行的状态表
    assert "设备状态" in html
    assert "同步进度" in html
    assert "本次同步耗时" in html
    assert "全局状态" not in html
    # 本地模板入口已移除
    assert "存为模板" not in html


def test_http_index_has_log_level_filters(live_app):
    """运行日志支持按等级过滤。"""
    app, base = live_app
    with OPENER.open(base + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    assert "log-filters" in html
    for level in ("debug", "info", "warn", "error"):
        assert f'data-log-level="{level}"' in html


def test_http_index_places_save_button_in_card_head(live_app):
    """保存配置按钮应在同步配置卡片右上角。"""
    app, base = live_app
    with OPENER.open(base + "/", timeout=10) as resp:
        html = resp.read().decode("utf-8")
    editor_pos = html.index('id="editor-card"')
    head_pos = html.index('id="btn-save-config"', editor_pos)
    # 保存按钮位于卡片头部（card-head）内、YAML 预览之前
    card_head_end = html.index("</div>", html.index('class="card-head"', editor_pos))
    assert head_pos < card_head_end
    # YAML 预览位于规则列表下方
    assert html.index('id="cfg-rules"') < html.index('id="yaml-preview"')
    # 旧的侧栏容器已移除
    assert "editor-side" not in html


def test_sync_now_records_started_at(tmp_path, monkeypatch):
    """同步进行中应记录开始时间，供前端实时计算耗时。"""
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=True)
    app.on_device_inserted(card)
    key = str(card).upper().rstrip("\\")
    assert key in app._devices
    assert "started_at" in app._devices[key]


def test_device_snapshot_exposes_elapsed_fields(tmp_path):
    """设备状态需带上耗时相关的字段。"""
    app = SyncApplication()
    card = _make_card(tmp_path, with_config=True)
    app.on_device_inserted(card)
    snapshot = app.snapshot()
    assert snapshot["devices"]
    device = snapshot["devices"][0]
    for field in ("total_files", "done_files", "skipped_files",
                  "copied_files", "elapsed", "started_at"):
        assert field in device

