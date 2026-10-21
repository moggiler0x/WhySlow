"""Журнал изменений. Всё, что WhySlow меняет в системе, записывается сюда,
чтобы это можно было откатить одной кнопкой."""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

from .util import app_dir, read_json, write_json

# Записи добавляются и из фоновых задач: без замка две одновременные потеряли бы одна другую
_lock = threading.RLock()


def _path() -> Path:
    return app_dir() / "journal.json"


def load() -> list[dict]:
    data = read_json(_path(), [])
    return data if isinstance(data, list) else []


def _save(entries: list[dict]) -> None:
    write_json(_path(), entries)


def add(kind: str, title: str, data: dict, undoable: bool = True) -> dict:
    entry = {
        "id": uuid.uuid4().hex[:12],
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": kind,
        "title": title,
        "data": data,
        "undoable": undoable,
        "undone": False,
    }
    with _lock:
        entries = load()
        entries.append(entry)
        _save(entries)
    return entry


def mark_undone(entry_id: str) -> None:
    with _lock:
        entries = load()
        for e in entries:
            if e["id"] == entry_id:
                e["undone"] = True
        _save(entries)
