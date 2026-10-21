"""Общие мелочи: пути, запуск команд, форматирование."""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
from pathlib import Path

from .i18n import tr

IS_WINDOWS = platform.system() == "Windows"
IS_LINUX = platform.system() == "Linux"
IS_MAC = platform.system() == "Darwin"


def by_os(win: str, mac: str, linux: str) -> str:
    """Подсказка для текущей ОС: пути, меню и названия везде разные."""
    if IS_WINDOWS:
        return win
    return mac if IS_MAC else linux


def app_dir() -> Path:
    """Папка для кеша базы, журнала и резервных копий."""
    if IS_WINDOWS:
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "WhySlow"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / "whyslow"
    base.mkdir(parents=True, exist_ok=True)
    return base


def backup_dir() -> Path:
    d = app_dir() / "backup"
    d.mkdir(parents=True, exist_ok=True)
    return d


def run(cmd: list[str], timeout: float = 10, cwd=None) -> tuple[int, str]:
    """Запускает команду без окна консоли. Возвращает (код, stdout+stderr)."""
    if not shutil.which(cmd[0]):
        return 127, tr(f"{cmd[0]}: не найдено", f"{cmd[0]}: not found")
    kwargs = {}
    if IS_WINDOWS:
        kwargs["creationflags"] = 0x08000000  # CREATE_NO_WINDOW
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd,
                           encoding="utf-8", errors="replace", **kwargs)
    except (OSError, subprocess.TimeoutExpired) as e:
        return 1, str(e)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def is_admin() -> bool:
    if IS_WINDOWS:
        try:
            import ctypes
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False
    return os.geteuid() == 0


def write_json(path: Path, data) -> None:
    """Пишет JSON через временный файл: если программа упадёт посреди записи,
    старая версия останется целой, а не превратится в полфайла."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path, default):
    """Читает JSON. Повреждённый файл не затирается следующей записью, а
    откладывается рядом (*.broken), чтобы его можно было восстановить вручную."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return default
    try:
        return json.loads(text)
    except ValueError:
        try:
            os.replace(path, path.with_name(path.name + ".broken"))
        except OSError:
            pass
        return default


def human_size(n: float) -> str:
    units = tr("Б КБ МБ ГБ ТБ", "B KB MB GB TB").split()
    for unit in units:
        if abs(n) < 1024 or unit == units[-1]:
            return f"{n:.0f} {unit}" if unit == units[0] else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} {units[-1]}"


def human_duration(seconds: float) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return tr(f"{d} д {h} ч", f"{d} d {h} h")
    if h:
        return tr(f"{h} ч {m} мин", f"{h} h {m} min")
    return tr(f"{m} мин", f"{m} min")


# Внизу модуля: язык читается из настроек, а им нужен app_dir выше
ADMIN_HINT = by_os(tr("нужны права администратора", "administrator rights required"),
                   tr("нужен пароль администратора или это системный процесс",
                      "needs an administrator password, or this is a system process"),
                   tr("нужны права root (sudo)", "root rights required (sudo)"))
