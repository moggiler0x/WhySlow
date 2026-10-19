"""Журнал изменений. Всё, что WhySlow меняет в системе, записывается сюда,
чтобы это можно было откатить одной кнопкой."""

from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

from .util import app_dir


def _path() -> Path:
    return app_dir() / "journal.json"


def load() -> list[dict]:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def _save(entries: list[dict]) -> None:
    _path().write_text(json.dumps(entries, ensure_ascii=False, indent=1), encoding="utf-8")


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
    entries = load()
    entries.append(entry)
    _save(entries)
    return entry


def mark_undone(entry_id: str) -> None:
    entries = load()
    for e in entries:
        if e["id"] == entry_id:
            e["undone"] = True
    _save(entries)
