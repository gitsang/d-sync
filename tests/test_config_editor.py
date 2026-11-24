"""Config-editing tests: load -> edit -> validate -> save -> take effect."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsync.app import SyncApplication  # noqa: E402
from dsync.config import (  # noqa: E402
    ConfigError,
    config_to_editable,
    editable_to_yaml_text,
    load_config_file,
    parse_yaml_text,
)


# ---------------------------------------------------------------- serialisation
def test_editable_roundtrip():
    """edit model -> YAML -> parse -> edit model: the fields must survive the round trip."""
    model = {
        "version": 1,
        "destination_root": "D:/Backup",
        "overwrite": True,
        "exclude": ["*.tmp"],
        "rules": [
            {"source": "DCIM", "destination": "Photos", "exclude": ["*.bak"],
             "overwrite": None},
            {"source": "Docs", "destination": "Documents", "exclude": [],
             "overwrite": False},
        ],
    }
    text = editable_to_yaml_text(model)
    config = parse_yaml_text(text)
    back = config_to_editable(config)

    assert back["destination_root"] == "D:/Backup"
    assert back["overwrite"] is True
    assert back["exclude"] == ["*.tmp"]
    assert len(back["rules"]) == 2
    assert back["rules"][0]["source"] == "DCIM"
    assert back["rules"][0]["destination"] == "Photos"
    assert back["rules"][0]["exclude"] == ["*.bak"]
    assert back["rules"][0]["overwrite"] is None
    assert back["rules"][1]["overwrite"] is False


def test_label_field_is_gone():
    """The label field was removed and must appear in neither the model nor the YAML."""
    model = {"rules": [{"source": "DCIM", "destination": "Photos",
                        "label": "camera photos"}]}
    text = editable_to_yaml_text(model)
    assert "label" not in text
    config = parse_yaml_text(text)
    assert not hasattr(config.rules[0], "label")
    back = config_to_editable(config)
    assert "label" not in back["rules"][0]


def test_editable_requires_rules():
    with pytest.raises(ConfigError):
        editable_to_yaml_text({"rules": []})


def test_editable_skips_incomplete_rules():
    model = {
        "rules": [
            {"source": "DCIM", "destination": "Photos"},
            {"source": "", "destination": "X"},       # missing source -> dropped
            {"source": "Y", "destination": ""},        # missing destination -> dropped
        ]
    }
    text = editable_to_yaml_text(model)
    config = parse_yaml_text(text)
    assert len(config.rules) == 1


def test_yaml_text_contains_comment():
    text = editable_to_yaml_text({
        "rules": [{"source": "A", "destination": "B"}]
    })
    assert text.startswith("#")
    assert "source" in text and "destination" in text


# ---------------------------------------------------------------- app API
def _make_card(tmp_path: Path) -> Path:
    card = tmp_path / "CARD"
    (card / "DCIM").mkdir(parents=True)
    (card / "DCIM" / "a.jpg").write_bytes(b"data")
    (card / "dsync.yml").write_text(
        "version: 1\n"
        f"destination_root: '{tmp_path / 'OUT'}'\n"
        "rules:\n"
        "  - source: DCIM\n"
        "    destination: Photos\n",
        encoding="utf-8")
    return card


def test_get_config_reads_existing(tmp_path):
    card = _make_card(tmp_path)
    app = SyncApplication()
    result = app.action_get_config({"card_path": str(card)})
    assert result["exists"] is True
    assert result["path"].endswith("dsync.yml")
    assert result["model"]["rules"][0]["source"] == "DCIM"
    assert result["model"]["rules"][0]["destination"] == "Photos"


def test_get_config_missing_file_returns_blank_model(tmp_path):
    """With no config file on the card, return a blank model with no preset rules."""
    card = tmp_path / "EMPTY"
    card.mkdir()
    app = SyncApplication()
    result = app.action_get_config({"card_path": str(card)})
    assert result["exists"] is False
    assert "hint" in result
    assert result["model"] is not None
    assert result["model"]["rules"] == []
    assert result["model"]["destination_root"] == ""


def test_save_config_writes_yaml(tmp_path):
    card = _make_card(tmp_path)
    app = SyncApplication()
    result = app.action_save_config({
        "card_path": str(card),
        "model": {
            "destination_root": str(tmp_path / "OUT2"),
            "overwrite": True,
            "exclude": ["*.tmp"],
            "rules": [{"source": "DCIM", "destination": "NewPhotos", "exclude": [],
                       "overwrite": None}],
        },
    })
    assert result["saved"] is True

    # The file really was updated and can be parsed again
    text = (card / "dsync.yml").read_text(encoding="utf-8")
    assert "NewPhotos" in text
    config = load_config_file(card / "dsync.yml", card_root=card)
    assert config.rules[0].destination == "NewPhotos"
    assert config.overwrite is True


def test_save_config_rejects_destination_inside_card(tmp_path):
    card = _make_card(tmp_path)
    app = SyncApplication()
    result = app.action_save_config({
        "card_path": str(card),
        "model": {"rules": [{"source": "DCIM", "destination": str(card / "sub")}]},
    })
    assert result["saved"] is False
    assert "SD card" in result["error"]


def test_validate_config_detects_error(tmp_path):
    card = _make_card(tmp_path)
    app = SyncApplication()
    result = app.action_validate_config({
        "card_path": str(card),
        "model": {"rules": [{"source": "DCIM", "destination": str(card / "inside")}]},
    })
    assert result["valid"] is False

    ok = app.action_validate_config({
        "card_path": str(card),
        "model": {"rules": [{"source": "DCIM", "destination": "D:/Safe/Photos"}]},
    })
    assert ok["valid"] is True


def test_no_template_fallback_for_card_without_config(tmp_path):
    """No local templates: with no config file on the card there is no sync and no output."""
    app = SyncApplication()
    card = tmp_path / "NOCFG"
    (card / "DCIM").mkdir(parents=True)
    (card / "DCIM" / "x.jpg").write_bytes(b"z")
    app.on_device_inserted(card)

    snapshot = app.snapshot()
    assert snapshot["stats"] is None, "no sync should run without a config file"
    assert not (tmp_path / "TPL_OUT").exists()
    # The device is recorded as "detected" and flagged as having no config
    devices = snapshot["devices"]
    assert len(devices) == 1
    assert devices[0]["config"] == ""
    assert devices[0]["state"] == "detected"


def test_save_config_never_writes_template(tmp_path):
    """Saving writes only the YAML on the card and leaves no template file in the app data dir."""
    card = _make_card(tmp_path)
    app = SyncApplication()
    app.action_save_config({
        "card_path": str(card),
        "model": {"rules": [{"source": "DCIM", "destination": "Photos"}]},
    })
    data_dir = Path(__import__("dsync.logging_setup", fromlist=["x"]).app_data_dir())
    leftovers = list(data_dir.glob("*.yml")) + list(data_dir.glob("*.yaml"))
    assert leftovers == [], f"no config file should exist in the app data dir: {leftovers}"


def test_set_enabled_on_card_without_config_is_pending(tmp_path):
    """With no config file on the card, the toggle is kept in memory (pending) and not written."""
    card = tmp_path / "NOSW"
    card.mkdir()
    app = SyncApplication()
    result = app.action_set_enabled({"path": str(card), "enable": False})
    assert result["enable"] is False
    assert result.get("pending") is True
    assert not (card / "dsync.yml").exists()


def test_delete_config(tmp_path):
    card = _make_card(tmp_path)
    app = SyncApplication()
    result = app.action_delete_config({"card_path": str(card)})
    assert result["deleted"] is True
    assert not (card / "dsync.yml").exists()


def test_preview_config_returns_yaml(tmp_path):
    card = _make_card(tmp_path)
    app = SyncApplication()
    result = app.action_preview_config({"card_path": str(card)})
    assert "source" in result["yaml"]
    assert "DCIM" in result["yaml"]
