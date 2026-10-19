"""Настройки программы: небольшой JSON рядом с кешем базы и журналом."""

from __future__ import annotations

import json

DEFAULTS = {
    "check_updates": True,   # искать новые версии при запуске
    "auto_update": False,    # ставить их без вопроса
    "language": "",          # "ru" или "en"; пусто — как в системе
    "mac_autoclean": False,  # macOS: сама чистить безопасные «Системные данные»
    "mac_autoclean_days": 7, # раз во сколько дней
    "mac_autoclean_last": 0, # когда чистила в последний раз (time.time())
}


def _path():
    from .util import app_dir  # util при импорте сам читает настройки
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
