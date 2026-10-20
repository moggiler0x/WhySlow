"""Графический интерфейс WhySlow (PySide6)."""

from __future__ import annotations

import math
import os
import sys
import threading
import time
import traceback
import webbrowser
from collections import deque
from html import escape as _esc
from pathlib import Path

import psutil
from PySide6.QtCore import QObject, QPointF, QRectF, QRunnable, QSize, Qt, QThread, QThreadPool, QTimer, Signal
from PySide6.QtGui import (
    QBrush, QColor, QConicalGradient, QFont, QFontMetrics, QIcon, QImage, QLinearGradient, QPainter, QPainterPath, QPalette, QPen,
    QPixmap, QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractItemView, QApplication, QCheckBox, QDialog, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QComboBox, QFileDialog, QHeaderView, QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton, QScrollArea,
    QSizePolicy, QTabBar, QTableWidget, QTableWidgetItem, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)

from . import (
    __version__, checks, journal, junk, knowledge, lhm, macapps, macstorage, network, online, optimize, passwords, settings, startup, sysinfo, updater,
)
from .i18n import LANGS, lang, set_lang, tr
from .processes import ProcessMonitor, kill
from .util import ADMIN_HINT, IS_LINUX, IS_MAC, IS_WINDOWS, by_os, human_duration, human_size, is_admin

# ------------------------------------------------------------------ стиль ---

C = {
    "bg": "#07060d", "bg2": "#0d0a1c", "panel": "#0c0b16", "panel2": "#15131f", "border": "#2a2640",
    "text": "#f5f3ff", "muted": "#a19cb8", "accent": "#8b5cf6", "accent2": "#c4b5fd", "pink": "#e77edc",
    "ok": "#2ee59d", "info": "#4cc9f0", "warn": "#ffb547", "bad": "#ff4d6d",
}
VERDICT_COLORS = {"safe": C["ok"], "optional": C["warn"], "keep": C["info"], "bad": C["bad"], "unknown": C["muted"]}

# Прозрачные слои поверх живого фона — «стекло», как в образце дизайна
GLASS = "rgba(255, 255, 255, 9)"
GLASS2 = "rgba(255, 255, 255, 16)"
LINE = "rgba(255, 255, 255, 22)"
LINE2 = "rgba(255, 255, 255, 60)"
SERIF = "Georgia, 'Times New Roman', 'DejaVu Serif', serif"

# На macOS пункт шрифта = пиксель, на Windows и Linux — 1,33 пикселя. Чтобы текст везде
# был одинаково крупным, все размеры задаются в «условных пунктах» и пересчитываются здесь.
SCALE = 1.5 if IS_MAC else 1.15


def pt(size: float) -> str:
    return f"{size * SCALE:.1f}pt"


def px(size: float) -> int:
    """Отступы и высоты растут вместе со шрифтом, но слабее."""
    return round(size * (1 + (SCALE - 1) * 0.6) if not IS_MAC else size * 1.15)


GRAD = f"qlineargradient(x1:0, y1:0, x2:1, y2:0, stop:0 {C['accent']}, stop:1 {C['accent2']})"


def dark_palette() -> QPalette:
    """Тёмная палитра для того, что таблица стилей не задаёт: диалоги, меню, поля ввода."""
    pal = QPalette()
    for role, color in [(QPalette.Window, C["panel"]), (QPalette.WindowText, C["text"]), (QPalette.Base, C["bg"]),
                        (QPalette.AlternateBase, C["panel2"]), (QPalette.Text, C["text"]), (QPalette.Button, C["panel2"]),
                        (QPalette.ButtonText, C["text"]), (QPalette.Highlight, C["accent"]),
                        (QPalette.HighlightedText, "#ffffff"), (QPalette.ToolTipBase, C["panel2"]),
                        (QPalette.ToolTipText, C["text"]), (QPalette.PlaceholderText, C["muted"]), (QPalette.Link, C["accent2"])]:
        pal.setColor(role, QColor(color))
    pal.setColor(QPalette.Disabled, QPalette.Text, QColor(C["muted"]))
    pal.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(C["muted"]))
    return pal


def build_style() -> str:
    """Таблица стилей. Строится после выбора шрифта: «пилюлям» нужен радиус ровно в полвысоты,
    а Qt молча рисует прямые углы, если радиус хоть на пиксель больше."""
    line = QFontMetrics(QApplication.font()).height()

    def pill(pad: int, text: float = 1.0, border: int = 2) -> int:
        return (round(line * text) + 2 * pad + border) // 2 - 1

    btn, prim, small, combo = pill(px(7)), pill(px(8), border=0), pill(3), pill(px(6))
    badge, tab = pill(px(3), 0.85), pill(px(7), border=0)
    nav = (round(line) + 2 * px(7) + 2 * px(3) + 2) // 2 - 1
    return f"""
* {{ color: {C['text']}; font-size: {pt(10)}; }}
QMainWindow {{ background: {C['bg']}; }}
QToolTip {{ background: {C['panel2']}; color: {C['text']}; border: 1px solid {C['border']}; padding: 4px; }}
QTabWidget::pane {{ border: 1px solid {LINE}; border-radius: 20px; background: rgba(9, 8, 18, 215); }}
QFrame#nav {{ background: {GLASS}; border: 1px solid {LINE}; border-radius: {nav}px; }}
QTabBar {{ background: transparent; }}
QTabBar::tab {{ background: transparent; color: rgba(255, 255, 255, 190); padding: {px(7)}px {px(10)}px;
               margin: {px(3)}px 1px; border: none; border-radius: {tab}px; font-size: {pt(10)}; }}
QTabBar::tab:selected {{ color: white; background: rgba(255, 255, 255, 28); }}
QTabBar::tab:hover:!selected {{ color: white; background: rgba(255, 255, 255, 14); }}
QPushButton {{ background: transparent; border: 1px solid rgba(255, 255, 255, 77); border-radius: {btn}px;
              padding: {px(7)}px {px(18)}px; }}
QPushButton:hover {{ background: rgba(255, 255, 255, 26); border-color: rgba(255, 255, 255, 128); }}
QPushButton:disabled {{ color: {C['muted']}; border-color: {LINE}; }}
QPushButton#primary {{ background: white; border: none; color: #09090b; font-weight: 600;
                      padding: {px(8)}px {px(24)}px; border-radius: {prim}px; }}
QPushButton#primary:hover {{ background: rgba(255, 255, 255, 225); }}
QPushButton#primary:disabled {{ background: rgba(255, 255, 255, 110); color: #27272a; }}
QPushButton#danger:hover {{ border-color: {C['bad']}; color: {C['bad']}; }}
QPushButton#link {{ background: transparent; border: none; color: {C['accent2']}; padding: 2px 4px; font-weight: 600; }}
QPushButton#link:hover {{ color: white; }}
QGroupBox {{ border: 1px solid {LINE}; border-radius: 16px; margin-top: {px(18)}px;
             padding: {px(16)}px {px(12)}px {px(12)}px {px(12)}px; background: {GLASS}; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; color: {C['accent2']};
                    font-weight: 600; font-size: {pt(10.5)}; }}
QLabel#muted {{ color: {C['muted']}; }}
QLabel#h1 {{ font-size: {pt(19)}; font-weight: 300; }}
QLabel#hero {{ font-size: {pt(25)}; font-weight: 300; color: white; }}
QLabel#heroSub {{ color: rgba(255, 255, 255, 180); font-weight: 300; }}
QLabel#badge {{ background: rgba(255, 255, 255, 13); border: 1px solid rgba(255, 255, 255, 24);
               border-top-color: {LINE2}; border-radius: {badge}px; padding: {px(3)}px {px(12)}px;
               color: rgba(255, 255, 255, 230); font-size: {pt(8.5)}; font-weight: 300; }}
QLabel#section {{ color: {C['muted']}; font-size: {pt(9)}; font-weight: 600; letter-spacing: 1px; }}
QLabel#big {{ font-size: {pt(22)}; font-weight: 300; }}
QLabel#field {{ background: rgba(0, 0, 0, 90); border: 1px solid {LINE}; border-radius: 8px; padding: {px(4)}px {px(8)}px; }}
QFrame#card {{ background: {GLASS}; border: 1px solid {LINE}; border-radius: 16px; }}
QFrame#tile {{ background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 rgba(139, 92, 246, 34), stop:1 {GLASS});
              border: 1px solid {LINE}; border-top-color: rgba(255, 255, 255, 40); border-radius: 18px; }}
QFrame#tile:hover {{ border-color: {C['accent']}; }}
QTableWidget {{ background: transparent; alternate-background-color: rgba(255, 255, 255, 7); border: none;
               gridline-color: transparent; selection-background-color: rgba(139, 92, 246, 80); selection-color: white; }}
QHeaderView {{ background: rgba(255, 255, 255, 10); border: none; }}
QHeaderView::section {{ background: transparent; color: {C['muted']}; border: none; font-weight: 600;
                        border-bottom: 1px solid {LINE}; padding: {px(8)}px; }}
QTableCornerButton::section {{ background: transparent; border: none; }}
QProgressBar {{ background: rgba(255, 255, 255, 10); border: 1px solid {LINE}; border-radius: 8px; text-align: center;
               font-size: {pt(8.5)}; font-weight: 600; }}
QProgressBar::chunk {{ background: {GRAD}; border-radius: 7px; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollArea > QWidget > QWidget {{ background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: rgba(255, 255, 255, 30); border-radius: 4px; min-height: 30px; }}
QScrollBar::handle:vertical:hover {{ background: {C['accent']}; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}
QTextEdit {{ background: rgba(0, 0, 0, 90); border: 1px solid {LINE}; border-radius: 10px; }}
QStatusBar {{ background: transparent; color: {C['muted']}; }}
QStatusBar::item {{ border: none; }}
QStatusBar QPushButton {{ padding: 3px 14px; border-radius: {small}px; }}
QStatusBar QPushButton#update {{ background: white; border: none; color: #09090b; font-weight: 600; }}
QCheckBox::indicator {{ width: {px(18)}px; height: {px(18)}px; border: 1px solid {LINE2}; border-radius: 5px;
                       background: {GLASS}; }}
QCheckBox::indicator:hover {{ border-color: {C['accent2']}; }}
QCheckBox::indicator:checked {{ background: {GRAD}; border-color: {C['accent']}; }}
QTableView::indicator {{ width: {px(16)}px; height: {px(16)}px; border: 1px solid {LINE2}; border-radius: 4px;
                        background: {GLASS}; }}
QTableView::indicator:checked {{ background: {GRAD}; border-color: {C['accent']}; }}
QMessageBox, QDialog {{ background: {C['panel']}; }}
QMessageBox QLabel {{ font-size: {pt(10)}; }}
QComboBox {{ background: {GLASS}; border: 1px solid {LINE}; border-radius: {combo}px; color: {C['text']};
            padding: {px(6)}px {px(14)}px; }}
QComboBox:hover {{ border-color: rgba(255, 255, 255, 110); }}
QComboBox::drop-down {{ border: none; width: {px(22)}px; }}
QComboBox QAbstractItemView {{ background: {C['panel2']}; color: {C['text']}; border: 1px solid {C['border']};
                             selection-background-color: rgba(139, 92, 246, 90); selection-color: white; outline: none; }}
"""


def flag_icon(code: str) -> QIcon:
    """Флажок языка, нарисованный кодом: эмодзи-флаги Windows не показывает."""
    w, h, k = 30, 20, 3  # рисуем крупнее и сжимаем — края получаются ровными
    pm = QPixmap(w * k, h * k)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.scale(k, k)
    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, w, h), 3, 3)
    p.setClipPath(clip)
    if code == "ru":
        for i, color in enumerate(("#ffffff", "#0039a6", "#d52b1e")):
            p.fillRect(QRectF(0, i * h / 3, w, h / 3 + 0.5), QColor(color))
    else:  # британский флаг
        p.fillRect(QRectF(0, 0, w, h), QColor("#012169"))
        for color, width in (("#ffffff", 4.0), ("#c8102e", 1.6)):
            p.setPen(QPen(QColor(color), width))
            p.drawLine(QPointF(0, 0), QPointF(w, h))
            p.drawLine(QPointF(0, h), QPointF(w, 0))
        p.setPen(Qt.NoPen)
        p.fillRect(QRectF(w / 2 - 3.3, 0, 6.6, h), QColor("#ffffff"))
        p.fillRect(QRectF(0, h / 2 - 3.3, w, 6.6), QColor("#ffffff"))
        p.fillRect(QRectF(w / 2 - 2, 0, 4, h), QColor("#c8102e"))
        p.fillRect(QRectF(0, h / 2 - 2, w, 4), QColor("#c8102e"))
    p.setClipping(False)
    p.setPen(QPen(QColor(0, 0, 0, 60), 0.6))
    p.setBrush(Qt.NoBrush)
    p.drawRoundedRect(QRectF(0.3, 0.3, w - 0.6, h - 0.6), 3, 3)
    p.end()
    return QIcon(pm)


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
    return human_size(n) + tr("/с", "/s")


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


def nice_ticks(peak: float, n: int = 4, binary: bool = False) -> list[float]:
    """Круглые деления шкалы от нуля до чуть выше peak: 0, 250, 500, 750, 1000.
    binary=True — для байтов: деления круглые в КБ/МБ/ГБ (0, 2, 4, 6 МБ)."""
    if binary and peak >= 1024:
        unit = 1024 ** min(int(math.log(peak, 1024)), 4)
        return [t * unit for t in nice_ticks(peak / unit, n)]
    raw = max(peak, 1e-9) / n
    mag = 10 ** math.floor(math.log10(raw))
    step = next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw)
    return [step * i for i in range(math.ceil(peak / step - 1e-9) + 1)] if peak > 0 else [0.0, 1.0]


def pct(v: float) -> str:
    return f"{v:.0f}%"


def rate_short(v: float) -> str:
    """Подпись деления для скорости: «512 КБ/с», без лишних «.0»."""
    return rate(v).replace(".0 ", " ") if v else "0"


