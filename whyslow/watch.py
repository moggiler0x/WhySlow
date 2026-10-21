"""Запись тормозов в фоне. Тормоза часто приходят и уходят, и к моменту, когда
открываешь WhySlow, виновник уже затих. Пока программа открыта (или свёрнута в трей),
она замечает долгие пики нагрузки и запоминает, кто в это время грузил систему:
потом можно посмотреть «в 14:32 две минуты тормозило из-за Chrome»."""

from __future__ import annotations

import os
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .i18n import tr
from .util import IS_MAC, IS_WINDOWS, app_dir, read_json, write_json

CPU_HIGH = 85.0     # % всего процессора
RAM_HIGH = 90.0     # % оперативной памяти
MIN_SECONDS = 15    # пик короче — не тормоза, а обычная работа
CALM_SECONDS = 10   # столько нагрузка должна быть в норме, чтобы эпизод закончился
KEEP = 300          # сколько эпизодов хранить


@dataclass
class Episode:
    start: float
    end: float
    kind: str                       # cpu / ram / cpu+ram
    peak_cpu: float = 0.0
    peak_ram: float = 0.0
    culprits: list = field(default_factory=list)  # [[имя, макс. % ЦП, макс. байт памяти]]

    @property
    def duration(self) -> float:
        return max(self.end - self.start, 0)

    def headline(self) -> str:
        what = {"cpu": tr("процессор", "CPU"), "ram": tr("память", "memory"),
                "cpu+ram": tr("процессор и память", "CPU and memory")}.get(self.kind, self.kind)
        who = ", ".join(c[0] for c in self.culprits[:3]) or tr("непонятно кто", "unclear")
        return tr(f"Перегружен {what}: {who}", f"{what} overloaded: {who}")


class Recorder:
    """Получает замеры раз в секунду-две и отдаёт законченный эпизод тормозов."""

    def __init__(self):
        self.groups: list = []
        self._high_since: float | None = None
        self._calm_since: float | None = None
        self._kinds: set[str] = set()
        self._peak_cpu = self._peak_ram = 0.0
        self._who: dict[str, list] = {}

    def feed_procs(self, groups) -> None:
        self.groups = groups

    def feed(self, cpu: float, ram: float, t: float | None = None) -> Episode | None:
        t = time.time() if t is None else t
        kinds = ({"cpu"} if cpu >= CPU_HIGH else set()) | ({"ram"} if ram >= RAM_HIGH else set())
        if kinds:
            if self._high_since is None:
                self._high_since = t
            self._calm_since = None
            self._kinds |= kinds
            self._peak_cpu = max(self._peak_cpu, cpu)
            self._peak_ram = max(self._peak_ram, ram)
            self._note_culprits(kinds)
            return None
        if self._high_since is None:
            return None
        if self._calm_since is None:
            self._calm_since = t
        if t - self._calm_since < CALM_SECONDS:
            return None
        ep = None
        if self._calm_since - self._high_since >= MIN_SECONDS:
            kind = "cpu+ram" if self._kinds == {"cpu", "ram"} else next(iter(self._kinds))
            culprits = sorted(self._who.values(), key=lambda c: (c[1], c[2]) if "cpu" in kind else (c[2], c[1]),
                              reverse=True)[:5]
            ep = Episode(self._high_since, self._calm_since, kind, round(self._peak_cpu, 1),
                         round(self._peak_ram, 1), culprits)
        self.__init__()  # сброс; groups подтянутся со следующим замером
        return ep

    def _note_culprits(self, kinds: set[str]) -> None:
        top = []
        if "cpu" in kinds:
            top += sorted(self.groups, key=lambda g: g.cpu, reverse=True)[:3]
        if "ram" in kinds:
            top += sorted(self.groups, key=lambda g: g.mem, reverse=True)[:3]
        for g in top:
            c = self._who.setdefault(g.name, [g.name, 0.0, 0])
            c[1] = round(max(c[1], g.cpu), 1)
            c[2] = max(c[2], g.mem)


# ----------------------------------------------------------------- хранение ---

def _path() -> Path:
    return app_dir() / "episodes.json"


def load() -> list[Episode]:
    """Эпизоды, свежие первыми."""
    data = read_json(_path(), [])
    out = []
    for d in data if isinstance(data, list) else []:
        try:
            out.append(Episode(**d))
        except TypeError:
            continue
    return sorted(out, key=lambda e: e.start, reverse=True)


def save(ep: Episode) -> None:
    data = [asdict(e) for e in reversed(load())] + [asdict(ep)]
    write_json(_path(), data[-KEEP:])


def clear() -> None:
    write_json(_path(), [])


# ------------------------------------------- запуск WhySlow вместе с системой ---

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_MAC_AGENT = Path.home() / "Library/LaunchAgents/io.github.moggiler0x.whyslow.plist"
_XDG_FILE = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart" / "whyslow.desktop"


def _command() -> list[str]:
    """Как запустить этот же WhySlow свёрнутым в трей."""
    if os.environ.get("APPIMAGE"):  # AppImage монтируется каждый раз в новое место
        return [os.environ["APPIMAGE"], "--tray"]
    if getattr(sys, "frozen", False):
        return [sys.executable, "--tray"]
    py = sys.executable
    if IS_WINDOWS and py.lower().endswith("python.exe"):
        py = py[:-len("python.exe")] + "pythonw.exe"  # без чёрного окна консоли
    repo = str(Path(__file__).resolve().parents[1])
    return [py, "-c", f"import sys; sys.path.insert(0, {repo!r}); sys.argv += ['--tray']; "
                      "from whyslow.__main__ import main; main()"]


def selfstart_enabled() -> bool:
    if IS_WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
                winreg.QueryValueEx(k, "WhySlow")
                return True
        except OSError:
            return False
    return (_MAC_AGENT if IS_MAC else _XDG_FILE).exists()


def set_selfstart(on: bool) -> None:
    cmd = _command()
    if IS_WINDOWS:
        import subprocess
        import winreg
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            if on:
                winreg.SetValueEx(k, "WhySlow", 0, winreg.REG_SZ, subprocess.list2cmdline(cmd))
            else:
                try:
                    winreg.DeleteValue(k, "WhySlow")
                except OSError:
                    pass
        return
    path = _MAC_AGENT if IS_MAC else _XDG_FILE
    if not on:
        path.unlink(missing_ok=True)
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if IS_MAC:
        import plistlib
        path.write_bytes(plistlib.dumps({"Label": "io.github.moggiler0x.whyslow", "ProgramArguments": cmd,
                                         "RunAtLoad": True, "ProcessType": "Interactive"}))
    else:
        import shlex
        path.write_text("[Desktop Entry]\nType=Application\nName=WhySlow\n"
                        f"Exec={shlex.join(cmd)}\nX-GNOME-Autostart-enabled=true\nNoDisplay=true\n",
                        encoding="utf-8")
