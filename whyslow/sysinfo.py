"""Сведения о железе в духе CPU-Z / GPU-Z."""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import time
from pathlib import Path

import psutil

from . import lhm, macsensors
from .util import IS_LINUX, IS_MAC, IS_WINDOWS, run


def cpu_name() -> str:
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as k:
                return winreg.QueryValueEx(k, "ProcessorNameString")[0].strip()
        except OSError:
            pass
    elif IS_LINUX:
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        except OSError:
            pass
    elif IS_MAC:
        code, out = run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if code == 0:
            return out.strip()
    return platform.processor() or "Неизвестно"


def os_name() -> str:
    if IS_LINUX:
        try:
            for line in Path("/etc/os-release").read_text().splitlines():
                if line.startswith("PRETTY_NAME="):
                    return line.split("=", 1)[1].strip('"')
        except OSError:
            pass
    if IS_WINDOWS:
        rel, build = platform.release(), platform.version().split(".")[-1]
        if rel == "10" and build.isdigit() and int(build) >= 22000:
            rel = "11"  # Windows 11 честно представляется десяткой
        return f"Windows {rel} (сборка {build})"
    if IS_MAC and platform.mac_ver()[0]:
        return f"macOS {platform.mac_ver()[0]}"
    return f"{platform.system()} {platform.release()}"


def board() -> str:
    if IS_LINUX:
        base = Path("/sys/class/dmi/id")
        parts = []
        for f in ("board_vendor", "board_name"):
            try:
                parts.append((base / f).read_text().strip())
            except OSError:
                pass
        return " ".join(parts) or "Неизвестно"
    if IS_WINDOWS:
        code, out = run(["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_BaseBoard | Select Manufacturer,Product | ConvertTo-Json"])
        try:
            d = json.loads(out)
            return f"{d.get('Manufacturer', '')} {d.get('Product', '')}".strip()
        except ValueError:
            pass
    return "Неизвестно"


def gpus() -> list[dict]:
    """Список видеокарт. Для NVIDIA — ещё и загрузка, память и температура."""
    result = []
    if shutil.which("nvidia-smi"):
        code, out = run(["nvidia-smi", "--query-gpu=name,driver_version,memory.total,memory.used,"
                         "utilization.gpu,temperature.gpu", "--format=csv,noheader,nounits"])
        if code == 0:
            for line in out.strip().splitlines():
                f = [x.strip() for x in line.split(",")]
                if len(f) == 6:
                    result.append({"name": f[0], "driver": f[1], "vram_mb": _num(f[2]), "vram_used_mb": _num(f[3]),
                                   "load": _num(f[4]), "temp": _num(f[5])})
    if IS_WINDOWS:
        code, out = run(["powershell", "-NoProfile", "-Command",
                         "Get-CimInstance Win32_VideoController | Select Name,DriverVersion,AdapterRAM | ConvertTo-Json"])
        try:
            data = json.loads(out)
            for d in data if isinstance(data, list) else [data]:
                if not any(d["Name"] in g["name"] or g["name"] in d["Name"] for g in result):
                    ram = d.get("AdapterRAM") or 0
                    result.append({"name": d["Name"], "driver": d.get("DriverVersion", ""),
                                   "vram_mb": ram / 2**20 if ram > 0 else None})
        except (ValueError, KeyError, TypeError):
            pass
    elif IS_LINUX:
        code, out = run(["lspci"])
        for line in out.splitlines() if code == 0 else []:
            if re.search(r"VGA|3D controller|Display controller", line):
                name = line.split(": ", 1)[-1]
                if not any(g["name"] in name for g in result):
                    result.append({"name": name, "driver": _linux_gpu_driver(line.split()[0])})
    elif IS_MAC:
        code, out = run(["system_profiler", "SPDisplaysDataType"], timeout=20)
        for m in re.finditer(r"Chipset Model: (.+)", out):
            result.append({"name": m.group(1).strip(), "driver": ""})
    return result


def _linux_gpu_driver(slot: str) -> str:
    link = Path(f"/sys/bus/pci/devices/0000:{slot}/driver")
    try:
        return os.path.basename(os.readlink(link))
    except OSError:
        return ""


def _num(s: str):
    try:
        return float(s)
    except ValueError:
        return None


def temperatures(wait: bool = False) -> dict[str, float]:
    """Максимальная температура по каждому датчику (где ОС её отдаёт).

    В Windows — через LibreHardwareMonitor (см. lhm.py). wait=False отдаёт кеш
    и обновляет его в фоне, чтобы не подвешивать интерфейс.
    """
    if IS_WINDOWS:
        return lhm.temperatures(wait)
    if IS_MAC:
        return macsensors.temperatures()
    out = {}
    try:
        sensors = psutil.sensors_temperatures()
    except (AttributeError, OSError):
        return out
    for name, entries in sensors.items():
        vals = [e.current for e in entries if e.current and 0 < e.current < 130]
        if vals:
            out[name] = max(vals)
    return out


def core_kinds() -> list[str]:
    """Тип каждого логического ядра: «P» — производительное, «E» — энергоэффективное.
    Пока умеем только Apple Silicon: там первыми идут E-ядра, за ними P-ядра."""
    if not IS_MAC:
        return []
    code_p, perf = run(["sysctl", "-n", "hw.perflevel0.logicalcpu"])
    code_e, eff = run(["sysctl", "-n", "hw.perflevel1.logicalcpu"])
    if code_p or code_e or not perf.strip().isdigit() or not eff.strip().isdigit():
        return []
    kinds = ["E"] * int(eff) + ["P"] * int(perf)
    return kinds if len(kinds) == psutil.cpu_count() else []


