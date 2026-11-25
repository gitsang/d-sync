"""Unit tests: config parsing, dedup decisions and rule matching."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsync.config import ConfigError, parse_config  # noqa: E402
from dsync.syncer import SyncEngine, match_rules, quick_hash  # noqa: E402


# ---------------------------------------------------------------- config parsing
def test_parse_simple_form():
    config = parse_config({"source": "DCIM", "destination": "D:/Photos/DCIM"})
    assert len(config.rules) == 1
    assert config.rules[0].source == "DCIM"
    assert config.rules[0].destination == "D:/Photos/DCIM"


def test_parse_rules_list():
    config = parse_config({
        "version": 1,
        "destination_root": "D:/Backup",
        "rules": [
            {"source": "DCIM", "destination": "Photos"},
            {"source": "Documents/*.pdf", "destination": "Docs"},
        ],
    })
    assert len(config.rules) == 2
    assert config.destination_root == "D:/Backup"
    # Relative destinations resolve against destination_root
    resolved = config.resolve_destination(config.rules[0])
    assert str(resolved).replace("\\", "/").lower().endswith("d:/backup/photos")


def test_parse_mappings_dict():
    config = parse_config({
        "destination_root": "D:/Backup",
        "mappings": {"DCIM": "Photos", "Music": "Audio"},
    })
    assert len(config.rules) == 2
    assert {r.source for r in config.rules} == {"DCIM", "Music"}


def test_parse_aliases():
    config = parse_config({
        "rules": [{"src": "DCIM", "dest": "P", "exclude": ["*.tmp"]}]
    })
    rule = config.rules[0]
    assert rule.source == "DCIM"
    assert rule.destination == "P"
    assert rule.exclude == ("*.tmp",)


def test_parse_missing_destination_raises():
    with pytest.raises(ConfigError):
        parse_config({"rules": [{"source": "DCIM"}]})


def test_parse_empty_raises():
    with pytest.raises(ConfigError):
        parse_config({})


def test_parse_overwrite_inheritance():
    config = parse_config({
        "overwrite": True,
        "rules": [{"source": "A", "destination": "B"}],
    })
    assert config.overwrite is True
    assert config.rules[0].overwrite is None


def test_exclude_matching():
    config = parse_config({
        "rules": [{"source": "DCIM", "destination": "P", "exclude": ["*.tmp", "System Volume Information"]}]
    })
    rule = config.rules[0]
    assert rule.matches("a/b/c.tmp") is True
    assert rule.matches("System Volume Information") is True
    assert rule.matches("a/b/photo.jpg") is False


# ---------------------------------------------------------------- file matching
def _make_tree(root: Path):
    (root / "DCIM" / "100CAMERA").mkdir(parents=True)
    (root / "DCIM" / "100CAMERA" / "a.jpg").write_bytes(b"photo-a")
    (root / "DCIM" / "100CAMERA" / "b.jpg").write_bytes(b"photo-b")
    (root / "Docs").mkdir()
    (root / "Docs" / "c.pdf").write_bytes(b"doc-c")


def test_match_rules(tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    _make_tree(card)

    config = parse_config({
        "destination_root": str(tmp_path / "out"),
        "rules": [{"source": "DCIM", "destination": "Photos"},
                  {"source": "Docs", "destination": "Docs"}],
    })
    tasks = match_rules(card, config)
    assert len(tasks) == 3
    dests = {str(t.destination).replace("\\", "/") for t in tasks}
    assert any(d.endswith("100CAMERA/a.jpg") for d in dests)


def test_match_rules_respects_exclude(tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    _make_tree(card)
    (card / "DCIM" / "100CAMERA" / "cache.tmp").write_bytes(b"x")

    config = parse_config({
        "destination_root": str(tmp_path / "out"),
        "rules": [{"source": "DCIM", "destination": "Photos", "exclude": ["*.tmp"]}],
    })
    tasks = match_rules(card, config)
    assert all(t.source.name != "cache.tmp" for t in tasks)
    assert len(tasks) == 2


def test_match_rules_respects_global_exclude(tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    _make_tree(card)

    config = parse_config({
        "destination_root": str(tmp_path / "out"),
        "exclude": ["*.pdf"],
        "rules": [{"source": ".", "destination": "All"}],
    })
    tasks = match_rules(card, config)
    assert all(t.source.name != "c.pdf" for t in tasks)
    assert len(tasks) == 2


# ---------------------------------------------------------------- dedup
def test_quick_hash_equal(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"hello world" * 100)
    b.write_bytes(b"hello world" * 100)
    assert quick_hash(a) == quick_hash(b)


def test_quick_hash_different(tmp_path):
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    a.write_bytes(b"hello world" * 100)
    b.write_bytes(b"goodbye world" * 100)
    assert quick_hash(a) != quick_hash(b)


def test_sync_copies_then_skips_duplicate(tmp_path):
    """Core scenario: copy on the first run, skip everything as duplicates on the second."""
    card = tmp_path / "card"
    card.mkdir()
    _make_tree(card)
    out = tmp_path / "out"

    config = parse_config({
        "destination_root": str(out),
        "rules": [{"source": "DCIM", "destination": "Photos"}],
    })

    engine = SyncEngine(config)
    stats = engine.run(card)
    assert stats.copied_files == 2
    assert stats.skipped_files == 0
    assert (out / "Photos" / "100CAMERA" / "a.jpg").exists()

    # Second run: everything should be skipped
    engine2 = SyncEngine(config)
    stats2 = engine2.run(card)
    assert stats2.copied_files == 0
    assert stats2.skipped_files == 2


def test_sync_overwrite_policy(tmp_path):
    """Different content is overwritten when allowed and skipped when not."""
    card = tmp_path / "card"
    card.mkdir()
    (card / "f.txt").write_bytes(b"v1")
    out = tmp_path / "out"
    out.mkdir()
    (out / "P").mkdir()
    (out / "P" / "f.txt").write_bytes(b"v2")

    config = parse_config({
        "destination_root": str(out),
        "overwrite": True,
        "rules": [{"source": "f.txt", "destination": "P"}],
    })
    stats = SyncEngine(config).run(card)
    assert stats.copied_files == 1
    assert (out / "P" / "f.txt").read_bytes() == b"v1"

    # overwrite=false -> different content is skipped too (keep the destination)
    config2 = parse_config({
        "destination_root": str(out),
        "overwrite": False,
        "rules": [{"source": "f.txt", "destination": "P"}],
    })
    (card / "f.txt").write_bytes(b"v3")
    stats2 = SyncEngine(config2).run(card)
    assert stats2.copied_files == 0
    assert stats2.skipped_files == 1
    assert (out / "P" / "f.txt").read_bytes() == b"v1"


def test_sync_reports_progress_events(tmp_path):
    card = tmp_path / "card"
    card.mkdir()
    _make_tree(card)
    config = parse_config({
        "destination_root": str(tmp_path / "out"),
        "rules": [{"source": "DCIM", "destination": "P"}],
    })

    events = []
    engine = SyncEngine(config, progress=lambda e, p: events.append((e, p)))
    engine.run(card)

    kinds = [e for e, _ in events]
    assert "plan" in kinds
    assert "file_start" in kinds
    assert "file_done" in kinds
    assert "finished" in kinds
    finished = [p for e, p in events if e == "finished"][0]
    assert finished["stats"]["copied_files"] == 2


def test_file_done_carries_log_level(tmp_path):
    """file_done events must carry a log level: skip=DEBUG, copy=INFO, failure=ERROR."""
    card = tmp_path / "card"
    card.mkdir()
    _make_tree(card)
    config = parse_config({
        "destination_root": str(tmp_path / "out"),
        "rules": [{"source": "DCIM", "destination": "P"}],
    })

    events = []
    engine = SyncEngine(config, progress=lambda e, p: events.append((e, p)))
    engine.run(card)
    # Second run: everything hits a duplicate -> skipped
    engine.run(card)

    levels = {}
    for kind, payload in events:
        if kind == "file_done":
            levels.setdefault(payload["status"], set()).add(payload.get("level"))

    assert levels["copied"] == {"info"}
    assert levels["skipped"] == {"debug"}


# ---------------------------------------------------------------- legacy .dsync format
def test_parse_legacy_config_basic():
    """The shell implementation's key=value card config must still load."""
    from dsync.config import parse_legacy_config

    config = parse_legacy_config(
        "enabled=true\n"
        "name=camera-card-1\n"
        "sync=DCIM /mnt/backup/photos\n"
        "sync=PRIVATE/M4ROOT/CLIP /mnt/backup/video clips\n"
        "exclude=.Trashes .Spotlight-V100\n"
        "overwrite=true\n"
    )
    assert [r.source for r in config.rules] == ["DCIM", "PRIVATE/M4ROOT/CLIP"]
    # The destination may contain spaces: only the first whitespace run splits.
    assert config.rules[1].destination == "/mnt/backup/video clips"
    assert config.exclude == (".Trashes", ".Spotlight-V100")
    assert config.overwrite is True
    assert config.enabled is True


