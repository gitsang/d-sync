"""全局测试夹具：把应用数据目录隔离到临时路径。

没有这层隔离，测试里的「本地模板」会写进真实的
``%LOCALAPPDATA%\\D-Sync``，污染用户环境。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """把应用数据目录指向临时路径，避免测试污染真实用户目录。"""
    data_dir = tmp_path / "_appdata"
    monkeypatch.setenv("SDSYNC_DATA_DIR", str(data_dir))
    yield data_dir
