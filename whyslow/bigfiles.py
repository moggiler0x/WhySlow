"""Большие файлы: что занимает место в ваших папках. Ничего не удаляется само —
выбранный файл уходит в Корзину, откуда его можно вернуть."""

from __future__ import annotations

import heapq
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import journal
from .i18n import tr
from .util import IS_MAC, IS_WINDOWS, human_size, run

MIN_SIZE = 200 * 2**20

# Служебные папки программ: файлы там большие, но удалять их вручную — сломать программу
_SKIP = {"appdata", "library", "application data", "node_modules", ".git", "$recycle.bin", ".trash",
         "system volume information", "onedrive"}


@dataclass
class BigFile:
    path: str
    size: int
    mtime: float

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    @property
    def folder(self) -> str:
        return os.path.dirname(self.path)


def find(root: Path | None = None, min_size: int = MIN_SIZE, limit: int = 40,
         progress=lambda text: None) -> list[BigFile]:
    """Самые большие файлы в папке (по умолчанию — домашняя), крупнее min_size."""
    root = Path(root or Path.home())
    heap: list[tuple[int, str, float]] = []
    seen_dirs = 0
    for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
        dirnames[:] = [d for d in dirnames
                       if d.lower() not in _SKIP and not (d.startswith(".") and dirpath == str(root))
                       and not os.path.islink(os.path.join(dirpath, d))]
        seen_dirs += 1
        if seen_dirs % 500 == 0:
            progress(dirpath)
        for name in filenames:
            p = os.path.join(dirpath, name)
            try:
                st = os.lstat(p)
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode) or st.st_size < min_size:
                continue
            item = (st.st_size, p, st.st_mtime)
            if len(heap) < limit:
                heapq.heappush(heap, item)
            elif item > heap[0]:
                heapq.heapreplace(heap, item)
    return [BigFile(p, size, mtime) for size, p, mtime in sorted(heap, reverse=True)]


def to_trash(f: BigFile) -> None:
    """В Корзину, как «Удалить» в проводнике/Finder. Ошибку объясняет исключением."""
    if IS_WINDOWS:
        _win_trash(f.path)
    elif IS_MAC:
        from .macstorage import _to_trash
        if not _to_trash(f.path):
            raise OSError(tr("macOS не дала переместить файл в Корзину", "macOS refused to move the file to the Trash"))
    else:
        code, out = run(["gio", "trash", f.path], timeout=60)
        if code != 0:
            raise OSError(out.strip() or tr("не удалось переместить в Корзину", "couldn't move to the Trash"))
    journal.add("bigfile_trash", tr(f"В Корзину: {f.name} ({human_size(f.size)})",
                                    f"Moved to Trash: {f.name} ({human_size(f.size)})"),
                {"path": f.path, "size": f.size}, undoable=False)


def _win_trash(path: str) -> None:
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_uint16), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]

    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 0x4, 0x10, 0x40, 0x400
    op = SHFILEOPSTRUCTW(None, FO_DELETE, path + "\0", None,
                         FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    code = ctypes.windll.shell32.SHFileOperationW(ctypes.byref(op))
    if code or op.fAnyOperationsAborted:
        raise OSError(tr(f"Windows не удалось переместить файл в Корзину (код {code}). Возможно, он открыт в программе.",
                         f"Windows couldn't move the file to the Recycle Bin (code {code}). It may be open in a program."))


def reveal(path: str) -> None:
    """Показать файл в проводнике / Finder."""
    if IS_WINDOWS:
        subprocess.Popen(f'explorer /select,"{path}"')
    elif IS_MAC:
        run(["open", "-R", path])
    else:
        run(["xdg-open", os.path.dirname(path)])
