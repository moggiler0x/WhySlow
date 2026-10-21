"""Откат того, что сделали платные функции. Модуль открытый (не шифруется): вернуть файл
из карантина или снятую политику браузера можно и тогда, когда подписка уже закончилась."""

from __future__ import annotations

import shutil
from pathlib import Path

from .i18n import tr


class RestoreError(Exception):
    pass


def undo(entry: dict) -> None:
    d = entry["data"]
    if entry["kind"] == "pest_quarantine":
        if Path(d["src"]).exists():
            raise RestoreError(tr("На старом месте уже есть файл с таким именем", "There is already a file with that name in the original place"))
        shutil.move(d["dst"], d["src"])
    elif entry["kind"] == "pest_policy":
        import winreg
        hive = winreg.HKEY_CURRENT_USER if d["hive"] == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        with winreg.CreateKey(hive, d["key"]) as k:
            winreg.SetValueEx(k, d["name"], 0, d.get("type", winreg.REG_SZ), d["value"])
