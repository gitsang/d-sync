"""Device discovery: platform dispatch, key identity and the POSIX backend.

The POSIX backend is exercised through its pure helpers (mount-table parsing, octal
unescaping, filtering) so the tests do not need real hardware.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import dsync.devices as devices  # noqa: E402


def test_module_imports_without_touching_win32():
    """Importing on a POSIX host must not evaluate any Win32-only symbol."""
    assert devices.IS_WINDOWS is False
    assert devices.IS_LINUX is True


def test_unescape_mount_field():
    """The kernel escapes spaces/tabs/newlines/backslashes as octal in /proc/mounts."""
    assert devices._unescape_mount_field("/media/my\\040card") == "/media/my card"
    assert devices._unescape_mount_field("/a\\011b") == "/a\tb"
    assert devices._unescape_mount_field("/a\\012b") == "/a\nb"
    assert devices._unescape_mount_field("/a\\134b") == "/a\\b"
    assert devices._unescape_mount_field("/plain/path") == "/plain/path"


def test_device_key_is_case_sensitive_on_posix(tmp_path, monkeypatch):
    """Folding case would make two distinct POSIX paths collide."""
    monkeypatch.setattr(devices, "IS_WINDOWS", False)
    lower = tmp_path / "card"
    upper = tmp_path / "CARD"
    lower.mkdir()
    upper.mkdir()
    assert devices.device_key(lower) != devices.device_key(upper)


def test_device_key_folds_case_on_windows(monkeypatch):
    monkeypatch.setattr(devices, "IS_WINDOWS", True)
    assert devices.device_key("E:\\") == devices.device_key("e:\\")


def test_device_key_normalises_trailing_slash(tmp_path, monkeypatch):
    monkeypatch.setattr(devices, "IS_WINDOWS", False)
    (tmp_path / "card").mkdir()
    assert devices.device_key(str(tmp_path / "card") + "/") == devices.device_key(tmp_path / "card")


def test_pseudo_filesystems_are_skipped(monkeypatch, tmp_path):
    """proc/sysfs/tmpfs/network mounts are never cards."""
    card = tmp_path / "card"
    nfs = tmp_path / "nfs"
    card.mkdir()
    nfs.mkdir()
    fake = [
        ("proc", "/proc", "proc"),
        ("sysfs", "/sys", "sysfs"),
        ("tmpfs", "/run", "tmpfs"),
        ("/dev/sda1", "/", "ext4"),
        ("/dev/sdb1", str(card), "vfat"),
        ("server:/export", str(nfs), "nfs"),
    ]
    monkeypatch.setattr(devices, "_posix_mounts", lambda: fake)
    monkeypatch.setattr(devices, "_posix_flags",
                        lambda dev, mp: (dev == "/dev/sdb1", dev == "/dev/sdb1"))
    monkeypatch.setattr(devices, "IS_WINDOWS", False)

    candidates = {str(mp) for mp, _r, _e in devices._posix_candidates()}
    assert str(card) in candidates
    assert str(nfs) not in candidates
    assert "/proc" not in candidates
    assert "/sys" not in candidates
    assert "/run" not in candidates


def test_non_dev_mounts_are_skipped(monkeypatch, tmp_path):
    """Bind mounts and overlays have no backing /dev node."""
    card = tmp_path / "card"
    card.mkdir()
    fake = [
        ("overlay", "/", "overlay"),
        ("none", "/tmp", "tmpfs"),
        ("/dev/sdb1", str(card), "vfat"),
    ]
    monkeypatch.setattr(devices, "_posix_mounts", lambda: fake)
    monkeypatch.setattr(devices, "_posix_flags", lambda dev, mp: (True, True))
    monkeypatch.setattr(devices, "IS_WINDOWS", False)

    candidates = [str(mp) for mp, _r, _e in devices._posix_candidates()]
    assert candidates == [str(card)]


def test_duplicate_mountpoints_counted_once(monkeypatch, tmp_path):
    """A device mounted twice (bind mount, docker) must not appear twice."""
    card = tmp_path / "card"
    card.mkdir()
    fake = [
        ("/dev/sdb1", str(card), "vfat"),
        ("/dev/sdb1", str(card), "vfat"),
    ]
    monkeypatch.setattr(devices, "_posix_mounts", lambda: fake)
    monkeypatch.setattr(devices, "_posix_flags", lambda dev, mp: (True, True))
    monkeypatch.setattr(devices, "IS_WINDOWS", False)

    assert len(devices._posix_candidates()) == 1


def test_conventional_mountpoints_fallback_without_sysfs(monkeypatch):
    """With no /sys entry, the conventional roots decide (FUSE mounts land here)."""
    monkeypatch.setattr(devices, "IS_WINDOWS", False)
    monkeypatch.setattr(devices, "_sysfs_block_dir", lambda dev: None)

    assert devices._posix_flags("/dev/fuse", "/media/card") == (True, True)
    assert devices._posix_flags("/dev/fuse", "/run/media/user/card") == (True, True)
    assert devices._posix_flags("/dev/fuse", "/Volumes/CARD") == (True, True)
    assert devices._posix_flags("/dev/fuse", "/srv/data") == (False, False)


def test_mountpoint_must_exist_on_disk(monkeypatch, tmp_path):
    """A stale /proc/mounts entry for a vanished directory is skipped."""
    fake = [("/dev/sdb1", str(tmp_path / "gone"), "vfat")]
    monkeypatch.setattr(devices, "_posix_mounts", lambda: fake)
    monkeypatch.setattr(devices, "_posix_flags", lambda dev, mp: (True, True))
    monkeypatch.setattr(devices, "IS_WINDOWS", False)

    assert devices._posix_candidates() == []


def test_monitor_reports_insert_and_remove(monkeypatch, tmp_path):
    """DeviceMonitor diffs successive scans into inserted/removed callbacks."""
    card = tmp_path / "card"
    card.mkdir()
    state = {"drives": []}
    monkeypatch.setattr(devices, "list_removable_drives",
                        lambda include_fixed=False: list(state["drives"]))
    monkeypatch.setattr(devices.DeviceChangeListener, "start", lambda self: None)
    monkeypatch.setattr(devices.DeviceChangeListener, "stop", lambda self: None)

    inserted: list[Path] = []
    removed: list[Path] = []
    monitor = devices.DeviceMonitor(inserted.append, removed.append)
    monitor._known = {}

    state["drives"] = [card]
    monitor._scan()
    assert inserted == [card]

    state["drives"] = []
    monitor._scan()
    assert removed == [card]


def test_identify_handles_unreadable_mountpoint(monkeypatch, capsys, tmp_path):
    """`--identify` must not abort when a mount point cannot be listed.

    /boot/efi is the usual offender: it is mounted but only readable by root, so
    probing for a config file there raises PermissionError.
    """
    from dsync import cli

    readable = tmp_path / "card"
    readable.mkdir()
    (readable / ".dsync").write_text("sync=DCIM /tmp/out\n")

    def fake_is_file(self):
        if self.name == "dsync.yml":
            raise PermissionError(13, "Permission denied", str(self))
        return super(type(self), self).is_file()

    monkeypatch.setattr(devices, "list_removable_drives", lambda include_fixed=False: [readable])
    monkeypatch.setattr(devices, "volume_label", lambda p: "CARD")
    monkeypatch.setattr(devices, "is_external", lambda p: True)
    monkeypatch.setattr(devices, "is_removable", lambda p: True)

    # Unreadable probe: the listing must still print and exit successfully.
    monkeypatch.setattr("pathlib.Path.is_file", fake_is_file, raising=False)
    assert cli._identify(include_fixed=True) == 0
    out = capsys.readouterr().out
    assert "CARD" in out
    assert "MOUNT POINT" in out
