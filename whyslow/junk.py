"""Временные файлы, кеши и корзина. Удаляются только файлы старше суток,
чтобы не задеть то, чем программы пользуются прямо сейчас."""

from __future__ import annotations

import os
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import journal
from .util import IS_MAC, IS_WINDOWS, by_os, human_size


@dataclass
class JunkCategory:
    key: str
    title: str
    note: str
    paths: list[Path] = field(default_factory=list)
    min_age_hours: float = 24
    exclude: list[Path] = field(default_factory=list)
    size: int = 0
    files: int = 0


def categories() -> list[JunkCategory]:
    home = Path.home()
    cats: list[JunkCategory] = []
    if IS_WINDOWS:
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        windir = Path(os.environ.get("WINDIR", r"C:\Windows"))
        cats += [
            JunkCategory("temp", "Временные файлы", "Остатки установщиков и программ",
                         [Path(os.environ.get("TEMP", local / "Temp")), windir / "Temp"]),
            JunkCategory("browser", "Кеш браузеров", "Браузер скачает заново то, что нужно",
                         [local / r"Google\Chrome\User Data\Default\Cache",
                          local / r"Microsoft\Edge\User Data\Default\Cache",
                          local / r"Yandex\YandexBrowser\User Data\Default\Cache",
                          local / r"BraveSoftware\Brave-Browser\User Data\Default\Cache"]),
            JunkCategory("crash", "Дампы падений", "Отчёты об ошибках программ",
                         [local / "CrashDumps", windir / "Minidump", local / r"Microsoft\Windows\WER"]),
            JunkCategory("wu", "Загрузки обновлений Windows", "Уже установленные обновления (нужны права админа)",
                         [windir / r"SoftwareDistribution\Download"]),
            JunkCategory("recycle", "Корзина", "Удалённые файлы", [], min_age_hours=0),
        ]
    elif IS_MAC:
        cats += [
            JunkCategory("cache", "Кеш программ", "Программы пересоздадут его при необходимости",
                         [home / "Library/Caches"]),
            JunkCategory("temp", "Временные файлы", "Ваши временные файлы",
                         [Path(os.environ.get("TMPDIR", "/tmp"))]),
            JunkCategory("logs", "Логи", "Старые журналы программ", [home / "Library/Logs"]),
            JunkCategory("trash", "Корзина", "Удалённые файлы", [home / ".Trash"], min_age_hours=0),
        ]
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME", home / ".cache"))
        cats += [
            JunkCategory("cache", "Кеш программ", "Программы пересоздадут его при необходимости", [cache],
                         exclude=[cache / "thumbnails"]),
            JunkCategory("thumbs", "Миниатюры", "Превью картинок в файловом менеджере",
                         [cache / "thumbnails", home / ".thumbnails"], min_age_hours=0),
            JunkCategory("temp", "Временные файлы", "Ваши файлы в /tmp и /var/tmp",
                         [Path("/tmp"), Path("/var/tmp")]),
            JunkCategory("trash", "Корзина", "Удалённые файлы", [home / ".local/share/Trash"], min_age_hours=0),
        ]
    return cats


# что удаляется целиком, без ограничения по возрасту
WHOLE = by_os("корзина — целиком", "корзина — целиком", "корзина и миниатюры — целиком")


def _walk(paths: list[Path], min_age_hours: float, exclude: list[Path] = ()):
    """Перебирает файлы, которые можно удалить (свои и достаточно старые)."""
    cutoff = time.time() - min_age_hours * 3600
    uid = os.getuid() if hasattr(os, "getuid") else None
    seen = set()
    for root in paths:
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            if dirpath in seen:
                continue
            seen.add(dirpath)
            dirnames[:] = [d for d in dirnames if not os.path.islink(os.path.join(dirpath, d))
                           and Path(dirpath, d) not in exclude]
            for name in filenames:
                p = os.path.join(dirpath, name)
                try:
                    st = os.lstat(p)
                except OSError:
                    continue
                if not stat.S_ISREG(st.st_mode) or st.st_mtime > cutoff \
                        or (uid is not None and st.st_uid != uid):
                    continue
                yield p, st.st_size


def scan(cat: JunkCategory) -> JunkCategory:
    if cat.key == "recycle":
        cat.size, cat.files = _recycle_query()
        return cat
    size = files = 0
    for _, sz in _walk(cat.paths, cat.min_age_hours, cat.exclude):
        size += sz
        files += 1
    cat.size, cat.files = size, files
    return cat


def clean(cat: JunkCategory) -> tuple[int, int]:
    """Удаляет мусор. Возвращает (освобождено байт, сколько файлов не удалось)."""
    if cat.key == "recycle":
        freed = _recycle_query()[0]
        _recycle_empty()
        failed = 0
    else:
        freed = failed = 0
        for p, sz in list(_walk(cat.paths, cat.min_age_hours, cat.exclude)):
            try:
                os.remove(p)
                freed += sz
            except OSError:
                failed += 1  # файл занят или нет прав — пропускаем
        # подчищаем старые опустевшие папки (сами корни и исключения не трогаем)
        cutoff = time.time() - cat.min_age_hours * 3600
        for root in cat.paths:
            if root.is_dir():
                for dirpath, _, _ in sorted(os.walk(root), key=lambda x: -len(x[0])):
                    if dirpath == str(root) or any(Path(dirpath).is_relative_to(e) for e in cat.exclude):
                        continue
                    try:
                        if os.lstat(dirpath).st_mtime < cutoff:
                            os.rmdir(dirpath)
                    except OSError:
                        pass
    journal.add("junk_clean", f"Очистка «{cat.title}»: освобождено {human_size(freed)}",
                {"key": cat.key, "freed": freed}, undoable=False)
    return freed, failed


def _recycle_query() -> tuple[int, int]:
    if not IS_WINDOWS:
        return 0, 0
    import ctypes
    from ctypes import wintypes

    class SHQUERYRBINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong), ("i64NumItems", ctypes.c_longlong)]

    info = SHQUERYRBINFO(ctypes.sizeof(SHQUERYRBINFO), 0, 0)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0, 0
    return info.i64Size, info.i64NumItems


def _recycle_empty() -> None:
    import ctypes
    # без подтверждения, без анимации, без звука — подтверждение уже было в WhySlow
    ctypes.windll.shell32.SHEmptyRecycleBinW(None, None, 0x1 | 0x2 | 0x4)
