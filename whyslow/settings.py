"""Настройки программы: небольшой JSON рядом с кешем базы и журналом."""

from __future__ import annotations

import json

from .util import app_dir

DEFAULTS = {
    "check_updates": True,   # искать новые версии при запуске
    "auto_update": False,    # ставить их без вопроса
}


def _path():
    return app_dir() / "settings.json"


def load() -> dict:
    data = dict(DEFAULTS)
    try:
        saved = json.loads(_path().read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            data.update({k: v for k, v in saved.items() if k in DEFAULTS})
    except (OSError, ValueError):
        pass
    return data


def get(key: str):
    return load()[key]


def set(key: str, value) -> None:  # noqa: A001 — settings.set(...) читается естественно
    data = load()
    data[key] = value
    try:
        _path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    except OSError:
        pass
