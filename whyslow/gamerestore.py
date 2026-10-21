"""Откат игрового режима. Лежит отдельно от платного кода и работает без подписки:
если WhySlow упал посреди игры или подписка закончилась, приостановленные программы,
приоритеты и схема питания всё равно возвращаются при следующем запуске."""

from __future__ import annotations

from pathlib import Path

import psutil

from .i18n import tr
from .util import IS_LINUX, IS_WINDOWS, app_dir, read_json, run


def state_path() -> Path:
    """Что игровой режим поменял: {"game": {...}, "undo": [{kind, pid, created, old}], ...}."""
    return app_dir() / "gamemode.json"


def same_process(pid: int, created: float) -> psutil.Process | None:
    """Процесс, если это всё ещё тот же (номер не достался новому процессу)."""
    try:
        p = psutil.Process(pid)
        return p if abs(p.create_time() - created) < 1 else None
    except psutil.Error:
        return None


def why(e: Exception) -> str:
    if isinstance(e, psutil.AccessDenied):
        return tr("нет прав", "access denied")
    if isinstance(e, psutil.NoSuchProcess):
        return tr("программа уже закрылась", "the program has already exited")
    return str(e)


def undo(entries: list) -> list[str]:
    """Возвращает записанное в обратном порядке. Закрывшиеся процессы пропускает."""
    errors = []
    for u in reversed(entries):
        try:
            if u["kind"] == "power":
                if IS_WINDOWS:
                    run(["powercfg", "/setactive", u["old"]], timeout=10)
                elif IS_LINUX:
                    run(["powerprofilesctl", "set", u["old"]], timeout=5)
                continue
            p = same_process(u["pid"], u["created"])
            if not p:
                continue
            if u["kind"] == "suspend":
                p.resume()
            elif u["kind"] == "nice":
                p.nice(u["old"])
        except (psutil.Error, OSError, KeyError) as e:
            errors.append(why(e))
    return errors


def recover() -> str | None:
    """Незакрытая сессия после падения: откатывает её. Возвращает имя игры или None."""
    path = state_path()
    data = read_json(path, None)
    if not isinstance(data, dict):
        return None
    undo(data.get("undo") or [])
    path.unlink(missing_ok=True)
    game = data.get("game") or {}
    return game.get("name") or "?"