class Sparkline(QWidget):
    """График за последнюю минуту, как в Диспетчере задач. Несколько линий — несколько цветов.

    axis=True — с подписями: слева шкала значений, снизу — секунды назад. При наведении
    мыши показывает точные значения в этот момент."""

    def __init__(self, colors: list[str], maximum: float | None = 100, height: int = 70,
                 axis: bool = False, fmt=pct, names: list[str] | None = None):
        super().__init__()
        self.colors = [QColor(c) for c in colors]
        self.maximum = maximum
        self.series: list[list[float]] = [[] for _ in colors]
        self.n = 60
        self.axis = axis
        self.fmt = fmt
        self.names = names or []
        self.hover: int | None = None
        self.setMinimumHeight(px(height))
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(axis)

    def set_series(self, *series):
        self.series = [list(s) for s in series]
        self.update()

    def _plot_rect(self, r: QRectF, fm: QFontMetrics, ticks: list[float]) -> QRectF:
        if not self.axis:
            return r.adjusted(2, 6, -2, -2)
        left = max(fm.horizontalAdvance(self.fmt(t)) for t in ticks) + px(10)
        return r.adjusted(left, fm.height() * 0.7, -px(8), -fm.height() - px(6))

    def mouseMoveEvent(self, e):
        r = self._last_inner if hasattr(self, "_last_inner") else None
        if r is None or not r.width():
            return
        i = round((e.position().x() - r.left()) / r.width() * (self.n - 1))
        self.hover = min(max(i, 0), self.n - 1)
        self.update()

    def leaveEvent(self, _e):
        self.hover = None
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(C["border"]), 1))
        p.setBrush(QColor(C["bg"]))
        p.drawRoundedRect(r, 8, 8)
        peak = max([max(s) for s in self.series if s] + [0])
        if self.maximum:
            ticks = nice_ticks(self.maximum)
        else:  # скорости: шкала не мельче 1 КБ/с, чтобы тишина не выглядела как нагрузка
            ticks = nice_ticks(max(peak * 1.1, 1024), binary=True)
        top = ticks[-1]
        font = QFont(self.font())
        font.setPointSizeF(font.pointSizeF() * 0.8)
        p.setFont(font)
        fm = QFontMetrics(font)
        inner = self._plot_rect(r, fm, ticks)
        self._last_inner = inner
        grid = QPen(QColor(255, 255, 255, 18), 1)
        if self.axis:
            for t in ticks:
                y = inner.bottom() - inner.height() * t / top
                p.setPen(grid)
                p.drawLine(QPointF(inner.left(), y), QPointF(inner.right(), y))
                p.setPen(QColor(C["muted"]))
                p.drawText(QRectF(r.left(), y - fm.height() / 2, inner.left() - r.left() - px(5), fm.height()),
                           Qt.AlignRight | Qt.AlignVCenter, self.fmt(t))
            for k in range(5):  # каждые 15 секунд
                x = inner.left() + inner.width() * k / 4
                p.setPen(grid)
                p.drawLine(QPointF(x, inner.top()), QPointF(x, inner.bottom()))
                label = tr("сейчас", "now") if k == 4 else tr(f"{60 - k * 15} с", f"{60 - k * 15}s")
                w = fm.horizontalAdvance(label)
                p.setPen(QColor(C["muted"]))
                p.drawText(QPointF(min(max(x - w / 2, inner.left()), inner.right() - w),
                                   inner.bottom() + fm.ascent() + px(4)), label)
        else:
            p.setPen(grid)
            for k in (1, 2, 3):
                y = r.top() + r.height() * k / 4
                p.drawLine(QPointF(r.left() + 4, y), QPointF(r.right() - 4, y))
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
        if self.axis and self.hover is not None:
            self._draw_hover(p, inner, fm, step)
        p.end()

    def _draw_hover(self, p: QPainter, inner: QRectF, fm: QFontMetrics, step: float):
        back = self.n - 1 - self.hover  # секунд назад
        lines = []
        for k, (color, values) in enumerate(zip(self.colors, self.series)):
            if back < len(values):
                name = self.names[k] + ": " if k < len(self.names) else ""
                lines.append((color, name + self.fmt(values[-1 - back])))
        if not lines:
            return
        x = inner.left() + self.hover * step
        p.setPen(QPen(QColor(255, 255, 255, 90), 1, Qt.DashLine))
        p.drawLine(QPointF(x, inner.top()), QPointF(x, inner.bottom()))
        title = tr("сейчас", "now") if back == 0 else tr(f"{back} с назад", f"{back}s ago")
        w = max(fm.horizontalAdvance(t) for t in [title] + [t for _, t in lines]) + px(22)
        h = fm.height() * (len(lines) + 1) + px(8)
        bx = x + px(8) if x + px(8) + w < inner.right() else x - px(8) - w
        box = QRectF(bx, inner.top() + px(2), w, h)
        p.setPen(QPen(QColor(C["border"]), 1))
        p.setBrush(QColor(C["panel2"]))
        p.drawRoundedRect(box, 6, 6)
        y = box.top() + px(4) + fm.ascent()
        p.setPen(QColor(C["muted"]))
        p.drawText(QPointF(box.left() + px(8), y), title)
        for color, text in lines:
            y += fm.height()
            p.setPen(Qt.NoPen)
            p.setBrush(color)
            p.drawEllipse(QPointF(box.left() + px(11), y - fm.ascent() / 2 + 1), 3.5, 3.5)
            p.setPen(QColor(C["text"]))
            p.drawText(QPointF(box.left() + px(18), y), text)


