"""Графический интерфейс WhySlow (PySide6)."""

from __future__ import annotations

import sys
import threading
import time
import traceback
import webbrowser
from collections import deque

import psutil
from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QFontMetrics, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QApplication, QCheckBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import __version__, checks, journal, junk, knowledge, lhm, network, startup, sysinfo
from .processes import ProcessMonitor, kill
from .util import ADMIN_HINT, IS_LINUX, IS_MAC, IS_WINDOWS, human_duration, human_size, is_admin

# ------------------------------------------------------------------ стиль ---

C = {
    "bg": "#0a0c16", "bg2": "#120f26", "panel": "#11142a", "panel2": "#181c38", "border": "#272c52",
    "text": "#eef0ff", "muted": "#8d93b8", "accent": "#7c5cff", "accent2": "#22d3ee",
    "ok": "#2ee59d", "info": "#4cc9f0", "warn": "#ffb547", "bad": "#ff4d6d",
}
VERDICT_COLORS = {"safe": C["ok"], "optional": C["warn"], "keep": C["info"], "bad": C["bad"], "unknown": C["muted"]}

# На macOS пункт шрифта = пиксель, на Windows и Linux — 1,33 пикселя. Чтобы текст везде
# был одинаково крупным, все размеры задаются в «условных пунктах» и пересчитываются здесь.
SCALE = 1.5 if IS_MAC else 1.15


def pt(size: float) -> str:
    return f"{size * SCALE:.1f}pt"


def px(size: float) -> int:
    """Отступы и высоты растут вместе со шрифтом, но слабее."""
    return round(size * (1 + (SCALE - 1) * 0.6) if not IS_MAC else size * 1.15)


GRAD = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {C['accent']}, stop:1 {C['accent2']})"

STYLE = f"""
* {{ color: {C['text']}; font-size: {pt(10)}; }}
QMainWindow, QWidget#root {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:1, stop:0 {C['bg']}, stop:1 {C['bg2']}); }}
QToolTip {{ background: {C['panel2']}; color: {C['text']}; border: 1px solid {C['border']}; padding: 4px; }}
QTabWidget::pane {{ border: 1px solid {C['border']}; border-radius: 12px; background: {C['panel']}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {C['muted']}; padding: {px(10)}px {px(20)}px; border: none;
               border-bottom: 3px solid transparent; font-size: {pt(10.5)}; font-weight: 600; }}
QTabBar::tab:selected {{ color: {C['text']}; border-bottom: 3px solid {C['accent2']}; }}
QTabBar::tab:hover {{ color: {C['text']}; }}
QPushButton {{ background: {C['panel2']}; border: 1px solid {C['border']}; border-radius: 8px;
              padding: {px(7)}px {px(16)}px; }}
QPushButton:hover {{ border-color: {C['accent']}; background: #20254a; }}
QPushButton:disabled {{ color: {C['muted']}; background: {C['panel']}; border-color: {C['panel2']}; }}
QPushButton#primary {{ background: {GRAD}; border: none; color: white; font-weight: 700; padding: {px(9)}px {px(26)}px; }}
QPushButton#primary:hover {{ background: qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 #9378ff, stop:1 #4fe0f5); }}
QPushButton#primary:disabled {{ background: #3a3470; color: #c9c4f5; }}
QPushButton#danger:hover {{ border-color: {C['bad']}; color: {C['bad']}; }}
QPushButton#link {{ background: transparent; border: none; color: {C['accent2']}; padding: 2px 4px; font-weight: 600; }}
QPushButton#link:hover {{ color: white; }}
QGroupBox {{ border: 1px solid {C['border']}; border-radius: 12px; margin-top: {px(18)}px;
             padding: {px(16)}px {px(12)}px {px(12)}px {px(12)}px; background: {C['panel2']}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 12px; padding: 0 6px; color: {C['accent2']};
                    font-weight: 700; font-size: {pt(10.5)}; }}
QLabel#muted {{ color: {C['muted']}; }}
QLabel#h1 {{ font-size: {pt(19)}; font-weight: 800; }}
QLabel#section {{ color: {C['muted']}; font-size: {pt(9)}; font-weight: 700; letter-spacing: 1px; }}
QLabel#big {{ font-size: {pt(22)}; font-weight: 800; }}
QLabel#field {{ background: {C['bg']}; border: 1px solid {C['border']}; border-radius: 6px; padding: {px(4)}px {px(8)}px; }}
QFrame#card {{ background: {C['panel2']}; border: 1px solid {C['border']}; border-radius: 12px; }}
QFrame#tile {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1c2142, stop:1 {C['panel2']});
              border: 1px solid {C['border']}; border-radius: 14px; }}
QFrame#tile:hover {{ border-color: {C['accent']}; }}
QTableWidget {{ background: {C['panel']}; alternate-background-color: {C['panel2']}; border: none;
               gridline-color: transparent; selection-background-color: #2e2a6b; selection-color: white; }}
QHeaderView::section {{ background: {C['panel2']}; color: {C['muted']}; border: none; font-weight: 700;
                        border-bottom: 1px solid {C['border']}; padding: {px(8)}px; }}
QProgressBar {{ background: {C['bg']}; border: 1px solid {C['border']}; border-radius: 6px; text-align: center;
               font-size: {pt(8.5)}; font-weight: 600; }}
QProgressBar::chunk {{ background: {GRAD}; border-radius: 5px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {C['border']}; border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {C['accent']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QTextEdit {{ background: {C['bg']}; border: 1px solid {C['border']}; border-radius: 6px; }}
QStatusBar {{ background: transparent; color: {C['muted']}; }}
QStatusBar QPushButton {{ padding: 3px 12px; }}
QCheckBox::indicator {{ width: {px(18)}px; height: {px(18)}px; }}
QMessageBox QLabel {{ font-size: {pt(10)}; }}
"""


def muted(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setWordWrap(True)
    return lbl


def section(text: str) -> QLabel:
    lbl = QLabel(text.upper())
    lbl.setObjectName("section")
    return lbl


def field(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("field")
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lbl


def make_table(cols: list[str]) -> QTableWidget:
    t = QTableWidget(0, len(cols))
    t.setHorizontalHeaderLabels(cols)
    t.setSelectionBehavior(QAbstractItemView.SelectRows)
    t.setSelectionMode(QAbstractItemView.SingleSelection)
    t.setEditTriggers(QAbstractItemView.NoEditTriggers)
    t.setAlternatingRowColors(True)
    t.verticalHeader().hide()
    t.verticalHeader().setDefaultSectionSize(int(QFontMetrics(QApplication.font()).height() * 2.0))
    t.setShowGrid(False)
    t.setFocusPolicy(Qt.NoFocus)
    t.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
    return t


def rate(n: float) -> str:
    return human_size(n) + "/с"


class Bar(QProgressBar):
    """Полоска, которая желтеет и краснеет при высокой нагрузке."""

    def __init__(self, warn=70, bad=90, height=22):
        super().__init__()
        self.setRange(0, 100)
        self.warn, self.bad = warn, bad
        self._color = None
        self.setFixedHeight(px(height))

    def set(self, value: float, text: str | None = None):
        self.setValue(int(value))
        self.setFormat(text if text is not None else f"{value:.0f}%")
        color = C["bad"] if value >= self.bad else C["warn"] if value >= self.warn else None
        if color != self._color:
            self._color = color
            self.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; border-radius: 5px; }}" if color else "")


