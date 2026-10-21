"""Рентген фриза: что происходило в ту секунду, когда всё подвисло.

Фриз длится секунду-две, и к тому моменту, когда открываешь диспетчер задач, виновника
уже не видно. Поэтому WhySlow постоянно держит в памяти последнюю минуту замеров
(процессор, память, диск, сеть, температуры и самые тяжёлые процессы), а по горячей
клавише (Ctrl+Alt+X) или сам, заметив, что система замерла целиком, сохраняет этот
отрезок и разбирает его: кто съел эту секунду — программа, драйвер, диск, сеть или перегрев.

Ничего в системе не меняется: рентген только смотрит и записывает.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

from ..i18n import tr
from ..util import IS_MAC, IS_WINDOWS, app_dir, human_size, read_json, write_json

WINDOW = 60          # сколько секунд замеров держать в памяти
LOOKBACK = 20        # сколько из них разбирать перед нажатием
STALL = 2.5          # замер раз в секунду опоздал на столько — система замирала целиком
SLEEP = 30.0         # а такой разрыв — это сон компьютера, а не фриз
AUTO_GAP = 120       # сам снимок при замирании — не чаще раза в две минуты
KEEP = 50            # сколько снимков хранить
HOTKEY = "⌘⌥X" if IS_MAC else "Ctrl+Alt+X"

CPU_HIGH = 85.0
CORE_HIGH = 95.0
RAM_HIGH = 90.0
DISK_FAST = 80 * 2**20     # байт/с: столько диск успевает не всегда, особенно HDD
DISK_BUSY = 90.0           # % времени, когда диск был занят
NET_FAST = 5 * 2**20
DPC_HIGH = 8.0             # % процессора на прерывания и DPC — это уже драйвер
TEMP_HOT = 95.0
GPU_HIGH = 97.0


@dataclass
class Frame:
    t: float
    dt: float = 1.0
    cpu: float = 0.0
    core: float = 0.0        # самое загруженное ядро
    ram: float = 0.0
    swap: float = 0.0
    disk_r: float = 0.0
    disk_w: float = 0.0
    net_rx: float = 0.0
    net_tx: float = 0.0
    gpu: float | None = None
    temp: float | None = None
    freq: float | None = None


@dataclass
class Suspect:
    kind: str        # stall / cpu / core / ram / disk / net / driver / thermal / gpu / none
    title: str
    detail: str = ""
    advice: str = ""
    who: str = ""
    score: float = 0.0


@dataclass
class Shot:
    time: float
    reason: str                           # hotkey / button / auto
    frames: list = field(default_factory=list)
    procs: list = field(default_factory=list)   # [[t, [[имя, % ЦП, байт]]]]
    probe: dict = field(default_factory=dict)
    suspects: list = field(default_factory=list)

    def headline(self) -> str:
        if not self.suspects:
            return tr("Явного виновника нет", "No clear culprit")
        return self.suspects[0]["title"]


class Buffer:
    """Последняя минута замеров. Кормится из того же фонового Sampler, что и графики."""

    def __init__(self):
        self.frames: deque[Frame] = deque(maxlen=WINDOW)
        self.procs: deque[tuple[float, list]] = deque(maxlen=WINDOW // 2)
        self.last_auto = 0.0

    def feed_stats(self, s: dict, t: float | None = None) -> Frame:
        t = time.time() if t is None else t
        temps = [v for vals in (s.get("temps") or {}).values() for v in _temp_values(vals)]
        per = s.get("per") or [s.get("cpu", 0.0)]
        f = Frame(t, round(s.get("dt", 1.0), 2), round(s.get("cpu", 0.0), 1), round(max(per), 1),
                  round(s["ram"][2] if s.get("ram") else 0.0, 1), round((s.get("swap") or (0, 0, 0))[2], 1),
                  s.get("disk_r", 0.0), s.get("disk_w", 0.0), s.get("net_rx", 0.0), s.get("net_tx", 0.0),
                  s.get("gpu"), max(temps) if temps else None, s.get("freq"))
        self.frames.append(f)
        return f

    def feed_procs(self, groups, t: float | None = None) -> None:
        t = time.time() if t is None else t
        top = {g.name: g for g in sorted(groups, key=lambda g: g.cpu, reverse=True)[:6]}
        top.update({g.name: g for g in sorted(groups, key=lambda g: g.mem, reverse=True)[:3]})
        self.procs.append((t, [[g.name, round(g.cpu, 1), g.mem] for g in top.values()]))

    def stalled(self) -> float:
        """Сколько секунд длилось замирание в последнем замере (0 — не было)."""
        if not self.frames:
            return 0.0
        dt = self.frames[-1].dt
        return dt if STALL <= dt < SLEEP else 0.0

    def want_auto(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if self.stalled() and now - self.last_auto >= AUTO_GAP:
            self.last_auto = now
            return True
        return False

    def window(self, now: float | None = None, seconds: float = LOOKBACK) -> tuple[list[Frame], list]:
        now = time.time() if now is None else now
        frames = [f for f in self.frames if now - seconds - 2 <= f.t <= now + 1]
        procs = [p for p in self.procs if now - seconds - 3 <= p[0] <= now + 1]
        return frames, procs


def _temp_values(vals) -> list[float]:
    """sysinfo.temperatures(): {группа: [(метка, градусы), …]} или {группа: градусы}."""
    if isinstance(vals, (int, float)):
        return [float(vals)]
    out = []
    for v in vals or []:
        v = v[-1] if isinstance(v, (list, tuple)) else v
        if isinstance(v, (int, float)) and 0 < v < 150:
            out.append(float(v))
    return out


# ------------------------------------------------- живые детали в момент снимка ---

def probe(seconds: float = 1.0) -> dict:
    """То, что нельзя держать в памяти постоянно (дорого), — снимается на секунду при нажатии:
    время на прерывания и DPC (драйверы), кто сейчас читает и пишет диск, занятость диска."""
    out: dict = {}
    procs = {}
    if not IS_MAC:  # macOS не отдаёт счётчики диска по процессам
        for p in psutil.process_iter(["name"]):
            try:
                io = p.io_counters()
                procs[p.pid] = (p, p.info["name"] or "?", io.read_bytes + io.write_bytes)
            except (psutil.Error, AttributeError, OSError):
                continue
    try:
        d0 = psutil.disk_io_counters()
    except (OSError, RuntimeError):
        d0 = None
    sw0 = psutil.swap_memory()
    cpu = psutil.cpu_times_percent(interval=seconds)
    for k in ("interrupt", "dpc", "iowait", "steal", "irq", "softirq"):
        v = getattr(cpu, k, None)
        if v is not None:
            out[k] = round(v, 1)
    try:
        d1 = psutil.disk_io_counters()
    except (OSError, RuntimeError):
        d1 = None
    if d0 and d1:
        if hasattr(d1, "busy_time"):  # Linux
            out["disk_busy"] = round(min((d1.busy_time - d0.busy_time) / (seconds * 10), 100), 1)
        elif IS_WINDOWS:  # время запросов в мс; при очереди может быть больше 100%
            ms = (d1.read_time - d0.read_time) + (d1.write_time - d0.write_time)
            out["disk_busy"] = round(min(ms / (seconds * 10), 100), 1)
    sw1 = psutil.swap_memory()
    out["swap_io"] = max(sw1.sin - sw0.sin, 0) + max(sw1.sout - sw0.sout, 0)
    io_by: dict[str, float] = {}
    for pid, (p, name, before) in procs.items():
        try:
            io = p.io_counters()
        except (psutil.Error, OSError):
            continue
        rate = (io.read_bytes + io.write_bytes - before) / seconds
        if rate > 0:
            io_by[name] = io_by.get(name, 0) + rate
    out["disk_procs"] = sorted(([n, r] for n, r in io_by.items()), key=lambda x: x[1], reverse=True)[:5]
    try:
        f = psutil.cpu_freq()
        if f and f.max:
            out["freq"] = [round(f.current), round(f.max)]
    except (OSError, NotImplementedError, AttributeError):
        pass
    return out


# ----------------------------------------------------------------- разбор ---

def _top(procs: list, by: int) -> tuple[str, float]:
    """Самый тяжёлый процесс за окно: by=1 — по процессору, by=2 — по памяти."""
    best: dict[str, float] = {}
    for _t, rows in procs:
        for row in rows:
            best[row[0]] = max(best.get(row[0], 0), row[by])
    if not best:
        return "", 0.0
    name = max(best, key=best.get)
    return name, best[name]


def analyze(frames: list[Frame], procs: list, live: dict) -> list[Suspect]:
    """Подозреваемые, самый вероятный первым. Пустого списка не бывает."""
    out: list[Suspect] = []
    if not frames:
        return [Suspect("none", tr("Нет замеров", "No samples"),
                        tr("WhySlow только что запустился и ещё не успел ничего записать.",
                           "WhySlow has just started and hasn't recorded anything yet."))]
    peak = lambda key: max((getattr(f, key) or 0) for f in frames)  # noqa: E731
    stall = max((f.dt for f in frames if f.dt < SLEEP), default=0)
    if stall >= STALL:
        out.append(Suspect("stall", tr(f"Система замерла целиком на {stall:.1f} с", f"The whole system froze for {stall:.1f} s"),
                           tr("Даже фоновый замер WhySlow не смог выполниться вовремя. Так бывает, когда драйвер "
                              "надолго занимает процессор, диск не отвечает или память уходит в файл подкачки.",
                              "Even WhySlow's background sample couldn't run on time. This happens when a driver "
                              "holds the CPU, the disk stops responding, or memory is being swapped out."),
                           tr("Смотрите на остальных подозреваемых ниже: обычно один из них и вызвал замирание.",
                              "Check the other suspects below: one of them usually caused the freeze."),
                           score=60 + min(stall, 10) * 3))
    dpc = (live.get("interrupt") or 0) + (live.get("dpc") or 0)
    if dpc >= DPC_HIGH:
        out.append(Suspect("driver", tr(f"Драйвер: {dpc:.0f}% процессора уходит на прерывания", f"Driver: {dpc:.0f}% of CPU goes to interrupts"),
                           tr("Процессор занят не программами, а обслуживанием устройств. Чаще всего виноваты "
                              "драйверы Wi-Fi или сетевой карты, звука, видеокарты или USB-устройства.",
                              "The CPU is busy servicing hardware, not running apps. Usually it's the Wi-Fi or network, "
                              "audio, graphics or a USB device driver."),
                           tr("Обновите драйверы сети, звука и видеокарты с сайта производителя; отключите по очереди "
                              "USB-устройства. Точно найти драйвер поможет бесплатная LatencyMon.",
                              "Update network, audio and graphics drivers from the vendor's site; unplug USB devices one by one. "
                              "The free LatencyMon tool pinpoints the exact driver."),
                           score=75 + min(dpc, 25)))
    cpu = peak("cpu")
    who, who_cpu = _top(procs, 1)
    if cpu >= CPU_HIGH:
        named = who and who_cpu >= 20
        out.append(Suspect("cpu", tr(f"Процессор загружен на {cpu:.0f}%", f"CPU at {cpu:.0f}%") +
                           (f": {who}" if named else ""),
                           tr(f"Больше всех грузил {who} — до {who_cpu:.0f}% всего процессора.",
                              f"{who} used the most — up to {who_cpu:.0f}% of the whole CPU.") if named else
                           tr("Нагрузка размазана по многим процессам.", "The load is spread across many processes."),
                           tr("Если это не то, чем вы сейчас пользуетесь, закройте его на вкладке «Процессы».",
                              "If you aren't using it right now, close it on the Processes tab."),
                           who if named else "", score=cpu))
    else:
        core = peak("core")
        if core >= CORE_HIGH and cpu < 60:
            out.append(Suspect("core", tr(f"Одно ядро упёрлось в {core:.0f}%", f"One core maxed out at {core:.0f}%") +
                               (f": {who}" if who else ""),
                               tr("Программа считает в одном потоке и упирается в скорость одного ядра — "
                                  "остальные ядра простаивают. Так часто тормозят игры и старые программы.",
                                  "An app is single-threaded and limited by one core while the others idle. "
                                  "Games and older apps often stutter this way."),
                               tr("Закройте фоновые программы и включите режим «Высокая производительность».",
                                  "Close background apps and switch to the high performance power mode."),
                               who, score=55 + (core - CORE_HIGH)))
    ram = peak("ram")
    swap_io = live.get("swap_io") or 0
    if ram >= RAM_HIGH or swap_io > 2 * 2**20:
        mem_who, mem = _top(procs, 2)
        out.append(Suspect("ram", tr(f"Память заполнена на {ram:.0f}%", f"Memory {ram:.0f}% full") +
                           (f": {mem_who}" if mem_who else ""),
                           tr(f"Больше всех занимает {mem_who} ({human_size(mem)}). Когда память кончается, система "
                              "выгружает её на диск, и всё подвисает.",
                              f"{mem_who} uses the most ({human_size(mem)}). When memory runs out, the system pages "
                              "to disk and everything stutters.") if mem_who else "",
                           tr("Закройте лишние вкладки и программы; если так постоянно — памяти мало для ваших задач.",
                              "Close extra tabs and apps; if it's always like this, you need more RAM."),
                           mem_who, score=ram - 5 + (15 if swap_io > 2 * 2**20 else 0)))
    disk = max(f.disk_r + f.disk_w for f in frames)
    busy = live.get("disk_busy") or 0
    iowait = live.get("iowait") or 0
    if disk >= DISK_FAST or busy >= DISK_BUSY or iowait >= 20:
        dwho = (live.get("disk_procs") or [["", 0]])[0]
        out.append(Suspect("disk", tr("Диск не успевает", "The disk can't keep up") + (f": {dwho[0]}" if dwho[0] else ""),
                           tr(f"Диск читал и писал до {human_size(disk)}/с", f"The disk moved up to {human_size(disk)}/s") +
                           (tr(f", занят {busy:.0f}% времени", f", busy {busy:.0f}% of the time") if busy else "") + "." +
                           (tr(f" Больше всех — {dwho[0]} ({human_size(dwho[1])}/с).", f" Mostly {dwho[0]} ({human_size(dwho[1])}/s).")
                            if dwho[0] else ""),
                           tr("Обычно это обновления, антивирус, облачная синхронизация или индексация. "
                              "На HDD такие фризы бывают постоянно — SSD решает проблему.",
                              "Usually updates, antivirus, cloud sync or indexing. On an HDD this happens all the time — "
                              "an SSD fixes it."),
                           dwho[0], score=max(min(disk / DISK_FAST * 50, 80), busy * 0.9, iowait * 2)))
    net = max(f.net_rx + f.net_tx for f in frames)
    if net >= NET_FAST:
        out.append(Suspect("net", tr(f"Сеть занята: до {human_size(net)}/с", f"Network busy: up to {human_size(net)}/s"),
                           tr("Что-то качает или отправляет данные. В онлайн-играх и звонках это даёт лаги и пинг.",
                              "Something is downloading or uploading. In online games and calls this causes lag and ping spikes."),
                           tr("Проверьте торренты, облачные диски и обновления Steam/системы.",
                              "Check torrents, cloud drives and Steam/system updates."),
                           score=35 + min(net / NET_FAST * 5, 30)))
    temp = peak("temp")
    freq = live.get("freq")
    throttled = bool(freq and cpu >= 50 and freq[0] < 0.6 * freq[1])
    if temp >= TEMP_HOT or throttled:
        out.append(Suspect("thermal", tr("Перегрев: процессор сбрасывает частоту", "Overheating: the CPU is throttling") if throttled
                           else tr(f"Перегрев: до {temp:.0f} °C", f"Overheating: up to {temp:.0f} °C"),
                           (tr(f"Частота {freq[0]} из {freq[1]} МГц под нагрузкой. ", f"Clock {freq[0]} of {freq[1]} MHz under load. ")
                            if throttled else "") +
                           tr("Горячее железо само замедляется, чтобы не сгореть.", "Hot hardware slows itself down to avoid damage."),
                           tr("Почистите систему охлаждения от пыли; ноутбук — не на мягкой поверхности.",
                              "Clean the dust out of the cooling; keep a laptop off soft surfaces."),
                           score=70 + (10 if throttled else 0)))
    gpu = peak("gpu")
    if gpu >= GPU_HIGH:
        out.append(Suspect("gpu", tr(f"Видеокарта на пределе: {gpu:.0f}%", f"GPU maxed out: {gpu:.0f}%"),
                           tr("Видеокарта не успевает рисовать кадры.", "The graphics card can't draw frames fast enough."),
                           tr("Понизьте графику или разрешение в игре, ограничьте FPS.",
                              "Lower the in-game graphics or resolution, cap the FPS."),
                           score=45))
    if not out:
        out.append(Suspect("none", tr("Явного виновника нет", "No clear culprit"),
                           tr(f"За {LOOKBACK} секунд до снимка процессор, память, диск и сеть были в норме.",
                              f"In the {LOOKBACK} seconds before the snapshot CPU, memory, disk and network were fine."),
                           tr("Скорее всего, подвисла сама программа (игра, браузер) или её сервер в интернете. "
                              "Если фриз повторяется — нажмите горячую клавишу ещё раз сразу в момент подвисания.",
                              "Most likely the app itself (a game, the browser) or its online server hung. "
                              "If it happens again, press the hotkey right at the moment of the freeze."),
                           score=0))
    out.sort(key=lambda s: s.score, reverse=True)
    return out


def capture(buf: Buffer, reason: str, live: dict | None = None, now: float | None = None) -> Shot:
    """Снимок: окно замеров перед нажатием + живые детали + разбор."""
    now = time.time() if now is None else now
    frames, procs = buf.window(now)
    return make_shot(frames, procs, reason, probe() if live is None else live, now)


def make_shot(frames: list[Frame], procs: list, reason: str, live: dict, now: float | None = None) -> Shot:
    """То же по уже взятому окну: окно копируется в GUI-потоке, а probe() идёт в фоне."""
    sus = analyze(frames, procs, live)
    return Shot(time.time() if now is None else now, reason, [asdict(f) for f in frames],
                [[t, rows] for t, rows in procs], live, [asdict(s) for s in sus])


# ----------------------------------------------------------------- хранение ---

def _path() -> Path:
    return app_dir() / "xrays.json"


def load() -> list[Shot]:
    """Снимки, свежие первыми."""
    data = read_json(_path(), [])
    out = []
    for d in data if isinstance(data, list) else []:
        try:
            out.append(Shot(**d))
        except TypeError:
            continue
    return sorted(out, key=lambda s: s.time, reverse=True)


def save(shot: Shot) -> None:
    data = [asdict(s) for s in reversed(load())] + [asdict(shot)]
    write_json(_path(), data[-KEEP:])


def clear() -> None:
    write_json(_path(), [])


# ------------------------------------------------------ глобальная горячая клавиша ---

class WinHotkey(threading.Thread):
    """Ctrl+Alt+X работает, даже когда WhySlow свёрнут, а на экране игра (только Windows).
    callback зовётся из этого потока — в окно его передают сигналом."""

    MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
    VK_X = 0x58
    WM_HOTKEY, WM_QUIT = 0x0312, 0x0012

    def __init__(self, callback):
        super().__init__(daemon=True)
        self.callback = callback
        self.ok = threading.Event()
        self.failed = threading.Event()
        self._tid = 0

    def run(self):
        import ctypes
        from ctypes import wintypes
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        self._tid = kernel32.GetCurrentThreadId()
        if not user32.RegisterHotKey(None, 1, self.MOD_CONTROL | self.MOD_ALT | self.MOD_NOREPEAT, self.VK_X):
            self.failed.set()  # уже занята другой программой
            return
        self.ok.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == self.WM_HOTKEY:
                    try:
                        self.callback()
                    except Exception:
                        pass
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self):
        if self._tid:
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._tid, self.WM_QUIT, 0, 0)


# ------------------------------------------------------------------ окно ---

from PySide6.QtCore import QObject, Qt, QTimer, Signal  # noqa: E402
from PySide6.QtGui import QAction, QKeySequence, QShortcut  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QScrollArea, QTableWidgetItem,
    QVBoxLayout, QWidget,
)

from .. import gui, license, settings  # noqa: E402
from ..gui import C, Sparkline, Switch, _esc, accent, by_os, confirm, make_table, muted, pt, px  # noqa: E402

class XRayTab(QWidget):
    """Снимки «одной лагающей секунды»: что было за 20 секунд до нажатия и кто виноват."""

    KIND = {"stall": "⏸", "cpu": "▲", "core": "▲", "ram": "▣", "disk": "◉", "net": "⇅", "driver": "⚙",
            "thermal": "♨", "gpu": "▦", "none": "✓"}

    def __init__(self, main, ctl: "_XRay"):
        super().__init__()
        self.main, self.ctl = main, ctl
        self.shots: list[Shot] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))
        head = QHBoxLayout()
        text = QVBoxLayout()
        title = QLabel(accent(tr("Рентген фриза", "Freeze X-ray")))
        title.setObjectName("h1")
        text.addWidget(title)
        hot = by_os(tr(f"Нажмите {HOTKEY} в момент подвисания — даже в игре и при свёрнутом WhySlow.",
                       f"Press {HOTKEY} the moment things freeze — even in a game with WhySlow minimized."),
                    tr(f"В момент подвисания выберите «Снять рентген» в значке WhySlow в строке меню "
                       f"(или {HOTKEY}, когда окно WhySlow активно).",
                       f"When things freeze, pick “Take X-ray” from the WhySlow menu bar icon "
                       f"(or {HOTKEY} while the WhySlow window is active)."),
                    tr(f"В момент подвисания выберите «Снять рентген» в значке WhySlow в трее "
                       f"(или {HOTKEY}, когда окно WhySlow активно).",
                       f"When things freeze, pick “Take X-ray” from the WhySlow tray icon "
                       f"(or {HOTKEY} while the WhySlow window is active)."))
        text.addWidget(muted(hot + " " + tr(
            f"WhySlow сохранит, что происходило последние {LOOKBACK} секунд, и покажет, кто съел эту секунду: "
            "программа, драйвер, диск, сеть или перегрев. Ничего в системе не меняется.",
            f"WhySlow saves what happened in the last {LOOKBACK} seconds and shows who ate that second: "
            "an app, a driver, the disk, the network or overheating. Nothing in the system is changed.")))
        head.addLayout(text, 1)
        self.b_shot = QPushButton(tr("Снять рентген сейчас", "Take X-ray now"))
        self.b_shot.setObjectName("primary")
        self.b_shot.setCursor(Qt.PointingHandCursor)
        self.b_shot.clicked.connect(lambda: self.ctl.take("button"))
        head.addWidget(self.b_shot, 0, Qt.AlignVCenter)
        lay.addLayout(head)
        opts = QHBoxLayout()
        self.sw_auto = Switch(settings.get("xray_auto"))
        self.sw_auto.clicked.connect(self._toggle_auto)
        opts.addWidget(self.sw_auto)
        opts.addWidget(QLabel(tr("Снимать самому, когда система замерла целиком", "Take one automatically when the whole system freezes")))
        opts.addStretch()
        lay.addLayout(opts)
        self.table = make_table([tr("Когда", "When"), tr("Как", "How"), tr("Главный подозреваемый", "Main suspect")])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self.on_select)
        self.table.setMaximumHeight(px(200))
        lay.addWidget(self.table)
        self.chart = Sparkline([C["accent"], C["warn"], C["info"]], 100, 70, axis=True,
                               names=[tr("ЦП", "CPU"), tr("Память", "Memory"), tr("Диск, % от пика", "Disk, % of peak")])
        lay.addWidget(self.chart)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.detail = QWidget()
        self.detail_lay = QVBoxLayout(self.detail)
        self.detail_lay.setContentsMargins(0, 0, px(6), 0)
        self.detail_lay.setSpacing(px(8))
        self.scroll.setWidget(self.detail)
        lay.addWidget(self.scroll, 1)
        row = QHBoxLayout()
        self.state = muted("")
        row.addWidget(self.state, 1)
        self.b_clear = QPushButton(tr("Удалить снимки", "Delete snapshots"))
        self.b_clear.clicked.connect(self.clear)
        row.addWidget(self.b_clear)
        lay.addLayout(row)
        self.reload()

    def _toggle_auto(self):
        on = not settings.get("xray_auto")
        settings.set("xray_auto", on)
        self.sw_auto.setChecked(on)

    def busy(self, on: bool):
        self.b_shot.setEnabled(not on)
        self.state.setText(tr("Снимаю рентген…", "Taking an X-ray…") if on else "")

    def reload(self, select_first: bool = False):
        self.shots = load()
        how = {"hotkey": tr("клавишей", "hotkey"), "button": tr("кнопкой", "button"), "auto": tr("сам", "auto")}
        self.table.setRowCount(len(self.shots))
        for r, s in enumerate(self.shots):
            cells = [time.strftime("%d.%m %H:%M:%S", time.localtime(s.time)), how.get(s.reason, s.reason), s.headline()]
            for c, text in enumerate(cells):
                self.table.setItem(r, c, QTableWidgetItem(text))
        self.b_clear.setEnabled(bool(self.shots))
        if select_first and self.shots:
            self.table.selectRow(0)
        elif not self.shots:
            self._clear()
            self.detail_lay.addWidget(muted(tr("Снимков пока нет. Когда что-то подвиснет — нажмите горячую клавишу "
                                               "или кнопку выше.",
                                               "No snapshots yet. When something freezes, press the hotkey or the button above.")))
            self.detail_lay.addStretch()
            self.chart.set_series([], [], [])

    def _clear(self):
        while self.detail_lay.count():
            w = self.detail_lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def on_select(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(self.shots):
            return
        s = self.shots[rows[0].row()]
        frames = s.frames
        peak = max((f["disk_r"] + f["disk_w"] for f in frames), default=0) or 1
        self.chart.n = max(len(frames), 2)
        self.chart.set_series([f["cpu"] for f in frames], [f["ram"] for f in frames],
                              [(f["disk_r"] + f["disk_w"]) / peak * 100 for f in frames])
        self._clear()
        for i, d in enumerate(s.suspects):
            self.detail_lay.addWidget(self._card(d, first=i == 0))
        self.detail_lay.addStretch()

    def _card(self, d: dict, first: bool) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        v = QVBoxLayout(card)
        v.setContentsMargins(px(16), px(10), px(14), px(10))
        v.setSpacing(px(3))
        color = C["bad"] if first and d["kind"] != "none" else (C["ok"] if d["kind"] == "none" else C["warn"])
        t = QLabel(f"<span style='color:{color}'>{self.KIND.get(d['kind'], '•')}</span>&nbsp; {_esc(d['title'])}")
        t.setWordWrap(True)
        t.setStyleSheet(f"font-weight: 700; font-size: {pt(11.5 if first else 10.5)};")
        v.addWidget(t)
        detail = d.get("detail", "")
        if d.get("who"):
            kb = self.main.kb.lookup(d["who"])
            if kb.verdict != "unknown":
                detail += f" {d['who']}: {kb.note}"
        if detail:
            v.addWidget(muted(detail))
        if d.get("advice"):
            v.addWidget(muted("→ " + d["advice"]))
        return card

    def clear(self):
        if confirm(self, tr("Удалить снимки?", "Delete snapshots?"),
                   tr("Все сохранённые рентгены будут удалены.", "All saved X-rays will be deleted.")):
            clear()
            self.reload()


# ---------------------------------------------------------- Игровой режим ---


class _XRay(QObject):
    """Фоновая часть: держит последнюю минуту замеров, ловит горячую клавишу и замирания."""

    hotkey_pressed = Signal()  # из потока WinHotkey — в поток окна

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.tab: XRayTab | None = None
        self.buf = Buffer()
        self.busy = False
        self.hotkey = None
        self.hotkey_pressed.connect(lambda: self.take("hotkey"))
        if IS_WINDOWS:  # по всей системе, даже поверх игры
            self.hotkey = WinHotkey(self.hotkey_pressed.emit)
            self.hotkey.start()
        sc = QShortcut(QKeySequence("Ctrl+Alt+X"), main)  # на Mac это ⌘⌥X
        sc.setContext(Qt.ApplicationShortcut)
        sc.activated.connect(lambda: self.take("hotkey"))
        QTimer.singleShot(0, self._attach)  # Sampler и трей создаются в MainWindow позже вкладок

    def _attach(self):
        self.main.sampler.stats.connect(self.on_stats)
        self.main.sampler.procs.connect(self.buf.feed_procs)
        QApplication.instance().aboutToQuit.connect(self.quit)
        menu = getattr(self.main, "_tray_menu", None)
        if menu:
            act = QAction(tr("Снять рентген фриза", "Take a freeze X-ray"), menu)
            act.triggered.connect(lambda: self.take("hotkey"))
            acts = menu.actions()
            menu.insertAction(acts[2] if len(acts) > 2 else None, act)

    def on_stats(self, s: dict):
        self.buf.feed_stats(s)
        if settings.get("xray_auto") and self.buf.want_auto() and license.has_premium("xray"):
            self.take("auto")

    def take(self, reason: str):
        if not license.has_premium("xray"):
            if reason != "auto":
                self.main.show_window()
                self.main.go("xray")
            return
        if self.busy:
            return
        self.busy = True
        frames, procs = self.buf.window()  # копия окна здесь: буфер пополняется из этого же потока
        if self.tab:
            self.tab.busy(True)

        def done(shot):
            self.busy = False
            try:
                save(shot)
            except OSError as e:
                QMessageBox.critical(self.main, tr("Ошибка", "Error"), str(e))
            if self.tab:
                self.tab.busy(False)
                self.tab.reload(select_first=True)
            self.main.status(tr("Рентген: ", "X-ray: ") + shot.headline())
            if reason != "button":
                self.main.notify(tr("Рентген фриза снят", "Freeze X-ray taken"), shot.headline(), "xray")

        def failed(e):
            self.busy = False
            if self.tab:
                self.tab.busy(False)
            if reason != "auto":
                QMessageBox.critical(self.main, tr("Ошибка", "Error"), e)

        gui.run_async(lambda _p: make_shot(frames, procs, reason, probe()), done, failed)

    def quit(self):
        if self.hotkey:
            self.hotkey.stop()


_ctl: _XRay | None = None


def build(main):
    """Вкладка «Рентген» (зовёт license_ui.PremiumGate). Фоновая часть создаётся один раз."""
    global _ctl
    if _ctl is None:
        _ctl = _XRay(main)
    _ctl.tab = XRayTab(main, _ctl)
    return _ctl.tab