def static_info() -> dict:
    freq = psutil.cpu_freq()
    vm = psutil.virtual_memory()
    return {
        "os": os_name(),
        "host": platform.node(),
        "cpu": cpu_name(),
        "cores": psutil.cpu_count(logical=False) or 0,
        "threads": psutil.cpu_count() or 0,
        "freq_max": freq.max if freq and freq.max else (freq.current if freq else 0),
        "ram_total": vm.total,
        "board": board(),
        "gpus": gpus(),
        "boot": psutil.boot_time(),
        "disks": disks(),
    }


class _WinGpuLoad:
    """Загрузка видеокарты в Windows — из счётчиков «GPU Engine», как в Диспетчере задач.

    Счётчик читается через PDH по английскому имени (PdhAddEnglishCounterW), поэтому
    работает на русской Windows. Запрос живёт между вызовами: проценты считаются
    по разнице двух замеров."""

    PATH = "\\GPU Engine(*)\\Utilization Percentage"

    def __init__(self):
        self.pdh = None
        self.counter = None
        self.failed = False

    def _open(self):
        import ctypes
        from ctypes import byref, c_void_p
        pdh = ctypes.WinDLL("pdh")
        query, counter = c_void_p(), c_void_p()
        if pdh.PdhOpenQueryW(None, None, byref(query)) or \
                pdh.PdhAddEnglishCounterW(query, ctypes.c_wchar_p(self.PATH), None, byref(counter)):
            raise OSError("PDH")
        pdh.PdhCollectQueryData(query)
        self.pdh, self.query, self.counter = pdh, query, counter

    def get(self) -> float | None:
        if self.failed:
            return None
        import ctypes
        from ctypes import byref, c_double, c_uint32, c_wchar_p

        class Item(ctypes.Structure):
            _fields_ = [("name", c_wchar_p), ("status", c_uint32), ("value", c_double)]

        try:
            if self.pdh is None:
                self._open()
                return None  # первый замер — точка отсчёта
            self.pdh.PdhCollectQueryData(self.query)
            size, count = c_uint32(0), c_uint32(0)
            self.pdh.PdhGetFormattedCounterArrayW(self.counter, 0x200, byref(size), byref(count), None)
            if not size.value:
                return 0.0
            buf = ctypes.create_string_buffer(size.value)
            if self.pdh.PdhGetFormattedCounterArrayW(self.counter, 0x200, byref(size), byref(count), buf):
                return None
            items = ctypes.cast(buf, ctypes.POINTER(Item * count.value)).contents
        except (OSError, AttributeError, ValueError):
            self.failed = True
            return None
        # Как Диспетчер задач: складываем процессы по типу движка (3D, Video Decode…)
        # и показываем самый загруженный движок
        engines: dict[str, float] = {}
        for it in items:
            if it.status in (0, 1) and it.name:
                kind = it.name.split("_engtype_")[-1]
                engines[kind] = engines.get(kind, 0.0) + it.value
        return min(max(engines.values(), default=0.0), 100.0)


_win_gpu = _WinGpuLoad()


def gpu_load() -> float | None:
    """Загрузка видеоядра в процентах или None, если система её не отдаёт."""
    if shutil.which("nvidia-smi"):
        code, out = run(["nvidia-smi", "--query-gpu=utilization.gpu", "--format=csv,noheader,nounits"], timeout=3)
        vals = [_num(x.strip()) for x in out.splitlines()] if code == 0 else []
        vals = [v for v in vals if v is not None]
        if vals:
            return max(vals)
    if IS_MAC:
        return macsensors.gpu_load()
    if IS_WINDOWS:
        return _win_gpu.get()
    if IS_LINUX:  # AMD и часть Intel отдают загрузку прямо в sysfs
        vals = []
        for f in Path("/sys/class/drm").glob("card*/device/gpu_busy_percent"):
            try:
                vals.append(float(f.read_text()))
            except (OSError, ValueError):
                pass
        return max(vals) if vals else None
    return None


_MAC_DATA = "/System/Volumes/Data"


def disks() -> list[dict]:
    out = []
    for p in psutil.disk_partitions(all=False):
        if IS_LINUX and (p.mountpoint.startswith(("/snap", "/boot/efi", "/var/snap")) or p.fstype in ("squashfs", "overlay")):
            continue
        # Служебные тома macOS (VM, Preboot, Update, xarts…): Finder их тоже не показывает
        if IS_MAC and p.mountpoint != _MAC_DATA and (p.mountpoint.startswith("/System/Volumes/") or "nobrowse" in p.opts):
            continue
        try:
            u = psutil.disk_usage(p.mountpoint)
        except OSError:
            continue
        if u.total < 1 << 30:
            continue
        out.append({"mount": p.mountpoint, "fs": p.fstype, "total": u.total, "free": u.free, "percent": u.percent})
    if IS_MAC and any(d["mount"] == _MAC_DATA for d in out):
        # «/» — только системный снимок, а файлы пользователя живут на томе Data
        # того же диска: показываем Data под привычным именем «/»
        out = [d for d in out if d["mount"] != "/"]
        for d in out:
            if d["mount"] == _MAC_DATA:
                d["mount"] = "/"
    return out


def memory() -> tuple[int, int, float]:
    """(занято, всего, процент) по одной формуле на всех ОС: занято всё, что
    нельзя сразу отдать программам. Голое vm.used на macOS не учитывает сжатую
    и служебную память, поэтому «2.8 ГБ из 8» соседствовало с «84%»."""
    vm = psutil.virtual_memory()
    used = vm.total - vm.available
    return used, vm.total, used / vm.total * 100 if vm.total else 0.0


def uptime() -> float:
    return time.time() - psutil.boot_time()
