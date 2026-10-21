"""Физические диски: SSD или HDD, что говорит о здоровье сам диск (SMART),
и насколько он занят прямо сейчас — с программами, которые его грузят."""

from __future__ import annotations

import json
import os
import plistlib
import time
from dataclasses import dataclass

import psutil

from .i18n import tr
from .util import IS_LINUX, IS_MAC, IS_WINDOWS, ps_json, run


@dataclass
class PhysicalDisk:
    name: str
    kind: str = ""        # ssd / hdd / "" — не удалось определить
    health: str = ""      # ok / warn / bad / "" — диск не сообщает
    system: bool = False  # на нём стоит система
    detail: str = ""      # что именно сказал диск, для подсказки

    @property
    def kind_label(self) -> str:
        return {"ssd": "SSD", "hdd": tr("HDD (жёсткий диск)", "HDD (hard drive)")}.get(self.kind, "")

    @property
    def health_label(self) -> str:
        return {"ok": tr("исправен", "healthy"), "warn": tr("есть признаки износа", "showing wear"),
                "bad": tr("неисправен", "failing")}.get(self.health, "")


def physical() -> list[PhysicalDisk]:
    try:
        if IS_WINDOWS:
            return _win()
        if IS_MAC:
            return _mac()
        if IS_LINUX:
            return _linux()
    except Exception:  # noqa: BLE001 — сведения о дисках не обязательны
        pass
    return []


_WIN_PS = (
    "$n = (Get-Partition -DriveLetter $env:SystemDrive[0] -ErrorAction SilentlyContinue).DiskNumber; "
    "Get-PhysicalDisk | ForEach-Object { [pscustomobject]@{ "
    "Name = $_.FriendlyName; Media = [string]$_.MediaType; Health = [string]$_.HealthStatus; "
    "Status = [string]($_.OperationalStatus -join ','); Bus = [string]$_.BusType; "
    "System = ([string]$_.DeviceId -eq [string]$n) } } | ConvertTo-Json"
)


def _win() -> list[PhysicalDisk]:
    out = []
    for d in ps_json(_WIN_PS, timeout=30):
        media, bus = str(d.get("Media") or "").lower(), str(d.get("Bus") or "").lower()
        kind = "ssd" if media == "ssd" or bus == "nvme" else "hdd" if media == "hdd" else ""
        health_raw = str(d.get("Health") or "").lower()
        health = {"healthy": "ok", "warning": "warn", "unhealthy": "bad"}.get(health_raw, "")
        status = str(d.get("Status") or "")
        if health == "ok" and any(s in status.lower() for s in ("predictive failure", "degraded", "failed")):
            health = "bad"
        out.append(PhysicalDisk(str(d.get("Name") or "?"), kind, health, bool(d.get("System")),
                                f"{d.get('Health') or ''} {status}".strip()))
    return out


def _mac() -> list[PhysicalDisk]:
    code, out = run(["diskutil", "info", "-plist", "/"], timeout=15)
    if code != 0:
        return []
    info = plistlib.loads(out.encode("utf-8"))
    kind = "ssd" if info.get("SolidState") else "hdd" if info.get("SolidState") is False else ""
    smart = str(info.get("SMARTStatus") or "")
    health = {"verified": "ok", "failing": "bad"}.get(smart.lower(), "")
    name = info.get("MediaName") or info.get("IORegistryEntryName") or tr("Системный диск", "System disk")
    return [PhysicalDisk(str(name), kind, health, True, f"SMART: {smart}" if smart else "")]


def _linux() -> list[PhysicalDisk]:
    code, out = run(["lsblk", "-J", "-o", "NAME,ROTA,TYPE,MOUNTPOINT,MODEL"], timeout=10)
    if code != 0:
        return []
    out_disks = []

    def mounts(node) -> set[str]:
        found = {node.get("mountpoint") or ""}
        for ch in node.get("children") or []:
            found |= mounts(ch)
        return found

    for dev in json.loads(out).get("blockdevices", []):
        if dev.get("type") != "disk" or str(dev.get("name", "")).startswith(("loop", "zram", "ram")):
            continue
        rota = dev.get("rota")
        kind = "hdd" if rota in (True, "1", 1) else "ssd" if rota in (False, "0", 0) else ""
        if str(dev["name"]).startswith(("vd", "xvd")):
            kind = ""  # диск виртуальной машины: «вращается» он или нет, знает только хозяин
        health, detail = _smartctl(dev["name"])
        out_disks.append(PhysicalDisk((dev.get("model") or dev["name"]).strip(), kind, health, "/" in mounts(dev), detail))
    return out_disks


def _smartctl(name: str) -> tuple[str, str]:
    """smartctl есть не везде и требует root — тогда здоровье просто не показываем."""
    code, out = run(["smartctl", "-H", f"/dev/{name}"], timeout=10)
    low = out.lower()
    if "passed" in low or ": ok" in low:
        return "ok", "SMART: PASSED"
    if "failed" in low and code != 127:
        return "bad", "SMART: FAILED"
    return "", ""


# ------------------------------------------------------------- загрузка диска ---

def io_snapshot():
    """Счётчики диска и процессов; разница двух снимков даёт загрузку за интервал."""
    try:
        disk = psutil.disk_io_counters()
    except (RuntimeError, OSError):
        disk = None
    procs: dict[int, tuple[str, int]] = {}
    if not IS_MAC:  # на macOS psutil не отдаёт ввод-вывод процессов
        for p in psutil.process_iter(["name", "io_counters"]):
            io = p.info.get("io_counters")
            if io:
                procs[p.pid] = (p.info.get("name") or "?", io.read_bytes + io.write_bytes)
    return time.monotonic(), disk, procs


def _busy_ms(d) -> float | None:
    if d is None:
        return None
    if hasattr(d, "busy_time"):  # Linux, FreeBSD
        return float(d.busy_time)
    if hasattr(d, "read_time") and hasattr(d, "write_time"):
        return float(d.read_time + d.write_time)
    return None


def busy_percent(a, b) -> float | None:
    """Доля времени, когда диск был занят, %. На Windows и macOS это сумма времени
    чтения и записи — при очереди запросов может быть больше 100, поэтому обрезаем."""
    t0, d0, _ = a
    t1, d1, _ = b
    x0, x1 = _busy_ms(d0), _busy_ms(d1)
    if x0 is None or x1 is None or t1 <= t0:
        return None
    return max(0.0, min(100.0, (x1 - x0) / ((t1 - t0) * 1000) * 100))


def top_io(a, b, n: int = 3) -> list[tuple[str, float]]:
    """Программы, больше всех читавшие и писавшие на диск между снимками: [(имя, байт/с)]."""
    t0, _, p0 = a
    t1, _, p1 = b
    dt = max(t1 - t0, 0.001)
    by_name: dict[str, float] = {}
    for pid, (name, total) in p1.items():
        if pid in p0 and pid != os.getpid():
            by_name[name] = by_name.get(name, 0) + max(total - p0[pid][1], 0) / dt
    return sorted(((k, v) for k, v in by_name.items() if v > 0), key=lambda kv: -kv[1])[:n]