class Sparkline(QWidget):
    """График за последнюю минуту, как в Диспетчере задач. Несколько линий — несколько цветов."""

    def __init__(self, colors: list[str], maximum: float | None = 100, height: int = 70):
        super().__init__()
        self.colors = [QColor(c) for c in colors]
        self.maximum = maximum
        self.series: list[list[float]] = [[] for _ in colors]
        self.n = 60
        self.setMinimumHeight(px(height))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_series(self, *series):
        self.series = [list(s) for s in series]
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(C["border"]), 1))
        p.setBrush(QColor(C["bg"]))
        p.drawRoundedRect(r, 8, 8)
        p.setPen(QPen(QColor(255, 255, 255, 14), 1))
        for k in (1, 2, 3):
            y = r.top() + r.height() * k / 4
            p.drawLine(QPointF(r.left() + 4, y), QPointF(r.right() - 4, y))
        top = self.maximum or max([max(s) for s in self.series if s] + [1]) * 1.15
        inner = r.adjusted(2, 6, -2, -2)
        step = inner.width() / (self.n - 1)
        for color, values in zip(self.colors, self.series):
            vals = values[-self.n:]
            if len(vals) < 2:
                continue
            x0 = inner.right() - step * (len(vals) - 1)
            pts = [QPointF(x0 + i * step, inner.bottom() - inner.height() * min(v / top, 1)) for i, v in enumerate(vals)]
            line = QPainterPath(pts[0])
            for q in pts[1:]:
                line.lineTo(q)
            area = QPainterPath(line)
            area.lineTo(pts[-1].x(), inner.bottom())
            area.lineTo(pts[0].x(), inner.bottom())
            area.closeSubpath()
            grad = QLinearGradient(0, inner.top(), 0, inner.bottom())
            c1, c2 = QColor(color), QColor(color)
            c1.setAlpha(110)
            c2.setAlpha(0)
            grad.setColorAt(0, c1)
            grad.setColorAt(1, c2)
            p.setPen(Qt.NoPen)
            p.setBrush(QBrush(grad))
            p.drawPath(area)
            p.setPen(QPen(color, 2))
            p.setBrush(Qt.NoBrush)
            p.drawPath(line)
        p.end()


class Switch(QAbstractButton):
    """Переключатель «вкл/выкл». Сам не переключается: решает обработчик clicked."""

    def __init__(self, on: bool):
        super().__init__()
        self.setCheckable(True)
        self.setChecked(on)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(px(44), px(24))

    def nextCheckState(self):
        pass

    def sizeHint(self):
        return QSize(px(44), px(24))

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        if not self.isEnabled():
            p.setOpacity(0.4)
        if self.isChecked():
            g = QLinearGradient(r.topLeft(), r.topRight())
            g.setColorAt(0, QColor(C["accent"]))
            g.setColorAt(1, QColor(C["accent2"]))
            p.setBrush(QBrush(g))
            p.setPen(Qt.NoPen)
        else:
            p.setBrush(QColor(C["bg"]))
            p.setPen(QPen(QColor(C["border"]), 1.5))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        d = r.height() - 6
        x = r.right() - d - 3 if self.isChecked() else r.left() + 3
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("white") if self.isChecked() else QColor(C["muted"]))
        p.drawEllipse(QRectF(x, r.top() + 3, d, d))
        p.end()


class NumItem(QTableWidgetItem):
    """Ячейка, которая сортируется по числу, а не по тексту."""

    def __init__(self, text: str, value: float):
        super().__init__(text)
        self.setData(Qt.UserRole, value)
        self.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)

    def __lt__(self, other):
        return (self.data(Qt.UserRole) or 0) < (other.data(Qt.UserRole) or 0)


# ------------------------------------------------------- фоновые задачи ---

class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)


class _Task(QRunnable):
    def __init__(self, fn, signals: _Signals):
        super().__init__()
        self.fn, self.signals = fn, signals

    def run(self):
        try:
            result = self.fn(self.signals.progress.emit)
        except Exception as e:
            traceback.print_exc()
            self.signals.failed.emit(str(e))
            return
        self.signals.done.emit(result)


_alive: set[_Signals] = set()


def run_async(fn, on_done, on_error=None, on_progress=None):
    """Запускает fn(progress) в фоне, результат отдаёт в GUI-поток."""
    sig = _Signals()
    _alive.add(sig)
    sig.done.connect(lambda r: (_alive.discard(sig), on_done(r)))
    sig.failed.connect(lambda e: (_alive.discard(sig), (on_error or _show_error)(e)))
    if on_progress:
        sig.progress.connect(on_progress)
    QThreadPool.globalInstance().start(_Task(fn, sig))


def _show_error(msg: str):
    QMessageBox.critical(QApplication.activeWindow(), "Ошибка", msg)


def confirm(parent, title: str, text: str, danger: bool = False) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
    box.button(QMessageBox.Yes).setText("Да, сделать")
    box.button(QMessageBox.Cancel).setText("Отмена")
    box.setDefaultButton(QMessageBox.Cancel if danger else QMessageBox.Yes)
    return box.exec() == QMessageBox.Yes


class Sampler(QThread):
    """Раз в секунду снимает показатели системы, раз в две — процессы. Всё в фоне,
    чтобы окно не подвисало: на Windows обход процессов занимает заметное время."""

    stats = Signal(object)
    procs = Signal(object)

    def __init__(self, kb_getter):
        super().__init__()
        self.kb_getter = kb_getter
        self._stop = threading.Event()

    def stop(self):
        self._stop.set()

    def run(self):
        mon = ProcessMonitor()
        mon.sample()
        psutil.cpu_percent(percpu=True)
        last_t = time.monotonic()
        disk0, net0 = _io_counters()
        temps, disks = {}, []
        tick = 0
        while not self._stop.wait(1.0):
            try:
                now = time.monotonic()
                dt = max(now - last_t, 0.001)
                last_t = now
                per = psutil.cpu_percent(percpu=True)
                disk1, net1 = _io_counters()
                if tick % 3 == 0:
                    temps = sysinfo.temperatures()
                if tick % 10 == 0:
                    disks = sysinfo.disks()
                freq = psutil.cpu_freq()
                sw = psutil.swap_memory()
                self.stats.emit({
                    "cpu": sum(per) / max(len(per), 1), "per": per, "freq": freq.current if freq else None,
                    "ram": sysinfo.memory(), "swap": (sw.used, sw.total, sw.percent),
                    "disk_r": max(disk1[0] - disk0[0], 0) / dt, "disk_w": max(disk1[1] - disk0[1], 0) / dt,
                    "net_rx": max(net1[0] - net0[0], 0) / dt, "net_tx": max(net1[1] - net0[1], 0) / dt,
                    "uptime": sysinfo.uptime(), "temps": temps, "disks": disks,
                })
                disk0, net0 = disk1, net1
                if tick % 2 == 1:
                    self.procs.emit(mon.sample(self.kb_getter()))
            except Exception:
                traceback.print_exc()
            tick += 1