class CoreGrid(QWidget):
    """Загрузка каждого ядра: плитка с номером, типом ядра, процентом и графиком за минуту."""

    KIND = {"P": tr("производительное", "performance"), "E": tr("энергоэффективное", "efficiency")}

    def __init__(self):
        super().__init__()
        self.hist: list[deque] = []
        self.kinds: list[str] = []
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_kinds(self, kinds: list[str]):
        self.kinds = kinds
        self.update()

    def set_values(self, per: list[float]):
        if len(self.hist) != len(per):
            self.hist = [deque(maxlen=60) for _ in per]
            self.updateGeometry()
        for h, v in zip(self.hist, per):
            h.append(v)
        if self.height() != self._height():
            self.setFixedHeight(self._height())
        self.update()

    def _cols(self) -> int:
        n = len(self.hist)
        return 4 if n <= 8 else 6 if n <= 24 else 8

    def _cell_h(self) -> int:
        return QFontMetrics(self.font()).height() + px(46)

    def _height(self) -> int:
        rows = math.ceil(len(self.hist) / self._cols()) if self.hist else 0
        return rows * (self._cell_h() + px(6))

    def paintEvent(self, _e):
        if not self.hist:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        cols, gap = self._cols(), px(6)
        w = (self.width() - gap * (cols - 1)) / cols
        h = self._cell_h()
        fm = QFontMetrics(self.font())
        bold = QFont(self.font())
        bold.setBold(True)
        for i, hist in enumerate(self.hist):
            v = hist[-1] if hist else 0
            color = QColor(C["bad"] if v >= 95 else C["warn"] if v >= 80 else C["accent2"])
            cell = QRectF((i % cols) * (w + gap), (i // cols) * (h + gap), w, h).adjusted(0.5, 0.5, -0.5, -0.5)
            p.setPen(QPen(QColor(C["border"]), 1))
            p.setBrush(QColor(C["bg"]))
            p.drawRoundedRect(cell, 8, 8)
            # заголовок: «Ядро 3 · P» слева, «56%» справа
            kind = self.kinds[i] if i < len(self.kinds) else ""
            p.setFont(self.font())
            p.setPen(QColor(C["muted"]))
            p.drawText(QPointF(cell.left() + px(8), cell.top() + px(4) + fm.ascent()),
                       tr(f"Ядро {i + 1}", f"Core {i + 1}") + (f" · {kind}" if kind else ""))
            p.setFont(bold)
            p.setPen(color)
            txt = f"{v:.0f}%"
            p.drawText(QPointF(cell.right() - px(8) - QFontMetrics(bold).horizontalAdvance(txt),
                               cell.top() + px(4) + fm.ascent()), txt)
            # мини-график
            g = QRectF(cell.left() + px(6), cell.top() + fm.height() + px(8), cell.width() - px(12),
                       cell.height() - fm.height() - px(14))
            p.setPen(QPen(QColor(255, 255, 255, 14), 1))
            p.drawLine(QPointF(g.left(), g.center().y()), QPointF(g.right(), g.center().y()))
            vals = list(hist)
            if len(vals) >= 2:
                step = g.width() / 59
                x0 = g.right() - step * (len(vals) - 1)
                path = QPainterPath(QPointF(x0, g.bottom()))
                for k, val in enumerate(vals):
                    path.lineTo(x0 + k * step, g.bottom() - g.height() * min(val, 100) / 100)
                path.lineTo(g.right(), g.bottom())
                path.closeSubpath()
                fill = QColor(color)
                fill.setAlpha(90)
                p.setPen(QPen(color, 1.5))
                p.setBrush(fill)
                p.drawPath(path)
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
    QMessageBox.critical(QApplication.activeWindow(), tr("Ошибка", "Error"), msg)


def confirm(parent, title: str, text: str, danger: bool = False) -> bool:
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Warning if danger else QMessageBox.Question)
    box.setWindowTitle(title)
    box.setText(text)
    box.setStandardButtons(QMessageBox.Yes | QMessageBox.Cancel)
    box.button(QMessageBox.Yes).setText(tr("Да, сделать", "Yes, do it"))
    box.button(QMessageBox.Cancel).setText(tr("Отмена", "Cancel"))
    box.setDefaultButton(QMessageBox.Cancel if danger else QMessageBox.Yes)
    return box.exec() == QMessageBox.Yes


_asking: set[str] = set()


def online_note(name: str, path: str, refresh) -> str:
    """Пояснение из интернета для программы, которой нет в базе.
    Если в кеше пусто — ищет в фоне и потом зовёт refresh(), чтобы окно перерисовалось."""
    hit, info = online.cached(name)
    if hit:
        return online.as_html(info, C["muted"])
    if name not in _asking:
        _asking.add(name)
        run_async(lambda _p: online.describe(name, path),
                  lambda _r: (_asking.discard(name), refresh()),
                  lambda _e: _asking.discard(name))
    return f"<span style='color:{C['muted']}'>" + tr("Ищу описание в интернете…", "Looking up a description online…") + "</span>"


def report_online(name: str, command: str):
    """Открывает готовый issue; найденное в интернете описание подставляется черновиком."""
    _hit, info = online.cached(name)
    webbrowser.open(knowledge.report_url(name, command, info.text if info else "", info.source if info else ""))


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
        temps, disks, gpu = {}, [], None
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
                if tick % 2 == 0:
                    gpu = sysinfo.gpu_load()
                freq = psutil.cpu_freq()
                sw = psutil.swap_memory()
                self.stats.emit({
                    "cpu": sum(per) / max(len(per), 1), "per": per, "freq": freq.current if freq else None,
                    "ram": sysinfo.memory(), "swap": (sw.used, sw.total, sw.percent),
                    "disk_r": max(disk1[0] - disk0[0], 0) / dt, "disk_w": max(disk1[1] - disk0[1], 0) / dt,
                    "net_rx": max(net1[0] - net0[0], 0) / dt, "net_tx": max(net1[1] - net0[1], 0) / dt,
                    "uptime": sysinfo.uptime(), "temps": temps, "disks": disks,
                    "gpu": gpu, "gpu_ready": tick >= 4,
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


# ------------------------------------------------------------ живой фон ---

# Пятна «сетчатого градиента»: цвет, яркость, центр (доли ширины и высоты), размах движения,
# скорости, фаза и радиус (доля большей стороны)
HERO_BLOBS = [
    (C["accent"], 210, 0.78, 0.30, 0.16, 0.35, 0.31, 0.23, 0.0, 0.42),
    ("#4c1d95", 230, 0.30, 0.85, 0.22, 0.30, 0.19, 0.27, 1.7, 0.50),
    ("#1e1b4b", 255, 0.10, 0.20, 0.10, 0.25, 0.23, 0.17, 3.1, 0.45),
    ("#ffffff", 70, 0.88, 0.75, 0.08, 0.20, 0.37, 0.29, 4.4, 0.20),
    (C["accent"], 120, 0.50, 0.10, 0.25, 0.20, 0.13, 0.21, 2.2, 0.30),
]
WINDOW_BLOBS = [
    (C["accent"], 70, 0.92, 0.02, 0, 0, 0, 0, 0, 0.45),
    ("#4c1d95", 110, 0.05, 1.00, 0, 0, 0, 0, 0, 0.55),
    ("#1e1b4b", 160, 0.40, 0.45, 0, 0, 0, 0, 0, 0.60),
]


def mesh_image(w: int, h: int, t: float, blobs) -> QImage:
    """Размытые пятна на чёрном. Рисуется в малом разрешении и растягивается — это почти бесплатно."""
    img = QImage(max(w, 2), max(h, 2), QImage.Format_RGB32)
    img.fill(QColor("#000000"))
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    p.setCompositionMode(QPainter.CompositionMode_Screen)
    p.setPen(Qt.NoPen)
    side = max(w, h)
    for color, alpha, cx, cy, ax, ay, sx, sy, ph, r in blobs:
        x = (cx + ax * math.sin(t * sx + ph)) * w
        y = (cy + ay * math.cos(t * sy + ph * 1.3)) * h
        g = QRadialGradient(QPointF(x, y), r * side)
        c0, c1 = QColor(color), QColor(color)
        c0.setAlpha(alpha)
        c1.setAlpha(0)
        g.setColorAt(0, c0)
        g.setColorAt(1, c1)
        p.setBrush(g)
        p.drawRect(0, 0, w, h)
    p.end()
    return img


class MeshBackground(QWidget):
    """Неподвижный фиолетовый градиент под всем окном; перерисовывается только при смене размера."""

    def __init__(self):
        super().__init__()
        self._cache: QPixmap | None = None

    def resizeEvent(self, e):
        self._cache = None
        super().resizeEvent(e)

    def paintEvent(self, _e):
        if self._cache is None or self._cache.size() != self.size():
            img = mesh_image(self.width() // 8, self.height() // 8, 0, WINDOW_BLOBS)
            self._cache = QPixmap.fromImage(img.scaled(self.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation))
        QPainter(self).drawPixmap(0, 0, self._cache)


def accent(text: str, color: str = "white") -> str:
    """Первое слово — курсивом с засечками, как заголовок в образце дизайна."""
    first, _, rest = text.partition(" ")
    return (f"<span style=\"font-family: {SERIF}; font-style: italic; font-weight: 500; color: {color};\">"
            f"{_esc(first)}</span> {_esc(rest)}")


class PulseRing(QWidget):
    """Переливающееся кольцо с бегущей по кругу надписью; в центре — итог проверки."""

    COLORS = ["#beecff", "#e77edc", "#ff4c3e", "#00ff88", "#ffd700", "#ff6b35", "#8a2be2"]

    def __init__(self):
        super().__init__()
        self.setFixedSize(px(124), px(124))
        self.t = 0.0
        self.glyph, self.color = "…", C["muted"]
        self._ring: QPixmap | None = None
        self.text = tr("почему тормозит • WhySlow • ", "why so slow • WhySlow • ")

    def set_state(self, glyph: str, color: str):
        self.glyph, self.color = glyph, color
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(self.width() / 2, self.height() / 2)
        side = min(self.width(), self.height())
        ro = side * 0.25 * (1 + 0.05 * math.sin(self.t * 2.4))
        glow = QRadialGradient(c, ro * 1.9)
        g0 = QColor(self.color if self.glyph != "…" else C["accent"])
        g0.setAlpha(90)
        glow.setColorAt(0.45, g0)
        glow.setColorAt(1, QColor(0, 0, 0, 0))
        p.setPen(Qt.NoPen)
        p.setBrush(glow)
        p.drawEllipse(c, ro * 1.9, ro * 1.9)
        cone = QConicalGradient(c, -self.t * 50)
        for i, col in enumerate(self.COLORS + self.COLORS[:1]):
            cone.setColorAt(i / len(self.COLORS), QColor(col))
        p.setBrush(QColor(0, 0, 0, 220))
        p.setPen(QPen(QBrush(cone), side * 0.045))
        p.drawEllipse(c, ro, ro)
        f = QFont(self.font())
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() * 1.5)
        p.setFont(f)
        p.setPen(QColor(self.color))
        p.drawText(QRectF(c.x() - ro, c.y() - ro, ro * 2, ro * 2), Qt.AlignCenter, self.glyph)

        # Надпись по кругу рисуется один раз, а потом только поворачивается
        if self._ring is None or self._ring.size() != self.size():
            self._ring = self._draw_ring_text()
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        p.translate(c)
        p.rotate(self.t * 18)
        p.drawPixmap(-self.width() // 2, -self.height() // 2, self._ring)

    def _draw_ring_text(self) -> QPixmap:
        """Повторяем фразу, пока она не обойдёт кольцо, и разносим буквы равномерно."""
        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        f = QFont(SERIF.split(",")[0])
        f.setItalic(True)
        f.setPointSizeF(self.font().pointSizeF() * 0.78)
        p.setFont(f)
        fm = QFontMetrics(f)
        rt = min(self.width(), self.height()) * 0.44
        text = self.text
        while fm.horizontalAdvance(text + self.text) < 2 * math.pi * rt:
            text += self.text
        total = fm.horizontalAdvance(text)
        angle = 0.0
        p.setPen(QColor(255, 255, 255, 200))
        p.translate(self.width() / 2, self.height() / 2)
        for ch in text:
            adv = fm.horizontalAdvance(ch)
            step = 360 * adv / total
            p.save()
            p.rotate(angle + step / 2)
            p.drawText(QPointF(-adv / 2, -rt + fm.ascent() / 2), ch)
            p.restore()
            angle += step
        p.end()
        return pm


class Hero(QFrame):
    """Приветственный блок обзора: живой градиент, плашка, крупный заголовок, кнопки и кольцо."""

    FPS = 10  # кольцо; градиент плывёт медленно, ему хватает каждого третьего кадра
    AWAKE = 20  # сколько секунд градиент плывёт после открытия вкладки или проверки

    def __init__(self):
        super().__init__()
        self.t = 0.0  # часы кольца
        self.tm = 0.0  # часы градиента: идут, только пока он «проснулся»
        self._frame = 0
        self._awake_until = 0.0
        self._bg: QPixmap | None = None
        self.calm = False  # компьютер и так загружен — не добавляем ему работы
        self.timer = QTimer(self)
        self.timer.setInterval(1000 // self.FPS)
        self.timer.timeout.connect(self._tick)
        h = QHBoxLayout(self)
        h.setContentsMargins(px(28), px(14), px(22), px(14))
        h.setSpacing(px(20))
        v = QVBoxLayout()
        v.setSpacing(px(8))
        self.badge = QLabel()
        self.badge.setObjectName("badge")
        v.addWidget(self.badge, 0, Qt.AlignLeft)
        self.title = QLabel()
        self.title.setObjectName("hero")
        self.title.setWordWrap(True)
        v.addWidget(self.title)
        self.sub = QLabel()
        self.sub.setObjectName("heroSub")
        self.sub.setWordWrap(True)
        v.addWidget(self.sub)
        self.buttons = QHBoxLayout()
        self.buttons.setSpacing(px(12))
        self.buttons.setContentsMargins(0, px(4), 0, 0)
        v.addLayout(self.buttons)
        h.addLayout(v, 1)
        self.ring = PulseRing()
        h.addWidget(self.ring, 0, Qt.AlignVCenter)

    def add_button(self, text: str, on_click, primary=False) -> QPushButton:
        b = QPushButton(text)
        if primary:
            b.setObjectName("primary")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(on_click)
        self.buttons.addWidget(b)
        return b

    def set_text(self, badge: str, title: str, sub: str, color: str = "white"):
        self.badge.setText(badge)
        self.title.setText(accent(title, color))
        self.sub.setText(sub)

    # Анимация идёт, только пока блок виден и окно не свёрнуто. Градиент, самая дорогая часть,
    # плывёт лишь первые секунды и пока над ним курсор — как в образце, где фон оживает под мышью.
    def wake(self):
        self._awake_until = time.monotonic() + self.AWAKE

    def showEvent(self, e):
        self.wake()
        self.timer.start()
        super().showEvent(e)

    def enterEvent(self, e):
        self.wake()
        super().enterEvent(e)

    def hideEvent(self, e):
        self.timer.stop()
        super().hideEvent(e)

    def resizeEvent(self, e):
        self._bg = None
        super().resizeEvent(e)

    def _tick(self):
        # Стоим, если окно свёрнуто, пользователь в другой программе или процессор и так загружен
        if self.calm or not self.window().isActiveWindow():
            return
        self.t += 1 / self.FPS
        self.ring.t = self.t
        self._frame += 1
        awake = self.underMouse() or time.monotonic() < self._awake_until
        if awake:
            self.tm += 1 / self.FPS
        if awake and self._frame % 3 == 0:
            self._bg = None
            self.update()
        else:
            self.ring.update()

    def paintEvent(self, _e):
        if self._bg is None or self._bg.size() != self.size():
            self._bg = self._draw_background()
        QPainter(self).drawPixmap(0, 0, self._bg)

    def _draw_background(self) -> QPixmap:
        img = mesh_image(self.width() // 4, self.height() // 4, self.tm * 0.35, HERO_BLOBS)
        q = QPainter(img)  # затемнение слева, чтобы текст читался на любом месте градиента
        shade = QLinearGradient(0, 0, img.width(), 0)
        shade.setColorAt(0, QColor(0, 0, 0, 150))
        shade.setColorAt(0.6, QColor(0, 0, 0, 40))
        shade.setColorAt(1, QColor(0, 0, 0, 0))
        q.fillRect(img.rect(), shade)
        q.end()

        pm = QPixmap(self.size())
        pm.fill(Qt.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.SmoothPixmapTransform)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawImage(QRectF(self.rect()), img)
        # Тонкие волны поверх — как сетка второго слоя в образце
        p.setPen(QPen(QColor(255, 255, 255, 16), 1))
        w, hgt = r.width(), r.height()
        for k in range(7):
            path = QPainterPath()
            for i in range(41):
                x = w * i / 40
                y = hgt * (0.1 + 0.13 * k) + math.sin(i / 40 * math.tau * 1.3 + self.tm * 0.3 + k * 0.9) * hgt * 0.07
                path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
            p.drawPath(path)
        # Скругляем углы маской: это заметно дешевле, чем рисовать с обрезкой по контуру
        p.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        p.setPen(Qt.NoPen)
        p.setBrush(Qt.black)
        p.drawRoundedRect(r, 20, 20)
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.setPen(QPen(QColor(255, 255, 255, 30), 1))
        p.setBrush(Qt.NoBrush)
        p.drawRoundedRect(r, 20, 20)
        p.end()
        return pm


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
        self.graph = Sparkline(colors, maximum, 46)
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
        g.setVerticalSpacing(px(4))
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
        lay.setContentsMargins(px(20), px(16), px(20), px(14))
        lay.setSpacing(px(14))
        self.hero = Hero()
        self.hero.set_text(tr("✨ Без вашего согласия ничего не меняется", "✨ Nothing changes without your consent"),
                           tr("Почему тормозит компьютер?", "Why is my computer slow?"),
                           tr("Нажмите «Проверить»: WhySlow посмотрит на нагрузку, автозагрузку, мусор и сеть. "
                              "Любое изменение — только после вашего подтверждения, и его можно откатить во вкладке «Журнал».",
                              "Press “Scan”: WhySlow looks at load, startup apps, junk and network. "
                              "Any change happens only after you confirm it, and it can be undone in the Journal tab."))
        self.hero.add_button(tr("Оптимизировать", "Optimize"), self.main.optimize, primary=True)
        self.hero.add_button(tr("Автозагрузка", "Startup apps"), lambda: self.main.go("startup"))
        self.hero.add_button(tr("Очистка", "Cleanup"), lambda: self.main.go("junk"))
        self.hero.buttons.addStretch()
        lay.addWidget(self.hero)

        tiles = QHBoxLayout()
        tiles.setSpacing(px(12))
        go_sys = lambda: self.main.go("system")  # noqa: E731
        self.t_cpu = StatTile(tr("Процессор", "CPU"), [C["accent2"]], 100, go_sys)
        self.t_ram = StatTile(tr("Память", "Memory"), [C["accent"]], 100, go_sys)
        self.t_disk = StatTile(tr("Диск", "Disk"), [C["ok"], C["warn"]], None, go_sys)
        self.t_net = StatTile(tr("Сеть", "Network"), [C["info"], C["pink"]], None, go_sys)
        for t in (self.t_cpu, self.t_ram, self.t_disk, self.t_net):
            tiles.addWidget(t, 1)
        lay.addLayout(tiles)

        body = QHBoxLayout()
        body.setSpacing(px(14))
        left = QVBoxLayout()
        left.setSpacing(px(6))
        left.addWidget(section(tr("Что нашла проверка", "Scan findings")))
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
        sv.setContentsMargins(px(16), px(12), px(16), px(10))
        sv.setSpacing(px(10))
        self.top_cpu = TopList(tr("Сейчас грузят процессор", "Top CPU right now"))
        self.top_mem = TopList(tr("Больше всех памяти", "Top memory"))
        sv.addWidget(self.top_cpu)
        sv.addWidget(self.top_mem)
        sv.addStretch()
        b = QPushButton(tr("Все процессы →", "All processes →"))
        b.setObjectName("link")
        b.clicked.connect(lambda: self.main.go("procs"))
        sv.addWidget(b, 0, Qt.AlignRight)
        # Списки длинные: на невысоком экране блок прокручивается, а не раздувает окно
        side_scroll = QScrollArea()
        side_scroll.setWidgetResizable(True)
        side_scroll.setWidget(side)
        body.addWidget(side_scroll, 2)
        lay.addLayout(body, 1)

    def on_stats(self, s: dict, h: dict):
        self.t_cpu.value.setText(f"{s['cpu']:.0f}%")
        self.t_cpu.sub.setText(f"{s['freq'] / 1000:.2f} " + tr("ГГц", "GHz") if s["freq"]
                              else tr(f"{len(s['per'])} потоков", f"{len(s['per'])} threads"))
        self.t_cpu.graph.set_series(h["cpu"])
        used, total, pct = s["ram"]
        self.t_ram.value.setText(f"{pct:.0f}%")
        self.t_ram.sub.setText(tr(f"{human_size(used)} из {human_size(total)}", f"{human_size(used)} of {human_size(total)}"))
        self.t_ram.graph.set_series(h["ram"])
        self.t_disk.value.setText(rate(s["disk_r"] + s["disk_w"]))
        self.t_disk.sub.setText(tr(f"чтение {rate(s['disk_r'])} · запись {rate(s['disk_w'])}",
                                   f"read {rate(s['disk_r'])} · write {rate(s['disk_w'])}"))
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
        self.hero.set_text(tr("✨ Проверка идёт", "✨ Scanning"), tr("Проверяю компьютер…", "Checking your computer…"), text)
        self.hero.ring.set_state("…", C["muted"])

    def show_result(self, r: checks.ScanResult):
        bad = sum(f.level == checks.BAD for f in r.findings)
        warn = sum(f.level == checks.WARN for f in r.findings)
        if bad:
            title = (tr(f"Серьёзных проблем: {bad}", f"Serious problems: {bad}")
                     + (tr(f", замечаний: {warn}", f", warnings: {warn}") if warn else ""))
            color, glyph = C["bad"], str(bad + warn)
        elif warn:
            title, color, glyph = tr(f"Есть что улучшить: {warn}", f"Things to improve: {warn}"), C["warn"], str(warn)
        else:
            title, color, glyph = tr("Всё в порядке", "All good"), C["ok"], "✓"
        self.hero.set_text(tr(f"✨ Проверено в {time.strftime('%H:%M')} за {r.took:.1f} с",
                              f"✨ Checked at {time.strftime('%H:%M')} in {r.took:.1f}s"),
                           title,
                           tr("Показатели ниже обновляются каждую секунду. Любое изменение можно откатить в «Журнале».",
                              "The gauges below update every second. Any change can be undone in the Journal."), color)
        self.hero.ring.set_state(glyph, color)
        self.hero.wake()
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
            btn = QPushButton(tr("Выключить прокси", "Turn off proxy"))
            btn.setObjectName("primary")
            btn.clicked.connect(self.main.fix_proxy)
            h.addWidget(btn, 0, Qt.AlignVCenter)
        elif f.tab:
            btn = QPushButton(tr("Перейти →", "Open →"))
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

        cpu = QGroupBox(tr("Процессор", "CPU"))
        g = QGridLayout(cpu)
        g.setVerticalSpacing(px(8))
        self._row(g, 0, tr("Модель", "Model"), "cpu", span=3)
        self._row(g, 1, tr("Ядра", "Cores"), "cores")
        self._row(g, 1, tr("Потоки", "Threads"), "threads", col=2)
        self._row(g, 2, tr("Частота", "Clock"), "freq")
        self._row(g, 2, tr("Макс.", "Max"), "freq_max", col=2)
        g.addWidget(muted(tr("Загрузка", "Load")), 3, 0)
        self.cpu_bar = Bar(60, 85, 26)
        g.addWidget(self.cpu_bar, 3, 1, 1, 3)
        self.cpu_graph = Sparkline([C["accent2"]], 100, 170, axis=True, names=[tr("загрузка", "load")])
        g.addWidget(self.cpu_graph, 4, 0, 1, 4)
        self.core_title = muted("")
        g.addWidget(self.core_title, 5, 0, 1, 4)
        self.cores = CoreGrid()
        g.addWidget(self.cores, 6, 0, 1, 4)
        g.setColumnStretch(1, 1)
        g.setColumnStretch(3, 1)

        mem = QGroupBox(tr("Память", "Memory"))
        g = QGridLayout(mem)
        g.setVerticalSpacing(px(8))
        self._row(g, 0, tr("Всего", "Total"), "ram_total")
        g.addWidget(muted(tr("ОЗУ", "RAM")), 1, 0)
        self.ram_bar = Bar(70, 90, 26)
        g.addWidget(self.ram_bar, 1, 1)
        g.addWidget(muted(tr("Подкачка", "Swap")), 2, 0)
        self.swap_bar = Bar(50, 80)
        g.addWidget(self.swap_bar, 2, 1)
        self.ram_graph = Sparkline([C["accent"]], 100, 130, axis=True, names=[tr("занято", "used")])
        g.addWidget(self.ram_graph, 3, 0, 1, 2)
        g.setColumnStretch(1, 1)

        self.sys_box = QGroupBox(tr("Система", "System"))
        g = QGridLayout(self.sys_box)
        self._row(g, 0, tr("ОС", "OS"), "os")
        self._row(g, 1, tr("Компьютер", "Computer"), "host")
        self._row(g, 2, tr("Плата", "Motherboard"), "board")
        self._row(g, 3, tr("Работает", "Uptime"), "uptime")
        g.setColumnStretch(1, 1)

        self.gpu_box = QGroupBox(tr("Видеокарта", "Graphics"))
        gv = QVBoxLayout(self.gpu_box)
        self.gpu_lay = QGridLayout()
        self.gpu_lay.addWidget(muted(tr("Загрузка…", "Loading…")), 0, 0)
        gv.addLayout(self.gpu_lay)
        gl = QHBoxLayout()
        self.gpu_label = muted(tr("Загрузка видеоядра", "GPU load"))
        self.gpu_label.setWordWrap(False)
        gl.addWidget(self.gpu_label)
        self.gpu_bar = Bar(80, 95)
        gl.addWidget(self.gpu_bar, 1)
        gv.addLayout(gl)
        self.gpu_graph = Sparkline(["#a78bfa"], 100, 110, axis=True, names=["GPU"])
        gv.addWidget(self.gpu_graph)
        self.gpu_note = muted("")
        gv.addWidget(self.gpu_note)
        self.gpu_hist: deque = deque(maxlen=60)
        self._gpu_seen = False

        self.disk_box = QGroupBox(tr("Диски", "Disks"))
        dv = QVBoxLayout(self.disk_box)
        self.disk_lay = QGridLayout()
        dv.addLayout(self.disk_lay)
        self.disk_io = muted("")
        dv.addWidget(self.disk_io)
        self.disk_graph = Sparkline([C["ok"], C["warn"]], None, 130, axis=True, fmt=rate_short,
                                    names=[tr("чтение", "read"), tr("запись", "write")])
        dv.addWidget(self.disk_graph)
        self.disk_bars: dict[str, Bar] = {}

        net = QGroupBox(tr("Сеть", "Network"))
        nv = QVBoxLayout(net)
        self.net_io = muted("")
        nv.addWidget(self.net_io)
        self.net_graph = Sparkline([C["info"], C["pink"]], None, 130, axis=True, fmt=rate_short,
                                   names=[tr("получено", "received"), tr("отправлено", "sent")])
        nv.addWidget(self.net_graph)

        self.temp_box = QGroupBox(tr("Температуры", "Temperatures"))
        self.temp_lay = QGridLayout(self.temp_box)
        self.temp_bars: dict[str, Bar] = {}
        self._temp_names: tuple | None = None
        self._lhm_state = ""  # текст хода скачивания LibreHardwareMonitor

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
            run_async(lambda _p: sysinfo.core_kinds(), self._set_kinds)

    def _set_kinds(self, kinds: list[str]):
        self.cores.set_kinds(kinds)
        self._kinds = kinds

    def fill(self, info: dict):
        self.info = info
        self.f["cpu"].setText(info["cpu"])
        self.f["cores"].setText(str(info["cores"]))
        self.f["threads"].setText(str(info["threads"]))
        self.f["freq_max"].setText(f"{info['freq_max']:.0f} " + tr("МГц", "MHz") if info["freq_max"] else "—")
        self.f["ram_total"].setText(human_size(info["ram_total"]))
        self.f["os"].setText(info["os"])
        self.f["host"].setText(info["host"])
        self.f["board"].setText(info["board"])

        self._clear(self.gpu_lay)
        if not info["gpus"]:
            self.gpu_lay.addWidget(muted(tr("Не удалось определить", "Could not detect")), 0, 0)
        row = 0
        for gpu in info["gpus"]:
            name = field(gpu["name"])
            name.setStyleSheet("font-weight: 700;")
            self.gpu_lay.addWidget(name, row, 0, 1, 4)
            row += 1
            self.gpu_lay.addWidget(muted(tr("Драйвер", "Driver")), row, 0)
            self.gpu_lay.addWidget(field(gpu.get("driver") or "—"), row, 1)
            self.gpu_lay.addWidget(muted(tr("Память", "Memory")), row, 2)
            vram = gpu.get("vram_mb")
            self.gpu_lay.addWidget(field(f"{vram / 1024:.1f} " + tr("ГБ", "GB") if vram else "—"), row, 3)
            row += 1
            if gpu.get("temp"):
                self.gpu_lay.addWidget(muted(tr("Темп.", "Temp")), row, 0)
                self.gpu_lay.addWidget(field(f"{gpu['temp']:.0f} °C"), row, 1)
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
            self.disk_bars[d["mount"]].set(d["percent"], tr(f"свободно {human_size(d['free'])} из {human_size(d['total'])}",
                                                     f"{human_size(d['free'])} free of {human_size(d['total'])}"))

    @staticmethod
    def _clear(lay: QGridLayout):
        while lay.count():
            w = lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def on_stats(self, s: dict, h: dict):
        per = s["per"]
        self.cores.set_values(per)
        if per:
            busiest = max(range(len(per)), key=per.__getitem__)
            kinds = getattr(self, "_kinds", [])
            legend = (tr(" P — производительные ядра (тяжёлые задачи), E — энергоэффективные (фоновые).",
                         " P = performance cores (heavy work), E = efficiency cores (background).")
                      if kinds else "")
            self.core_title.setText(tr(f"Каждое ядро отдельно, график за минуту. Сейчас загруженнее всех "
                                       f"ядро {busiest + 1} — {per[busiest]:.0f}%.",
                                       f"Each core separately, last minute. Busiest right now: "
                                       f"core {busiest + 1} at {per[busiest]:.0f}%.") + legend)
        self.cpu_bar.set(s["cpu"])
        self.cpu_graph.set_series(h["cpu"])
        self.f["freq"].setText(f"{s['freq']:.0f} " + tr("МГц", "MHz") if s["freq"] else "—")
        used, total, pct = s["ram"]
        self.ram_bar.set(pct, f"{human_size(used)} / {human_size(total)}  ({pct:.0f}%)")
        self.ram_graph.set_series(h["ram"])
        su, st, sp = s["swap"]
        self.swap_bar.set(sp, f"{human_size(su)} / {human_size(st)}" if st else tr("нет", "none"))
        self.f["uptime"].setText(human_duration(s["uptime"]))
        if s["disks"]:
            self._fill_disks(s["disks"])
        self.disk_io.setText(f"<span style='color:{C['ok']}'>■</span> {tr('чтение', 'read')} {rate(s['disk_r'])}   "
                             f"<span style='color:{C['warn']}'>■</span> {tr('запись', 'write')} {rate(s['disk_w'])}")
        self.disk_graph.set_series(h["disk_r"], h["disk_w"])
        self.net_io.setText(f"<span style='color:{C['info']}'>■</span> {tr('получено', 'received')} {rate(s['net_rx'])}   "
                            f"<span style='color:{C['pink']}'>■</span> {tr('отправлено', 'sent')} {rate(s['net_tx'])}")
        self.net_graph.set_series(h["net_rx"], h["net_tx"])
        self._fill_gpu(s.get("gpu"), s.get("gpu_ready"))
        self._fill_temps(s["temps"])

    def _fill_gpu(self, load: float | None, ready: bool):
        if load is not None:
            self._gpu_seen = True
            self.gpu_hist.append(load)
            self.gpu_bar.set(load)
            self.gpu_graph.set_series(self.gpu_hist)
        elif ready and not self._gpu_seen:
            self.gpu_note.setText(by_os(tr("Windows не отдаёт загрузку видеокарты (нужна Windows 10 1709 или новее).",
                                           "Windows doesn't report GPU load (requires Windows 10 1709 or newer)."),
                                        tr("macOS не отдаёт загрузку видеоядра на этом Mac.",
                                           "macOS doesn't report GPU load on this Mac."),
                                        tr("Драйвер видеокарты не отдаёт загрузку (умеют NVIDIA и AMD).",
                                           "The graphics driver doesn't report load (NVIDIA and AMD do).")))
        for w in (self.gpu_label, self.gpu_bar, self.gpu_graph):
            w.setVisible(self._gpu_seen)
        self.gpu_note.setVisible(not self._gpu_seen)

    def _fill_temps(self, temps: dict):
        names = tuple(sorted(temps))
        note, action = "", ""
        if not temps:
            if IS_MAC:
                note = tr("Датчики температуры не найдены. На Mac с процессором Intel macOS не отдаёт их "
                           "программам без прав администратора.",
                           "No temperature sensors found. On Intel Macs, macOS only exposes them "
                           "to apps with administrator rights.")
            elif not IS_WINDOWS:
                note = tr("Система не отдаёт датчики температуры", "The system doesn't expose temperature sensors")
            elif not lhm.ready():
                note = tr("Ищу датчики…", "Looking for sensors…")
            else:
                note, action = lhm.problem(), self._lhm_state or lhm.action()
        elif lhm.source():
            note = tr(f"Источник: {lhm.source()}", f"Source: {lhm.source()}") + (f". {lhm.problem()}" if lhm.problem() else "")
        if (names, note, action) != self._temp_names:
            self._temp_names = (names, note, action)
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
            if action:
                self.temp_lay.addWidget(self._lhm_buttons(action), len(names) + 1, 0, 1, 2)
            self.temp_lay.setColumnStretch(1, 1)
        for name, t in temps.items():
            self.temp_bars[name].set(t, f"{t:.0f} °C")

    def _lhm_buttons(self, action: str) -> QWidget:
        box = QWidget()
        row = QHBoxLayout(box)
        row.setContentsMargins(0, px(4), 0, 0)
        if action == "install":
            b = QPushButton(tr("Скачать и запустить LibreHardwareMonitor", "Download and run LibreHardwareMonitor"))
            b.clicked.connect(self.install_lhm)
            row.addWidget(b)
        elif action == "launch":
            b = QPushButton(tr("Запустить LibreHardwareMonitor", "Run LibreHardwareMonitor"))
            b.clicked.connect(self.launch_lhm)
            row.addWidget(b)
        else:  # идёт скачивание: action — текст хода
            row.addWidget(muted(action))
        link = muted(f'<a href="{lhm.RELEASES_URL}">' + tr("страница программы", "program page") + "</a>")
        link.setOpenExternalLinks(True)
        link.setWordWrap(False)
        row.addWidget(link)
        row.addStretch(1)
        return box

    def install_lhm(self):
        if not confirm(self, tr("Скачать LibreHardwareMonitor?", "Download LibreHardwareMonitor?"),
                       tr("LibreHardwareMonitor — бесплатная программа с открытым кодом, которая читает "
                          "датчики температуры. WhySlow скачает её последний официальный релиз:\n\n"
                          f"{lhm.RELEASES_URL}\n\n"
                          "Архив проверяется по контрольной сумме из GitHub и распаковывается сюда:\n"
                          f"{lhm.install_dir()}\n\n"
                          "Потом WhySlow запустит её: Windows спросит разрешение на права администратора, "
                          "без них датчики не читаются. Новые версии могут ещё предложить поставить драйвер PawnIO.",
                          "LibreHardwareMonitor is a free, open-source program that reads temperature "
                          "sensors. WhySlow will download its latest official release:\n\n"
                          f"{lhm.RELEASES_URL}\n\n"
                          "The archive is verified against the GitHub checksum and unpacked here:\n"
                          f"{lhm.install_dir()}\n\n"
                          "Then WhySlow will start it: Windows will ask for administrator rights, which "
                          "are needed to read sensors. Newer versions may also offer to install the PawnIO driver.")):
            return
        self._lhm_state = tr("Скачиваю LibreHardwareMonitor…", "Downloading LibreHardwareMonitor…")

        def progress(text):
            self._lhm_state = text
            self._fill_temps({})

        def done(version):
            self._lhm_state = ""
            journal.add("lhm", tr(f"Скачан LibreHardwareMonitor {version}", f"Downloaded LibreHardwareMonitor {version}"), {"dir": str(lhm.install_dir())})
            self.launch_lhm()

        def failed(e):
            self._lhm_state = ""
            self._fill_temps({})
            _show_error(e)

        run_async(lhm.install, done, failed, progress)
        self._fill_temps({})

    def launch_lhm(self):
        if not lhm.launch():
            QMessageBox.information(self, "LibreHardwareMonitor",
                                    tr("LibreHardwareMonitor не запущен. Без прав администратора он не видит "
                                       "датчики, поэтому WhySlow запускает его через запрос Windows.",
                                       "LibreHardwareMonitor isn't running. Without administrator rights it can't "
                                       "see the sensors, so WhySlow starts it through the Windows prompt."))
        self._fill_temps({})


# ------------------------------------------------------------ Автозагрузка ---

class StartupTab(QWidget):
    COLS = [tr("Вкл.", "On"), tr("Программа", "Program"), tr("Вердикт базы", "Verdict"), tr("Откуда", "Source"),
            tr("Команда", "Command")]

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
        top.addWidget(muted(tr("Что запускается вместе с системой. Щёлкните переключатель, чтобы отключить программу "
                               "или включить обратно. Вердикт — из общей базы WhySlow на GitHub.",
                               "What starts with your system. Click a switch to disable a program "
                               "or turn it back on. Verdicts come from the shared WhySlow database on GitHub.")), 1)
        self.show_services = QCheckBox(tr("Показывать системные службы", "Show system services"))
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

        self.detail = QLabel(tr("Выберите программу, чтобы увидеть пояснение.", "Select a program to see what it is."))
        self.detail.setWordWrap(True)
        self.detail.setStyleSheet(f"QLabel {{ background: {C['panel2']}; border: 1px solid {C['border']};"
                                  f"border-radius: 12px; padding: {px(12)}px; }}")
        self.detail.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.detail.setOpenExternalLinks(True)
        self.detail.setMinimumHeight(px(80))
        lay.addWidget(self.detail)

        btns = QHBoxLayout()
        self.b_off = QPushButton(tr("Отключить", "Disable"))
        self.b_off.setObjectName("primary")
        self.b_on = QPushButton(tr("Включить", "Enable"))
        self.b_del = QPushButton(tr("Удалить из автозагрузки", "Remove from startup"))
        self.b_del.setObjectName("danger")
        self.b_report = QPushButton(tr("Предложить в базу…", "Suggest to the base…"))
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
            sw.setToolTip(tr("Включено — щёлкните, чтобы отключить", "Enabled — click to disable") if it.enabled
                          else tr("Отключено — щёлкните, чтобы включить", "Disabled — click to enable"))
            sw.clicked.connect(lambda _=False, it=it: self.toggle(it, not it.enabled))
            if not it.can_toggle:
                sw.setEnabled(False)
                sw.setToolTip(tr("Запустится один раз при следующем входе; такую запись можно только удалить",
                                  "Runs once at next sign-in; this entry can only be removed"))
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
            self.detail.setText(tr("Выберите программу, чтобы увидеть пояснение.", "Select a program to see what it is."))
            return
        self.b_off.setEnabled(it.enabled and it.can_toggle and not self.busy)
        self.b_on.setEnabled(not it.enabled and it.can_toggle and not self.busy)
        self.b_del.setEnabled(it.can_delete and not self.busy)
        v = it.verdict
        color = VERDICT_COLORS.get(v.verdict, C["muted"])
        cat = f" · {v.category}" if v.category else ""
        admin = (f"<br><span style='color:{C['muted']}'>" + tr("Для изменения нужны права администратора",
                                                               "Changing this requires administrator rights")
                 + (tr(" — macOS спросит пароль.", " — macOS will ask for your password.") if IS_MAC else ".")
                 + "</span>") if it.needs_admin else ""
        if not it.can_toggle:
            admin += (f"<br><span style='color:{C['muted']}'>"
                      + tr("Запустится один раз при следующем входе. Отключить такую запись нельзя, только удалить.",
                           "Runs once at next sign-in. This entry can't be disabled, only removed.")
                      + "</span>")
        note = v.note
        if v.verdict == "unknown":
            note = tr("Нет в базе. ", "Not in the database. ") + online_note(it.name, it.command, lambda it=it: self.current() is it and self.on_select())
        self.detail.setText(f"<b>{it.name}</b>{cat} — <b style='color:{color}'>{v.label}</b><br>{note}"
                            f"<br><span style='color:{C['muted']}'>{it.command}</span>{admin}")

    def _apply(self, fn, ok_text):
        """Изменение — в фоне: macOS может спросить пароль, Windows — подумать над службой."""
        self.busy = True
        self.table.setEnabled(False)
        self.on_select()
        self.main.status(tr("Применяю…", "Applying…"))

        def done(_r):
            self.busy = False
            self.table.setEnabled(True)
            self.main.status(ok_text)
            self.reload()

        def failed(msg):
            self.busy = False
            self.table.setEnabled(True)
            self.refill()
            QMessageBox.warning(self, tr("Не получилось", "Failed"), msg)

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
                if not confirm(self, tr("Точно отключить?", "Disable anyway?"),
                               tr(f"База WhySlow советует НЕ трогать «{it.name}».\n\n{v.note}\n\n"
                                  "Отключить всё равно? (включить обратно можно в любой момент)",
                                  f"The WhySlow database recommends leaving “{it.name}” alone.\n\n{v.note}\n\n"
                                  "Disable it anyway? (You can turn it back on at any time.)"), danger=True):
                    return
            elif it.kind == "win_service":
                if not confirm(self, tr("Перевести службу в режим «Вручную»?", "Set the service to Manual?"),
                               tr(f"Служба «{it.name}» перестанет запускаться сама при включении компьютера.\n"
                                  "Если она понадобится какой-то программе, Windows запустит её сама.\n\n"
                                  "Вернуть прежний режим можно здесь же или во вкладке «Журнал».",
                                  f"The “{it.name}” service will no longer start automatically at boot.\n"
                                  "If a program needs it, Windows will start it on demand.\n\n"
                                  "You can restore the previous mode here or in the Journal tab.")):
                    return
            elif not confirm(self, tr("Отключить из автозагрузки?", "Disable at startup?"),
                             tr(f"«{it.name}» перестанет запускаться при включении компьютера.\n"
                                "Сама программа останется установленной, её можно открыть вручную.\n\n"
                                "Включить обратно можно здесь же или во вкладке «Журнал».",
                                f"“{it.name}” will no longer start when your computer starts.\n"
                                "The program stays installed and you can still open it manually.\n\n"
                                "You can turn it back on here or in the Journal tab.")):
                return
        elif not confirm(self, tr("Включить автозапуск?", "Enable at startup?"),
                             tr(f"«{it.name}» снова будет запускаться вместе с системой.",
                                f"“{it.name}” will start with your system again.")):
            return
        self._apply(lambda: startup.set_enabled(it, enable), (tr(f"«{it.name}» включено", f"“{it.name}” enabled") if enable
                                                              else tr(f"«{it.name}» отключено", f"“{it.name}” disabled")))

    def delete(self):
        it = self.current()
        if not it or not confirm(self, tr("Удалить из автозагрузки?", "Remove from startup?"),
                                 tr(f"Запись «{it.name}» будет удалена из автозагрузки.\n"
                                    "Программа останется установленной. Резервная копия записи сохранится — "
                                    "её можно вернуть во вкладке «Журнал».",
                                    f"The “{it.name}” entry will be removed from startup.\n"
                                    "The program stays installed. A backup of the entry is kept — "
                                    "you can restore it in the Journal tab."), danger=it.verdict.verdict != "bad"):
            return
        self._apply(lambda: startup.delete(it), tr(f"«{it.name}» удалено из автозагрузки",
                                                           f"“{it.name}” removed from startup"))

    def report(self):
        it = self.current()
        if it:
            report_online(it.name, it.command)


# --------------------------------------------------------------- Процессы ---

class ProcessesTab(QWidget):
    COLS = [tr("Программа", "Program"), tr("Процессов", "Processes"), tr("ЦП", "CPU"), tr("Память", "Memory"),
            tr("Вердикт базы", "Verdict")]

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.groups = {}
        self.latest = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))
        lay.addWidget(muted(tr("Программы, сгруппированные по имени (браузер = десятки процессов). "
                               "Обновляется каждые 2 секунды. Нажмите на заголовок, чтобы отсортировать.",
                               "Programs grouped by name (a browser = dozens of processes). "
                               "Updates every 2 seconds. Click a column header to sort.")))
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
        self.detail.setTextFormat(Qt.RichText)
        self.detail.setTextInteractionFlags(Qt.TextBrowserInteraction)
        self.detail.setOpenExternalLinks(True)
        lay.addWidget(self.detail)
        row = QHBoxLayout()
        self.b_kill = QPushButton(tr("Закрыть программу", "Close program"))
        self.b_kill.setObjectName("danger")
        self.b_kill.clicked.connect(self.kill)
        self.b_kill.setEnabled(False)
        row.addWidget(self.b_kill)
        row.addStretch()
        self.b_report = QPushButton(tr("Предложить в базу…", "Suggest to the base…"))
        self.b_report.clicked.connect(self.report)
        self.b_report.setEnabled(False)
        row.addWidget(self.b_report)
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
            name = QTableWidgetItem(g.name + (tr("  (система)", "  (system)") if g.system else ""))
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
        self.b_report.setEnabled(g is not None)
        if g:
            v = g.verdict
            if v and v.verdict != "unknown":
                note = _esc(v.note)
            else:
                note = online_note(g.name, g.exe, lambda name=name: self.selected_name() == name and self.on_select())
            self.detail.setText(_esc(g.exe or g.name) + (f"<br>{note}" if note else ""))

    def report(self):
        g = self.groups.get(self.selected_name())
        if g:
            report_online(g.name, g.exe)

    def kill(self):
        g = self.groups.get(self.selected_name())
        if not g:
            return
        warn = ""
        if g.system or (g.verdict and g.verdict.verdict == "keep"):
            warn = tr("\n\n⚠ Это системный процесс или важный компонент. Закрытие может привести к сбою или перезагрузке.",
                      "\n\n⚠ This is a system process or an important component. Closing it may cause a crash or reboot.")
        if not confirm(self, tr("Закрыть программу?", "Close program?"),
                       tr(f"Закрыть «{g.name}» ({len(g.pids)} проц.)?\nНесохранённые данные в ней будут потеряны.{warn}",
                          f"Close “{g.name}” ({len(g.pids)} proc.)?\nAny unsaved data in it will be lost.{warn}"),
                       danger=bool(warn)):
            return
        self.b_kill.setEnabled(False)

        def done(r):
            closed, failed = r
            msg = tr(f"«{g.name}»: закрыто процессов — {closed}", f"“{g.name}”: processes closed — {closed}")
            if failed:
                msg += tr(f", не удалось — {failed} ({ADMIN_HINT})", f", failed — {failed} ({ADMIN_HINT})")
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
        self.total = QLabel(accent(tr("Нажмите «Проверить» вверху, чтобы найти мусор", "Press “Scan” above to find junk files")))
        self.total.setObjectName("h1")
        lay.addWidget(self.total)
        lay.addWidget(muted(tr(f"Удаляются только ваши файлы старше суток ({junk.WHOLE}). "
                               "Файлы, которые сейчас заняты программами, пропускаются.",
                               f"Only your own files older than a day are deleted ({junk.WHOLE}). "
                               "Files currently in use by programs are skipped.")))
        self.box = QGroupBox(tr("Что можно удалить", "What can be removed"))
        self.box_lay = QGridLayout(self.box)
        self.box_lay.setVerticalSpacing(px(10))
        self.box_lay.setHorizontalSpacing(px(14))
        lay.addWidget(self.box)
        lay.addStretch()
        row = QHBoxLayout()
        self.b_clean = QPushButton(tr("Очистить выбранное", "Clean selected"))
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
            size = QLabel(human_size(c.size) + (tr(f"  ·  {c.files} файлов", f"  ·  {c.files} files") if c.files else ""))
            size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.box_lay.addWidget(size, i, 2)
        self.box_lay.setColumnStretch(1, 1)
        self.update_total()

    def update_total(self):
        sel = sum(c.size for c, b in zip(self.cats, self.boxes) if b.isChecked())
        total = sum(c.size for c in self.cats)
        self.total.setText(accent(tr(f"Можно освободить {human_size(total)}", f"{human_size(total)} can be freed") if total
                                  else tr("Мусора не найдено", "No junk found")))
        self.b_clean.setText(tr(f"Очистить выбранное ({human_size(sel)})", f"Clean selected ({human_size(sel)})"))
        self.b_clean.setEnabled(sel > 0)

    def clean(self):
        chosen = [c for c, b in zip(self.cats, self.boxes) if b.isChecked()]
        names = "\n".join(f"• {c.title} — {human_size(c.size)}" for c in chosen)
        if not confirm(self, tr("Удалить мусор?", "Delete junk?"),
                       tr(f"Будут удалены:\n{names}\n\nЭто действие нельзя отменить.",
                          f"The following will be deleted:\n{names}\n\nThis can't be undone."), danger=True):
            return
        self.b_clean.setEnabled(False)
        self.b_clean.setText(tr("Удаляю…", "Deleting…"))

        def work(_p):
            res = [(c, *junk.clean(c)) for c in chosen]
            return res, [junk.scan(c) for c in junk.categories()]

        def done(r):
            res, cats = r
            freed = sum(x[1] for x in res)
            failed = sum(x[2] for x in res)
            self.set_cats(cats)
            self.main.journal_tab.reload()
            msg = tr(f"Освобождено {human_size(freed)}.", f"Freed {human_size(freed)}.")
            if failed:
                msg += tr(f"\nПропущено файлов: {failed} (заняты программами или нет прав).",
                          f"\nFiles skipped: {failed} (in use or no permission).")
            QMessageBox.information(self, tr("Готово", "Done"), msg)

        run_async(work, done)


# ------------------------------------------------- Хранилище (только Mac) ---

class StorageTab(QWidget):
    """Из чего состоят «Системные данные» macOS. Считается при первом открытии вкладки:
    обход Library занимает время, поэтому отдельно от общей проверки."""

    LEVEL_COLORS = {macstorage.KEEP: C["info"], macstorage.SAFE: C["ok"], macstorage.AUTO: C["muted"],
                    macstorage.MIXED: C["accent2"]}
    # пометки отдельных программ в «Данных программ»
    ITEM_COLORS = {macapps.SYSTEM: C["muted"], macapps.USED: C["info"], macapps.UNKNOWN: C["warn"],
                   macapps.OLD: C["ok"], macapps.LEFTOVER: C["ok"], macapps.APP: C["warn"]}
    RISKY = {macapps.USED, macapps.UNKNOWN, macapps.APP}  # просить проверить ещё раз

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.cats: list[macstorage.Category] = []
        self.checked: dict[tuple[str, str], bool] = {}
        self.busy = False
        self.started = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))

        head = QHBoxLayout()
        head.setSpacing(px(14))
        text = QVBoxLayout()
        text.setSpacing(px(4))
        self.title = QLabel(accent(tr("Системные данные", "System Data")))
        self.title.setObjectName("h1")
        text.addWidget(self.title)
        text.addWidget(muted(tr("В настройках хранилища macOS это одна серая полоса. Здесь она разложена по категориям "
                                "с пометками: что важно, что можно удалить и что macOS удалит сама. Удалённое уходит "
                                "в Корзину, и его можно вернуть из «Журнала».",
                                "In macOS storage settings this is one grey bar. Here it is split into categories "
                                "marked as important, safe to delete, or removed by macOS on its own. Deleted items "
                                "go to the Trash and can be restored from the Journal.")))
        head.addLayout(text, 1)
        self.b_scan = QPushButton(tr("Посчитать", "Measure"))
        self.b_scan.setObjectName("primary")
        self.b_scan.setCursor(Qt.PointingHandCursor)
        self.b_scan.clicked.connect(self.scan)
        head.addWidget(self.b_scan, 0, Qt.AlignVCenter)
        lay.addLayout(head)

        self.fda = QFrame()
        self.fda.setObjectName("card")
        fl = QHBoxLayout(self.fda)
        fl.setContentsMargins(px(14), px(10), px(14), px(10))
        fda_text = QLabel(f"<b style='color:{C['warn']}'>" + tr("Нет полного доступа к диску. ", "No Full Disk Access. ")
                          + "</b>" + _esc(macstorage.fda_hint()))
        fda_text.setWordWrap(True)
        fl.addWidget(fda_text, 1)
        b_fda = QPushButton(tr("Открыть настройки", "Open Settings"))
        b_fda.clicked.connect(macstorage.open_fda_settings)
        fl.addWidget(b_fda, 0, Qt.AlignVCenter)
        self.fda.setVisible(not macstorage.has_full_disk_access())
        lay.addWidget(self.fda)

        body = QHBoxLayout()
        body.setSpacing(px(14))
        self.table = make_table([tr("Категория", "Category"), tr("Размер", "Size"), tr("Пометка", "Label")])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setSortingEnabled(True)
        self.table.setWordWrap(False)
        self.table.itemSelectionChanged.connect(self.on_select)
        body.addWidget(self.table, 4)

        side = QVBoxLayout()
        side.setSpacing(px(6))
        self.cat_title = QLabel()
        self.cat_title.setStyleSheet(f"font-size: {pt(12)}; font-weight: 600;")
        self.cat_title.setWordWrap(True)
        side.addWidget(self.cat_title)
        self.cat_note = muted()
        self.cat_note.setTextFormat(Qt.RichText)
        side.addWidget(self.cat_note)
        self.items = make_table([tr("Что", "What"), tr("Размер", "Size"), tr("Изменено", "Modified"), tr("Пометка", "Label")])
        ih = self.items.horizontalHeader()
        ih.setSectionResizeMode(0, QHeaderView.Stretch)
        ih.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        ih.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        ih.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.items.setWordWrap(False)
        self.items.itemChanged.connect(self.on_check)
        side.addWidget(self.items, 1)
        pick = QHBoxLayout()
        self.b_all = QPushButton(tr("Отметить всё", "Check all"))
        self.b_all.setObjectName("link")
        self.b_all.clicked.connect(lambda: self.check_all(True))
        self.b_none = QPushButton(tr("Снять отметки", "Uncheck all"))
        self.b_none.setObjectName("link")
        self.b_none.clicked.connect(lambda: self.check_all(False))
        pick.addWidget(self.b_all)
        pick.addWidget(self.b_none)
        pick.addStretch()
        side.addLayout(pick)
        body.addLayout(side, 7)
        lay.addLayout(body, 1)

        auto = QFrame()
        auto.setObjectName("card")
        al = QHBoxLayout(auto)
        al.setContentsMargins(px(14), px(10), px(14), px(10))
        al.setSpacing(px(12))
        self.sw_auto = Switch(settings.get("mac_autoclean"))
        self.sw_auto.clicked.connect(self.toggle_auto)
        al.addWidget(self.sw_auto, 0, Qt.AlignTop)
        av = QVBoxLayout()
        av.setSpacing(px(2))
        t = QLabel(tr("Автоочистка", "Auto-clean"))
        t.setStyleSheet("font-weight: 700;")
        av.addWidget(t)
        self.auto_state = muted()
        av.addWidget(self.auto_state)
        al.addLayout(av, 1)
        self.every = QComboBox()
        for d in macstorage.INTERVALS:
            self.every.addItem({1: tr("каждый день", "every day"), 7: tr("раз в неделю", "every week"),
                                30: tr("раз в месяц", "every month")}[d], d)
        self.every.setCurrentIndex(max(self.every.findData(settings.get("mac_autoclean_days")), 0))
        self.every.currentIndexChanged.connect(lambda _i: (settings.set("mac_autoclean_days", self.every.currentData()),
                                                           self.show_auto()))
        al.addWidget(self.every, 0, Qt.AlignVCenter)
        lay.addWidget(auto)

        row = QHBoxLayout()
        self.state = muted()
        row.addWidget(self.state, 1)
        self.b_del = QPushButton(tr("Удалить выбранное", "Delete selected"))
        self.b_del.setObjectName("primary")
        self.b_del.setCursor(Qt.PointingHandCursor)
        self.b_del.clicked.connect(self.delete)
        row.addWidget(self.b_del)
        lay.addLayout(row)
        self.show_auto()
        self.on_select()
        self.update_count()

    # --- подсчёт ---

    def activate(self):
        if not self.started:
            self.scan()

    def scan(self):
        if self.busy:
            return
        self.started = self.busy = True
        self.b_scan.setEnabled(False)
        self.b_scan.setText(tr("Считаю…", "Measuring…"))
        self.update_count()

        def done(cats):
            self.busy = False
            self.b_scan.setEnabled(True)
            self.b_scan.setText(tr("Посчитать заново", "Measure again"))
            self.fda.setVisible(not macstorage.has_full_disk_access())
            self.set_cats(cats)

        def failed(e):
            self.busy = False
            self.b_scan.setEnabled(True)
            self.b_scan.setText(tr("Посчитать", "Measure"))
            self.update_count()
            _show_error(e)

        run_async(macstorage.scan_all, done, failed, self.state.setText)

    def set_cats(self, cats: list[macstorage.Category]):
        keep = self.current()
        self.cats = cats
        paths = {(c.key, it.path) for c in cats for it in c.items}
        self.checked = {k: v for k, v in self.checked.items() if k in paths}
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(cats))
        for r, c in enumerate(cats):
            name = QTableWidgetItem(c.title)
            name.setData(Qt.UserRole, c.key)
            if c.denied and c.fda:
                name.setToolTip(tr("Посчитано не всё: нет полного доступа к диску", "Partly measured: no Full Disk Access"))
                name.setForeground(QColor(C["warn"]))
            size = NumItem(human_size(c.size) if c.size_known else tr(f"{len(c.items)} шт.", f"{len(c.items)}"),
                           c.size if c.size_known else -1)
            tag = QTableWidgetItem(macstorage.level_label(c.level))
            tag.setForeground(QColor(self.LEVEL_COLORS[c.level]))
            if c.size_known and not c.size:
                for it in (name, size):
                    it.setForeground(QColor(C["muted"]))
            for col, it in enumerate([name, size, tag]):
                self.table.setItem(r, col, it)
        self.table.setSortingEnabled(True)
        self.table.sortItems(1, Qt.DescendingOrder)
        total = sum(c.size for c in cats)
        free = sum(c.size for c in cats if c.level == macstorage.SAFE) + \
            sum(it.size for c in cats if c.level == macstorage.MIXED for it in c.items
                if it.level in (macapps.OLD, macapps.LEFTOVER))
        self.title.setText(accent(tr(f"Системные данные: найдено {human_size(total)}", f"System Data: {human_size(total)} found")))
        self.state.setText(tr(f"Можно удалить до {human_size(free)}", f"Up to {human_size(free)} can be deleted"))
        row = next((r for r in range(self.table.rowCount())
                    if keep and self.table.item(r, 0).data(Qt.UserRole) == keep.key), 0)
        self.table.selectRow(row)
        self.update_count()

    def current(self) -> macstorage.Category | None:
        rows = self.table.selectionModel().selectedRows()
        if not rows:
            return None
        key = self.table.item(rows[0].row(), 0).data(Qt.UserRole)
        return next((c for c in self.cats if c.key == key), None)

    def on_select(self):
        c = self.current()
        self.items.blockSignals(True)
        self.items.setRowCount(0)
        if not c:
            self.cat_title.setText(tr("Выберите категорию", "Select a category") if self.cats else "")
            self.cat_note.setText("" if self.cats else tr("Нажмите «Посчитать».", "Press “Measure”."))
            for b in (self.b_all, self.b_none):
                b.setVisible(False)
            self.items.blockSignals(False)
            return
        color = self.LEVEL_COLORS[c.level]
        self.cat_title.setText(f"{_esc(c.title)}  <span style='color:{color}; font-size:{pt(9.5)}; font-weight:400'>"
                               f"· {_esc(macstorage.level_label(c.level))}</span>")
        note = _esc(c.note)
        if c.key == "apps":
            note += "<br>" + _esc(tr(f"Зелёное можно удалить: остатки удалённых программ и то, что не открывали "
                                     f"больше {macapps.OLD_DAYS // 30} месяцев. Голубое используется, жёлтое проверьте сами.",
                                     f"Green can go: leftovers of removed apps and apps unused for over "
                                     f"{macapps.OLD_DAYS // 30} months. Blue is in use, yellow: check yourself."))
        if c.advice:
            note += "<br>→ " + _esc(c.advice)
        elif not c.deletable:
            note += "<br>" + _esc(tr("WhySlow это не удаляет.", "WhySlow doesn't delete this."))
        if c.denied and c.fda:
            note += f"<br><span style='color:{C['warn']}'>" + _esc(tr("Посчитано не всё: нет полного доступа к диску.",
                                                                      "Partly measured: no Full Disk Access.")) + "</span>"
        elif c.denied:
            note += "<br>" + _esc(tr("Часть файлов принадлежит системе, их размер не учтён.",
                                     "Some files belong to the system and are not counted."))
        self.cat_note.setText(note)
        self.items.setRowCount(len(c.items))
        for r, it in enumerate(c.items):
            name = QTableWidgetItem(it.name + (f"  ·  {it.note}" if it.note and it.note != it.name else ""))
            name.setToolTip("\n".join(it.targets))
            if c.deletable and it.deletable:
                name.setFlags(name.flags() | Qt.ItemIsUserCheckable)
                name.setCheckState(Qt.Checked if self.checked.get((c.key, it.path)) else Qt.Unchecked)
            size = NumItem(human_size(it.size) if c.size_known else "—", it.size)
            when = QTableWidgetItem(time.strftime("%d.%m.%Y", time.localtime(it.mtime)) if it.mtime else "")
            tag = QTableWidgetItem(it.tag)
            tag.setForeground(QColor(self.ITEM_COLORS.get(it.level, C["muted"])))
            if it.level == macapps.SYSTEM:
                name.setForeground(QColor(C["muted"]))
            for col, cell in enumerate([name, size, when, tag]):
                self.items.setItem(r, col, cell)
        if not c.items:
            self.items.setRowCount(1)
            empty = QTableWidgetItem(tr("Пусто", "Empty"))
            empty.setForeground(QColor(C["muted"]))
            self.items.setItem(0, 0, empty)
        for b in (self.b_all, self.b_none):
            b.setVisible(c.deletable and bool(c.items))
        self.b_all.setText(tr("Отметить остатки и давно не открытое", "Check leftovers and long-unused")
                           if c.key == "apps" else tr("Отметить всё", "Check all"))
        self.items.horizontalHeaderItem(2).setText(tr("Открывали", "Last opened") if c.key == "apps" else tr("Изменено", "Modified"))
        self.items.setColumnHidden(3, c.key != "apps")
        self.items.blockSignals(False)

    def on_check(self, cell: QTableWidgetItem):
        c = self.current()
        if not c or cell.column() != 0 or cell.row() >= len(c.items):
            return
        self.checked[(c.key, c.items[cell.row()].path)] = cell.checkState() == Qt.Checked
        self.update_count()

    def check_all(self, on: bool):
        c = self.current()
        if not c:
            return
        for it in c.items:
            # в данных программ «всё» — только остатки и давно не запускавшееся, без самих программ
            if c.key != "apps" or it.level in (macapps.OLD, macapps.LEFTOVER) or not on:
                self.checked[(c.key, it.path)] = on
        self.on_select()
        self.update_count()

    def chosen(self) -> list[tuple[macstorage.Category, macstorage.Item]]:
        return [(c, it) for c in self.cats if c.deletable for it in c.items
                if it.deletable and self.checked.get((c.key, it.path))]

    def update_count(self):
        chosen = self.chosen()
        size = sum(it.size for _c, it in chosen)
        self.b_del.setText(tr(f"Удалить выбранное ({len(chosen)}, {human_size(size)})",
                              f"Delete selected ({len(chosen)}, {human_size(size)})") if chosen
                           else tr("Удалить выбранное", "Delete selected"))
        self.b_del.setEnabled(bool(chosen) and not self.busy)
        MainWindow._repolish(self.b_del)

    # --- удаление ---

    def delete(self):
        chosen = self.chosen()
        if not chosen or self.busy:
            return
        lines, important = [], []
        for c in self.cats:
            mine = [it for cc, it in chosen if cc is c]
            if not mine:
                continue
            size = human_size(sum(it.size for it in mine)) if c.size_known else tr(f"{len(mine)} шт.", f"{len(mine)}")
            lines.append(f"• {c.title} ({macstorage.level_label(c.level).lower()}): {size}")
            risky = mine if c.level == macstorage.KEEP else [it for it in mine if it.level in self.RISKY]
            if risky:
                important.append(f"• {c.title}: " + ", ".join(f"{it.note or it.name}" + (f" ({it.tag.lower()})" if it.tag else "")
                                                           for it in risky[:5]))
        text = tr("Будет удалено:\n", "To be deleted:\n") + "\n".join(lines)
        if important:
            text += tr("\n\nЭто важные данные, проверьте ещё раз:\n", "\n\nThis is important data, double-check:\n") + "\n".join(important)
        methods = {c.method for c, _it in chosen}
        if "trash" in methods:
            text += tr("\n\nФайлы переместятся в Корзину. Вернуть их можно из Корзины или во вкладке «Журнал». "
                       "Место освободится, когда вы очистите Корзину. Закройте программы, чьи кеши удаляете.",
                       "\n\nFiles will be moved to the Trash. You can restore them from the Trash or the Journal tab. "
                       "The space is freed once you empty the Trash. Quit the apps whose caches you delete.")
        if "empty" in methods:
            text += tr("\n\nВыбранное из Корзины удалится навсегда.", "\n\nThe selected Trash items will be deleted forever.")
        if "snapshots" in methods:
            text += tr("\n\nСнимки Time Machine удалятся навсегда, macOS спросит пароль администратора. "
                       "Резервные копии на внешнем диске не пострадают.",
                       "\n\nTime Machine snapshots will be deleted forever; macOS will ask for an administrator password. "
                       "Backups on the external drive are not affected.")
        if not confirm(self, tr("Удалить выбранное?", "Delete the selected items?"), text,
                       danger=bool(important) or bool(methods - {"trash"})):
            return
        self.busy = True
        self.update_count()

        def done(r):
            ok, failed = r
            self.busy = False
            self.checked.clear()
            self.main.journal_tab.reload()
            msg = tr(f"Удалено: {ok}.", f"Deleted: {ok}.")
            if failed:
                msg += tr(f"\nНе получилось: {failed} (заняты, принадлежат системе или нет доступа).",
                          f"\nFailed: {failed} (in use, owned by the system, or no access).")
            QMessageBox.information(self, tr("Готово", "Done"), msg)
            self.scan()

        def failed(e):
            self.busy = False
            self.update_count()
            _show_error(e)

        run_async(lambda progress: macstorage.delete(chosen, progress), done, failed, self.state.setText)

    # --- автоочистка ---

    def toggle_auto(self):
        on = not settings.get("mac_autoclean")
        if on and not confirm(self, tr("Включить автоочистку?", "Turn on auto-clean?"),
                              macstorage.autoclean_text() + tr(
                                  "\n\nФайлы удаляются сразу, без Корзины, пока WhySlow запущен. "
                                  "Каждая очистка записывается в «Журнал». Выключить можно здесь же.",
                                  "\n\nFiles are deleted right away, without the Trash, while WhySlow is running. "
                                  "Each run is recorded in the Journal. You can turn it off here.")):
            return
        settings.set("mac_autoclean", on)
        self.sw_auto.setChecked(on)
        self.show_auto()
        if on:
            self.main.autoclean()

    def show_auto(self):
        on = settings.get("mac_autoclean")
        self.every.setEnabled(on)
        last = settings.get("mac_autoclean_last")
        text = macstorage.autoclean_text()
        if on:
            text += " " + (tr(f"Последний раз: {time.strftime('%d.%m.%Y %H:%M', time.localtime(last))}.",
                              f"Last run: {time.strftime('%Y-%m-%d %H:%M', time.localtime(last))}.") if last
                           else tr("Ещё не запускалась.", "Hasn't run yet."))
        else:
            text += " " + tr("Выключена.", "Off.")
        self.auto_state.setText(text)


# ------------------------------------------------------------ Оптимизация ---

class OptimizeTab(QWidget):
    """Одна кнопка: WhySlow сам подбирает, что исправить, и показывает план с галочками.
    Ничего не меняется, пока человек не нажмёт «Применить»."""

    FRESH = 120  # секунд: проверка свежее этого — план строится сразу, без новой проверки

    @staticmethod
    def kind_label(kind: str) -> str:
        return {"proxy": tr("Сеть", "Network"), "startup": tr("Автозагрузка", "Startup"),
                "process": tr("Фоновые программы", "Background apps"), "junk": tr("Мусор", "Junk")}[kind]

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.steps: list[optimize.Step] = []
        self.boxes: list[QCheckBox] = []
        self.wanted = False  # план просили кнопкой — показать его, как только закончится проверка
        self.busy = False
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))

        head = QHBoxLayout()
        head.setSpacing(px(14))
        text = QVBoxLayout()
        text.setSpacing(px(4))
        self.title = QLabel(accent(tr("Оптимизация в один клик", "One-click optimization")))
        self.title.setObjectName("h1")
        self.title.setWordWrap(True)
        text.addWidget(self.title)
        text.addWidget(muted(tr("WhySlow сам подберёт, что исправить: мусор, лишнее в автозагрузке по вердиктам базы, "
                                "зависший прокси, тяжёлые фоновые программы. Безопасное уже отмечено, сомнительное — "
                                "нет, а то, что база велит не трогать, не предлагается. Ничего не меняется, пока вы "
                                "не нажмёте «Применить».",
                                "WhySlow picks what to fix on its own: junk, unneeded startup apps per the database, "
                                "a stuck proxy, heavy background apps. Safe items are pre-checked, questionable ones "
                                "are not, and anything the database says to keep is never offered. Nothing changes "
                                "until you press “Apply”.")))
        head.addLayout(text, 1)
        self.b_go = QPushButton(tr("Оптимизировать", "Optimize"))
        self.b_go.setObjectName("primary")
        self.b_go.setCursor(Qt.PointingHandCursor)
        self.b_go.clicked.connect(self.start)
        head.addWidget(self.b_go, 0, Qt.AlignVCenter)
        lay.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list = QWidget()
        self.list_lay = QVBoxLayout(self.list)
        self.list_lay.setContentsMargins(0, 0, px(6), 0)
        self.list_lay.setSpacing(px(8))
        self.scroll.setWidget(self.list)
        lay.addWidget(self.scroll, 1)

        row = QHBoxLayout()
        self.state = muted("")
        row.addWidget(self.state, 1)
        self.b_apply = QPushButton(tr("Применить", "Apply"))
        self.b_apply.setObjectName("primary")
        self.b_apply.setCursor(Qt.PointingHandCursor)
        self.b_apply.clicked.connect(self.apply)
        row.addWidget(self.b_apply)
        lay.addLayout(row)
        self._message(tr("Нажмите «Оптимизировать» — план появится здесь.", "Press “Optimize” and the plan appears here."))

    # --- план ---

    def start(self):
        """Кнопка «Оптимизировать»: свежая проверка → план. Старый результат не используем."""
        r = self.main.scan_result
        if r and time.time() - self.main.scan_time < self.FRESH and self.main.b_scan.isEnabled():
            self.show_plan(r)
            return
        self.wanted = True
        self._message(tr("Проверяю компьютер, чтобы составить план…", "Checking your computer to build the plan…"))
        if self.main.b_scan.isEnabled():
            self.main.scan()

    def on_scan(self, r: checks.ScanResult | None):
        """Проверка закончилась (r=None — упала). План обновляется, если его ждали или он уже показан."""
        if self.busy:
            return
        if r is None:
            if self.wanted:
                self.wanted = False
                self._message(tr("Проверка не удалась — план не составлен.", "The scan failed, so there is no plan."))
            return
        if self.wanted or self.steps:
            self.wanted = False
            self.show_plan(r)

    def show_plan(self, r: checks.ScanResult):
        self.steps = optimize.plan(r)
        self._clear()
        self.boxes = []
        kind = None
        for s in self.steps:
            if s.kind != kind:
                kind = s.kind
                self.list_lay.addWidget(section(self.kind_label(kind)))
            self.list_lay.addWidget(self._row(s))
        tips = optimize.manual(r)
        if tips:
            self.list_lay.addWidget(section(tr("Сделать вручную", "Do it yourself")))
            for f in tips:
                card = QFrame()
                card.setObjectName("card")
                v = QVBoxLayout(card)
                v.setContentsMargins(px(16), px(10), px(14), px(10))
                v.setSpacing(px(2))
                t = QLabel(f.title)
                t.setWordWrap(True)
                t.setStyleSheet("font-weight: 700;")
                v.addWidget(t)
                v.addWidget(muted("→ " + f.advice))
                self.list_lay.addWidget(card)
        if not self.steps:
            self.list_lay.insertWidget(0, self._note(tr("Исправлять нечего — всё, что WhySlow умеет чинить сам, в порядке.",
                                                        "Nothing to fix: everything WhySlow can repair on its own is fine.")))
        self.list_lay.addStretch()
        self.update_count()

    def _row(self, s: optimize.Step) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        h = QHBoxLayout(card)
        h.setContentsMargins(px(14), px(10), px(14), px(10))
        h.setSpacing(px(12))
        cb = QCheckBox()
        cb.setChecked(s.checked)
        cb.toggled.connect(self.update_count)
        self.boxes.append(cb)
        h.addWidget(cb, 0, Qt.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(px(2))
        t = QLabel(s.title)
        t.setWordWrap(True)
        t.setStyleSheet("font-weight: 700;")
        v.addWidget(t)
        if s.detail:
            v.addWidget(muted(s.detail))
        h.addLayout(v, 1)
        tag = QLabel(tr("можно откатить", "can be undone") if s.undoable else tr("необратимо", "permanent"))
        tag.setStyleSheet(f"color: {C['ok'] if s.undoable else C['warn']}; font-size: {pt(9)};")
        h.addWidget(tag, 0, Qt.AlignTop)
        # Щелчок по строке — то же, что по галочке
        card.mousePressEvent = lambda _e, cb=cb: cb.isEnabled() and cb.toggle()
        return card

    def _note(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(f"font-size: {pt(11.5)};")
        return lbl

    def _message(self, text: str):
        self.steps, self.boxes = [], []
        self._clear()
        self.list_lay.addWidget(self._note(text))
        self.list_lay.addStretch()
        self.update_count()

    def _clear(self):
        while self.list_lay.count():
            w = self.list_lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def chosen(self) -> list[optimize.Step]:
        return [s for s, b in zip(self.steps, self.boxes) if b.isChecked()]

    def update_count(self):
        chosen = self.chosen()
        freed = sum(s.size for s in chosen)
        self.b_apply.setText(tr(f"Применить ({len(chosen)})", f"Apply ({len(chosen)})") if chosen else tr("Применить", "Apply"))
        self.b_apply.setEnabled(bool(chosen) and not self.busy)
        self.b_go.setEnabled(not self.busy)
        if self.steps:
            self.state.setText(tr(f"Отмечено {len(chosen)} из {len(self.steps)}", f"{len(chosen)} of {len(self.steps)} checked")
                               + (tr(f" · освободится до {human_size(freed)}", f" · up to {human_size(freed)} freed") if freed else ""))
        else:
            self.state.setText("")
        MainWindow._repolish(self.b_apply)

    # --- выполнение ---

    def apply(self):
        chosen = self.chosen()
        if not chosen or self.busy:
            return
        permanent = [s for s in chosen if not s.undoable]
        text = tr(f"Будет выполнено пунктов: {len(chosen)}.", f"{len(chosen)} items will be applied.")
        if permanent:
            text += tr("\n\nНельзя будет отменить:\n", "\n\nThese can't be undone:\n") + "\n".join(f"• {s.title}" for s in permanent)
        text += tr("\n\nОтключение автозагрузки и прокси можно откатить во вкладке «Журнал».",
                   "\n\nStartup and proxy changes can be undone in the Journal tab.")
        if not confirm(self, tr("Применить оптимизацию?", "Apply the optimization?"), text, danger=bool(permanent)):
            return
        self.busy = True
        for b in self.boxes:
            b.setEnabled(False)
        self.update_count()

        def done(outcomes):
            self.busy = False
            self.main.journal_tab.reload()
            self.main.status(tr("Оптимизация завершена", "Optimization finished"))
            QMessageBox.information(self, tr("Готово", "Done"), optimize.summary(outcomes))
            self.wanted = True  # показать, что осталось после исправлений
            if self.main.b_scan.isEnabled():
                self.main.scan()

        def failed(e):
            self.busy = False
            for b in self.boxes:
                b.setEnabled(True)
            self.update_count()
            _show_error(e)

        run_async(lambda progress: optimize.apply(chosen, progress), done, failed, self.state.setText)


# ---------------------------------------------------------------- Пароли ---

class PasswordsTab(QWidget):
    """Пароли из выгрузки менеджера паролей: какие нашлись в утечках и замена на новые.
    Новый пароль попадает в файл для импорта только после «Я сменил пароль на сайте»."""

    CLEAR_CLIPBOARD_MS = 60_000

    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        self.vault: passwords.Vault | None = None
        self.shown: list[passwords.Entry] = []
        self.saved: Path | None = None
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))

        head = QHBoxLayout()
        head.setSpacing(px(14))
        text = QVBoxLayout()
        text.setSpacing(px(4))
        self.title = QLabel(accent(tr("Пароли в утечках", "Leaked passwords")))
        self.title.setObjectName("h1")
        text.addWidget(self.title)
        text.addWidget(muted(tr(
            "Выгрузите пароли из менеджера в CSV и откройте файл здесь. WhySlow проверит каждый пароль по базе утечек "
            "Have I Been Pwned. Сами пароли не покидают компьютер: на сервер уходят только первые 5 символов "
            "их хеша SHA-1.",
            "Export your passwords from the password manager to CSV and open the file here. WhySlow checks every "
            "password against the Have I Been Pwned breach database. Passwords never leave your computer: only the "
            "first 5 characters of their SHA-1 hash are sent.")))
        how = muted(tr(
            "<b>Где взять файл:</b> Chrome и Edge — Настройки → Пароли → Экспорт; Firefox — about:logins → ⋯ → "
            "Экспорт; Safari и «Пароли» macOS — Файл → Экспортировать; Bitwarden, 1Password, KeePass, LastPass — "
            "экспорт в CSV.",
            "<b>Where to get the file:</b> Chrome and Edge — Settings → Passwords → Export; Firefox — about:logins → "
            "⋯ → Export; Safari and macOS Passwords — File → Export; Bitwarden, 1Password, KeePass, LastPass — "
            "CSV export."))
        how.setTextFormat(Qt.RichText)
        text.addWidget(how)
        head.addLayout(text, 1)
        self.b_open = QPushButton(tr("Открыть файл…", "Open file…"))
        self.b_open.setObjectName("primary")
        self.b_open.setCursor(Qt.PointingHandCursor)
        self.b_open.clicked.connect(self.open_file)
        head.addWidget(self.b_open, 0, Qt.AlignVCenter)
        lay.addLayout(head)

        body = QHBoxLayout()
        body.setSpacing(px(14))
        self.table = make_table([tr("Сайт", "Site"), tr("Логин", "Login"), tr("В утечках", "In breaches"),
                                 tr("Статус", "Status")])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeToContents)
        self.table.setWordWrap(False)
        self.table.itemSelectionChanged.connect(self.on_select)
        body.addWidget(self.table, 5)

        side = QFrame()
        side.setObjectName("card")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(px(14), px(12), px(14), px(12))
        sl.setSpacing(px(8))
        self.e_title = QLabel()
        self.e_title.setTextFormat(Qt.PlainText)
        self.e_title.setStyleSheet(f"font-size: {pt(12)}; font-weight: 600;")
        self.e_title.setWordWrap(True)
        sl.addWidget(self.e_title)
        self.e_note = muted()
        self.e_note.setTextFormat(Qt.RichText)
        sl.addWidget(self.e_note)
        self.new_pw = QLabel()
        self.new_pw.setTextFormat(Qt.PlainText)
        self.new_pw.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.new_pw.setStyleSheet(f"font-family: Consolas, Menlo, 'DejaVu Sans Mono', monospace; font-size: {pt(12)}; "
                                  f"padding: {px(8)}px; border: 1px solid {LINE2}; border-radius: {px(8)}px;")
        sl.addWidget(self.new_pw)
        self.cb_symbols = QCheckBox(tr("Со спецсимволами", "With symbols"))
        self.cb_symbols.setChecked(True)
        self.cb_symbols.toggled.connect(lambda _on: self.regenerate())
        sl.addWidget(self.cb_symbols)
        r1 = QHBoxLayout()
        self.b_regen = QPushButton(tr("Другой пароль", "Another one"))
        self.b_regen.clicked.connect(self.regenerate)
        self.b_copy = QPushButton(tr("Скопировать", "Copy"))
        self.b_copy.clicked.connect(self.copy)
        r1.addWidget(self.b_regen)
        r1.addWidget(self.b_copy)
        r1.addStretch()
        sl.addLayout(r1)
        self.b_site = QPushButton(tr("Открыть страницу смены пароля", "Open the password change page"))
        self.b_site.clicked.connect(self.open_site)
        sl.addWidget(self.b_site)
        self.b_done = QPushButton(tr("Я сменил пароль на сайте", "I changed it on the site"))
        self.b_done.setObjectName("primary")
        self.b_done.setCursor(Qt.PointingHandCursor)
        self.b_done.clicked.connect(self.mark_changed)
        sl.addWidget(self.b_done)
        sl.addStretch()
        body.addWidget(side, 3)
        lay.addLayout(body, 1)

        row = QHBoxLayout()
        self.state = muted()
        row.addWidget(self.state, 1)
        self.b_wipe = QPushButton(tr("Удалить файлы с паролями", "Delete password files"))
        self.b_wipe.clicked.connect(self.wipe)
        row.addWidget(self.b_wipe)
        self.b_save = QPushButton(tr("Сохранить файл для импорта", "Save file for import"))
        self.b_save.setObjectName("primary")
        self.b_save.setCursor(Qt.PointingHandCursor)
        self.b_save.clicked.connect(self.save)
        row.addWidget(self.b_save)
        lay.addLayout(row)
        self.refill()

    # --- загрузка и проверка ---

    def open_file(self):
        path, _ = QFileDialog.getOpenFileName(self, tr("Выгрузка паролей", "Password export"), str(Path.home()),
                                              tr("Файлы CSV (*.csv);;Все файлы (*)", "CSV files (*.csv);;All files (*)"))
        if path:
            self.load(path)

    def load(self, path: str):
        if self.vault and self.vault.changed and not confirm(
                self, tr("Открыть другой файл?", "Open another file?"),
                tr("Новые пароли, которые вы уже сменили на сайтах, ещё не сохранены в файл для импорта. "
                   "Открыть другой файл и потерять их?",
                   "New passwords you already changed on sites are not saved to an import file yet. "
                   "Open another file and lose them?"), danger=True):
            return
        try:
            vault = passwords.load(path)
        except passwords.VaultError:
            QMessageBox.warning(self, tr("Не получилось", "Failed"),
                                tr("Это не похоже на выгрузку менеджера паролей: в файле нет колонки с паролями.",
                                   "This doesn't look like a password manager export: there is no password column."))
            return
        self.vault, self.saved = vault, None
        self.refill()
        self.b_open.setEnabled(False)
        self.state.setText(tr("Проверяю пароли…", "Checking passwords…"))

        def done(_r):
            self.b_open.setEnabled(True)
            self.refill()

        def failed(_e):
            self.b_open.setEnabled(True)
            self.refill()
            QMessageBox.warning(self, tr("Не получилось", "Failed"),
                                tr("Не удалось связаться с базой утечек (api.pwnedpasswords.com). "
                                   "Проверьте подключение к интернету и откройте файл ещё раз.",
                                   "Could not reach the breach database (api.pwnedpasswords.com). "
                                   "Check your internet connection and open the file again."))

        run_async(lambda p: passwords.check(vault, p), done, failed,
                  lambda t: self.state.setText(tr(f"Проверяю пароли… {t}", f"Checking passwords… {t}")))

    def refill(self):
        v = self.vault
        entries = v.entries if v else []
        # Утёкшие сверху, самые частые первыми
        self.shown = sorted(entries, key=lambda e: (-(e.leaks or 0), e.site.lower()))
        self.table.setRowCount(len(self.shown))
        for r, e in enumerate(self.shown):
            if e.leaks is None:
                leaks, status, color = "…", tr("проверяется", "checking"), C["muted"]
            elif e.changed:
                leaks, status, color = f"{e.leaks:,}".replace(",", "\u202f"), tr("заменён", "replaced"), C["ok"]
            elif e.leaks:
                leaks, status, color = f"{e.leaks:,}".replace(",", "\u202f"), tr("утёк, сменить", "leaked, change it"), C["bad"]
            else:
                leaks, status, color = "0", tr("не найден", "not found"), C["muted"]
            st = QTableWidgetItem(status)
            st.setForeground(QColor(color))
            for c, it in enumerate([QTableWidgetItem(e.site), QTableWidgetItem(e.username), NumItem(leaks, e.leaks or 0), st]):
                self.table.setItem(r, c, it)
        checked = [e for e in entries if e.leaks is not None]
        if not v:
            self.title.setText(accent(tr("Пароли в утечках", "Leaked passwords")))
            self.state.setText("")
        elif len(checked) == len(entries):
            n = len(v.leaked)
            self.title.setText(accent(tr(f"В утечках: {n} из {len(entries)}", f"Leaked: {n} of {len(entries)}") if n
                                      else tr("Утёкших паролей нет", "No leaked passwords")))
            self.state.setText(tr(f"Файл: {v.path.name}. Заменено: {len(v.changed)} из {n}.",
                                  f"File: {v.path.name}. Replaced: {len(v.changed)} of {n}.") if n
                               else tr(f"Файл: {v.path.name}.", f"File: {v.path.name}."))
        self.b_save.setEnabled(bool(v and v.changed))
        self.b_wipe.setEnabled(bool(v))
        self.on_select()

    # --- одна запись ---

    def current(self) -> passwords.Entry | None:
        rows = self.table.selectionModel().selectedRows()
        return self.shown[rows[0].row()] if rows else None

    def on_select(self):
        e = self.current()
        actionable = bool(e and e.leaked)
        for w in (self.new_pw, self.cb_symbols, self.b_regen, self.b_copy, self.b_site, self.b_done):
            w.setVisible(actionable)
        if not e:
            self.e_title.setText(tr("Выберите пароль в списке", "Pick a password in the list") if self.vault else "")
            self.e_note.setText("")
            return
        self.e_title.setText(e.site + (f" · {e.username}" if e.username else ""))
        if not e.leaked:
            self.e_note.setText(tr("Этот пароль не найден в известных утечках.", "This password is not in known breaches.")
                                if e.leaks == 0 else "")
            return
        if not e.new_password:
            e.new_password = passwords.generate(symbols=self.cb_symbols.isChecked())
        self.new_pw.setText(e.new_password)
        if e.changed:
            self.e_note.setText(tr("Пароль сменён на сайте. Новый пароль попадёт в файл для импорта.",
                                   "Changed on the site. The new password goes into the import file."))
        else:
            n = f"{e.leaks:,}".replace(",", "\u202f")
            self.e_note.setText(tr(
                f"Пароль встречается в утечках <b>{n}</b> раз — его знают те, кто подбирает пароли.<br><br>"
                "1. Скопируйте новый пароль.<br>2. Откройте сайт и смените пароль там.<br>"
                "3. Нажмите «Я сменил пароль на сайте».<br><br>"
                "В менеджере пароль поменяется, когда вы импортируете файл. Пока пароль не сменён на сайте, "
                "менять его в менеджере нельзя: вы не сможете войти.",
                f"This password appears in breaches <b>{n}</b> times — attackers try it first.<br><br>"
                "1. Copy the new password.<br>2. Open the site and change the password there.<br>"
                "3. Press “I changed it on the site”.<br><br>"
                "The manager is updated when you import the file. Until the site has the new password, "
                "the manager must keep the old one, or you won't be able to sign in."))
        for w in (self.cb_symbols, self.b_regen, self.b_done):
            w.setEnabled(not e.changed)
        self.b_site.setEnabled(bool(passwords.change_url(e.url)))

    def regenerate(self):
        e = self.current()
        if e and e.leaked and not e.changed:
            e.new_password = passwords.generate(symbols=self.cb_symbols.isChecked())
            self.on_select()

    def copy(self):
        e = self.current()
        if not (e and e.new_password):
            return
        cb = QApplication.clipboard()
        pw = e.new_password
        cb.setText(pw)
        # Через минуту убираем пароль из буфера обмена, если там всё ещё он
        QTimer.singleShot(self.CLEAR_CLIPBOARD_MS, lambda: cb.text() == pw and cb.clear())
        self.main.status(tr("Новый пароль скопирован, через минуту буфер обмена очистится",
                            "New password copied; the clipboard is cleared in a minute"))

    def open_site(self):
        e = self.current()
        url = passwords.change_url(e.url) if e else ""
        if url:
            webbrowser.open(url)

    def mark_changed(self):
        e = self.current()
        if not (e and e.leaked) or e.changed:
            return
        if not confirm(self, tr("Пароль сменён на сайте?", "Changed on the site?"),
                       tr(f"Вы сменили пароль на {e.site} на новый, показанный в WhySlow, и вошли с ним?\n\n"
                          "Он будет записан в файл для импорта в менеджер паролей.",
                          f"Did you change the password on {e.site} to the new one shown in WhySlow and sign in with it?\n\n"
                          "It will be written to the file for importing into your password manager.")):
            return
        e.changed = True
        self.refill()

    # --- файлы ---

    def save(self):
        v = self.vault
        if not (v and v.changed):
            return
        default = v.path.with_name(v.path.stem + "-new.csv")
        path, _ = QFileDialog.getSaveFileName(self, tr("Файл для импорта", "Import file"), str(default),
                                              tr("Файлы CSV (*.csv)", "CSV files (*.csv)"))
        if not path:
            return
        try:
            n = passwords.export(v, path)
        except OSError as ex:
            QMessageBox.warning(self, tr("Не получилось", "Failed"), str(ex))
            return
        self.saved = Path(path)
        QMessageBox.information(self, tr("Файл сохранён", "File saved"), tr(
            f"В файле {n} новых паролей. Импортируйте его в тот же менеджер паролей, из которого делали выгрузку: "
            "он заменит старые пароли у этих сайтов. Если менеджер создаст копии записей, удалите старые.\n\n"
            "После импорта удалите оба файла кнопкой «Удалить файлы с паролями»: в них пароли открытым текстом.",
            f"The file has {n} new passwords. Import it into the same password manager you exported from: "
            "it replaces the old passwords for these sites. If the manager creates duplicate entries, delete the old ones.\n\n"
            "After importing, delete both files with “Delete password files”: they hold passwords in plain text."))

    def wipe(self):
        v = self.vault
        if not v:
            return
        files = [p for p in (v.path, self.saved) if p and p.exists()]
        unsaved = bool(v.changed) and not self.saved
        text = "\n".join(f"• {p}" for p in files) or tr("(файлов уже нет)", "(the files are already gone)")
        if not confirm(self, tr("Удалить файлы с паролями?", "Delete password files?"),
                       tr(f"Будут удалены без Корзины:\n{text}\n\n", f"These will be deleted, bypassing the Trash:\n{text}\n\n")
                       + (tr("Новые пароли ещё не сохранены в файл для импорта — они пропадут.\n\n",
                             "New passwords are not saved to an import file yet — they will be lost.\n\n") if unsaved else "")
                       + tr("Сначала убедитесь, что импортировали новые пароли в менеджер.",
                            "Make sure you have imported the new passwords into your manager first."), danger=True):
            return
        left = [p for p in files if not passwords.wipe(p)]
        if left:
            QMessageBox.warning(self, tr("Не получилось", "Failed"),
                                tr("Не удалось удалить:\n", "Could not delete:\n") + "\n".join(str(p) for p in left))
        self.vault, self.saved = None, None
        self.refill()


