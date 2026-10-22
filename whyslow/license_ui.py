"""Окно «Подписка» и замок на платных вкладках.

    from .license_ui import PremiumGate, show_license_dialog
    tabs.addTab(PremiumGate("compare", lambda: CompareTab(main)), "Сравнение")

PremiumGate показывает замок с кнопкой «Ввести ключ», пока подписки нет, а после активации
сам строит вкладку и подменяет ею замок (и наоборот, когда подписка закончилась).
"""

from __future__ import annotations

import webbrowser
from typing import Callable

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication, QDialog, QFrame, QHBoxLayout, QLabel, QLineEdit, QPushButton, QStackedWidget, QVBoxLayout, QWidget,
)

from . import license
from .i18n import tr


def _g():
    from . import gui  # gui импортирует этот модуль, поэтому стили берём при вызове
    return gui


class _Bus(QObject):
    changed = Signal()


_bus: _Bus | None = None


def bus() -> _Bus:
    """Сигнал changed — из потока интерфейса, когда подписка включилась или выключилась."""
    global _bus
    if _bus is None:
        _bus = _Bus(QApplication.instance())
        license.on_change(_bus.changed.emit)  # из чужого потока сигнал придёт через очередь
        timer = QTimer(_bus)
        timer.timeout.connect(license.tick)         # заметить, что срок вышел
        timer.timeout.connect(license.refresh_async)  # и продлить онлайн-ключ раз в сутки
        timer.start(10 * 60 * 1000)
        license.tick()
        license.refresh_async()
    return _bus


class LicenseDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        g = _g()
        C, px, pt = g.C, g.px, g.pt
        self.setWindowTitle(tr("Подписка WhySlow", "WhySlow subscription"))
        self.setMinimumWidth(px(600))
        self.setStyleSheet(f"QDialog {{ background: {C['panel']}; }}")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))

        self.head = QLabel()
        self.head.setStyleSheet(f"font-size: {pt(14)}; font-weight: 700;")
        lay.addWidget(self.head)
        self.state = QLabel()
        self.state.setWordWrap(True)
        lay.addWidget(self.state)

        plans = QHBoxLayout()
        for code, p in license.PLANS.items():
            box = QFrame()
            box.setStyleSheet(f"QFrame {{ background: {g.GLASS2}; border: 1px solid {g.LINE}; border-radius: {px(10)}px; }}"
                              "QLabel { background: transparent; border: none; }")
            bl = QVBoxLayout(box)
            name = QLabel(tr(*p["name"]))
            name.setStyleSheet(f"font-weight: 700; font-size: {pt(12)};")
            price = QLabel(tr(f"{p['price']} ⭐ в месяц", f"{p['price']} ⭐ per month"))
            price.setStyleSheet(f"color: {C['accent2']}; font-size: {pt(13)}; font-weight: 600;")
            bl.addWidget(name)
            bl.addWidget(price)
            bl.addWidget(g.muted(tr("1 компьютер", "1 computer") if p["devices"] == 1 else
                                 tr(f"до {p['devices']} компьютеров", f"up to {p['devices']} computers")))
            plans.addWidget(box)
        lay.addLayout(plans)
        lay.addWidget(g.muted(tr("Оплата звёздами Telegram в боте @WhySlowPCbot (кнопка «Купить подписку») — код придёт сразу.",
                                 "Pay with Telegram Stars in the @WhySlowPCbot bot (the Buy button) — the code arrives at once.")))

        feats = ", ".join(tr(*v) for v in license.FEATURES.values())
        lay.addWidget(g.muted(tr(f"Подписка открывает: {feats}.", f"The subscription unlocks: {feats}.")))

        dev = QHBoxLayout()
        dev.addWidget(QLabel(tr("Код этого компьютера:", "This computer's code:")))
        self.dev = QLabel(license.device_id())
        self.dev.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.dev.setStyleSheet(f"font-family: monospace; font-weight: 700; color: {C['accent2']};")
        dev.addWidget(self.dev)
        b_copy = QPushButton(tr("Копировать", "Copy"))
        b_copy.clicked.connect(lambda: QApplication.clipboard().setText(license.device_id()))
        dev.addWidget(b_copy)
        dev.addStretch()
        lay.addLayout(dev)
        lay.addWidget(g.muted(tr("Если ключ выдают вручную, сообщите этот код продавцу: ключ будет работать только здесь.",
                                 "If the key is issued by hand, send this code to the seller: the key will only work here.")))

        row = QHBoxLayout()
        self.edit = QLineEdit()
        self.edit.setPlaceholderText(tr("Ключ: WS-XXXX-XXXX-XXXX-XXXX или WS1....", "Key: WS-XXXX-XXXX-XXXX-XXXX or WS1...."))
        self.edit.returnPressed.connect(self.activate)
        row.addWidget(self.edit, 1)
        self.b_act = QPushButton(tr("Активировать", "Activate"))
        self.b_act.setObjectName("primary")
        self.b_act.clicked.connect(self.activate)
        row.addWidget(self.b_act)
        lay.addLayout(row)
        self.msg = QLabel()
        self.msg.setWordWrap(True)
        lay.addWidget(self.msg)

        bottom = QHBoxLayout()
        b_buy = QPushButton(tr("Купить подписку", "Buy a subscription"))
        b_buy.setObjectName("link")
        b_buy.clicked.connect(lambda: webbrowser.open(license.PREMIUM_URL))
        bottom.addWidget(b_buy)
        bottom.addStretch()
        self.b_off = QPushButton(tr("Отключить на этом компьютере", "Turn off on this computer"))
        self.b_off.clicked.connect(self.deactivate)
        bottom.addWidget(self.b_off)
        b_close = QPushButton(tr("Закрыть", "Close"))
        b_close.clicked.connect(self.accept)
        bottom.addWidget(b_close)
        lay.addLayout(bottom)
        self.refill()

    def refill(self):
        C = _g().C
        st = license.status()
        self.head.setText(license.status_text(st))
        self.head.setStyleSheet(self.head.styleSheet() + f"color: {C['ok'] if st.active else C['text']};")
        if st.active:
            how = tr("ключ проверяется на сервере раз в сутки и работает без интернета до недели",
                     "the key is checked online once a day and works offline for up to a week") if st.online else \
                tr("ключ привязан к этому компьютеру", "the key is bound to this computer")
            self.state.setText(tr(f"Все платные функции открыты, {how}.", f"All paid features are unlocked, {how}."))
        else:
            self.state.setText(st.error or tr("Купите подписку и введите полученный ключ ниже.",
                                              "Buy a subscription and enter the key you received below."))
        self.b_off.setVisible(bool(license._load().get("token")))

    def _say(self, text: str, ok: bool):
        C = _g().C
        self.msg.setStyleSheet(f"color: {C['ok'] if ok else C['bad']};")
        self.msg.setText(text)

    def activate(self):
        text = self.edit.text()
        self.b_act.setEnabled(False)
        self._say(tr("Проверяю ключ…", "Checking the key…"), True)

        def done(st):
            self.b_act.setEnabled(True)
            self.edit.clear()
            self._say(tr("Готово! ", "Done! ") + license.status_text(st), True)
            self.refill()

        def failed(msg):
            self.b_act.setEnabled(True)
            self._say(msg or tr("ошибка", "error"), False)

        _g().run_async(lambda _p: license.enter_key(text), done, failed)

    def deactivate(self):
        if not _g().confirm(self, tr("Отключить подписку", "Turn off the subscription"),
                            tr("Убрать ключ с этого компьютера? Его можно будет ввести на другом.",
                               "Remove the key from this computer? You can then enter it on another one.")):
            return
        _g().run_async(lambda _p: license.deactivate(), lambda _r: (self._say(tr("Ключ убран.", "The key was removed."), True),
                                                       self.refill()), lambda m: self._say(m, False))