def _io_counters() -> tuple[tuple[int, int], tuple[int, int]]:
    try:
        d = psutil.disk_io_counters()
        disk = (d.read_bytes, d.write_bytes) if d else (0, 0)
    except (OSError, RuntimeError):
        disk = (0, 0)
    try:
        n = psutil.net_io_counters()
        net = (n.bytes_recv, n.bytes_sent) if n else (0, 0)
    except OSError:
        net = (0, 0)
    return disk, net


# ----------------------------------------------------------------- Обзор ---

LEVEL_ICON = {checks.OK: "✓", checks.INFO: "i", checks.WARN: "!", checks.BAD: "✕"}


class StatTile(QFrame):
    """Плитка «ЦП 23%» с графиком — кликом ведёт на вкладку «Система»."""

    def __init__(self, title: str, colors: list[str], maximum: float | None, on_click):
        super().__init__()
        self.setObjectName("tile")
        self.setCursor(Qt.PointingHandCursor)
        self.on_click = on_click
        v = QVBoxLayout(self)
        v.setContentsMargins(px(16), px(12), px(16), px(14))
        v.setSpacing(px(4))
        v.addWidget(section(title))
        self.value = QLabel("—")
        self.value.setObjectName("big")
        v.addWidget(self.value)
        self.sub = muted("")
        self.sub.setWordWrap(False)
        v.addWidget(self.sub)
        self.graph = Sparkline(colors, maximum, 64)
        v.addWidget(self.graph, 1)

    def mousePressEvent(self, e):
        self.on_click()
        super().mousePressEvent(e)


class TopList(QWidget):
    """Пять самых прожорливых программ по одному показателю."""

    N = 5

    def __init__(self, title: str):
        super().__init__()
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setVerticalSpacing(px(6))
        g.addWidget(section(title), 0, 0, 1, 2)
        self.rows = []
        for i in range(self.N):
            name, val = QLabel(""), QLabel("")
            val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            val.setStyleSheet("font-weight: 700;")
            g.addWidget(name, i + 1, 0)
            g.addWidget(val, i + 1, 1)
            self.rows.append((name, val))
        g.setColumnStretch(0, 1)

    def set(self, rows: list[tuple[str, str, str]]):
        for (name, val), row in zip(self.rows, rows + [("", "", "")] * self.N):
            text, value, color = row
            fm = name.fontMetrics()
            name.setText(f"<span style='color:{color}'>●</span>&nbsp; " + fm.elidedText(text, Qt.ElideRight, px(220))
                         if text else "")
            val.setText(value)


