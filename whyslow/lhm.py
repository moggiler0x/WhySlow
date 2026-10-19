"""Температуры в Windows через LibreHardwareMonitor.

Windows сама не отдаёт датчики процессора, платы и дисков. Их умеет читать
LibreHardwareMonitor (LHM). Пробуем два способа, по очереди:

1. WMI. Если программа LibreHardwareMonitor запущена, она публикует датчики
   в пространстве root\\LibreHardwareMonitor (старый OpenHardwareMonitor —
   в root\\OpenHardwareMonitor). Ничего ставить не нужно.
2. Библиотека. LibreHardwareMonitorLib.dll загружается прямо в WhySlow через
   pythonnet. Нужны права администратора и сама DLL (см. dll_dirs()).

Если LHM нигде нет, WhySlow может сам скачать официальный релиз с GitHub
(install()) в свою папку и запустить его с запросом прав администратора
(launch()). Оба шага — только по кнопке и после подтверждения.

Опрос медленный (PowerShell / драйвер), поэтому идёт в фоне, а интерфейс
получает последний результат из кеша.
"""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
import zipfile
from pathlib import Path, PurePosixPath

from . import net
from .util import IS_WINDOWS, app_dir, is_admin, run

LHM_REPO = "LibreHardwareMonitor/LibreHardwareMonitor"
RELEASES_URL = f"https://github.com/{LHM_REPO}/releases"
LATEST_API = f"https://api.github.com/repos/{LHM_REPO}/releases/latest"
EXE = "LibreHardwareMonitor.exe"
# Сборка под .NET Framework 4.7.2 есть в любой Windows 10/11; под .NET 8 нужен отдельный рантайм.
ASSET_ORDER = (r"net4\d*", r"net\d+", r"")
MAX_SIZE = 100 * 1024 * 1024
REFRESH = 3.0   # секунд между опросами, пока датчики есть
RETRY = 15.0    # …и пока их нет: не дёргаем PowerShell зря

KINDS = {"cpu": "Процессор", "board": "Плата", "storage": "Диск"}
# Датчики, которые лучше всего описывают процессор целиком.
CPU_MAIN = ("Core (Tctl/Tdie)", "CPU Package", "Core Max", "Core (Tctl)", "CPU")

_WMI_SCRIPT = r"""
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$ErrorActionPreference = 'Stop'
foreach ($ns in 'root/LibreHardwareMonitor', 'root/OpenHardwareMonitor') {
  try {
    $h = Get-CimInstance -Namespace $ns -ClassName Hardware
    $s = Get-CimInstance -Namespace $ns -ClassName Sensor -Filter "SensorType='Temperature'"
  } catch { continue }
  @{ ns = $ns
     hw = @($h | ForEach-Object { @{ id = $_.Identifier; name = $_.Name; type = "$($_.HardwareType)" } })
     s  = @($s | ForEach-Object { @{ name = $_.Name; value = $_.Value; parent = $_.Parent } }) } |
    ConvertTo-Json -Compress -Depth 4
  exit 0
}
exit 3
"""


def _kind(hw_type: str, ident: str) -> str | None:
    t, i = hw_type.lower(), ident.lower()
    if t == "cpu" or i.startswith(("/intelcpu", "/amdcpu", "/cpu")):
        return "cpu"
    if t in ("motherboard", "superio", "embeddedcontroller", "mainboard") or i.startswith(("/lpc", "/mainboard", "/motherboard", "/ec")):
        return "board"
    if t in ("storage", "hdd") or i.startswith(("/hdd", "/nvme", "/ssd", "/storage")):
        return "storage"
    return None


def _summarize(rows: list[tuple[str, str, str, float]]) -> dict[str, float]:
    """rows: (вид, имя железа, имя датчика, °C) → {«Процессор: Ryzen 7 5800X»: °C}."""
    groups: dict[tuple[str, str], dict[str, float]] = {}
    for kind, hw, sensor, value in rows:
        if value is None or not 0 < value < 120:  # отсекаем «-55» и «127» от пустых входов платы
            continue
        groups.setdefault((kind, hw), {})[sensor] = value
    out = {}
    for (kind, hw), sensors in groups.items():
        main = next((sensors[n] for n in CPU_MAIN if n in sensors), None) if kind == "cpu" else None
        out[f"{KINDS[kind]}: {hw}"] = main if main is not None else max(sensors.values())
    return out


# ------------------------------------------------------------------ WMI ---

