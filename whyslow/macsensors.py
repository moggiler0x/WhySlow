"""Датчики Mac на Apple Silicon (M1, M2, M3…) без прав администратора.

Температуры чипа macOS отдаёт через IOKit как HID-устройства «датчик температуры»
(страница 0xff00, назначение 5) — так их читают «Мониторинг системы» и утилиты вроде
Stats. Вызовы идут через ctypes, ничего ставить не нужно. На Intel Mac таких
датчиков нет, тогда результат пустой.

Загрузку видеоядра отдаёт ioreg: у ускорителя (IOAccelerator) есть
PerformanceStatistics → «Device Utilization %».
"""

from __future__ import annotations

import ctypes
import re
import threading
from ctypes import byref, c_char_p, c_double, c_int32, c_int64, c_long, c_uint32, c_void_p

from .i18n import tr
from .util import IS_MAC, run

_UTF8 = 0x08000100
_SINT32 = 3
_TEMPERATURE = 15            # kIOHIDEventTypeTemperature
_FIELD = _TEMPERATURE << 16  # IOHIDEventFieldBase(kIOHIDEventTypeTemperature)


class _HID:
    """Клиент IOHIDEventSystem и список датчиков. Создаётся один раз."""

    def __init__(self):
        cf = ctypes.CDLL("/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")
        io = ctypes.CDLL("/System/Library/Frameworks/IOKit.framework/IOKit")
        self.cf, self.io = cf, io
        for fn, res, args in [
            (cf.CFStringCreateWithCString, c_void_p, [c_void_p, c_char_p, c_uint32]),
            (cf.CFNumberCreate, c_void_p, [c_void_p, c_long, c_void_p]),
            (cf.CFDictionaryCreate, c_void_p, [c_void_p, c_void_p, c_void_p, c_long, c_void_p, c_void_p]),
            (cf.CFArrayGetCount, c_long, [c_void_p]),
            (cf.CFArrayGetValueAtIndex, c_void_p, [c_void_p, c_long]),
            (cf.CFStringGetCString, ctypes.c_bool, [c_void_p, c_char_p, c_long, c_uint32]),
            (cf.CFGetTypeID, c_long, []),
            (cf.CFStringGetTypeID, c_long, []),
            (cf.CFRelease, None, [c_void_p]),
            (io.IOHIDEventSystemClientCreate, c_void_p, [c_void_p]),
            (io.IOHIDEventSystemClientSetMatching, c_int32, [c_void_p, c_void_p]),
            (io.IOHIDEventSystemClientCopyServices, c_void_p, [c_void_p]),
            (io.IOHIDServiceClientCopyProperty, c_void_p, [c_void_p, c_void_p]),
            (io.IOHIDServiceClientCopyEvent, c_void_p, [c_void_p, c_int64, c_int32, c_int64]),
            (io.IOHIDEventGetFloatValue, c_double, [c_void_p, c_int32]),
        ]:
            fn.restype, fn.argtypes = res, args
        cf.CFGetTypeID.argtypes = [c_void_p]

        self.client = io.IOHIDEventSystemClientCreate(None)
        if not self.client:
            raise OSError("IOHIDEventSystemClientCreate")
        keys = (c_void_p * 2)(self._str("PrimaryUsagePage"), self._str("PrimaryUsage"))
        vals = (c_void_p * 2)(self._num(0xff00), self._num(5))
        kcb = ctypes.addressof(c_void_p.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
        vcb = ctypes.addressof(c_void_p.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
        match = cf.CFDictionaryCreate(None, keys, vals, 2, kcb, vcb)
        io.IOHIDEventSystemClientSetMatching(self.client, match)
        self.product = self._str("Product")
        self.services: list[tuple[c_void_p, str]] = []
        arr = io.IOHIDEventSystemClientCopyServices(self.client)
        if arr:
            for i in range(cf.CFArrayGetCount(arr)):
                sc = cf.CFArrayGetValueAtIndex(arr, i)
                name = self._prop_str(sc)
                if name:
                    self.services.append((sc, name))
            self._keep = arr  # службы живут, пока жив массив

    def _str(self, s: str):
        return self.cf.CFStringCreateWithCString(None, s.encode(), _UTF8)

    def _num(self, n: int):
        v = c_int32(n)
        return self.cf.CFNumberCreate(None, _SINT32, byref(v))

    def _prop_str(self, sc) -> str:
        ref = self.io.IOHIDServiceClientCopyProperty(sc, self.product)
        if not ref:
            return ""
        try:
            if self.cf.CFGetTypeID(ref) != self.cf.CFStringGetTypeID():
                return ""
            buf = ctypes.create_string_buffer(256)
            return buf.value.decode("utf-8", "replace") if self.cf.CFStringGetCString(ref, buf, 256, _UTF8) else ""
        finally:
            self.cf.CFRelease(ref)

    def read(self) -> dict[str, float]:
        out = {}
        for sc, name in self.services:
            ev = self.io.IOHIDServiceClientCopyEvent(sc, _TEMPERATURE, 0, 0)
            if not ev:
                continue
            try:
                t = self.io.IOHIDEventGetFloatValue(ev, _FIELD)
            finally:
                self.cf.CFRelease(ev)
            if 0 < t < 130:
                out[name] = t
        return out


_hid: _HID | None = None
_hid_failed = False
_lock = threading.Lock()


def raw_temperatures() -> dict[str, float]:
    """Все датчики как есть: {«PMU tdie3»: 47.2, «NAND CH0 temp»: 38.0, …}."""
    global _hid, _hid_failed
    if not IS_MAC or _hid_failed:
        return {}
    with _lock:
        try:
            if _hid is None:
                _hid = _HID()
            return _hid.read()
        except (OSError, AttributeError, ValueError):
            _hid_failed = True
            return {}


# Как сгруппировать сырые датчики в понятные строки. Порядок важен: первое совпадение.
_GROUPS = [
    (tr("Процессор", "CPU"), re.compile(r"^(PMU2? tdie|pACC|eACC|CPU)", re.I)),
    (tr("Видеоядро", "GPU"), re.compile(r"^GPU", re.I)),
    (tr("Накопитель", "Storage"), re.compile(r"NAND|SSD", re.I)),
    (tr("Аккумулятор", "Battery"), re.compile(r"battery|gas gauge", re.I)),
]


def temperatures() -> dict[str, float]:
    """Температуры, сведённые к понятным строкам: максимум по группе датчиков."""
    out: dict[str, float] = {}
    for name, t in raw_temperatures().items():
        for label, pat in _GROUPS:
            if pat.search(name):
                out[label] = max(out.get(label, 0.0), t)
                break
    return out


def gpu_load() -> float | None:
    """Загрузка видеоядра в процентах (Apple Silicon и большинство Intel/AMD Mac)."""
    if not IS_MAC:
        return None
    code, out = run(["ioreg", "-r", "-d", "1", "-w", "0", "-c", "IOAccelerator"], timeout=3)
    if code != 0:
        return None
    vals = [float(v) for v in re.findall(r'"Device Utilization %"\s*=\s*(\d+)', out)]
    return max(vals) if vals else None