def test_parse_legacy_config_disable_variants():
    from dsync.config import parse_legacy_config

    for value in ("false", "0", "no", "off", "OFF"):
        assert parse_legacy_config(f"enabled={value}\nsync=DCIM /mnt/x\n").enabled is False


def test_parse_legacy_config_ignores_rsync_flags():
    """`flags` drove rsync in the shell version; here it must be accepted and ignored."""
    from dsync.config import parse_legacy_config

    config = parse_legacy_config(
        "flags=--archive --delete --info=progress2\nsync=DCIM /mnt/x\n"
    )
    assert len(config.rules) == 1


def test_parse_legacy_config_malformed_sync_is_skipped():
    from dsync.config import parse_legacy_config

    config = parse_legacy_config("sync=onlyoneword\nsync=DCIM /mnt/good\n")
    assert [r.source for r in config.rules] == ["DCIM"]


def test_parse_legacy_config_without_sync_is_not_an_error():
    """A card with a config but no rules is 'nothing to do', not a failure."""
    from dsync.config import parse_legacy_config

    assert parse_legacy_config("enabled=true\nname=empty\n").rules == []


def test_legacy_config_is_discovered_and_loaded(tmp_path):
    """End to end: a .dsync card is found by find_config_file and parsed by load_config_file."""
    from dsync.config import find_config_file, load_config_file

    (tmp_path / ".dsync").write_text("sync=DCIM /mnt/out\n", encoding="utf-8")
    found = find_config_file(tmp_path)
    assert found is not None and found.name == ".dsync"
    config = load_config_file(found, card_root=tmp_path)
    assert config.rules[0].source == "DCIM"


