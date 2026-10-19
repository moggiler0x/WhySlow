"""Графический интерфейс WhySlow (PySide6)."""

from __future__ import annotations

import sys
import time
import traceback
import webbrowser

import psutil
from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QHeaderView, QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
)

from . import __version__, checks, journal, junk, knowledge, network, startup, sysinfo
from .processes import ProcessMonitor, kill
from .util import IS_LINUX, IS_WINDOWS, human_duration, human_size, is_admin

# ------------------------------------------------------------------ стиль ---

C = {
    "bg": "#14161b", "panel": "#1c1f26", "panel2": "#232731", "border": "#2c313c",
    "text": "#e7e9ee", "muted": "#8f98a8", "accent": "#4f8cff",
    "ok": "#3fb950", "info": "#58a6ff", "warn": "#d29922", "bad": "#f85149",
}
VERDICT_COLORS = {"safe": C["ok"], "optional": C["warn"], "keep": C["info"], "bad": C["bad"], "unknown": C["muted"]}

STYLE = f"""
* {{ color: {C['text']}; font-size: 10pt; }}
QMainWindow, QWidget#root {{ background: {C['bg']}; }}
QTabWidget::pane {{ border: 1px solid {C['border']}; border-radius: 8px; background: {C['panel']}; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {C['muted']}; padding: 8px 16px; border: none;
               border-bottom: 2px solid transparent; }}
QTabBar::tab:selected {{ color: {C['text']}; border-bottom: 2px solid {C['accent']}; }}
QTabBar::tab:hover {{ color: {C['text']}; }}
QPushButton {{ background: {C['panel2']}; border: 1px solid {C['border']}; border-radius: 6px; padding: 6px 14px; }}
QPushButton:hover {{ border-color: {C['accent']}; }}
QPushButton:disabled {{ color: {C['muted']}; background: {C['panel']}; }}
QPushButton#primary {{ background: {C['accent']}; border: none; color: white; font-weight: 600; padding: 8px 22px; }}
QPushButton#primary:disabled {{ background: #34507f; }}
QPushButton#danger:hover {{ border-color: {C['bad']}; color: {C['bad']}; }}
QGroupBox {{ border: 1px solid {C['border']}; border-radius: 8px; margin-top: 14px; padding: 12px 10px 10px 10px;
             background: {C['panel2']}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {C['accent']}; font-weight: 600; }}
QLabel#muted {{ color: {C['muted']}; }}
QLabel#h1 {{ font-size: 17pt; font-weight: 700; }}
QLabel#field {{ background: {C['bg']}; border: 1px solid {C['border']}; border-radius: 4px; padding: 3px 6px; }}
QFrame#card {{ background: {C['panel2']}; border: 1px solid {C['border']}; border-radius: 8px; }}
QTableWidget {{ background: {C['panel']}; alternate-background-color: {C['panel2']}; border: none;
               gridline-color: transparent; selection-background-color: #2b3d5c; }}
QHeaderView::section {{ background: {C['panel2']}; color: {C['muted']}; border: none;
                        border-bottom: 1px solid {C['border']}; padding: 6px; }}
QProgressBar {{ background: {C['bg']}; border: 1px solid {C['border']}; border-radius: 4px; text-align: center;
               height: 16px; font-size: 8pt; }}
QProgressBar::chunk {{ background: {C['accent']}; border-radius: 3px; }}
QScrollArea {{ border: none; background: transparent; }}
QTextEdit {{ background: {C['bg']}; border: 1px solid {C['border']}; border-radius: 6px; }}
QStatusBar {{ background: {C['bg']}; color: {C['muted']}; }}
QCheckBox::indicator {{ width: 16px; height: 16px; }}
"""