class OverviewTab(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(14))
        self.headline = QLabel("Нажмите «Проверить», чтобы узнать, почему тормозит компьютер")
        self.headline.setObjectName("h1")
        self.headline.setWordWrap(True)
        self.sub = muted("Проверка ничего не меняет в системе. Любое изменение — только после вашего подтверждения, "
                         "и его можно откатить во вкладке «Журнал».")
        head = QVBoxLayout()
        head.setSpacing(px(2))
        head.addWidget(self.headline)
        head.addWidget(self.sub)
        lay.addLayout(head)

        tiles = QHBoxLayout()
        tiles.setSpacing(px(12))
        go_sys = lambda: self.main.go("system")  # noqa: E731
        self.t_cpu = StatTile("Процессор", [C["accent2"]], 100, go_sys)
        self.t_ram = StatTile("Память", [C["accent"]], 100, go_sys)
        self.t_disk = StatTile("Диск", [C["ok"], C["warn"]], None, go_sys)
        self.t_net = StatTile("Сеть", [C["info"], "#ff7ad9"], None, go_sys)
        for t in (self.t_cpu, self.t_ram, self.t_disk, self.t_net):
            tiles.addWidget(t, 1)
        lay.addLayout(tiles)

        body = QHBoxLayout()
        body.setSpacing(px(14))
        left = QVBoxLayout()
        left.setSpacing(px(6))
        left.addWidget(section("Что нашла проверка"))
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list = QWidget()
        self.list.setStyleSheet("background: transparent;")
        self.list_lay = QVBoxLayout(self.list)
        self.list_lay.setContentsMargins(0, 0, px(6), 0)
        self.list_lay.setSpacing(px(8))
        self.list_lay.addStretch()
        self.scroll.setWidget(self.list)
        left.addWidget(self.scroll, 1)
        body.addLayout(left, 3)

        side = QFrame()
        side.setObjectName("card")
        sv = QVBoxLayout(side)
        sv.setContentsMargins(px(16), px(14), px(16), px(12))
        sv.setSpacing(px(14))
        self.top_cpu = TopList("Сейчас грузят процессор")
        self.top_mem = TopList("Больше всех памяти")
        sv.addWidget(self.top_cpu)
        sv.addWidget(self.top_mem)
        sv.addStretch()
        b = QPushButton("Все процессы →")
        b.setObjectName("link")
        b.clicked.connect(lambda: self.main.go("procs"))
        sv.addWidget(b, 0, Qt.AlignRight)
        body.addWidget(side, 2)
        lay.addLayout(body, 1)

    def on_stats(self, s: dict, h: dict):
        self.t_cpu.value.setText(f"{s['cpu']:.0f}%")
        self.t_cpu.sub.setText(f"{s['freq'] / 1000:.2f} ГГц" if s["freq"] else f"{len(s['per'])} потоков")
        self.t_cpu.graph.set_series(h["cpu"])
        used, total, pct = s["ram"]
        self.t_ram.value.setText(f"{pct:.0f}%")
        self.t_ram.sub.setText(f"{human_size(used)} из {human_size(total)}")
        self.t_ram.graph.set_series(h["ram"])
        self.t_disk.value.setText(rate(s["disk_r"] + s["disk_w"]))
        self.t_disk.sub.setText(f"чтение {rate(s['disk_r'])} · запись {rate(s['disk_w'])}")
        self.t_disk.graph.set_series(h["disk_r"], h["disk_w"])
        self.t_net.value.setText(rate(s["net_rx"]))
        self.t_net.sub.setText(f"↓ {rate(s['net_rx'])} · ↑ {rate(s['net_tx'])}")
        self.t_net.graph.set_series(h["net_rx"], h["net_tx"])

    def on_procs(self, groups):
        def color(g):
            return VERDICT_COLORS.get(g.verdict.verdict if g.verdict else "unknown", C["muted"])
        by_cpu = sorted(groups, key=lambda g: g.cpu, reverse=True)[:TopList.N]
        by_mem = sorted(groups, key=lambda g: g.mem, reverse=True)[:TopList.N]
        self.top_cpu.set([(g.name, f"{g.cpu:.1f}%", color(g)) for g in by_cpu])
        self.top_mem.set([(g.name, human_size(g.mem), color(g)) for g in by_mem])

    def show_progress(self, text: str):
        self.headline.setText("Проверяю…")
        self.headline.setStyleSheet("")
        self.sub.setText(text)

    def show_result(self, r: checks.ScanResult):
        bad = sum(f.level == checks.BAD for f in r.findings)
        warn = sum(f.level == checks.WARN for f in r.findings)
        if bad:
            self.headline.setText(f"Найдено серьёзных проблем: {bad}" + (f", замечаний: {warn}" if warn else ""))
            self.headline.setStyleSheet(f"color: {C['bad']};")
        elif warn:
            self.headline.setText(f"Есть что улучшить: {warn}")
            self.headline.setStyleSheet(f"color: {C['warn']};")
        else:
            self.headline.setText("Всё в порядке")
            self.headline.setStyleSheet(f"color: {C['ok']};")
        self.sub.setText(f"Проверка заняла {r.took:.1f} с · {time.strftime('%H:%M')}. "
                         "Показатели вверху обновляются каждую секунду.")
        while self.list_lay.count() > 1:
            w = self.list_lay.takeAt(0).widget()
            if w:
                w.deleteLater()
        for i, f in enumerate(r.findings):
            self.list_lay.insertWidget(i, self._card(f))

    def _card(self, f: checks.Finding) -> QFrame:
        color = C[f.level]
        card = QFrame()
        card.setObjectName("card")
        card.setStyleSheet(f"QFrame#card {{ border-left: 4px solid {color}; }}")
        h = QHBoxLayout(card)
        h.setContentsMargins(px(16), px(12), px(14), px(12))
        h.setSpacing(px(12))
        icon = QLabel(LEVEL_ICON[f.level])
        d = px(30)
        icon.setFixedSize(d, d)
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(f"background: {color}; color: {C['bg']}; border-radius: {d // 2}px; font-weight: 800;")
        h.addWidget(icon, 0, Qt.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(px(4))
        title = QLabel(f.title)
        title.setWordWrap(True)
        title.setStyleSheet(f"font-weight: 700; font-size: {pt(11.5)};")
        v.addWidget(title)
        if f.detail:
            d = QLabel(f.detail)
            d.setWordWrap(True)
            v.addWidget(d)
        if f.advice:
            v.addWidget(muted("→ " + f.advice))
        h.addLayout(v, 1)
        if f.action == "proxy_off":
            btn = QPushButton("Выключить прокси")
            btn.setObjectName("primary")
            btn.clicked.connect(self.main.fix_proxy)
            h.addWidget(btn, 0, Qt.AlignVCenter)
        elif f.tab:
            btn = QPushButton("Перейти →")
            btn.clicked.connect(lambda _=False, t=f.tab: self.main.go(t))
            h.addWidget(btn, 0, Qt.AlignVCenter)
        return card


# ---------------------------------------------------------------- Система ---

class SystemTab(QWidget):
    """Сводка о железе в духе CPU-Z плюс живые графики, как в Диспетчере задач."""

    def __init__(self):
        super().__init__()
        self.loaded = False
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        body.setStyleSheet("background: transparent;")
        self.grid = QGridLayout(body)
        self.grid.setContentsMargins(px(20), px(8), px(20), px(20))
        self.grid.setHorizontalSpacing(px(16))
        self.grid.setVerticalSpacing(px(6))
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.f: dict[str, QLabel] = {}

        cpu = QGroupBox("Процессор")
        g = QGridLayout(cpu)
        g.setVerticalSpacing(px(8))
        self._row(g, 0, "Модель", "cpu", span=3)
        self._row(g, 1, "Ядра", "cores")
        self._row(g, 1, "Потоки", "threads", col=2)
        self._row(g, 2, "Частота", "freq")
        self._row(g, 2, "Макс.", "freq_max", col=2)
        g.addWidget(muted("Загрузка"), 3, 0)
        self.cpu_bar = Bar(60, 85, 26)
        g.addWidget(self.cpu_bar, 3, 1, 1, 3)
        self.cpu_graph = Sparkline([C["accent2"]], 100, 150)
        g.addWidget(self.cpu_graph, 4, 0, 1, 4)
        self.core_box = QGridLayout()
        self.core_box.setSpacing(px(5))
        g.addLayout(self.core_box, 5, 0, 1, 4)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)
        self.core_bars: list[Bar] = []

        mem = QGroupBox("Память")
        g = QGridLayout(mem)
        g.setVerticalSpacing(px(8))
        self._row(g, 0, "Всего", "ram_total")
        g.addWidget(muted("ОЗУ"), 1, 0)
        self.ram_bar = Bar(70, 90, 26)
        g.addWidget(self.ram_bar, 1, 1)
        g.addWidget(muted("Подкачка"), 2, 0)
        self.swap_bar = Bar(50, 80)
        g.addWidget(self.swap_bar, 2, 1)
        self.ram_graph = Sparkline([C["accent"]], 100, 100)
        g.addWidget(self.ram_graph, 3, 0, 1, 2)
        g.setColumnStretch(1, 1)

        self.sys_box = QGroupBox("Система")
        g = QGridLayout(self.sys_box)
        self._row(g, 0, "ОС", "os")
        self._row(g, 1, "Компьютер", "host")
        self._row(g, 2, "Плата", "board")
        self._row(g, 3, "Работает", "uptime")
        g.setColumnStretch(1, 1)

        self.gpu_box = QGroupBox("Видеокарта")
        self.gpu_lay = QGridLayout(self.gpu_box)
        self.gpu_lay.addWidget(muted("Загрузка…"), 0, 0)

        self.disk_box = QGroupBox("Диски")
        dv = QVBoxLayout(self.disk_box)
        self.disk_lay = QGridLayout()
        dv.addLayout(self.disk_lay)
        self.disk_io = muted("")
        dv.addWidget(self.disk_io)
        self.disk_graph = Sparkline([C["ok"], C["warn"]], None, 80)
        dv.addWidget(self.disk_graph)
        self.disk_bars: dict[str, Bar] = {}

        net = QGroupBox("Сеть")
        nv = QVBoxLayout(net)
        self.net_io = muted("")
        nv.addWidget(self.net_io)
        self.net_graph = Sparkline([C["info"], "#ff7ad9"], None, 80)
        nv.addWidget(self.net_graph)

        self.temp_box = QGroupBox("Температуры")
        self.temp_lay = QGridLayout(self.temp_box)
        self.temp_bars: dict[str, Bar] = {}
        self._temp_names: tuple | None = None

        # Две независимые колонки: высота блока слева не растягивает соседа справа
        for col, boxes in enumerate([(cpu, mem, self.temp_box), (self.sys_box, self.gpu_box, self.disk_box, net)]):
            v = QVBoxLayout()
            v.setSpacing(px(6))
            for box in boxes:
                v.addWidget(box)
            v.addStretch()
            self.grid.addLayout(v, 0, col)
            self.grid.setColumnStretch(col, 1)

    def _row(self, g: QGridLayout, row: int, label: str, key: str, col: int = 0, span: int = 1):
        g.addWidget(muted(label), row, col)
        self.f[key] = field("…")
        g.addWidget(self.f[key], row, col + 1, 1, span)

    def activate(self):
        if not self.loaded:
            self.loaded = True
            run_async(lambda _p: sysinfo.static_info(), self.fill)

    def fill(self, info: dict):
        self.info = info
        self.f["cpu"].setText(info["cpu"])
        self.f["cores"].setText(str(info["cores"]))
        self.f["threads"].setText(str(info["threads"]))
        self.f["freq_max"].setText(f"{info['freq_max']:.0f} МГц" if info["freq_max"] else "—")
        self.f["ram_total"].setText(human_size(info["ram_total"]))
        self.f["os"].setText(info["os"])
        self.f["host"].setText(info["host"])
        self.f["board"].setText(info["board"])

        self._clear(self.gpu_lay)
        if not info["gpus"]:
            self.gpu_lay.addWidget(muted("Не удалось определить"), 0, 0)
        row = 0
        for gpu in info["gpus"]:
            name = field(gpu["name"])
            name.setStyleSheet("font-weight: 700;")
            self.gpu_lay.addWidget(name, row, 0, 1, 4)
            row += 1
            self.gpu_lay.addWidget(muted("Драйвер"), row, 0)
            self.gpu_lay.addWidget(field(gpu.get("driver") or "—"), row, 1)
            self.gpu_lay.addWidget(muted("Память"), row, 2)
            vram = gpu.get("vram_mb")
            self.gpu_lay.addWidget(field(f"{vram / 1024:.1f} ГБ" if vram else "—"), row, 3)
            row += 1
            if gpu.get("load") is not None:
                self.gpu_lay.addWidget(muted("Загрузка"), row, 0)
                b = Bar(80, 95)
                b.set(gpu["load"])
                self.gpu_lay.addWidget(b, row, 1)
                self.gpu_lay.addWidget(muted("Темп."), row, 2)
                self.gpu_lay.addWidget(field(f"{gpu['temp']:.0f} °C" if gpu.get("temp") else "—"), row, 3)
                row += 1
        self.gpu_lay.setColumnStretch(1, 1)
        self.gpu_lay.setColumnStretch(3, 1)
        self._fill_disks(info["disks"])

    def _fill_disks(self, disks: list[dict]):
        if [d["mount"] for d in disks] != list(self.disk_bars):
            self._clear(self.disk_lay)
            self.disk_bars = {}
            for i, d in enumerate(disks):
                self.disk_lay.addWidget(muted(d["mount"]), i, 0)
                self.disk_bars[d["mount"]] = b = Bar(80, 90)
                self.disk_lay.addWidget(b, i, 1)
            self.disk_lay.setColumnStretch(1, 1)
        for d in disks:
            self.disk_bars[d["mount"]].set(d["percent"], f"свободно {human_size(d['free'])} из {human_size(d['total'])}")

    @staticmethod
    def _clear(lay: QGridLayout):
        while lay.count():
            w = lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def on_stats(self, s: dict, h: dict):
        per = s["per"]
        if len(self.core_bars) != len(per):
            for b in self.core_bars:
                b.deleteLater()
            self.core_bars = []
            cols = 4 if len(per) > 8 else 2
            for i in range(len(per)):
                b = Bar(80, 95, 18)
                self.core_bars.append(b)
                self.core_box.addWidget(b, i // cols, i % cols)
        for i, (b, v) in enumerate(zip(self.core_bars, per)):
            b.set(v, f"{i}: {v:.0f}%")
        self.cpu_bar.set(s["cpu"])
        self.cpu_graph.set_series(h["cpu"])
        self.f["freq"].setText(f"{s['freq']:.0f} МГц" if s["freq"] else "—")
        used, total, pct = s["ram"]
        self.ram_bar.set(pct, f"{human_size(used)} / {human_size(total)}  ({pct:.0f}%)")
        self.ram_graph.set_series(h["ram"])
        su, st, sp = s["swap"]
        self.swap_bar.set(sp, f"{human_size(su)} / {human_size(st)}" if st else "нет")
        self.f["uptime"].setText(human_duration(s["uptime"]))
        if s["disks"]:
            self._fill_disks(s["disks"])
        self.disk_io.setText(f"<span style='color:{C['ok']}'>■</span> чтение {rate(s['disk_r'])}   "
                             f"<span style='color:{C['warn']}'>■</span> запись {rate(s['disk_w'])}")
        self.disk_graph.set_series(h["disk_r"], h["disk_w"])
        self.net_io.setText(f"<span style='color:{C['info']}'>■</span> получено {rate(s['net_rx'])}   "
                            f"<span style='color:#ff7ad9'>■</span> отправлено {rate(s['net_tx'])}")
        self.net_graph.set_series(h["net_rx"], h["net_tx"])
        self._fill_temps(s["temps"])

    def _fill_temps(self, temps: dict):
        names = tuple(sorted(temps))
        note = ""
        if not temps:
            if IS_MAC:
                note = "Система не отдаёт датчики температуры (macOS не даёт их программам без прав администратора)"
            elif not IS_WINDOWS:
                note = "Система не отдаёт датчики температуры"
            elif not lhm.ready():
                note = "Ищу датчики…"
            else:
                note = f'{lhm.problem()} <a href="{lhm.RELEASES_URL}">Скачать LibreHardwareMonitor</a>'
        elif lhm.source():
            note = f"Источник: {lhm.source()}" + (f". {lhm.problem()}" if lhm.problem() else "")
        if (names, note) != self._temp_names:
            self._temp_names = (names, note)
            self._clear(self.temp_lay)
            self.temp_bars = {}
            for i, name in enumerate(names):
                self.temp_lay.addWidget(muted(name), i, 0)
                self.temp_bars[name] = b = Bar(75, 85)
                self.temp_lay.addWidget(b, i, 1)
            if note:
                lbl = muted(note)
                lbl.setOpenExternalLinks(True)
                self.temp_lay.addWidget(lbl, len(names), 0, 1, 2)
            self.temp_lay.setColumnStretch(1, 1)
        for name, t in temps.items():
            self.temp_bars[name].set(t, f"{t:.0f} °C")


# ------------------------------------------------------------ Автозагрузка ---

class StartupTab(QWidget):
    COLS = ["Вкл.", "Программа", "Вердикт базы", "Откуда", "Команда"]

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.items: list[startup.StartupItem] = []
        self.shown: list[startup.StartupItem] = []
        self.busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))

        top = QHBoxLayout()
        top.addWidget(muted("Что запускается вместе с системой. Щёлкните переключатель, чтобы отключить программу "
                            "или включить обратно. Вердикт — из общей базы WhySlow на GitHub."), 1)
        self.show_services = QCheckBox("Показывать системные службы")
        self.show_services.setVisible(IS_LINUX or IS_WINDOWS)
        self.show_services.toggled.connect(self.refill)
        top.addWidget(self.show_services)
        lay.addLayout(top)
        self.warning = muted("")
        self.warning.setStyleSheet(f"color: {C['warn']};")
        self.warning.hide()
        lay.addWidget(self.warning)

        self.table = make_table(self.COLS)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, px(70))
        for i in (1, 2, 3):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self.on_select)
        lay.addWidget(self.table, 1)

        self.detail = QLabel("Выберите программу, чтобы увидеть пояснение.")
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"QLabel {{ background: {C['panel2']}; border: 1px solid {C['border']};"
                                  f"border-radius: 12px; padding: {px(12)}px; }}")
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setMinimumHeight(px(80))
        lay.addWidget(self.detail)

        btns = QHBoxLayout()
        self.b_off = QPushButton("Отключить")
        self.b_off.setObjectName("primary")
        self.b_on = QPushButton("Включить")
        self.b_del = QPushButton("Удалить из автозагрузки")
        self.b_del.setObjectName("danger")
        self.b_report = QPushButton("Сообщить в базу…")
        self.b_off.clicked.connect(lambda: self.toggle(self.current(), False))
        self.b_on.clicked.connect(lambda: self.toggle(self.current(), True))
        self.b_del.clicked.connect(self.delete)
        self.b_report.clicked.connect(self.report)
        for b in (self.b_off, self.b_on, self.b_del):
            btns.addWidget(b)
        btns.addStretch()
        btns.addWidget(self.b_report)
        lay.addLayout(btns)
        self.on_select()

    def set_items(self, items):
        self.items = items
        self.warning.setText(startup.last_error)
        self.warning.setVisible(bool(startup.last_error))
        self.refill()

    def visible_items(self):
        return [i for i in self.items if self.show_services.isChecked() or not i.system
                or not i.enabled or (i.verdict and i.verdict.verdict in ("bad", "safe"))]

    def refill(self):
        keep = self.current()
        rows = self.visible_items()
        order = {"bad": 0, "safe": 1, "optional": 2, "unknown": 3, "keep": 4}
        rows.sort(key=lambda i: (not i.enabled, order.get(i.verdict.verdict if i.verdict else "unknown", 5), i.name.lower()))
        self.shown = rows
        self.table.setRowCount(len(rows))
        for r, it in enumerate(rows):
            sw = Switch(it.enabled)
            sw.setToolTip("Включено — щёлкните, чтобы отключить" if it.enabled else "Отключено — щёлкните, чтобы включить")
            sw.clicked.connect(lambda _=False, it=it: self.toggle(it, not it.enabled))
            if not it.can_toggle:
                sw.setEnabled(False)
                sw.setToolTip("Запустится один раз при следующем входе; такую запись можно только удалить")
            cell = QWidget()
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(0, 0, 0, 0)
            cl.addWidget(sw, 0, Qt.AlignCenter)
            self.table.setCellWidget(r, 0, cell)
            title = it.verdict.title if it.verdict else ""
            same = not title or title.lower() in it.name.lower() or it.name.lower() in title.lower()
            name = QTableWidgetItem(it.name if same else f"{it.name}  ·  {title}")
            f = QFont()
            f.setBold(it.enabled)
            name.setFont(f)
            if not it.enabled:
                name.setForeground(QColor(C["muted"]))
            v = it.verdict
            verdict = QTableWidgetItem(v.label if v else "—")
            verdict.setForeground(QColor(VERDICT_COLORS.get(v.verdict if v else "unknown")))
            f = QFont()
            f.setBold(True)
            verdict.setFont(f)
            cmd = QTableWidgetItem(it.command)
            cmd.setForeground(QColor(C["muted"]))
            for c, item in enumerate([QTableWidgetItem(""), name, verdict, QTableWidgetItem(it.source), cmd]):
                self.table.setItem(r, c, item)
        if keep:
            for r, it in enumerate(rows):
                if (it.kind, it.name, it.source) == (keep.kind, keep.name, keep.source):
                    self.table.selectRow(r)
                    break
        self.on_select()

    def current(self) -> startup.StartupItem | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.shown[rows[0].row()] if rows and rows[0].row() < len(self.shown) else None

    def on_select(self):
        it = self.current()
        for b in (self.b_off, self.b_on, self.b_del, self.b_report):
            b.setEnabled(it is not None and not self.busy)
        if not it:
            self.detail.setText("Выберите программу, чтобы увидеть пояснение.")
            return
        self.b_off.setEnabled(it.enabled and it.can_toggle and not self.busy)
        self.b_on.setEnabled(not it.enabled and it.can_toggle and not self.busy)
        self.b_del.setEnabled(it.can_delete and not self.busy)
        v = it.verdict
        color = VERDICT_COLORS.get(v.verdict, C["muted"])
        cat = f" · {v.category}" if v.category else ""
        admin = (f"<br><span style='color:{C['muted']}'>Для изменения нужны права администратора"
                 + (" — macOS спросит пароль." if IS_MAC else ".") + "</span>") if it.needs_admin else ""
        if not it.can_toggle:
            admin += (f"<br><span style='color:{C['muted']}'>Запустится один раз при следующем входе. "
                      "Отключить такую запись нельзя, только удалить.</span>")
        self.detail.setText(f"<b>{it.name}</b>{cat} — <b style='color:{color}'>{v.label}</b><br>{v.note}"
                            f"<br><span style='color:{C['muted']}'>{it.command}</span>{admin}")

    def _apply(self, fn, ok_text):
        """Изменение — в фоне: macOS может спросить пароль, Windows — подумать над службой."""
        self.busy = True
        self.table.setEnabled(False)
        self.on_select()
        self.main.status("Применяю…")

        def done(_r):
            self.busy = False
            self.table.setEnabled(True)
            self.main.status(ok_text)
            self.reload()

        def failed(msg):
            self.busy = False
            self.table.setEnabled(True)
            self.refill()
            QMessageBox.warning(self, "Не получилось", msg)

        run_async(lambda _p: fn(), done, failed)

    def reload(self):
        run_async(lambda _p: startup.list_items(self.main.kb), self.set_items)
        self.main.journal_tab.reload()

    def toggle(self, it: startup.StartupItem | None, enable: bool):
        if not it or self.busy:
            return
        if not enable:
            v = it.verdict
            if v and v.verdict == "keep":
                if not confirm(self, "Точно отключить?",
                               f"База WhySlow советует НЕ трогать «{it.name}».\n\n{v.note}\n\n"
                               "Отключить всё равно? (включить обратно можно в любой момент)", danger=True):
                    return
            elif it.kind == "win_service":
                if not confirm(self, "Перевести службу в режим «Вручную»?",
                               f"Служба «{it.name}» перестанет запускаться сама при включении компьютера.\n"
                               "Если она понадобится какой-то программе, Windows запустит её сама.\n\n"
                               "Вернуть прежний режим можно здесь же или во вкладке «Журнал»."):
                    return
            elif not confirm(self, "Отключить из автозагрузки?",
                             f"«{it.name}» перестанет запускаться при включении компьютера.\n"
                             "Сама программа останется установленной, её можно открыть вручную.\n\n"
                             "Включить обратно можно здесь же или во вкладке «Журнал»."):
                return
        elif not confirm(self, "Включить автозапуск?", f"«{it.name}» снова будет запускаться вместе с системой."):
            return
        self._apply(lambda: startup.set_enabled(it, enable), f"«{it.name}» {'включено' if enable else 'отключено'}")

    def delete(self):
        it = self.current()
        if not it or not confirm(self, "Удалить из автозагрузки?",
                                 f"Запись «{it.name}» будет удалена из автозагрузки.\n"
                                 "Программа останется установленной. Резервная копия записи сохранится — "
                                 "её можно вернуть во вкладке «Журнал».", danger=it.verdict.verdict != "bad"):
            return
        self._apply(lambda: startup.delete(it), f"«{it.name}» удалено из автозагрузки")

    def report(self):
        it = self.current()
        if it:
            webbrowser.open(knowledge.report_url(it.name, it.command))


