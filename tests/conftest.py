"""Каждый тест работает со своей пустой папкой данных, а не с настоящими журналом и настройками."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture(autouse=True)
def data_dir(tmp_path, monkeypatch):
    d = tmp_path / "data"
    monkeypatch.setenv("XDG_DATA_HOME", str(d))
    monkeypatch.setenv("LOCALAPPDATA", str(d))
    from whyslow import i18n
    monkeypatch.setattr(i18n, "_lang", "ru")
    return d
