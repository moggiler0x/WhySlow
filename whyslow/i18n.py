"""Язык интерфейса: русский или английский.

Строки пишутся парой прямо в коде: tr("Проверить", "Scan"). Язык выбирается один раз
при запуске (из настроек, иначе по языку системы); смена языка перезапускает программу.
"""

from __future__ import annotations

import locale
import os

LANGS = {"ru": "Русский", "en": "English"}
_lang: str | None = None


def system_lang() -> str:
    """Русский для русско- и близкоязычных систем, иначе английский."""
    names = []
    try:  # Windows: язык интерфейса системы, а не региональный формат
        import ctypes
        lcid = ctypes.windll.kernel32.GetUserDefaultUILanguage()
        names.append(locale.windows_locale.get(lcid, ""))
    except (AttributeError, OSError):
        pass
    names += [os.environ.get(k, "") for k in ("LC_ALL", "LC_MESSAGES", "LANG")]
    try:
        names.append(locale.getlocale()[0] or "")
    except ValueError:
        pass
    if not any(names):
        try:  # macOS: язык из системных настроек, переменные окружения там часто пустые
            from .util import run
            names.append(run(["defaults", "read", "-g", "AppleLanguages"], timeout=2)[1])
        except Exception:
            pass
    for n in names:
        n = n.strip().lower().lstrip('(" \n')
        if n:
            return "ru" if n[:2] in ("ru", "uk", "be", "kk") or n.startswith("russian") else "en"
    return "en"


def lang() -> str:
    global _lang
    if _lang is None:
        from . import settings
        saved = settings.get("language")
        _lang = saved if saved in LANGS else system_lang()
    return _lang


def set_lang(code: str, save: bool = True) -> None:
    """Меняет язык (и запоминает его, если save). Уже показанные строки — после перезапуска."""
    global _lang
    if save:
        from . import settings
        settings.set("language", code)
    _lang = code


def tr(ru: str, en: str) -> str:
    return en if lang() == "en" else ru


def pick(entry: dict, key: str) -> str:
    """Поле записи базы на текущем языке: note_en / title_en, если есть, иначе русское."""
    if lang() == "en":
        return entry.get(f"{key}_en") or entry.get(key, "")
    return entry.get(key, "")