# --------------------------------------------------------------- Процессы ---

class ProcessesTab(QWidget):
    COLS = ["Программа", "Процессов", "ЦП", "Память", "Вердикт базы"]

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.groups = {}
        self.latest = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))
        lay.addWidget(muted("Программы, сгруппированные по имени (браузер = десятки процессов). "
                            "Обновляется каждые 2 секунды. Нажмите на заголовок, чтобы отсортировать."))
        self.table = make_table(self.COLS)
        self.table.setSortingEnabled(True)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for i in range(1, len(self.COLS)):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        self.table.sortItems(3, Qt.DescendingOrder)
        self.table.itemSelectionChanged.connect(self.on_select)
        lay.addWidget(self.table, 1)
        self.detail = muted("")
        lay.addWidget(self.detail)
        row = QHBoxLayout()
        self.b_kill = QPushButton("Закрыть программу")
        self.b_kill.setObjectName("danger")
        self.b_kill.clicked.connect(self.kill)
        self.b_kill.setEnabled(False)
        row.addWidget(self.b_kill)
        row.addStretch()
        lay.addLayout(row)

    def on_procs(self, groups):
        self.latest = groups
        if self.isVisible():
            self.refresh()

    def selected_name(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def refresh(self):
        keep = self.selected_name()
        self.groups = {g.name: g for g in self.latest}
        top = sorted((g for g in self.groups.values() if g.mem), key=lambda g: g.mem, reverse=True)[:80]
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(top))
        for r, g in enumerate(top):
            name = QTableWidgetItem(g.name + ("  (система)" if g.system else ""))
            name.setData(Qt.UserRole, g.name)
            v = g.verdict
            verdict = QTableWidgetItem(v.label if v and v.verdict != "unknown" else "")
            verdict.setForeground(QColor(VERDICT_COLORS.get(v.verdict if v else "unknown")))
            cpu = NumItem(f"{g.cpu:.1f}%", g.cpu)
            if g.cpu > 25:
                cpu.setForeground(QColor(C["bad"] if g.cpu > 60 else C["warn"]))
            mem = NumItem(human_size(g.mem), g.mem)
            if g.mem > 1.5 * 2**30:
                mem.setForeground(QColor(C["warn"]))
            for c, it in enumerate([name, NumItem(str(len(g.pids)), len(g.pids)), cpu, mem, verdict]):
                self.table.setItem(r, c, it)
        self.table.setSortingEnabled(True)
        if keep:
            for r in range(self.table.rowCount()):
                if self.table.item(r, 0).data(Qt.UserRole) == keep:
                    self.table.selectRow(r)
                    break

    def on_select(self):
        name = self.selected_name()
        g = self.groups.get(name) if name else None
        self.b_kill.setEnabled(g is not None)
        if g:
            v = g.verdict
            note = v.note if v and v.verdict != "unknown" else ""
            self.detail.setText(f"{g.exe or g.name}" + (f"\n{note}" if note else ""))

    def kill(self):
        g = self.groups.get(self.selected_name())
        if not g:
            return
        warn = ""
        if g.system or (g.verdict and g.verdict.verdict == "keep"):
            warn = "\n\n⚠ Это системный процесс или важный компонент. Закрытие может привести к сбою или перезагрузке."
        if not confirm(self, "Закрыть программу?",
                       f"Закрыть «{g.name}» ({len(g.pids)} проц.)?\nНесохранённые данные в ней будут потеряны.{warn}",
                       danger=bool(warn)):
            return
        self.b_kill.setEnabled(False)

        def done(r):
            closed, failed = r
            msg = f"«{g.name}»: закрыто процессов — {closed}"
            if failed:
                msg += f", не удалось — {failed} ({ADMIN_HINT})"
            self.main.status(msg)
            self.main.journal_tab.reload()

        run_async(lambda _p: kill(g), done)


