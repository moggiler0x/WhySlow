"""Переустановка системы одной кнопкой.

Сама WhySlow систему не стирает и не ставит: она запускает штатный механизм ОС —
«Вернуть компьютер в исходное состояние» в Windows, установщик macOS (переустановка
поверх, файлы и программы остаются) или «Стереть контент и настройки» в macOS.
Перед этим выбранные папки копируются на другой диск, а рядом с копией кладётся
скрипт, который вернёт их на место даже без WhySlow. Окно ОС ещё раз спросит,
что делать, — последнее слово всегда за человеком.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .i18n import tr
from .util import IS_MAC, IS_WINDOWS, human_size, run

KEEP = "keep"    # переустановить с сохранением файлов
ERASE = "erase"  # стереть всё и поставить заново
MANIFEST = "whyslow-backup.json"


@dataclass
class Folder:
    title: str
    path: Path
    preset: bool = True
    size: int = -1      # -1 — ещё не посчитано
    files: int = 0


@dataclass
class Drive:
    title: str
    path: Path
    free: int
    total: int


@dataclass
class Backup:
    path: Path
    copied: int = 0
    size: int = 0
    failed: list[str] = field(default_factory=list)


class ReinstallError(Exception):
    pass


# ------------------------------------------------------------- что сохранить ---

def _win_shell_folders() -> dict[str, str]:
    """Настоящие пути папок пользователя: их часто переносят в OneDrive или на другой диск."""
    try:
        import winreg
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders")
    except OSError:
        return {}
    out = {}
    names = {"Desktop": "Desktop", "Personal": "Documents", "{374DE290-123F-4565-9164-39C4925E467B}": "Downloads",
             "My Pictures": "Pictures", "My Music": "Music", "My Video": "Videos"}
    for reg, name in names.items():
        try:
            value, _ = winreg.QueryValueEx(key, reg)
            out[name] = os.path.expandvars(value)
        except OSError:
            pass
    return out


def default_folders() -> list[Folder]:
    """Папки, которые обычно хотят сохранить. Только те, что есть на диске."""
    home = Path.home()
    names = [("Desktop", tr("Рабочий стол", "Desktop")), ("Documents", tr("Документы", "Documents")),
             ("Downloads", tr("Загрузки", "Downloads")), ("Pictures", tr("Изображения", "Pictures")),
             ("Music", tr("Музыка", "Music")),
             ("Movies" if IS_MAC else "Videos", tr("Фильмы", "Movies") if IS_MAC else tr("Видео", "Videos"))]
    real = _win_shell_folders() if IS_WINDOWS else {}
    out = []
    for key, title in names:
        p = Path(real.get(key) or home / key)
        if p.is_dir():
            out.append(Folder(title, p))
    return out


def measure(folder: Folder, stop=lambda: False) -> Folder:
    size = files = 0
    for root, dirs, names in os.walk(folder.path, onerror=lambda e: None):
        if stop():
            break
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(root, d))]
        for n in names:
            try:
                st = os.lstat(os.path.join(root, n))
            except OSError:
                continue
            size += st.st_size
            files += 1
    folder.size, folder.files = size, files
    return folder


# ------------------------------------------------------------ куда сохранить ---

def _device(p: Path) -> int | None:
    try:
        return os.stat(p).st_dev
    except OSError:
        return None


def system_drive() -> Path:
    if IS_WINDOWS:
        return Path(os.environ.get("SystemDrive", "C:") + "\\")
    return Path.home()  # в macOS дом лежит на томе данных, а не на «/», стирается он


def same_disk(a: Path, b: Path) -> bool:
    if IS_WINDOWS:
        return a.drive.upper() == b.drive.upper() if a.drive and b.drive else False
    da, db = _device(a), _device(b)
    return da is not None and da == db


def drives() -> list[Drive]:
    """Диски, на которые можно сложить копию: всё, кроме диска с системой."""
    sysd = system_drive()
    out, seen = [], set()
    for part in psutil.disk_partitions(all=False):
        mp = Path(part.mountpoint)
        if IS_WINDOWS:
            if "cdrom" in part.opts or not part.fstype:
                continue
        elif IS_MAC:
            if not str(mp).startswith("/Volumes/"):
                continue
        elif not re.match(r"^/(media|mnt|run/media)/", str(mp)):
            continue
        if same_disk(mp, sysd) or str(mp) in seen:
            continue
        try:
            u = shutil.disk_usage(mp)
        except OSError:
            continue
        seen.add(str(mp))
        label = mp.name or str(mp)
        out.append(Drive(f"{label} — {tr('свободно', 'free')} {_gb(u.free)} {tr('из', 'of')} {_gb(u.total)}",
                         mp, u.free, u.total))
    return out


def _gb(n: int) -> str:
    return f"{n / 2**30:.0f} " + tr("ГБ", "GB")


def check_target(dest: Path, folders: list[Folder]) -> str:
    """Почему сюда нельзя сложить копию. Пустая строка — можно."""
    if not dest.is_dir():
        return tr("Папка для копии не найдена.", "The backup folder was not found.")
    if same_disk(dest, system_drive()):
        return tr("Это диск с системой — при переустановке копия пропадёт вместе с ним. "
                  "Выберите другой диск или флешку.",
                  "This is the system disk: the backup would be wiped along with it. "
                  "Choose another disk or a USB drive.")
    d = dest.resolve()
    for f in folders:
        s = f.path.resolve()
        if d == s or s in d.parents or d in s.parents:
            return tr(f"Копию нельзя класть в сохраняемую папку «{f.title}» и наоборот.",
                      f"The backup can't go inside the saved folder “{f.title}” or the other way round.")
    need = sum(max(f.size, 0) for f in folders)
    try:
        free = shutil.disk_usage(dest).free
    except OSError:
        free = 0
    if need and free < need * 1.02 + 2**26:
        return tr(f"Не хватит места: нужно {human_size(need)}, свободно {human_size(free)}.",
                  f"Not enough space: {human_size(need)} needed, {human_size(free)} free.")
    return ""


# ------------------------------------------------------------- копирование ---

def _unique(base: Path, name: str) -> Path:
    p, i = base / name, 2
    while p.exists():
        p, i = base / f"{name} ({i})", i + 1
    return p


def backup(folders: list[Folder], dest: Path, progress=lambda done, total, name: None, stop=lambda: False) -> Backup:
    """Копирует папки в dest/WhySlow-<дата>. Ничего не удаляет и не перезаписывает."""
    why = check_target(dest, folders)
    if why:
        raise ReinstallError(why)
    root = _unique(dest, time.strftime("WhySlow-%Y-%m-%d-%H%M"))
    root.mkdir(parents=True)
    res = Backup(root)
    total = sum(max(f.size, 0) for f in folders)
    items = []
    for f in folders:
        target = _unique(root, f.path.name or "folder")
        target.mkdir()
        n = size = 0
        for src_dir, dirs, names in os.walk(f.path, onerror=lambda e: res.failed.append(str(e.filename))):
            if stop():
                raise ReinstallError(tr("Копирование остановлено.", "Copying stopped."))
            dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(src_dir, d))]
            rel = os.path.relpath(src_dir, f.path)
            out_dir = target if rel == "." else target / rel
            out_dir.mkdir(parents=True, exist_ok=True)
            for name in names:
                src = os.path.join(src_dir, name)
                try:
                    shutil.copy2(src, out_dir / name, follow_symlinks=False)
                    sz = os.lstat(src).st_size
                except OSError:
                    res.failed.append(src)
                    continue
                n += 1
                size += sz
                res.copied += 1
                res.size += sz
                progress(res.size, total, f.title)
        items.append({"title": f.title, "dir": target.name, "source": _portable(f.path), "files": n, "size": size})
    (root / MANIFEST).write_text(json.dumps({
        "created": time.strftime("%Y-%m-%d %H:%M:%S"), "os": "windows" if IS_WINDOWS else "mac" if IS_MAC else "linux",
        "items": items, "failed": res.failed[:500]}, ensure_ascii=False, indent=1), encoding="utf-8")
    _write_restore_script(root, items)
    return res


def _portable(p: Path) -> str:
    """Путь внутри дома пишется как ~/…: после переустановки имя пользователя может стать другим."""
    try:
        return "~/" + p.resolve().relative_to(Path.home().resolve()).as_posix()
    except ValueError:
        return str(p)


def short_path(p: Path) -> str:
    """Путь для показа: дом пишется как ~."""
    s = _portable(p)
    return s if not IS_WINDOWS else s.replace("/", "\\")


def _expand(src: str) -> Path:
    return Path.home() / src[2:] if src.startswith("~/") else Path(src)


def _write_restore_script(root: Path, items: list[dict]) -> None:
    """Скрипт возврата рядом с копией: работает и без WhySlow. Существующие файлы не трогает."""
    if IS_WINDOWS:
        lines = ["@echo off", "chcp 65001 >nul", "cd /d \"%~dp0\""]
        for it in items:
            src = it["source"]
            to = "%USERPROFILE%\\" + src[2:].replace("/", "\\") if src.startswith("~/") else src
            lines.append(f'robocopy "{it["dir"]}" "{to}" /E /XC /XN /XO /R:1 /W:1 /NFL /NDL')
        lines += ["echo.", "echo Done / Готово", "pause"]
        (root / "restore.bat").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return
    lines = ["#!/bin/sh", 'cd "$(dirname "$0")" || exit 1']
    for it in items:
        src = it["source"]
        to = '"$HOME/' + src[2:].replace('"', '\\"') + '"' if src.startswith("~/") else '"' + src + '"'
        lines.append(f'mkdir -p {to} && cp -Rpn "{it["dir"]}/." {to}/')
    lines.append('echo "Done / Готово"')
    path = root / "restore.command"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


def read_manifest(root: Path) -> dict:
    try:
        data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise ReinstallError(tr("В этой папке нет копии WhySlow.", "This folder has no WhySlow backup."))
    if not isinstance(data.get("items"), list):
        raise ReinstallError(tr("Файл копии повреждён.", "The backup file is damaged."))
    return data


def restore(root: Path, progress=lambda done, total, name: None) -> tuple[int, int, int]:
    """Возвращает файлы из копии туда, откуда они были. Уже существующие файлы не перезаписываются.
    Возвращает (скопировано, пропущено как уже существующие, ошибок)."""
    data = read_manifest(root)
    total = sum(int(it.get("size") or 0) for it in data["items"])
    done = copied = skipped = failed = 0
    for it in data["items"]:
        src_root = root / it["dir"]
        dst_root = _expand(it["source"])
        for src_dir, _dirs, names in os.walk(src_root):
            rel = os.path.relpath(src_dir, src_root)
            out_dir = dst_root if rel == "." else dst_root / rel
            for name in names:
                src = Path(src_dir) / name
                dst = out_dir / name
                try:
                    sz = src.lstat().st_size
                    if dst.exists() or dst.is_symlink():
                        skipped += 1
                    else:
                        out_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(src, dst, follow_symlinks=False)
                        copied += 1
                except OSError:
                    failed += 1
                    continue
                done += sz
                progress(done, total, it.get("title", ""))
    return copied, skipped, failed


# ----------------------------------------------------- запуск средствами ОС ---

def supported() -> bool:
    return IS_WINDOWS or IS_MAC


def _systemreset() -> Path | None:
    win = Path(os.environ.get("SystemRoot", r"C:\Windows"))
    # 32-битная программа на 64-битной Windows видит System32 подменённым — настоящий лежит в Sysnative
    for p in (win / "Sysnative" / "systemreset.exe", win / "System32" / "systemreset.exe"):
        if p.exists():
            return p
    return None


ERASE_ASSISTANT = Path("/System/Library/CoreServices/Erase Assistant.app")


def mac_installer() -> Path | None:
    """Скачанный установщик macOS в «Программах», самый свежий."""
    apps = sorted(Path("/Applications").glob("Install macOS*.app"), key=lambda p: p.stat().st_mtime, reverse=True)
    return apps[0] if apps else None


def mac_version() -> str:
    code, out = run(["sw_vers", "-productVersion"])
    return out.strip() if code == 0 else ""


def fetch_mac_installer(progress=lambda text: None) -> Path:
    """Скачивает полный установщик той же версии macOS (несколько гигабайт) через softwareupdate."""
    if not shutil.which("softwareupdate"):
        raise ReinstallError(tr("softwareupdate не найден.", "softwareupdate not found."))
    attempts = []
    ver = mac_version()
    if ver:
        attempts.append(["softwareupdate", "--fetch-full-installer", "--full-installer-version", ver])
    attempts.append(["softwareupdate", "--fetch-full-installer"])
    last = ""
    for cmd in attempts:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace")
        for line in p.stdout:
            line = line.strip()
            if line:
                last = line
                m = re.search(r"(\d+(?:\.\d+)?)%", line)
                progress(tr(f"Скачиваю установщик macOS: {m.group(1)}%", f"Downloading the macOS installer: {m.group(1)}%")
                         if m else line)
        if p.wait() == 0 and mac_installer():
            return mac_installer()
    raise ReinstallError(tr(f"Установщик macOS не скачался: {last}", f"The macOS installer did not download: {last}"))


def launch(mode: str, progress=lambda text: None) -> str:
    """Открывает окно переустановки ОС. Само ничего не стирает: окно ОС спросит ещё раз.
    Возвращает подсказку, что выбрать в этом окне."""
    if IS_WINDOWS:
        exe = _systemreset()
        if exe:
            import ctypes
            ok = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), "-factoryreset", None, 1) > 32
            if not ok:
                raise ReinstallError(tr("Окно сброса не открылось: нет прав администратора или запуск отменён.",
                                        "The reset window didn't open: no administrator rights or it was cancelled."))
        else:
            os.startfile("ms-settings:recovery")  # type: ignore[attr-defined]
        return windows_hint(mode)
    if IS_MAC:
        if mode == ERASE:
            if not ERASE_ASSISTANT.exists():
                raise ReinstallError(mac_recovery_hint())
            code, out = run(["open", str(ERASE_ASSISTANT)])
            if code:
                raise ReinstallError(out.strip() or mac_recovery_hint())
            return tr("Откроется «Ассистент стирания» — он попросит пароль и ещё раз подтвердит стирание. "
                      "После него Mac будет как новый, файлы вернёте из копии.",
                      "Erase Assistant will open: it asks for your password and confirms once more. "
                      "Afterwards the Mac is like new; bring your files back from the backup.")
        app = mac_installer() or fetch_mac_installer(progress)
        code, out = run(["open", str(app)])
        if code:
            raise ReinstallError(out.strip())
        return tr(f"Открыт установщик «{app.stem}». Нажмите «Продолжить» и выберите диск Macintosh HD — "
                  "macOS поставится заново поверх, а ваши файлы, программы и настройки останутся.",
                  f"“{app.stem}” is open. Click Continue and pick Macintosh HD: macOS is installed again "
                  "on top, and your files, apps and settings stay.")
    raise ReinstallError(tr("В Linux нет общего способа переустановки — используйте установщик вашего дистрибутива. "
                            "Копию файлов WhySlow сделать может.",
                            "Linux has no common way to reinstall: use your distribution's installer. "
                            "WhySlow can still back up your files."))


def windows_hint(mode: str) -> str:
    if mode == KEEP:
        return tr("В окне Windows выберите «Удалить всё» — останется чистая система, а выбранные папки вернутся "
                  "из копии (restore.bat в папке копии или кнопка «Вернуть из копии»). Если хотите оставить вообще "
                  "все личные файлы, выберите «Сохранить мои файлы». Не включайте «Удалить файлы со всех дисков», "
                  "иначе пропадёт и копия на другом внутреннем диске.",
                  "In the Windows window choose “Remove everything”: you get a clean system, and the chosen folders "
                  "come back from the backup (restore.bat in the backup folder or “Restore from backup”). To keep all "
                  "your personal files instead, choose “Keep my files”. Don't turn on “Remove files from all drives”, "
                  "or a backup on another internal drive is wiped too.")
    return tr("В окне Windows выберите «Удалить всё». Не включайте «Удалить файлы со всех дисков», если на других "
              "дисках есть нужное.",
              "In the Windows window choose “Remove everything”. Don't turn on “Remove files from all drives” "
              "if other drives hold anything you need.")


def mac_recovery_hint() -> str:
    return tr("На этом Mac стирание из системы недоступно. Выключите Mac и включите в режиме восстановления "
              "(Apple Silicon — держите кнопку питания; Intel — Cmd+R), затем «Дисковая утилита» → стереть "
              "Macintosh HD → «Переустановить macOS».",
              "Erasing from within the system isn't available on this Mac. Shut down and start in Recovery "
              "(Apple Silicon: hold the power button; Intel: Cmd+R), then Disk Utility → erase Macintosh HD → "
              "Reinstall macOS.")


def what_happens(mode: str) -> str:
    """Что будет с компьютером — для текста вкладки и подтверждения."""
    if IS_WINDOWS:
        return tr("Windows поставится заново средствами самой Windows. Все программы удалятся — их придётся "
                  "установить снова. Выбранные папки сначала скопируются на другой диск.",
                  "Windows reinstalls itself with its own tools. All programs are removed and need installing "
                  "again. The chosen folders are copied to another disk first.") if mode == KEEP else \
            tr("Windows поставится заново, а диск с системой будет стёрт: файлы, программы и настройки.",
               "Windows reinstalls and the system disk is wiped: files, programs and settings.")
    if IS_MAC:
        return tr("macOS поставится заново поверх текущей. Файлы, программы и настройки останутся; выбранные "
                  "папки на всякий случай скопируются на другой диск.",
                  "macOS is installed again on top of the current one. Files, apps and settings stay; the chosen "
                  "folders are copied to another disk just in case.") if mode == KEEP else \
            tr("Mac будет стёрт до заводского состояния: файлы, программы, настройки и учётные записи. "
               "Выбранные папки сначала скопируются на другой диск.",
               "The Mac is erased to factory state: files, apps, settings and accounts. "
               "The chosen folders are copied to another disk first.")
    return tr("В Linux WhySlow только скопирует выбранные папки, переустановку запустите установщиком дистрибутива.",
              "On Linux WhySlow only backs up the chosen folders; reinstall with your distribution's installer.")
