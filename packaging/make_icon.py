"""Рисует иконку приложения в стиле интерфейса: packaging/icon.png (1024×1024, для сборок)
и whyslow/data/icon.png (256×256, для окна). Иконка —
тёмный фон с фиолетовым свечением, радужное кольцо как у значка на «Обзоре»
и стрелка спидометра, опущенная влево, — «медленно».

Запуск: python packaging/make_icon.py (нужен PySide6). Готовая иконка лежит в
репозитории, перерисовывать её нужно, только если меняется дизайн.
"""

import math
import os
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import (QColor, QConicalGradient, QGuiApplication, QImage, QPainter, QPainterPath, QPen,
                           QRadialGradient)

S = 1024


def squircle(rect: QRectF) -> QPainterPath:
    """Скруглённый квадрат как у иконок macOS (суперэллипс n=5)."""
    path = QPainterPath()
    cx, cy, a = rect.center().x(), rect.center().y(), rect.width() / 2
    n = 5
    for i in range(361):
        t = math.radians(i)
        c, s = math.cos(t), math.sin(t)
        x = cx + a * math.copysign(abs(c) ** (2 / n), c)
        y = cy + a * math.copysign(abs(s) ** (2 / n), s)
        path.moveTo(x, y) if i == 0 else path.lineTo(x, y)
    path.closeSubpath()
    return path


def draw() -> QImage:
    img = QImage(S, S, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)

    # Поля 10 %, как в шаблоне иконок macOS: на Windows и Linux тоже смотрится аккуратно
    body = QRectF(S * 0.1, S * 0.1, S * 0.8, S * 0.8)
    shape = squircle(body)
    p.fillPath(shape, QColor("#07060d"))
    p.save()
    p.setClipPath(shape)
    for (x, y, r, color) in ((0.25, 0.2, 0.75, "#7c3aed"), (0.85, 0.35, 0.55, "#c026d3"),
                             (0.6, 0.95, 0.6, "#4c1d95")):
        g = QRadialGradient(QPointF(S * x, S * y), S * r)
        c = QColor(color)
        c.setAlpha(170)
        g.setColorAt(0, c)
        c.setAlpha(0)
        g.setColorAt(1, c)
        p.fillRect(body, g)
    p.restore()
    p.setPen(QPen(QColor(255, 255, 255, 40), S * 0.004))
    p.drawPath(shape)

    # Радужное кольцо
    c = QPointF(S / 2, S / 2)
    ring_r = S * 0.25
    ring = QConicalGradient(c, 90)
    for pos, color in ((0, "#2ee59d"), (0.2, "#4cc9f0"), (0.45, "#8b5cf6"), (0.7, "#e77edc"),
                       (0.88, "#ff4d6d"), (1, "#2ee59d")):
        ring.setColorAt(pos, QColor(color))
    p.setPen(QPen(ring, S * 0.06, Qt.SolidLine, Qt.RoundCap))
    p.setBrush(Qt.NoBrush)
    p.drawEllipse(c, ring_r, ring_r)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#0d0a1c"))
    p.drawEllipse(c, ring_r - S * 0.03, ring_r - S * 0.03)

    # Деления шкалы
    inner = ring_r - S * 0.03
    for i in range(9):
        a = math.radians(225 - i * 270 / 8)
        r1, r2 = inner * 0.78, inner * 0.9
        p.setPen(QPen(QColor(255, 255, 255, 110 if i % 2 else 200), S * 0.012, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(c.x() + r1 * math.cos(a), c.y() - r1 * math.sin(a)),
                   QPointF(c.x() + r2 * math.cos(a), c.y() - r2 * math.sin(a)))

    # Стрелка у самого начала шкалы — компьютер еле едет
    a = math.radians(200)
    tip = QPointF(c.x() + inner * 0.7 * math.cos(a), c.y() - inner * 0.7 * math.sin(a))
    p.setPen(QPen(QColor("#f5f3ff"), S * 0.028, Qt.SolidLine, Qt.RoundCap))
    p.drawLine(c, tip)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor("#f5f3ff"))
    p.drawEllipse(c, S * 0.035, S * 0.035)
    p.setBrush(QColor("#8b5cf6"))
    p.drawEllipse(c, S * 0.015, S * 0.015)
    p.end()
    return img


if __name__ == "__main__":
    app = QGuiApplication(sys.argv)
    here = Path(__file__).resolve().parent
    img = draw()
    img.save(str(here / "icon.png"))
    img.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(str(here.parent / "whyslow" / "data" / "icon.png"))
