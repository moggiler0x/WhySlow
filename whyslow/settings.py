"""Настройки программы: небольшой JSON рядом с кешем базы и журналом."""

from __future__ import annotations

DEFAULTS = {
    "check_updates": True,   # искать новые версии при запуске
    "auto_update": False,    # ставить их без вопроса
    "language": "",          # "ru" или "en"; пусто — как в системе
    "mac_autoclean": False,  # macOS: сама чистить безопасные «Системные данные»
    "mac_autoclean_days": 7, # раз во сколько дней
    "mac_autoclean_last": 0, # когда чистила в последний раз (time.time())
    "background": True,      # при закрытии окна оставаться в трее и записывать тормоза
    "watch": True,           # записывать тормоза, пока WhySlow открыт
    "tray_hint_shown": False,  # подсказку «я в трее» показываем один раз
    "forecast_profile": "everyday",  # вкладка «Прогноз»: для какой задачи считать главный прогноз
}


def _path():
    from .util import app_dir  # util при импорте сам читает настройки
    return app_dir() / "settings.json"


def load() -> dict:
    from .util import read_json
    data = dict(DEFAULTS)
    saved = read_json(_path(), {})
    if isinstance(saved, dict):
        data.update({k: v for k, v in saved.items() if k in DEFAULTS})
    return data


def get(key: str):
    return load()[key]


def set(key: str, value) -> None:  # noqa: A001 — settings.set(...) читается естественно
    from .util import write_json
    data = load()
    data[key] = value
    try:
        write_json(_path(), data)
    except OSError:
        pass
