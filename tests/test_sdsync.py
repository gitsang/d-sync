"""单元测试：配置解析、去重判定、规则匹配。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dsync.config import ConfigError, parse_config  # noqa: E402
from dsync.syncer import SyncEngine, match_rules, quick_hash  # noqa: E402


# ---------------------------------------------------------------- 配置解析
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
    # 相对目标应基于 destination_root 解析
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


# ---------------------------------------------------------------- 文件匹配
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


# ---------------------------------------------------------------- 去重
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
    """核心场景：首次复制，第二次应全部跳过（重复）。"""
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

    # 第二次运行：应全部跳过
    engine2 = SyncEngine(config)
    stats2 = engine2.run(card)
    assert stats2.copied_files == 0
    assert stats2.skipped_files == 2


def test_sync_overwrite_policy(tmp_path):
    """内容不同且允许覆盖时，应覆盖；不允许覆盖时应跳过。"""
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

    # overwrite=false -> 内容不同也应跳过（保留目标）
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
    """file_done 事件需带日志等级：跳过=DEBUG，复制成功=INFO，失败=ERROR。"""
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
    # 第二次运行：全部命中重复 -> skipped
    engine.run(card)

    levels = {}
    for kind, payload in events:
        if kind == "file_done":
            levels.setdefault(payload["status"], set()).add(payload.get("level"))

    assert levels["copied"] == {"info"}
    assert levels["skipped"] == {"debug"}