# ----------------------------------------------------------------- Журнал ---

class JournalTab(QWidget):
    def __init__(self, main: "MainWindow"):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))
        lay.addWidget(muted(tr("Всё, что WhySlow поменял в системе. Отключение автозагрузки, удаление записей, "
                               "выключение прокси и скачанный LibreHardwareMonitor можно откатить.",
                               "Everything WhySlow has changed on your system. Disabled startup items, removed entries, "
                               "proxy changes and the downloaded LibreHardwareMonitor can be undone.")
                            + (tr(" Перемещённое в Корзину из «Хранилища» возвращается на место.",
                                  " Items moved to the Trash from Storage are put back.") if IS_MAC else "")))
        self.table = make_table([tr("Когда", "When"), tr("Что сделано", "What was done"), tr("Статус", "Status")])
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        hh.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.itemSelectionChanged.connect(self.on_select)
        lay.addWidget(self.table, 1)
        row = QHBoxLayout()
        self.b_undo = QPushButton(tr("Откатить", "Undo"))
        self.b_undo.clicked.connect(self.undo)
        row.addWidget(self.b_undo)
        row.addStretch()
        lay.addLayout(row)
        self.reload()

    def reload(self):
        self.entries = list(reversed(journal.load()))
        self.table.setRowCount(len(self.entries))
        for r, e in enumerate(self.entries):
            status = (tr("откачено", "undone") if e["undone"]
                      else tr("можно откатить", "can be undone") if e["undoable"] else tr("необратимо", "permanent"))
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
        if not confirm(self, tr("Откатить?", "Undo?"), tr(f"Вернуть как было:\n{e['title']}", f"Restore the previous state:\n{e['title']}")):
            return
        try:
            if e["kind"].startswith("startup_"):
                startup.undo(e)
            elif e["kind"] == "proxy_off":
                network.undo(e)
            elif e["kind"] == "update":
                updater.undo(e)
            elif e["kind"] == "lhm":
                lhm.uninstall()
            elif e["kind"] == "macdata_trash":
                macstorage.undo(e)
        except Exception as ex:
            QMessageBox.warning(self, tr("Не получилось", "Failed"), str(ex))
            return
        journal.mark_undone(e["id"])
        self.main.status(tr("Откачено: ", "Undone: ") + e["title"])
        self.reload()
        self.main.startup_tab.reload()
        if e["kind"] == "update":
            self.main.offer_restart(tr(f"Вернул версию {e['data']['old_version']}.",
                                       f"Restored version {e['data']['old_version']}."))


