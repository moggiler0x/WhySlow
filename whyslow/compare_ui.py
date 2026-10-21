"""Вкладка «Сравнение»: ваш ПК против похожих. Платная — за замком подписки."""

from __future__ import annotations

import json
import time
from html import escape as _esc

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel, QPushButton, QTableWidgetItem, QTextEdit,
    QVBoxLayout, QWidget,
)

from . import checks, compare, settings, sysinfo
from .gui import C, _show_error, confirm, make_table, muted, pt, px, run_async, section
from .i18n import tr


def _gate(build) -> QWidget:
    """Замок подписки. Если модуля подписки ещё нет — просто надпись, что функция платная."""
    try:
        from .license_ui import PremiumGate  # type: ignore[import-not-found]
    except ImportError:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.addStretch()
        lbl = muted(tr("Сравнение с похожими ПК доступно по подписке.",
                       "Comparison with similar PCs is part of the subscription."))
        lbl.setAlignment(Qt.AlignCenter)
        lay.addWidget(lbl)
        lay.addStretch()
        return w
    return PremiumGate(compare.FEATURE, build)


class CompareTab(QWidget):
    def __init__(self, main):
        super().__init__()
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(_gate(lambda: ComparePage(main)))


class ConsentDialog(QDialog):
    """Показывает ровно то, что уйдёт на сервер, и спрашивает согласие."""

    def __init__(self, parent, snap: compare.Snapshot):
        super().__init__(parent)
        self.setWindowTitle(tr("Что отправится на сервер", "What will be sent"))
        self.resize(px(620), px(560))
        lay = QVBoxLayout(self)
        lay.addWidget(muted(tr(
            "Чтобы сравнить ваш ПК с похожими, WhySlow отправит этот анонимный снимок. В нём нет имени, "
            "имени компьютера, путей к файлам и названий ваших личных программ: программы передаются только "
            "как записи из открытой базы WhySlow, остальные — лишь количеством. Ключ подписки сервер проверяет, "
            "но рядом со снимком не хранит, IP-адрес тоже не сохраняется. Снимок можно удалить с сервера в любой момент.",
            "To compare your PC with similar ones, WhySlow will send this anonymous snapshot. It has no name, "
            "computer name, file paths or names of your personal programs: programs are sent only as entries of "
            "WhySlow's open database, others only as a count. The server checks the subscription key but doesn't "
            "store it next to the snapshot, and your IP address isn't saved either. You can delete the snapshot "
            "from the server at any time.")))
        text = QTextEdit()
        text.setReadOnly(True)
        text.setPlainText(json.dumps(snap.to_json(), ensure_ascii=False, indent=2))
        lay.addWidget(text, 1)
        self.agree = QCheckBox(tr("Согласен отправлять этот снимок для сравнения", "I agree to send this snapshot for comparison"))
        lay.addWidget(self.agree)
        bb = QDialogButtonBox()
        self.b_ok = bb.addButton(tr("Отправить и сравнить", "Send and compare"), QDialogButtonBox.AcceptRole)
        self.b_ok.setObjectName("primary")
        self.b_ok.setEnabled(False)
        bb.addButton(tr("Отмена", "Cancel"), QDialogButtonBox.RejectRole)
        self.agree.toggled.connect(self.b_ok.setEnabled)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)