# ---------------------------------------------------------------- Очистка ---

class JunkTab(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.cats: list[junk.JunkCategory] = []
        self.boxes: list[QCheckBox] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))
        self.total = QLabel("Нажмите «Проверить» вверху, чтобы найти мусор")
        self.total.setObjectName("h1")
        lay.addWidget(self.total)
        lay.addWidget(muted(f"Удаляются только ваши файлы старше суток ({junk.WHOLE}). "
                            "Файлы, которые сейчас заняты программами, пропускаются."))
        self.box = QGroupBox("Что можно удалить")
        self.box_lay = QGridLayout(self.box)
        self.box_lay.setVerticalSpacing(px(10))
        self.box_lay.setHorizontalSpacing(px(14))
        lay.addWidget(self.box)
        lay.addStretch()
        row = QHBoxLayout()
        self.b_clean = QPushButton("Очистить выбранное")
        self.b_clean.setObjectName("primary")
        self.b_clean.setEnabled(False)
        self.b_clean.clicked.connect(self.clean)
        row.addWidget(self.b_clean)
        row.addStretch()
        lay.addLayout(row)

    def set_cats(self, cats):
        self.cats = cats
        SystemTab._clear(self.box_lay)
        self.boxes = []
        for i, c in enumerate(cats):
            cb = QCheckBox(c.title)
            cb.setChecked(c.size > 0 and c.key not in ("trash", "recycle"))
            cb.setEnabled(c.size > 0)
            cb.toggled.connect(self.update_total)
            self.boxes.append(cb)
            self.box_lay.addWidget(cb, i, 0)
            self.box_lay.addWidget(muted(c.note), i, 1)
            size = QLabel(human_size(c.size) + (f"  ·  {c.files} файлов" if c.files else ""))
            size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.box_lay.addWidget(size, i, 2)
        self.box_lay.setColumnStretch(1, 1)
        self.update_total()

    def update_total(self):
        sel = sum(c.size for c, b in zip(self.cats, self.boxes) if b.isChecked())
        total = sum(c.size for c in self.cats)
        self.total.setText(f"Можно освободить {human_size(total)}" if total else "Мусора не найдено")
        self.b_clean.setText(f"Очистить выбранное ({human_size(sel)})")
        self.b_clean.setEnabled(sel > 0)

    def clean(self):
        chosen = [c for c, b in zip(self.cats, self.boxes) if b.isChecked()]
        names = "\n".join(f"• {c.title} — {human_size(c.size)}" for c in chosen)
        if not confirm(self, "Удалить мусор?", f"Будут удалены:\n{names}\n\nЭто действие нельзя отменить.", danger=True):
            return
        self.b_clean.setEnabled(False)
        self.b_clean.setText("Удаляю…")

        def work(_p):
            res = [(c, *junk.clean(c)) for c in chosen]
            return res, [junk.scan(c) for c in junk.categories()]

        def done(r):
            res, cats = r
            freed = sum(x[1] for x in res)
            failed = sum(x[2] for x in res)
            self.set_cats(cats)
            self.main.journal_tab.reload()
            msg = f"Освобождено {human_size(freed)}."
            if failed:
                msg += f"\nПропущено файлов: {failed} (заняты программами или нет прав)."
            QMessageBox.information(self, "Готово", msg)

        run_async(work, done)