def _read_wmi() -> dict[str, float] | None:
    """None — LHM не запущен (нет пространства WMI)."""
    code, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command", _WMI_SCRIPT], timeout=20)
    if code != 0:
        return None
    try:
        data = json.loads(out)
    except ValueError:
        return None
    hw = {h["id"]: h for h in data.get("hw") or [] if h and h.get("id")}
    # Датчики платы висят на чипе SuperIO («Nuvoton NCT6798D») — подписываем их именем платы.
    board = next((h["name"] for h in hw.values() if (h.get("type") or "").lower() == "motherboard"), None)
    rows = []
    for s in data.get("s") or []:
        h = hw.get(s.get("parent")) or {}
        kind = _kind(h.get("type") or "", s.get("parent") or "")
        name = (board if kind == "board" else None) or h.get("name") or s.get("parent")
        if kind:
            rows.append((kind, name, s.get("name") or "", s.get("value")))
    return _summarize(rows)


# ------------------------------------------------------------ Библиотека ---

def dll_dirs() -> list[Path]:
    """Где искать LibreHardwareMonitorLib.dll (рядом должна лежать HidSharp.dll)."""
    dirs = []
    if os.environ.get("WHYSLOW_LHM_DIR"):
        dirs.append(Path(os.environ["WHYSLOW_LHM_DIR"]))
    dirs.append(app_dir() / "lhm")
    dirs.append(Path(__file__).parent / "lhm")
    for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        if os.environ.get(env):
            dirs.append(Path(os.environ[env]) / "LibreHardwareMonitor")
    return dirs


class _Library:
    def __init__(self):
        self.computer = None
        self.error = ""

    def open(self) -> bool:
        if self.computer is not None:
            return True
        dll = next((d / "LibreHardwareMonitorLib.dll" for d in dll_dirs()
                    if (d / "LibreHardwareMonitorLib.dll").is_file()), None)
        if dll is None:
            self.error = "nodll"
            return False
        try:
            import clr  # pythonnet
        except ImportError:
            self.error = "nopythonnet"
            return False
        try:
            clr.AddReference(str(dll))
            from LibreHardwareMonitor.Hardware import Computer
            c = Computer()
            c.IsCpuEnabled = True
            c.IsMotherboardEnabled = True
            c.IsStorageEnabled = True
            c.Open()
        except Exception as e:  # .NET-исключения приходят своими типами
            self.error = f"Не удалось загрузить {dll.name}: {e}"
            return False
        self.computer = c
        atexit.register(self.close)
        return True

    def read(self) -> dict[str, float]:
        from LibreHardwareMonitor.Hardware import SensorType
        rows = []

        def walk(hw, top):
            hw.Update()
            kind = _kind(str(top.HardwareType), str(top.Identifier))
            for s in hw.Sensors:
                if kind and s.SensorType == SensorType.Temperature and s.Value is not None:
                    rows.append((kind, top.Name, s.Name, float(s.Value)))
            for sub in hw.SubHardware:  # чипы SuperIO висят под платой
                walk(sub, top)

        for hw in self.computer.Hardware:
            walk(hw, hw)
        return _summarize(rows)

    def close(self):
        if self.computer is not None:
            try:
                self.computer.Close()
            except Exception:
                pass
            self.computer = None


# --------------------------------------------------------------- Опрос ---

