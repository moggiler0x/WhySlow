"""Питание: режим экономии энергии, работа от батареи и износ аккумулятора.
Экономия энергии нарочно режет частоты процессора — со стороны это выглядит
как «компьютер тормозит без причины»."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import psutil

from .i18n import tr
from .util import IS_LINUX, IS_MAC, IS_WINDOWS, ps_json, run

_WIN_POWER_SAVER = "a1841308-3541-4fab-bc81-f71556f20b4a"     # схема «Экономия энергии»
_WIN_BEST_EFFICIENCY = "961cc777-2547-4f9d-8174-7d86181b8a7a"  # Windows 11: режим «Лучшая энергоэффективность»


@dataclass
class PowerState:
    battery: bool = False       # есть ли аккумулятор вообще
    plugged: bool = True
    percent: float | None = None
    saver: bool = False         # включён режим экономии
    saver_name: str = ""        # как он называется в системе
    health: float | None = None  # ёмкость от исходной, %
    condition: str = ""         # что говорит система об аккумуляторе (macOS: «Service Recommended»)
    cycles: int | None = None


def state() -> PowerState:
    st = PowerState()
    try:
        b = psutil.sensors_battery()
    except (RuntimeError, OSError, AttributeError):
        b = None
    if b is not None:
        st.battery = True
        st.percent = b.percent
        st.plugged = bool(b.power_plugged) if b.power_plugged is not None else True
    try:
        if IS_WINDOWS:
            _win(st)
        elif IS_MAC:
            _mac(st)
        elif IS_LINUX:
            _linux(st)
    except Exception:  # noqa: BLE001 — сведения о питании не обязательны
        pass
    return st


def _win(st: PowerState) -> None:
    import ctypes

    class SYSTEM_POWER_STATUS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_ubyte), ("BatteryFlag", ctypes.c_ubyte),
                    ("BatteryLifePercent", ctypes.c_ubyte), ("SystemStatusFlag", ctypes.c_ubyte),
                    ("BatteryLifeTime", ctypes.c_ulong), ("BatteryFullLifeTime", ctypes.c_ulong)]

    s = SYSTEM_POWER_STATUS()
    if ctypes.windll.kernel32.GetSystemPowerStatus(ctypes.byref(s)) and s.SystemStatusFlag == 1:
        st.saver, st.saver_name = True, tr("Экономия заряда", "Battery saver")
    if not st.saver:
        code, out = run(["powercfg", "/getactivescheme"])
        if code == 0 and _WIN_POWER_SAVER in out.lower():
            m = re.search(r"\((.+)\)", out)
            st.saver, st.saver_name = True, m.group(1) if m else tr("Экономия энергии", "Power saver")
    if not st.saver:
        import winreg
        key = r"SYSTEM\CurrentControlSet\Control\Power\User\PowerSchemes"
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key) as k:
                value = "ActiveOverlayAcPowerScheme" if st.plugged else "ActiveOverlayDcPowerScheme"
                if str(winreg.QueryValueEx(k, value)[0]).lower() == _WIN_BEST_EFFICIENCY:
                    st.saver, st.saver_name = True, tr("Лучшая энергоэффективность", "Best power efficiency")
        except OSError:
            pass
    if st.battery:
        full = ps_json("Get-CimInstance -Namespace root/wmi -ClassName BatteryFullChargedCapacity | "
                       "Select-Object FullChargedCapacity | ConvertTo-Json", timeout=15)
        design = ps_json("Get-CimInstance -Namespace root/wmi -ClassName BatteryStaticData | "
                         "Select-Object DesignedCapacity | ConvertTo-Json", timeout=15)
        if full and design:
            st.health = _ratio(full[0].get("FullChargedCapacity"), design[0].get("DesignedCapacity"))


def _mac(st: PowerState) -> None:
    code, out = run(["pmset", "-g"])
    if code == 0 and re.search(r"^\s*(lowpowermode|powermode)\s+1\b", out, re.M):
        st.saver, st.saver_name = True, tr("Режим энергосбережения", "Low Power Mode")
    if not st.battery:
        return
    code, out = run(["system_profiler", "SPPowerDataType"], timeout=20)
    if code != 0:
        return
    m = re.search(r"Maximum Capacity:\s*(\d+)\s*%", out)
    if m:
        st.health = float(m.group(1))
    m = re.search(r"Cycle Count:\s*(\d+)", out)
    if m:
        st.cycles = int(m.group(1))
    m = re.search(r"Condition:\s*(.+)", out)
    if m:
        st.condition = m.group(1).strip()


def _linux(st: PowerState) -> None:
    code, out = run(["powerprofilesctl", "get"])
    if code == 0 and out.strip() == "power-saver":
        st.saver, st.saver_name = True, tr("Энергосбережение", "Power saver")
    for bat in sorted(Path("/sys/class/power_supply").glob("BAT*")):
        for full, design in (("energy_full", "energy_full_design"), ("charge_full", "charge_full_design")):
            try:
                st.health = _ratio((bat / full).read_text().strip(), (bat / design).read_text().strip())
            except OSError:
                continue
            break
        try:
            st.cycles = int((bat / "cycle_count").read_text().strip()) or None
        except (OSError, ValueError):
            pass
        break


def _ratio(full, design) -> float | None:
    try:
        f, d = float(full), float(design)
    except (TypeError, ValueError):
        return None
    if d <= 0 or f <= 0:
        return None
    return min(f / d * 100, 100.0)