def muted(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("muted")
    lbl.setWordWrap(True)
    return lbl


def field(text: str = "") -> QLabel:
    lbl = QLabel(text)
    lbl.setObjectName("field")
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lbl


class Bar(QProgressBar):
    """Полоска, которая краснеет при высокой нагрузке."""

    def __init__(self, warn=70, bad=90):
        super().__init__()
        self.setRange(0, 100)
        self.warn, self.bad = warn, bad
        self._color = None
        self.setFixedHeight(16)

    def set(self, value: float, text: str | None = None):
        self.setValue(int(value))
        self.setFormat(text if text is not None else f"{value:.0f}%")
        color = C["bad"] if value >= self.bad else C["warn"] if value >= self.warn else C["accent"]
        if color != self._color:
            self._color = color
            self.setStyleSheet(f"QProgressBar::chunk {{ background: {color}; border-radius: 3px; }}")


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


# ----------------------------------------------------------------- Обзор ---

LEVEL_ICON = {checks.OK: "✓", checks.INFO: "i", checks.WARN: "!", checks.BAD: "✕"}


class OverviewTab(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        self.headline = QLabel("Нажмите «Проверить», чтобы узнать, почему тормозит компьютер")
        self.headline.setObjectName("h1")
        self.headline.setWordWrap(True)
        self.sub = muted("Проверка ничего не меняет в системе. Любое изменение — только после вашего подтверждения, "
                         "и его можно откатить во вкладке «Журнал».")
        lay.addWidget(self.headline)
        lay.addWidget(self.sub)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list = QWidget()
        self.list.setStyleSheet("background: transparent;")
        self.list_lay = QVBoxLayout(self.list)
        self.list_lay.setContentsMargins(0, 8, 0, 0)
        self.list_lay.addStretch()
        self.scroll.setWidget(self.list)
        lay.addWidget(self.scroll, 1)

    def show_progress(self, text: str):
        self.headline.setText("Проверяю…")
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
        self.sub.setText(f"Проверка заняла {r.took:.1f} с · {time.strftime('%H:%M')}")
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
        h.setContentsMargins(14, 10, 12, 10)
        icon = QLabel(LEVEL_ICON[f.level])
        icon.setFixedSize(26, 26)
        icon.setAlignment(Qt.AlignCenter)
        icon.setStyleSheet(f"background: {color}; color: {C['bg']}; border-radius: 13px; font-weight: 800;")
        h.addWidget(icon, 0, Qt.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(3)
        title = QLabel(f.title)
        title.setWordWrap(True)
        title.setStyleSheet("font-weight: 600; font-size: 11pt;")
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
    """Сводка о железе в духе CPU-Z: поля + живые полоски загрузки."""

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
        self.grid.setContentsMargins(16, 8, 16, 16)
        self.grid.setHorizontalSpacing(14)
        scroll.setWidget(body)
        outer.addWidget(scroll)
        self.f: dict[str, QLabel] = {}

        cpu = QGroupBox("Процессор")
        g = QGridLayout(cpu)
        self._row(g, 0, "Модель", "cpu", span=3)
        self._row(g, 1, "Ядра", "cores")
        self._row(g, 1, "Потоки", "threads", col=2)
        self._row(g, 2, "Частота", "freq")
        self._row(g, 2, "Макс.", "freq_max", col=2)
        g.addWidget(muted("Загрузка"), 3, 0)
        self.cpu_bar = Bar(60, 85)
        g.addWidget(self.cpu_bar, 3, 1, 1, 3)
        self.core_box = QGridLayout()
        self.core_box.setSpacing(4)
        g.addLayout(self.core_box, 4, 0, 1, 4)
        self.core_bars: list[Bar] = []

        mem = QGroupBox("Память")
        g = QGridLayout(mem)
        self._row(g, 0, "Всего", "ram_total")
        g.addWidget(muted("ОЗУ"), 1, 0)
        self.ram_bar = Bar(70, 85)
        g.addWidget(self.ram_bar, 1, 1)
        g.addWidget(muted("Подкачка"), 2, 0)
        self.swap_bar = Bar(50, 80)
        g.addWidget(self.swap_bar, 2, 1)

        self.sys_box = QGroupBox("Система")
        g = QGridLayout(self.sys_box)
        self._row(g, 0, "ОС", "os")
        self._row(g, 1, "Компьютер", "host")
        self._row(g, 2, "Плата", "board")
        self._row(g, 3, "Работает", "uptime")

        self.gpu_box = QGroupBox("Видеокарта")
        self.gpu_lay = QGridLayout(self.gpu_box)
        self.gpu_lay.addWidget(muted("Загрузка…"), 0, 0)

        self.disk_box = QGroupBox("Диски")
        self.disk_lay = QGridLayout(self.disk_box)

        self.temp_box = QGroupBox("Температуры")
        self.temp_lay = QGridLayout(self.temp_box)

        self.grid.addWidget(cpu, 0, 0)
        self.grid.addWidget(mem, 1, 0)
        self.grid.addWidget(self.temp_box, 2, 0)
        self.grid.addWidget(self.sys_box, 0, 1)
        self.grid.addWidget(self.gpu_box, 1, 1)
        self.grid.addWidget(self.disk_box, 2, 1)
        self.grid.setRowStretch(3, 1)
        self.grid.setColumnStretch(0, 1)
        self.grid.setColumnStretch(1, 1)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        psutil.cpu_percent(percpu=True)

    def _row(self, g: QGridLayout, row: int, label: str, key: str, col: int = 0, span: int = 1):
        g.addWidget(muted(label), row, col)
        self.f[key] = field("…")
        g.addWidget(self.f[key], row, col + 1, 1, span)

    def activate(self):
        if not self.loaded:
            self.loaded = True
            run_async(lambda _p: sysinfo.static_info(), self.fill)
        self.timer.start(1000)
        self.tick()

    def deactivate(self):
        self.timer.stop()

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
            name.setStyleSheet("font-weight: 600;")
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

        self._clear(self.disk_lay)
        for i, d in enumerate(info["disks"]):
            self.disk_lay.addWidget(muted(d["mount"]), i, 0)
            b = Bar(80, 90)
            b.set(d["percent"], f"свободно {human_size(d['free'])} из {human_size(d['total'])}")
            self.disk_lay.addWidget(b, i, 1)
        self.disk_lay.setColumnStretch(1, 1)

    @staticmethod
    def _clear(lay: QGridLayout):
        while lay.count():
            w = lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def tick(self):
        per = psutil.cpu_percent(percpu=True)
        if len(self.core_bars) != len(per):
            for b in self.core_bars:
                b.deleteLater()
            self.core_bars = []
            cols = 4 if len(per) > 8 else 2
            for i in range(len(per)):
                b = Bar(80, 95)
                b.setFixedHeight(12)
                self.core_bars.append(b)
                self.core_box.addWidget(b, i // cols, i % cols)
        for i, (b, v) in enumerate(zip(self.core_bars, per)):
            b.set(v, f"{i}: {v:.0f}%")
        self.cpu_bar.set(sum(per) / max(len(per), 1))
        freq = psutil.cpu_freq()
        self.f["freq"].setText(f"{freq.current:.0f} МГц" if freq else "—")
        vm, sw = psutil.virtual_memory(), psutil.swap_memory()
        self.ram_bar.set(vm.percent, f"{human_size(vm.used)} / {human_size(vm.total)}  ({vm.percent:.0f}%)")
        self.swap_bar.set(sw.percent, f"{human_size(sw.used)} / {human_size(sw.total)}" if sw.total else "нет")
        self.f["uptime"].setText(human_duration(sysinfo.uptime()))

        temps = sysinfo.temperatures()
        self._clear(self.temp_lay)
        if not temps:
            self.temp_lay.addWidget(muted("Система не отдаёт датчики температуры"
                                          + (" (на Windows нужен драйвер датчиков)" if IS_WINDOWS else "")), 0, 0)
        for i, (name, t) in enumerate(sorted(temps.items())):
            self.temp_lay.addWidget(muted(name), i, 0)
            b = Bar(75, 85)
            b.set(t, f"{t:.0f} °C")
            self.temp_lay.addWidget(b, i, 1)
        self.temp_lay.setColumnStretch(1, 1)


# ------------------------------------------------------------ Автозагрузка ---

class StartupTab(QWidget):
    COLS = ["", "Программа", "Вердикт базы", "Откуда", "Команда"]

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.items: list[startup.StartupItem] = []
        self.shown: list[startup.StartupItem] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)

        top = QHBoxLayout()
        top.addWidget(muted("Что запускается вместе с системой. Вердикт — из общей базы WhySlow на GitHub."), 1)
        self.show_services = QCheckBox("Показывать системные службы")
        self.show_services.setVisible(IS_LINUX)
        self.show_services.toggled.connect(self.refill)
        top.addWidget(self.show_services)
        lay.addLayout(top)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Fixed)
        self.table.setColumnWidth(0, 28)
        for i in (1, 2, 3):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self.on_select)
        lay.addWidget(self.table, 1)

        self.detail = QLabel("Выберите программу, чтобы увидеть пояснение.")
        self.detail.setWordWrap(True)
        self.detail.setObjectName("card")
        self.detail.setStyleSheet(f"QLabel {{ background: {C['panel2']}; border: 1px solid {C['border']};"
                                  "border-radius: 8px; padding: 10px; }")
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.detail.setMinimumHeight(70)
        lay.addWidget(self.detail)

        btns = QHBoxLayout()
        self.b_off = QPushButton("Отключить")
        self.b_on = QPushButton("Включить")
        self.b_del = QPushButton("Удалить из автозагрузки")
        self.b_del.setObjectName("danger")
        self.b_report = QPushButton("Сообщить в базу…")
        self.b_off.clicked.connect(lambda: self.toggle(False))
        self.b_on.clicked.connect(lambda: self.toggle(True))
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
        self.refill()

    def visible_items(self):
        return [i for i in self.items if self.show_services.isChecked() or i.kind != "systemd"
                or not i.enabled or (i.verdict and i.verdict.verdict in ("bad", "safe"))]

    def refill(self):
        rows = self.visible_items()
        order = {"bad": 0, "safe": 1, "optional": 2, "unknown": 3, "keep": 4}
        rows.sort(key=lambda i: (not i.enabled, order.get(i.verdict.verdict if i.verdict else "unknown", 5), i.name.lower()))
        self.shown = rows
        self.table.setRowCount(len(rows))
        for r, it in enumerate(rows):
            dot = QTableWidgetItem("●" if it.enabled else "○")
            dot.setForeground(QColor(C["ok"] if it.enabled else C["muted"]))
            dot.setTextAlignment(Qt.AlignCenter)
            dot.setToolTip("Включено" if it.enabled else "Отключено")
            title = it.verdict.title if it.verdict else ""
            same = not title or title.lower() in it.name.lower() or it.name.lower() in title.lower()
            name = QTableWidgetItem(it.name if same else f"{it.name}  ·  {title}")
            if not it.enabled:
                name.setForeground(QColor(C["muted"]))
            v = it.verdict
            verdict = QTableWidgetItem(v.label if v else "—")
            verdict.setForeground(QColor(VERDICT_COLORS.get(v.verdict if v else "unknown")))
            f = QFont()
            f.setBold(True)
            verdict.setFont(f)
            for c, item in enumerate([dot, name, verdict, QTableWidgetItem(it.source), QTableWidgetItem(it.command)]):
                self.table.setItem(r, c, item)
        self.on_select()

    def current(self) -> startup.StartupItem | None:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return self.shown[rows[0].row()] if rows else None

    def on_select(self):
        it = self.current()
        for b in (self.b_off, self.b_on, self.b_del, self.b_report):
            b.setEnabled(it is not None)
        if not it:
            self.detail.setText("Выберите программу, чтобы увидеть пояснение.")
            return
        self.b_off.setEnabled(it.enabled)
        self.b_on.setEnabled(not it.enabled)
        self.b_del.setEnabled(it.can_delete)
        v = it.verdict
        color = VERDICT_COLORS.get(v.verdict, C["muted"])
        cat = f" · {v.category}" if v.category else ""
        admin = "<br><span style='color:#8f98a8'>Для изменения нужны права администратора.</span>" if it.needs_admin else ""
        self.detail.setText(f"<b>{it.name}</b>{cat} — <b style='color:{color}'>{v.label}</b><br>{v.note}"
                            f"<br><span style='color:#8f98a8'>{it.command}</span>{admin}")

    def _apply(self, fn, it, ok_text):
        try:
            fn()
        except startup.ActionError as e:
            QMessageBox.warning(self, "Не получилось", str(e))
            return
        self.main.status(ok_text)
        self.reload()

    def reload(self):
        run_async(lambda _p: startup.list_items(self.main.kb), self.set_items)
        self.main.journal_tab.reload()

    def toggle(self, enable: bool):
        it = self.current()
        if not it:
            return
        if not enable:
            v = it.verdict
            if v.verdict == "keep":
                if not confirm(self, "Точно отключить?",
                               f"База WhySlow советует НЕ трогать «{it.name}».\n\n{v.note}\n\n"
                               "Отключить всё равно? (включить обратно можно в любой момент)", danger=True):
                    return
            elif not confirm(self, "Отключить из автозагрузки?",
                             f"«{it.name}» перестанет запускаться при включении компьютера.\n"
                             "Сама программа останется установленной, её можно открыть вручную.\n\n"
                             "Включить обратно можно здесь же или во вкладке «Журнал»."):
                return
        self._apply(lambda: startup.set_enabled(it, enable), it,
                    f"«{it.name}» {'включено' if enable else 'отключено'}")

    def delete(self):
        it = self.current()
        if not it or not confirm(self, "Удалить из автозагрузки?",
                                 f"Запись «{it.name}» будет удалена из автозагрузки.\n"
                                 "Программа останется установленной. Резервная копия записи сохранится — "
                                 "её можно вернуть во вкладке «Журнал».", danger=it.verdict.verdict != "bad"):
            return
        self._apply(lambda: startup.delete(it), it, f"«{it.name}» удалено из автозагрузки")

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
        self.mon = ProcessMonitor()
        self.groups = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.addWidget(muted("Программы, сгруппированные по имени (браузер = десятки процессов). "
                            "Обновляется каждые 2 секунды. Нажмите на заголовок, чтобы отсортировать."))
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
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
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.mon.sample()

    def activate(self):
        self.timer.start(2000)
        QTimer.singleShot(300, self.refresh)

    def deactivate(self):
        self.timer.stop()

    def selected_name(self):
        rows = self.table.selectionModel().selectedRows()
        return self.table.item(rows[0].row(), 0).data(Qt.UserRole) if rows else None

    def refresh(self):
        keep = self.selected_name()
        self.groups = {g.name: g for g in self.mon.sample(self.main.kb)}
        top = sorted((g for g in self.groups.values() if g.mem), key=lambda g: g.mem, reverse=True)[:60]
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
        closed, failed = kill(g)
        msg = f"«{g.name}»: закрыто процессов — {closed}"
        if failed:
            msg += f", не удалось — {failed} (нужны права администратора)"
        self.main.status(msg)
        self.main.journal_tab.reload()
        self.refresh()


# ---------------------------------------------------------------- Очистка ---

class JunkTab(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.cats: list[junk.JunkCategory] = []
        self.boxes: list[QCheckBox] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        self.total = QLabel("Нажмите «Проверить» вверху, чтобы найти мусор")
        self.total.setObjectName("h1")
        lay.addWidget(self.total)
        lay.addWidget(muted("Удаляются только ваши файлы старше суток (корзина и миниатюры — целиком). "
                            "Файлы, которые сейчас заняты программами, пропускаются."))
        self.box = QGroupBox("Что можно удалить")
        self.box_lay = QGridLayout(self.box)
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
        lay.setContentsMargins(16, 16, 16, 16)
        lay.addWidget(muted("Всё, что WhySlow поменял в системе. Отключение автозагрузки, удаление записей и "
                            "выключение прокси можно откатить."))
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Когда", "Что сделано", "Статус"])
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
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
        self.resize(1100, 740)
        self.kb = knowledge.load()
        self.scan_result: checks.ScanResult | None = None

        root = QWidget()
        root.setObjectName("root")
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(16, 12, 16, 8)

        header = QHBoxLayout()
        logo = QLabel("WhySlow")
        logo.setStyleSheet(f"font-size: 20pt; font-weight: 800; color: {C['accent']};")
        header.addWidget(logo)
        header.addWidget(muted("  объясняет, почему тормозит, и помогает это исправить"), 1)
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
        b_db.setStyleSheet("padding: 2px 10px;")
        b_db.clicked.connect(lambda: self.update_db(manual=True))
        sb.addPermanentWidget(b_db)
        if IS_WINDOWS and not is_admin():
            b_admin = QPushButton("Перезапустить от администратора")
            b_admin.setStyleSheet("padding: 2px 10px;")
            b_admin.clicked.connect(self.restart_as_admin)
            sb.addPermanentWidget(b_admin)
        self.show_db()
        QTimer.singleShot(200, self.update_db)
        QTimer.singleShot(400, self.scan)

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
        for t in (self.system_tab, self.procs_tab):
            t.activate() if t is cur else t.deactivate()

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
    app.setStyleSheet(STYLE)
    w = MainWindow()
    w.show()
    return app.exec()