class _Reader:
    def __init__(self):
        self.temps: dict[str, float] = {}
        self.source = ""
        self.problem = ""
        self.action = ""   # что предложить: install — скачать LHM, launch — запустить скачанный
        self.stamp = 0.0
        self.lib = _Library()
        self.lock = threading.Lock()
        self.busy = False

    def refresh(self):
        with self.lock:
            temps, source = _read_wmi(), "LibreHardwareMonitor"
            if not temps and self.lib.open():
                try:
                    temps, source = self.lib.read(), "библиотека LibreHardwareMonitor"
                except Exception as e:
                    self.lib.error, temps = f"Ошибка чтения датчиков: {e}", None
            self.action = "" if temps else (
                "install" if not installed() else "" if running() else "launch")
            self.temps = temps or {}
            self.source = source if temps else ""
            self.problem = "" if temps else self._explain(wmi_running=temps is not None)
            if temps and not any(k.startswith(KINDS["cpu"]) for k in temps):
                self.problem = ("Температура процессора не видна: запустите LibreHardwareMonitor"
                                + ("" if source == "LibreHardwareMonitor" else " или WhySlow")
                                + " от администратора.")
            self.stamp = time.time()
            self.busy = False

    def _explain(self, wmi_running: bool) -> str:
        if self.action == "launch":
            return ("LibreHardwareMonitor скачан, но не запущен. Запустите его от администратора "
                    "и не закрывайте: WhySlow подхватит датчики сам.")
        if self.lib.computer is not None and not is_admin():
            return ("Датчики не отдают данные без прав администратора. "
                    "Нажмите «Перезапустить от администратора» внизу окна.")
        if self.lib.computer is not None or wmi_running:
            return ("LibreHardwareMonitor работает, но не видит датчиков температуры. "
                    "Запустите его от администратора; в новых версиях нужен ещё драйвер PawnIO, "
                    "LibreHardwareMonitor предложит его поставить.")
        if self.lib.error == "nopythonnet":
            return ("Нашёл LibreHardwareMonitorLib.dll, но для неё нужен пакет pythonnet: "
                    "pip install pythonnet. Или просто запустите LibreHardwareMonitor от администратора.")
        if self.lib.error not in ("", "nodll"):
            return self.lib.error
        return ("Windows не отдаёт температуры процессора, платы и дисков сама. Их умеет читать "
                "бесплатная программа LibreHardwareMonitor: WhySlow может скачать её с GitHub "
                "и запустить.")

    def get(self, wait: bool) -> dict[str, float]:
        if wait and not self.stamp:
            self.refresh()
        elif time.time() - self.stamp > (REFRESH if self.temps else RETRY) and not self.busy:
            self.busy = True
            threading.Thread(target=self.refresh, daemon=True, name="lhm").start()
        return dict(self.temps)


_reader = _Reader()


def temperatures(wait: bool = False) -> dict[str, float]:
    """Температуры CPU, платы и дисков. wait=False — сразу из кеша, обновление в фоне."""
    if not IS_WINDOWS:
        return {}
    return _reader.get(wait)


def ready() -> bool:
    """Был ли уже хоть один опрос (до него пустой результат ещё ничего не значит)."""
    return bool(_reader.stamp)


def problem() -> str:
    """Почему температур нет — понятным языком. Пусто, если всё в порядке."""
    return _reader.problem


def source() -> str:
    return _reader.source


def action() -> str:
    """Что предложить человеку: "install", "launch" или ничего."""
    return _reader.action


def recheck() -> None:
    """Опросить датчики заново при следующем обращении, не дожидаясь паузы RETRY."""
    if _reader.stamp:
        _reader.stamp = 1.0


# ------------------------------------------------- Скачивание и запуск ---

def install_dir() -> Path:
    return app_dir() / "lhm"


def exe_path() -> Path | None:
    """LibreHardwareMonitor.exe: скачанный WhySlow или поставленный вручную."""
    return next((d / EXE for d in dll_dirs() if (d / EXE).is_file()), None)


def installed() -> bool:
    return exe_path() is not None


def running() -> bool:
    import psutil
    for p in psutil.process_iter(["name"]):
        if (p.info["name"] or "").lower() == EXE.lower():
            return True
    return False


class InstallError(Exception):
    """Ошибка с текстом, который можно показать человеку."""


def pick_asset(assets: list[dict]) -> dict | None:
    """Архив программы из релиза: предпочитаем сборку под .NET Framework."""
    zips = [a for a in assets if re.fullmatch(r"LibreHardwareMonitor.*\.zip", a.get("name") or "", re.I)]
    for pattern in ASSET_ORDER:
        for a in zips:
            if re.search(pattern, a["name"], re.I):
                return a
    return None


def latest_release(timeout: float = 10) -> tuple[str, dict]:
    """(версия, файл архива) последнего релиза LHM на GitHub."""
    try:
        with net.urlopen(LATEST_API, timeout, {"Accept": "application/vnd.github+json"}) as resp:
            rel = json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        raise InstallError(f"Не удалось узнать последнюю версию LibreHardwareMonitor: {net.explain(e, 'релиз')}") from e
    asset = pick_asset(rel.get("assets") or [])
    if not asset:
        raise InstallError(f"В последнем релизе LibreHardwareMonitor нет архива программы. Скачайте её вручную: {RELEASES_URL}")
    return rel.get("tag_name") or "", asset


def _check_url(url: str) -> None:
    if not re.match(rf"https://github\.com/{re.escape(LHM_REPO)}/releases/download/", url):
        raise InstallError(f"Архив лежит не в официальном репозитории LibreHardwareMonitor: {url}")