def show_license_dialog(parent=None) -> None:
    bus()
    LicenseDialog(parent).exec()


class PremiumGate(QStackedWidget):
    """Платная вкладка: замок без подписки, сама вкладка с подпиской."""

    def __init__(self, feature: str, build: Callable[[], QWidget] | None = None, parent=None, *, main=None):
        super().__init__(parent)
        # build задаёт, как построить вкладку. Если не задан — грузим защищённый модуль
        # whyslow/premium/<feature>.py (в релизе зашифрованный), ему нужен main.
        if build is None:
            from . import premium
            build = lambda: premium.widget(feature, main)  # noqa: E731
        self.feature, self.build, self.content = feature, build, None
        self.lock = self._lock_page()
        self.addWidget(self.lock)
        bus().changed.connect(self.refresh)
        self.refresh()

    def _lock_page(self) -> QWidget:
        g = _g()
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addStretch(1)
        name = license.FEATURES.get(self.feature)
        title = QLabel("🔒 " + (tr(*name) if name else tr("Платная функция", "Paid feature")))
        title.setAlignment(Qt.AlignCenter)
        title.setStyleSheet(f"font-size: {g.pt(18)}; font-weight: 700;")
        lay.addWidget(title)
        prices = " · ".join(f"{tr(*p['name'])} — {p['price']} ⭐/{tr('мес', 'mo')}" for p in license.PLANS.values())
        note = g.muted(tr(f"Доступно по подписке: {prices}", f"Available with a subscription: {prices}"))
        note.setAlignment(Qt.AlignCenter)
        lay.addWidget(note)
        row = QHBoxLayout()
        row.addStretch(1)
        b = QPushButton(tr("Ввести ключ", "Enter a key"))
        b.setObjectName("primary")
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(lambda: show_license_dialog(self.window()))
        row.addWidget(b)
        b_buy = QPushButton(tr("Купить", "Buy"))
        b_buy.setObjectName("link")
        b_buy.clicked.connect(lambda: webbrowser.open(license.PREMIUM_URL))
        row.addWidget(b_buy)
        row.addStretch(1)
        lay.addLayout(row)
        lay.addStretch(2)
        return w

    def refresh(self):
        # Вкладку строим, только если подписка открыта И модуль удалось расшифровать
        # (ключ функции есть). Без ключа build() бросит premium.PremiumLocked — остаётся замок.
        if license.has_premium(self.feature) and self.content is None:
            try:
                self.content = self.build()
                self.addWidget(self.content)
            except Exception:
                import traceback
                traceback.print_exc()
                self.content = None
        self.setCurrentWidget(self.content if self.content is not None else self.lock)


class PremiumButton(QPushButton):
    """Кнопка в шапке: «Подписка» или срок действующей."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setCursor(Qt.PointingHandCursor)
        self.clicked.connect(lambda: show_license_dialog(self.window()))
        bus().changed.connect(self.refresh)
        self.refresh()

    def refresh(self):
        st = license.status()
        self.setText("★ " + (st.plan_name if st.active else tr("Подписка", "Subscription")))
        self.setToolTip(license.status_text(st))