class UpdateDialog(QDialog):
    """Проверка и установка новых версий, настройки автообновления."""

    def __init__(self, main: "MainWindow"):
        super().__init__(main)
        self.main = main
        self.rel: updater.Release | None = main.new_release
        self.setWindowTitle(tr("Обновления WhySlow", "WhySlow updates"))
        self.setMinimumWidth(px(560))
        self.setStyleSheet(f"QDialog {{ background: {C['panel']}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))
        head = QLabel(tr(f"Установлена версия {__version__}", f"Installed version: {__version__}"))
        head.setStyleSheet(f"font-size: {pt(13)}; font-weight: 700;")
        lay.addWidget(head)
        lay.addWidget(muted(updater.mode_hint()))
        self.state = QLabel()
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        self.notes = QTextEdit()
        self.notes.setReadOnly(True)
        self.notes.setMinimumHeight(px(140))
        lay.addWidget(self.notes)

        self.c_check = QCheckBox(tr("Проверять новые версии при запуске", "Check for new versions at startup"))
        self.c_check.setChecked(settings.get("check_updates"))
        self.c_check.toggled.connect(lambda v: settings.set("check_updates", v))
        self.c_auto = QCheckBox(tr("Устанавливать обновления автоматически", "Install updates automatically"))
        self.c_auto.setChecked(settings.get("auto_update"))
        self.c_auto.toggled.connect(lambda v: settings.set("auto_update", v))
        self.c_auto.setEnabled(updater.mode() != "manual")
        lay.addWidget(self.c_check)
        lay.addWidget(self.c_auto)
        lay.addWidget(muted(tr("Неудачное обновление откатывается само. Удачное можно откатить во вкладке «Журнал».",
                             "A failed update rolls back on its own. A successful one can be undone in the Journal tab.")))

        row = QHBoxLayout()
        self.b_check = QPushButton(tr("Проверить сейчас", "Check now"))
        self.b_check.clicked.connect(self.check)
        row.addWidget(self.b_check)
        self.b_page = QPushButton(tr("Страница релизов", "Releases page"))
        self.b_page.setObjectName("link")
        self.b_page.clicked.connect(lambda: webbrowser.open(f"https://github.com/{updater.REPO}/releases"))
        row.addWidget(self.b_page)
        row.addStretch()
        self.b_install = QPushButton(tr("Установить", "Install"))
        self.b_install.setObjectName("primary")
        self.b_install.clicked.connect(self.install)
        row.addWidget(self.b_install)
        lay.addLayout(row)
        self.show_release()
        if not self.rel:
            self.check()

    def show_release(self, msg: str = ""):
        rel = self.rel
        self.b_install.setVisible(bool(rel) and updater.mode() != "manual")
        if rel:
            self.b_install.setText(tr(f"Установить {rel.version}", f"Install {rel.version}"))
            self.state.setText(msg or tr(f"Доступна новая версия {rel.version}.", f"New version {rel.version} is available."))
            self.state.setStyleSheet(f"color: {C['ok']}; font-weight: 600;")
        else:
            self.state.setText(msg)
            self.state.setStyleSheet("")
        self.notes.setVisible(bool(rel and rel.notes))
        self.notes.setPlainText(rel.notes if rel else "")

    def check(self):
        self.b_check.setEnabled(False)
        self.state.setText(tr("Проверяю…", "Checking…"))

        def done(r):
            self.b_check.setEnabled(True)
            self.rel, msg = r
            self.main.set_release(self.rel)
            self.show_release(msg)

        def failed(e):
            self.b_check.setEnabled(True)
            self.show_release(e)

        run_async(lambda _p: updater.check(), done, failed)

    def install(self):
        rel = self.rel
        if not rel or not confirm(self, tr("Обновить WhySlow?", "Update WhySlow?"),
                                  tr(f"Установить версию {rel.version} вместо {__version__}?\n"
                                     "Если новая версия не запустится, старая вернётся сама.",
                                     f"Install version {rel.version} in place of {__version__}?\n"
                                     "If the new version fails to start, the old one comes back automatically.")):
            return
        self.b_install.setEnabled(False)
        self.b_check.setEnabled(False)

        def done(msg):
            self.main.journal_tab.reload()
            self.main.set_release(None)
            self.accept()
            self.main.offer_restart(msg)

        def failed(e):
            self.b_install.setEnabled(True)
            self.b_check.setEnabled(True)
            self.state.setText(e)
            self.state.setStyleSheet(f"color: {C['bad']};")

        run_async(lambda progress: updater.install(rel, progress), done, failed, self.state.setText)


# ---------------------------------------------------------- Главное окно ---

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(tr(f"WhySlow {__version__} — почему тормозит компьютер",
                               f"WhySlow {__version__} — why is my computer slow"))
        avail = QApplication.primaryScreen().availableGeometry()
        self.resize(min(1360, int(avail.width() * 0.92)), min(900, int(avail.height() * 0.92)))
        self.kb = knowledge.load()
        self.scan_result: checks.ScanResult | None = None
        self.scan_time = 0.0
        self.hist = {k: deque(maxlen=60) for k in ("cpu", "ram", "disk_r", "disk_w", "net_rx", "net_tx")}

        root = MeshBackground()
        self.setCentralWidget(root)
        lay = QVBoxLayout(root)
        lay.setContentsMargins(px(20), px(14), px(20), px(8))
        lay.setSpacing(px(12))

        header = QHBoxLayout()
        header.setSpacing(px(12))
        logo = QLabel(accent("Why Slow").replace("</span> ", "</span>"))
        logo.setStyleSheet(f"font-size: {pt(22)}; font-weight: 600; color: white;")
        logo.setToolTip(tr("объясняет, почему тормозит, и помогает это исправить",
                           "explains why your computer is slow and helps fix it"))
        header.addWidget(logo)
        header.addStretch(1)
        # Вкладки — «пилюлями» в шапке, как навигация в образце; сами страницы ниже, в QTabWidget
        nav = QFrame()
        nav.setObjectName("nav")
        nl = QHBoxLayout(nav)
        nl.setContentsMargins(px(3), 0, px(3), 0)
        self.nav = QTabBar()
        self.nav.setDrawBase(False)
        self.nav.setExpanding(False)
        self.nav.setCursor(Qt.PointingHandCursor)
        nl.addWidget(self.nav)
        header.addWidget(nav)
        header.addStretch(1)
        self.lang_box = QComboBox()
        for code, name in LANGS.items():
            self.lang_box.addItem(flag_icon(code), name, code)
        self.lang_box.setIconSize(QSize(px(21), px(14)))
        self.lang_box.setCurrentIndex(max(self.lang_box.findData(lang()), 0))
        self.lang_box.setToolTip(tr("Язык интерфейса", "Interface language"))
        self.lang_box.currentIndexChanged.connect(self.change_lang)
        header.addWidget(self.lang_box)
        self.b_scan = QPushButton(tr("Проверить", "Scan"))
        self.b_scan.setObjectName("primary")
        self.b_scan.setCursor(Qt.PointingHandCursor)
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
        self.optimize_tab = OptimizeTab(self)
        self.passwords_tab = PasswordsTab(self)
        self.storage_tab = StorageTab(self) if IS_MAC else None  # «Системные данные» есть только в macOS
        self.tab_keys = {"overview": self.overview, "optimize": self.optimize_tab, "system": self.system_tab,
                         "startup": self.startup_tab, "procs": self.procs_tab, "junk": self.junk_tab,
                         "passwords": self.passwords_tab, "journal": self.journal_tab}
        pages = [(self.overview, tr("Обзор", "Overview")), (self.optimize_tab, tr("Оптимизация", "Optimize")),
                 (self.system_tab, tr("Система", "System")),
                 (self.startup_tab, tr("Автозагрузка", "Startup")), (self.procs_tab, tr("Процессы", "Processes")),
                 (self.junk_tab, tr("Очистка", "Cleanup")),
                 (self.passwords_tab, tr("Пароли", "Passwords")), (self.journal_tab, tr("Журнал", "Journal"))]
        if self.storage_tab:
            self.tab_keys["storage"] = self.storage_tab
            pages.insert(6, (self.storage_tab, tr("Хранилище", "Storage")))
        for w, title in pages:
            self.tabs.addTab(w, title)
            self.nav.addTab(title)
        self.tabs.tabBar().hide()
        self.nav.currentChanged.connect(self.tabs.setCurrentIndex)
        self.tabs.currentChanged.connect(self.nav.setCurrentIndex)
        self.tabs.currentChanged.connect(self.on_tab)
        lay.addWidget(self.tabs, 1)

        sb = self.statusBar()
        self.db_label = QLabel()
        sb.addPermanentWidget(self.db_label)
        b_db = QPushButton(tr("Обновить базу", "Update database"))
        b_db.clicked.connect(lambda: self.update_db(manual=True))
        sb.addPermanentWidget(b_db)
        self.new_release: updater.Release | None = None
        self.b_update = QPushButton(tr("Обновления", "Updates"))
        self.b_update.clicked.connect(lambda: UpdateDialog(self).exec())
        sb.addPermanentWidget(self.b_update)
        if IS_WINDOWS and not is_admin():
            b_admin = QPushButton(tr("Перезапустить от администратора", "Restart as administrator"))
            b_admin.clicked.connect(self.restart_as_admin)
            sb.addPermanentWidget(b_admin)
        self.show_db()
        self.sampler = Sampler(lambda: self.kb)
        self.sampler.stats.connect(self.on_stats)
        self.sampler.procs.connect(self.on_procs)
        self.sampler.start()
        QTimer.singleShot(200, self.update_db)
        QTimer.singleShot(400, self.scan)
        QTimer.singleShot(1500, self.check_updates)
        if IS_MAC:  # автоочистка по расписанию: проверяем при запуске и раз в час
            QTimer.singleShot(30000, self.autoclean_if_due)
            self.auto_timer = QTimer(self)
            self.auto_timer.timeout.connect(self.autoclean_if_due)
            self.auto_timer.start(3600 * 1000)

    def on_stats(self, s: dict):
        for k in ("disk_r", "disk_w", "net_rx", "net_tx"):
            self.hist[k].append(s[k])
        self.hist["cpu"].append(s["cpu"])
        self.hist["ram"].append(s["ram"][2])
        self.overview.on_stats(s, self.hist)
        self.overview.hero.calm = s["cpu"] >= 70
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
        self.db_label.setText(tr(f"База: v{self.kb.version} ({self.kb.source}), {len(self.kb.entries)} записей",
                                 f"Database: v{self.kb.version} ({self.kb.source}), {len(self.kb.entries)} entries"))

    def update_db(self, manual=False):
        def done(r):
            self.kb, msg = r
            self.show_db()
            if manual:
                self.status(msg)
        run_async(lambda _p: knowledge.update(), done)

    def check_updates(self):
        """Тихая проверка при запуске. Автоустановка — только если её включили в «Обновлениях»."""
        cfg = settings.load()
        if not (cfg["check_updates"] or cfg["auto_update"]):
            return

        def found(r):
            rel, _ = r
            self.set_release(rel)
            if rel and cfg["auto_update"] and updater.mode() != "manual":
                self.status(tr(f"Устанавливаю обновление {rel.version}…", f"Installing update {rel.version}…"))
                run_async(lambda _p: updater.install(rel), installed, lambda e: self.status(e.splitlines()[0]))

        def installed(msg):
            self.set_release(None)
            self.journal_tab.reload()
            self.b_update.setText(tr("Перезапустить", "Restart"))
            self.b_update.setObjectName("update")
            self._repolish(self.b_update)
            self.b_update.clicked.disconnect()
            self.b_update.clicked.connect(self.restart)
            self.status(msg)

        run_async(lambda _p: updater.check(timeout=5), found, lambda _e: None)

    def set_release(self, rel):
        self.new_release = rel
        self.b_update.setText(tr(f"Обновить до {rel.version}", f"Update to {rel.version}") if rel else tr("Обновления", "Updates"))
        self.b_update.setObjectName("update" if rel else "")
        self._repolish(self.b_update)

    @staticmethod
    def _repolish(w):
        """Подхватить стиль после смены objectName и пересчитать ширину."""
        w.style().unpolish(w)
        w.style().polish(w)
        w.setMinimumWidth(w.sizeHint().width())
        w.updateGeometry()

    def offer_restart(self, msg: str):
        box = QMessageBox(self)
        box.setWindowTitle(tr("Готово", "Done"))
        box.setText(msg + tr("\nПерезапустить WhySlow сейчас?", "\nRestart WhySlow now?"))
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
        box.button(QMessageBox.Yes).setText(tr("Перезапустить", "Restart"))
        box.button(QMessageBox.No).setText(tr("Позже", "Later"))
        if box.exec() == QMessageBox.Yes:
            self.restart()

    def restart(self):
        import subprocess
        flags = 0x00000008 if IS_WINDOWS else 0  # DETACHED_PROCESS: без лишнего окна консоли
        try:
            subprocess.Popen(updater.restart_command(), cwd=updater.restart_cwd(), creationflags=flags,
                             start_new_session=not IS_WINDOWS, env=updater.child_env())
        except OSError as e:
            _show_error(tr(f"Не удалось перезапустить: {e}. Запустите WhySlow заново вручную.",
                           f"Couldn't restart: {e}. Please start WhySlow again manually."))
            return
        self.close()
        QApplication.quit()

    def change_lang(self, _i):
        code = self.lang_box.currentData()
        if code and code != lang():
            set_lang(code)
            self.restart()

    def go(self, key: str):
        self.tabs.setCurrentWidget(self.tab_keys[key])

    def on_tab(self, _i):
        cur = self.tabs.currentWidget()
        if cur is self.system_tab:
            self.system_tab.activate()
        elif cur is self.procs_tab:
            self.procs_tab.refresh()
        elif cur is self.storage_tab:
            self.storage_tab.activate()

    def autoclean_if_due(self):
        if macstorage.autoclean_due():
            self.autoclean()

    def autoclean(self):
        """Автоочистка «Системных данных» в фоне; итог — в строке состояния и в «Журнале»."""
        if getattr(self, "_autocleaning", False):
            return
        self._autocleaning = True

        def done(r):
            self._autocleaning = False
            self.status(tr(f"Автоочистка: освобождено {human_size(r[0])}", f"Auto-clean: freed {human_size(r[0])}"))
            self.journal_tab.reload()
            if self.storage_tab:
                self.storage_tab.show_auto()

        def failed(e):
            self._autocleaning = False
            self.status(tr(f"Автоочистка не удалась: {e}", f"Auto-clean failed: {e}"))

        run_async(macstorage.autoclean, done, failed)

    def scan(self):
        self.b_scan.setEnabled(False)
        self.b_scan.setText(tr("Проверяю…", "Scanning…"))
        self.overview.show_progress(tr("Начинаю…", "Starting…"))

        def done(r: checks.ScanResult):
            self.scan_result = r
            self.scan_time = time.time()
            self.overview.show_result(r)
            self.startup_tab.set_items(r.startup)
            self.junk_tab.set_cats(r.junk)
            self.b_scan.setEnabled(True)
            self.b_scan.setText(tr("Проверить снова", "Scan again"))
            self.optimize_tab.on_scan(r)

        def failed(e):
            self.b_scan.setEnabled(True)
            self.b_scan.setText(tr("Проверить", "Scan"))
            self.optimize_tab.on_scan(None)
            _show_error(tr(f"Проверка упала: {e}", f"Scan failed: {e}"))

        run_async(lambda progress: checks.scan(self.kb, progress), done, failed, self.overview.show_progress)

    def optimize(self):
        self.go("optimize")
        self.optimize_tab.start()

    def fix_proxy(self):
        st = self.scan_result.proxy if self.scan_result else None
        if not st or not confirm(self, tr("Выключить прокси?", "Turn off proxy?"),
                                 tr(f"Системный прокси {st.server} будет выключен.\n"
                                    "Если интернет пропал из-за VPN — это вернёт его.\n"
                                    "Откатить можно во вкладке «Журнал».",
                                    f"The system proxy {st.server} will be turned off.\n"
                                    "If your internet stopped working because of a VPN, this will bring it back.\n"
                                    "You can undo this in the Journal tab.")):
            return
        try:
            network.disable_proxy(st)
        except Exception as e:
            _show_error(str(e))
            return
        self.journal_tab.reload()
        self.status(tr("Прокси выключен", "Proxy turned off"))
        self.scan()

    def restart_as_admin(self):
        import ctypes
        args = " ".join(f'"{a}"' for a in ["-m", "whyslow"]) if not updater.FROZEN else ""
        os.environ.update(updater.child_env())  # новый WhySlow.exe не должен брать наши временные файлы
        if ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, args, None, 1) > 32:
            QApplication.quit()


def main() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("WhySlow")
    app.setWindowIcon(QIcon(str(Path(__file__).resolve().parent / "data" / "icon.png")))
    updater.cleanup()
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    f = app.font()
    f.setPointSizeF(10 * SCALE)
    app.setFont(f)
    app.setStyleSheet(build_style())
    w = MainWindow()
    w.show()
    return app.exec()