class ComparePage(QWidget):
    def __init__(self, main):
        super().__init__()
        self.main = main
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))
        lay.addWidget(muted(tr(
            "WhySlow сравнивает ваш компьютер с другими пользователями на таком же железе: сколько грузится "
            "система, насколько она занята в простое, сколько всего в автозагрузке. И показывает, какие программы "
            "есть у вас, но редко встречаются у быстрых похожих ПК.",
            "WhySlow compares your computer with other users on the same hardware: how long the system takes "
            "to boot, how busy it is when idle, how much is in startup. And it shows which programs you have "
            "that are rare on fast similar PCs.")))
        row = QHBoxLayout()
        self.b_run = QPushButton(tr("Сравнить", "Compare"))
        self.b_run.setObjectName("primary")
        self.b_run.setCursor(Qt.PointingHandCursor)
        self.b_run.clicked.connect(self.run)
        row.addWidget(self.b_run)
        self.progress = muted("")
        row.addWidget(self.progress, 1)
        lay.addLayout(row)

        self.headline = QLabel()
        self.headline.setWordWrap(True)
        self.headline.setStyleSheet(f"font-size: {pt(15)}; font-weight: 600; color: white;")
        lay.addWidget(self.headline)
        self.level = muted("")
        lay.addWidget(self.level)
        self.table = make_table([tr("Показатель", "Metric"), tr("У вас", "Yours"), tr("Обычно у похожих", "Typical"),
                                 tr("У быстрых", "Fast ones"), tr("Итог", "Verdict")])
        hh = self.table.horizontalHeader()
        for i in range(4):
            hh.setSectionResizeMode(i, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.Stretch)
        lay.addWidget(self.table, 1)
        lay.addWidget(section(tr("Чем ваш ПК отличается от быстрых", "What sets your PC apart from fast ones")))
        self.suspects = muted("")
        self.suspects.setTextFormat(Qt.RichText)
        lay.addWidget(self.suspects)

        foot = QHBoxLayout()
        self.b_startup = QPushButton(tr("Открыть автозагрузку", "Open startup"))
        self.b_startup.clicked.connect(lambda: self.main.go("startup"))
        foot.addWidget(self.b_startup)
        foot.addStretch()
        self.b_forget = QPushButton(tr("Удалить мои данные с сервера", "Delete my data from the server"))
        self.b_forget.clicked.connect(self.forget)
        foot.addWidget(self.b_forget)
        lay.addLayout(foot)
        self.show_last()

    def show_last(self):
        snap, data, when = compare.last()
        self.b_forget.setEnabled(bool(settings.get("compare_id")))
        self.b_startup.setVisible(False)
        if snap and data:
            self.show_result(snap, data, when)
        else:
            self.headline.setText(tr("Нажмите «Сравнить», чтобы узнать, как ваш ПК выглядит на фоне таких же.",
                                     "Press Compare to see how your PC stacks up against similar ones."))

    def run(self):
        self.b_run.setEnabled(False)
        main = self.main

        def work(progress):
            r = main.scan_result or checks.scan(main.kb, progress)
            progress(tr("Собираю сведения о железе…", "Collecting hardware details…"))
            return r, compare.collect(r, sysinfo.cpu_name(), sysinfo.gpus(), progress)

        def collected(res):
            r, snap = res
            if main.scan_result is None:
                main.scan_result = r
            if not settings.get("compare_consent"):
                dlg = ConsentDialog(self, snap)
                if dlg.exec() != QDialog.Accepted:
                    self._idle()
                    return
                settings.set("compare_consent", True)
            self.progress.setText(tr("Сравниваю с похожими ПК…", "Comparing with similar PCs…"))
            run_async(lambda _p: compare.compare(snap), lambda data: (self._idle(), self.show_result(snap, data, time.time())),
                      self._failed)

        run_async(work, collected, self._failed, self.progress.setText)

    def _idle(self):
        self.b_run.setEnabled(True)
        self.progress.setText("")
        self.b_forget.setEnabled(bool(settings.get("compare_id")))

    def _failed(self, e: str):
        self._idle()
        _show_error(e)

    def show_result(self, snap: compare.Snapshot, data: dict, when: float):
        rep = compare.explain_result(snap, data, self.main.kb)
        stamp = time.strftime("%d.%m.%Y %H:%M", time.localtime(when))
        if rep is None:
            self.headline.setText(tr("Похожих ПК пока слишком мало для честного сравнения.",
                                     "There aren't enough similar PCs yet for a fair comparison."))
            self.level.setText(tr(f"Ваш снимок сохранён ({stamp}): как только наберётся хотя бы {compare.MIN_PEERS} "
                                  "похожих компьютеров, сравнение заработает.",
                                  f"Your snapshot is saved ({stamp}): once there are at least {compare.MIN_PEERS} "
                                  "similar computers, the comparison will work."))
            self.table.setRowCount(0)
            self.suspects.setText("")
            return
        self.headline.setText(rep.headline)
        self.level.setText(tr(f"Сравнение с: {rep.level_text}. Снимок от {stamp}.",
                              f"Compared with: {rep.level_text}. Snapshot from {stamp}."))
        rows = [r for r in rep.rows if r.mine is not None or r.median is not None]
        self.table.setRowCount(len(rows))
        for i, r in enumerate(rows):
            color = C["muted"]
            if r.diff_pct is not None and abs(r.diff_pct) >= 10:
                color = C["bad"] if r.diff_pct >= 20 else C["warn"] if r.diff_pct > 0 else C["ok"]
            cells = [r.title, *(compare._fmt(v, r.unit) if isinstance(v, (int, float)) else "—"
                                for v in (r.mine, r.median, r.best)), r.text()]
            for c, text in enumerate(cells):
                it = QTableWidgetItem(text)
                if c == 4:
                    it.setForeground(self._qcolor(color))
                self.table.setItem(i, c, it)
        if rep.suspects:
            lines = []
            for s in rep.suspects:
                share = tr(f"есть только у {s.share_fast * 100:.0f}% быстрых похожих ПК",
                           f"only {s.share_fast * 100:.0f}% of fast similar PCs have it")
                note = f" — {_esc(s.note)}" if s.note else ""
                lines.append(f"<b>{_esc(s.title)}</b>: {share}{note}")
            self.suspects.setText("<br>".join(lines))
            self.b_startup.setVisible(True)
        else:
            self.suspects.setText(tr("Заметных отличий в программах нет.", "No notable differences in programs."))
            self.b_startup.setVisible(False)

    @staticmethod
    def _qcolor(hex_color: str):
        from PySide6.QtGui import QColor
        return QColor(hex_color)

    def forget(self):
        if not confirm(self, tr("Удалить данные?", "Delete data?"),
                       tr("Снимок этого компьютера будет удалён с сервера сравнения, согласие — отозвано.",
                          "This computer's snapshot will be deleted from the comparison server and consent withdrawn.")):
            return
        run_async(lambda _p: compare.forget(),
                  lambda _r: (self.main.status(tr("Данные удалены с сервера", "Data deleted from the server")),
                              self.table.setRowCount(0), self.suspects.setText(""), self.level.setText(""),
                              self.show_last()),
                  _show_error)