# ----------------------------------------------------------------- Журнал ---

class JournalTab(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))
        lay.addWidget(muted("Всё, что WhySlow поменял в системе. Отключение автозагрузки, удаление записей и "
                            "выключение прокси можно откатить."))
        self.table = make_table(["Когда", "Что сделано", "Статус"])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.on_select)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.b_undo = QPushButton("Откатить")
        self.b_undo.clicked.connect(self.undo)
        row.addWidget(self.b_undo)
        row.addStretch()
        lay.addLayout(row)
        self.reload()

    def reload(self):
        self.entries = list(reversed(journal.load()))
        self.table.setRowCount(len(self.entries))
        for r, e in enumerate(self.entries):
            status = "откачено" if e["undone"] else ("можно откатить" if e["undoable"] else "необратимо")
            st = QTableWidgetItem(status)
            st.setForeground(QColor(C["muted"] if e["undone"] or not e["undoable"] else C["ok"]))
            for c, it in enumerate([QTableWidgetItem(e["time"]), QTableWidgetItem(e["title"]), st]):
                self.table.setItem(r, c, it)
        self.on_select()

    def on_select(self):
        rows = self.table.selectionModel().selectedRows()
        e = self.entries[rows[0].row()] if rows else None
        self.b_undo.setEnabled(bool(e and e["undoable"] and not e["undone"]))

    def undo(self):
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return
        e = self.entries[rows[0].row()]
        if not confirm(self, "Откатить?", f"Вернуть как было:\n{e['title']}"):
            return
        try:
            if e["kind"].startswith("startup_"):
                startup.undo(e)
            elif e["kind"] == "proxy_off":
                network.undo(e)
        except Exception as ex:
            QMessageBox.warning(self, "Не получилось", str(ex))
            return
        journal.mark_undone(e["id"])
        self.main.status("Откачено: " + e["title"])
        self.reload()
        self.main.startup_tab.reload()