def test_yaml_config_wins_over_legacy(tmp_path):
    """When a card carries both formats the richer YAML one is used."""
    from dsync.config import find_config_file

    (tmp_path / "dsync.yml").write_text("rules:\n  - source: A\n    destination: /mnt/a\n")
    (tmp_path / ".dsync").write_text("sync=B /mnt/b\n")
    found = find_config_file(tmp_path)
    assert found is not None and found.name == "dsync.yml"


# ---------------------------------------------------------------- safety guards
def test_rule_source_rejects_absolute_and_dotdot():
    from dsync.syncer import validate_rule_source

    assert validate_rule_source("DCIM") is None
    assert validate_rule_source("PRIVATE/M4ROOT/CLIP") is None
    assert validate_rule_source("") is None
    assert validate_rule_source("/etc") is not None
    assert validate_rule_source("../../etc") is not None
    assert validate_rule_source("a/../../b") is not None


def test_config_file_is_never_copied(tmp_path):
    """The card config that drives the sync must not land in the destination."""
    from dsync.config import parse_config

    card = tmp_path / "card"
    (card / "DCIM").mkdir(parents=True)
    (card / "DCIM" / "photo.jpg").write_bytes(b"x")
    (card / ".dsync").write_text("sync=DCIM /mnt/out\n")
    out = tmp_path / "out"
    out.mkdir()

    config = parse_config({"source": "DCIM", "destination": str(out)})
    engine = SyncEngine(config)
    engine.run(card)

    assert not (out / ".dsync").exists()
    assert (out / "photo.jpg").exists()