def _download(asset: dict, dest: Path, progress) -> None:
    url, size = asset["browser_download_url"], int(asset.get("size") or 0)
    _check_url(url)
    if size > MAX_SIZE:
        raise InstallError("Архив LibreHardwareMonitor подозрительно большой, не качаю.")
    digest = hashlib.sha256()
    got = 0
    try:
        with net.urlopen(url, 60) as resp, open(dest, "wb") as f:
            while chunk := resp.read(256 * 1024):
                got += len(chunk)
                if got > MAX_SIZE:
                    raise InstallError("Архив LibreHardwareMonitor подозрительно большой, не качаю.")
                digest.update(chunk)
                f.write(chunk)
                if size:
                    progress(f"Скачиваю LibreHardwareMonitor… {got * 100 // size}%")
    except InstallError:
        raise
    except Exception as e:
        raise InstallError(f"Не удалось скачать LibreHardwareMonitor: {net.explain(e, 'архив')}") from e
    if size and got != size:
        raise InstallError("Архив LibreHardwareMonitor скачался не полностью, попробуйте ещё раз.")
    # GitHub публикует SHA-256 каждого файла релиза (поле digest вида «sha256:…»).
    expected = (asset.get("digest") or "").lower()
    if expected.startswith("sha256:") and expected[7:] != digest.hexdigest():
        raise InstallError("Контрольная сумма архива LibreHardwareMonitor не совпала с GitHub, файл не установлен.")


def _extract(archive: Path, target: Path) -> None:
    try:
        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            if z.testzip() is not None:
                raise InstallError("Архив LibreHardwareMonitor повреждён.")
            for n in names:
                p = PurePosixPath(n.replace("\\", "/"))
                if p.is_absolute() or ".." in p.parts or (p.parts and ":" in p.parts[0]):
                    raise InstallError(f"В архиве LibreHardwareMonitor странный путь: {n}")
            if not any(PurePosixPath(n).name == EXE for n in names):
                raise InstallError(f"В архиве нет {EXE}.")
            z.extractall(target)
    except zipfile.BadZipFile as e:
        raise InstallError("Архив LibreHardwareMonitor повреждён.") from e
    # Программа могла лежать во вложенной папке — поднимаем её наверх.
    exe = next(target.rglob(EXE))
    if exe.parent != target:
        for item in list(exe.parent.iterdir()):
            shutil.move(str(item), target / item.name)


_CONFIG = """<?xml version="1.0" encoding="utf-8"?>
<configuration>
  <appSettings>
    <add key="startMinMenuItem" value="true" />
    <add key="minTrayMenuItem" value="true" />
    <add key="minCloseMenuItem" value="true" />
  </appSettings>
</configuration>
"""


def install(progress=lambda _t: None) -> str:
    """Скачивает последний релиз LHM с GitHub в install_dir(). Возвращает версию."""
    progress("Ищу последний релиз LibreHardwareMonitor на GitHub…")
    version, asset = latest_release()
    target = install_dir()
    with tempfile.TemporaryDirectory(prefix="whyslow-lhm-") as tmp:
        archive = Path(tmp) / "lhm.zip"
        _download(asset, archive, progress)
        progress("Проверяю и распаковываю…")
        fresh = Path(tmp) / "out"
        fresh.mkdir()
        _extract(archive, fresh)
        if target.exists():
            if running():
                raise InstallError("LibreHardwareMonitor сейчас запущен. Закройте его и попробуйте ещё раз.")
            shutil.rmtree(target)
        shutil.move(str(fresh), target)
    # Пусть LHM сворачивается в трей, а не висит окном поверх всего.
    cfg = target / "LibreHardwareMonitor.config"
    if not cfg.exists():
        cfg.write_text(_CONFIG, encoding="utf-8")
    recheck()
    return version


def uninstall() -> None:
    """Удаляет скачанный WhySlow LibreHardwareMonitor (откат из журнала)."""
    if running():
        raise InstallError("LibreHardwareMonitor сейчас запущен. Закройте его (значок в трее) и повторите.")
    _reader.lib.close()
    shutil.rmtree(install_dir(), ignore_errors=True)
    recheck()
    if install_dir().exists():
        raise InstallError(f"Часть файлов занята, удалите папку вручную после перезапуска WhySlow: {install_dir()}")


def launch() -> bool:
    """Запускает LHM с запросом прав администратора (окно UAC). False — отказались или не нашли."""
    exe = exe_path()
    if not IS_WINDOWS or exe is None:
        return False
    import ctypes
    ok = ctypes.windll.shell32.ShellExecuteW(None, "runas", str(exe), None, str(exe.parent), 7) > 32  # 7 — свёрнутым
    if ok:
        recheck()
    return ok
