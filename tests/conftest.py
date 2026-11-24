"""Global test fixtures: isolate the application data directory into a temp path.

Without this isolation the tests would write into the real
``%LOCALAPPDATA%\\D-Sync`` and pollute the user environment.
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point the application data directory at a temp path so tests never touch the real one."""
    data_dir = tmp_path / "_appdata"
    monkeypatch.setenv("SDSYNC_DATA_DIR", str(data_dir))
    yield data_dir