# ---------------------------------------------------------- Главное окно ---

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"WhySlow {__version__} — почему тормозит компьютер")
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1360, int(avail.width() * 0.92)), min(900, int(avail.height() * 0.92)))
        self.kb = knowledge.load()
        self.scan_result: checks.ScanResult | None = None
        self.hist = {k: deque(maxlen=60) for k in ("cpu", "ram", "disk_r", "disk_w", "net_rx", "net_tx")}

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(px(20), px(14), px(20), px(8))
        lay.setSpacing(px(12))

        header = QHBoxLayout()
        logo = QLabel(f"Why<span style='color:{C['accent2']}'>Slow</span>")
        logo.setStyleSheet(f"font-size: {pt(24)}; font-weight: 900; color: {C['accent']};")
        header.addWidget(logo)
        tagline = muted("объясняет, почему тормозит, и помогает это исправить")
        tagline.setContentsMargins(px(12), px(6), 0, 0)
        header.addWidget(tagline, 1)
        self.b_scan = QPushButton("Проверить")
        self.b_scan.setObjectName("primary")
        self.b_scan.clicked.connect(self.scan)
        header.addWidget(self.b_scan)
        lay.addLayout(header)

        self.tabs = QTabWidget()
        self.overview = OverviewTab(self)
        self.system_tab = SystemTab()
        self.startup_tab = StartupTab(self)
        self.procs_tab = ProcessesTab(self)
        self.junk_tab = JunkTab(self)
        self.journal_tab = JournalTab(self)
        self.tab_keys = {"overview": self.overview, "system": self.system_tab, "startup": self.startup_tab,
                         "procs": self.procs_tab, "junk": self.junk_tab, "journal": self.journal_tab}
        for w, title in [(self.overview, "Обзор"), (self.system_tab, "Система"), (self.startup_tab, "Автозагрузка"),
                         (self.procs_tab, "Процессы"), (self.junk_tab, "Очистка"), (self.journal_tab, "Журнал")]:
            self.tabs.addTab(w, title)
        self.tabs.currentChanged.connect(self.on_tab)
        lay.addWidget(self.tabs, 1)

        sb = self.statusBar()
        self.db_label = QLabel()
        sb.addPermanentWidget(self.db_label)
        b_db = QPushButton("Обновить базу")
        b_db.clicked.connect(lambda: self.update_db(manual=True))
        sb.addPermanentWidget(b_db)
        if IS_WINDOWS and not is_admin():
            b_admin = QPushButton("Перезапустить от администратора")
            b_admin.clicked.connect(self.restart_as_admin)
            sb.addPermanentWidget(b_admin)
        self.show_db()
        self.sampler = Sampler(lambda: self.kb)
        self.sampler.stats.connect(self.on_stats)
        self.sampler.procs.connect(self.on_procs)
        self.sampler.start()
        QTimer.singleShot(200, self.update_db)
        QTimer.singleShot(400, self.scan)

    def on_stats(self, s: dict):
        for k in ("disk_r", "disk_w", "net_rx", "net_tx"):
            self.hist[k].append(s[k])
        self.hist["cpu"].append(s["cpu"])
        self.hist["ram"].append(s["ram"][2])
        self.overview.on_stats(s, self.hist)
        self.system_tab.on_stats(s, self.hist)

    def on_procs(self, groups):
        self.overview.on_procs(groups)
        self.procs_tab.on_procs(groups)

    def closeEvent(self, e):
        self.sampler.stop()
        self.sampler.wait(3000)
        super().closeEvent(e)

    def status(self, text: str):
        self.statusBar().showMessage(text, 8000)

    def show_db(self):
        self.db_label.setText(f"База: v{self.kb.version} ({self.kb.source}), {len(self.kb.entries)} записей")

    def update_db(self, manual=False):
        def done(r):
            self.kb, msg = r
            self.show_db()
            if manual:
                self.status(msg)
        run_async(lambda _p: knowledge.update(), done)

    def go(self, key: str):
        self.tabs.setCurrentWidget(self.tab_keys[key])

    def on_tab(self, _i):
        cur = self.tabs.currentWidget()
        if cur is self.system_tab:
            self.system_tab.activate()
        elif cur is self.procs_tab:
            self.procs_tab.refresh()

    def scan(self):
        self.b_scan.setEnabled(False)
        self.b_scan.setText("Проверяю…")
        self.overview.show_progress("Начинаю…")

        def done(r: checks.ScanResult):
            self.scan_result = r
            self.overview.show_result(r)
            self.startup_tab.set_items(r.startup)
            self.junk_tab.set_cats(r.junk)
            self.b_scan.setEnabled(True)
            self.b_scan.setText("Проверить снова")

        def failed(e):
            self.b_scan.setEnabled(True)
            self.b_scan.setText("Проверить")
            _show_error(f"Проверка упала: {e}")

        run_async(lambda progress: checks.scan(self.kb, progress), done, failed, self.overview.show_progress)

    def fix_proxy(self):
        st = self.scan_result.proxy if self.scan_result else None
        if not st or not confirm(self, "Выключить прокси?",
                                 f"Системный прокси {st.server} будет выключен.\n"
                                 "Если интернет пропал из-за VPN — это вернёт его.\n"
                                 "Откатить можно во вкладке «Журнал»."):
            return
        try:
            network.disable_proxy(st)
        except Exception as e:
            _show_error(str(e))
            return
        self.journal_tab.reload()
        self.status("Прокси выключен")
        self.scan()

    def restart_as_admin(self):
        import ctypes
        args = " ".join(f'"{a}"' for a in ["-m", "whyslow"]) if not getattr(sys, "frozen", False) else ""
        if ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1) > 32:
            QApplication.quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("WhySlow")
    app.setStyle("Fusion")
    f = app.font()
    f.setPointSizeF(10 * SCALE)
    app.setFont(f)
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    return app.exec()
