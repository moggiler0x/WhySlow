"""Здоровье компьютера в деньгах: что может сломаться, насколько это вероятно
и сколько стоит заняться этим сейчас против того, сколько обойдётся потом.

«Диск с признаками износа: сделать копию — 0 ₽, восстановить данные после отказа —
15 000–50 000 ₽». Вероятности и цены лежат в hardware.json (раздел health) и
обновляются с GitHub вместе с ним. Вероятности для исправных дисков — годовая доля
отказов по большим выборкам; для изношенных — грубая оценка, и это сказано в тексте.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import tr
from .twin import money, price
from .util import IS_MAC, IS_WINDOWS, run

OK, WARN, BAD = "ok", "warn", "bad"
SAVER_CYCLES = 20  # циклов зарядки в месяц у типичного ноутбука — если система не говорит, сколько их было


@dataclass
class Risk:
    id: str
    title: str
    level: str                      # ok / warn / bad
    state: str                      # что сейчас
    chance: float                   # вероятность беды за 12 месяцев
    when: str                       # когда ждать
    now: tuple[int, int]            # сколько стоит заняться сейчас
    now_what: str
    later: tuple[int, int]          # сколько обойдётся, если случится
    later_what: str
    advice: str = ""
    urgent: bool = True             # False — ждать можно, дороже не станет
    backup: tuple[int, int] = (0, 0)  # часть now, которая — резервная копия (одна на все диски)

    @property
    def expected(self) -> float:
        """Ожидаемые потери за год: вероятность × средняя цена беды."""
        return self.chance * sum(self.later) / 2

    @property
    def act_now(self) -> bool:
        """Стоит заняться сейчас: деталь в плохом состоянии или защита почти ничего не стоит (копия за 0 ₽)."""
        return self.urgent and (self.level != OK or self.now[0] == 0 < self.now[1])


@dataclass
class Report:
    risks: list[Risk] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def todo(self) -> list[Risk]:
        return [r for r in self.risks if r.act_now]

    def now_total(self) -> tuple[int, int]:
        """Сколько стоит сделать всё нужное. Резервная копия одна на все диски — считаем её один раз."""
        todo = self.todo()
        lo = sum(r.now[0] - r.backup[0] for r in todo) + max((r.backup[0] for r in todo), default=0)
        hi = sum(r.now[1] - r.backup[1] for r in todo) + max((r.backup[1] for r in todo), default=0)
        return lo, hi

    def expected_total(self) -> float:
        return sum(r.expected for r in self.risks)

    def worst_total(self) -> int:
        """Худший случай за год: откажет самый «дорогой» из дисков и случится всё остальное."""
        todo = self.todo()
        disks = [r.later[1] for r in todo if r.id.startswith("disk:")]
        return max(disks, default=0) + sum(r.later[1] for r in todo if not r.id.startswith("disk:"))


def _p(db: dict, key: str) -> tuple[int, int]:
    return price(db.get("health", {}).get("prices", {}).get(key, {}))


def _add(*ranges: tuple[int, int]) -> tuple[int, int]:
    return sum(r[0] for r in ranges), sum(r[1] for r in ranges)


# ---------------------------------------------------------------- диски ---

def _disk(d, backup: bool | None, db: dict) -> Risk:
    afr = db.get("health", {}).get("afr", {})
    health = d.health or ""
    kind = d.kind or "hdd"
    chance = afr.get(health) if health in (WARN, BAD) else afr.get(kind, 0.02)
    level = health if health in (WARN, BAD) else OK
    where = tr("системный диск", "system disk") if d.system else tr("диск", "disk")
    title = f"{d.name} ({where}{', ' + d.kind_label if d.kind_label else ''})"
    if level == BAD:
        state = tr("Диск сам сообщает о неисправности", "The disk itself reports a failure")
        when = tr("может отказать в любой день — счёт идёт на недели", "may fail any day — think weeks, not months")
    elif level == WARN:
        state = tr("Есть признаки износа", "Showing signs of wear")
        when = tr("такие диски обычно отказывают в ближайшие месяцы, в среднем — примерно через полгода",
                  "disks like this usually fail within months, on average in about half a year")
    else:
        state = tr("Исправен", "Healthy") if health == OK else tr("Диск не сообщает о здоровье, считаю исправным",
                                                                  "The disk doesn't report its health; assuming healthy")
        when = tr(f"обычный фон: около {chance * 100:.0f}% дисков такого типа отказывают за год",
                  f"normal background: about {chance * 100:.0f}% of disks like this fail per year")

    backup_now = _p(db, "backup_free") if backup else (_p(db, "backup_free")[0], _p(db, "backup_drive")[1])
    replace = _p(db, "ssd")
    if level == OK:
        now, now_what = backup_now, (tr("резервная копия уже есть", "a backup already exists") if backup else
                                     tr("резервная копия: бесплатно встроенными средствами или внешний диск",
                                        "a backup: free with built-in tools, or an external drive"))
    else:
        now = _add(backup_now, replace)
        now_what = tr("скопировать данные сейчас и заменить диск на новый SSD",
                      "copy the data now and replace the disk with a new SSD")
    if backup:
        later, later_what = replace, tr("только новый диск: данные есть в копии", "only a new disk: the data is in the backup")
    else:
        later = _add(_p(db, "recovery"), replace)
        later_what = tr("восстановление данных в сервисе (и то не всегда удаётся) плюс новый диск",
                        "data recovery at a service (not always possible) plus a new disk")

    if level == BAD:
        advice = tr("Сегодня же скопируйте важное на другой диск или в облако и меньше пользуйтесь этим диском до замены.",
                    "Copy what matters to another disk or the cloud today and use this disk as little as possible until it's replaced.")
    elif level == WARN:
        advice = tr("Сделайте резервную копию на этой неделе и запланируйте замену диска.",
                    "Make a backup this week and plan to replace the disk.")
    elif not backup:
        advice = tr("Диск исправен, но копии важных файлов всё равно стоит держать: это бесплатно, а отказ дисков "
                    "случается без предупреждения.",
                    "The disk is healthy, but keeping a backup is still worth it: it's free, and disks can fail "
                    "without warning.")
    else:
        advice = ""
    return Risk(f"disk:{d.name}", title, level, state, chance, when, now, now_what, later, later_what, advice,
                backup=backup_now)


# ----------------------------------------------------------- аккумулятор ---

def _battery(ps, mac: bool, db: dict) -> Risk | None:
    if not ps.battery or ps.health is None:
        return None
    h = ps.health
    cond = (ps.condition or "").lower()
    service = any(w in cond for w in ("service", "replace", "poor"))
    cost = _p(db, "battery_mac" if mac else "battery")
    title = tr("Аккумулятор", "Battery")
    state = tr(f"Держит {h:.0f}% от новой ёмкости", f"Holds {h:.0f}% of its original capacity")
    if ps.cycles:
        state += tr(f", {ps.cycles} циклов зарядки", f", {ps.cycles} charge cycles")
    if h < 60 or service:
        return Risk("battery", title, BAD, state + (f" ({ps.condition})" if ps.condition else ""), 0.2,
                    tr("износ сильный: такие аккумуляторы начинают вздуваться", "heavily worn: batteries like this start to swell"),
                    cost, tr("заменить аккумулятор", "replace the battery"),
                    _add(cost, _p(db, "swollen")),
                    tr("вздувшийся аккумулятор ломает корпус, тачпад или экран", "a swollen battery damages the case, trackpad or screen"),
                    tr("Замените аккумулятор в ближайшее время. Если корпус начал выгибаться или тачпад "
                       "плохо нажимается — перестаньте заряжать ноутбук до замены.",
                       "Replace the battery soon. If the case is bulging or the trackpad is hard to click, "
                       "stop charging the laptop until it's replaced."))
    months = None
    if ps.cycles and h < 100:
        per_cycle = (100 - h) / ps.cycles
        months = (h - 70) / per_cycle / SAVER_CYCLES if per_cycle > 0 else None
    if h < 80:
        when = tr(f"до 70% — примерно {months:.0f} мес." if months else "заряда уже заметно меньше",
                  f"down to 70% in about {months:.0f} months" if months else "it already holds noticeably less")
        return Risk("battery", title, WARN, state, 0.5, when, cost, tr("заменить аккумулятор", "replace the battery"),
                    cost, tr("та же замена, но позже; до неё — работа ближе к розетке",
                             "the same replacement later; until then, staying near an outlet"),
                    tr("Срочности нет: замена не подорожает от того, что вы подождёте. Меняйте, когда заряда "
                       "перестанет хватать.",
                       "No rush: the replacement won't get pricier if you wait. Replace it when the charge "
                       "no longer lasts long enough."), urgent=False)
    when = (tr(f"до 70% — примерно через {months / 12:.0f} г." if months and months >= 18 else
               f"до 70% — примерно {months:.0f} мес." if months else "аккумулятор в хорошем состоянии",
               f"down to 70% in about {months / 12:.0f} years" if months and months >= 18 else
               f"down to 70% in about {months:.0f} months" if months else "the battery is in good shape"))
    return Risk("battery", title, OK, state, 0.03, when, (0, 0), tr("ничего не нужно", "nothing needed"),
                cost, tr("замена аккумулятора когда-нибудь потом", "a battery replacement some day"))


# -------------------------------------------------------------- перегрев ---

def _heat(temps: dict, db: dict) -> Risk | None:
    vals = [v for k, v in temps.items() if not re.search(r"disk|nvme|ssd|hdd|drive|battery", k, re.I)]
    if not vals:
        return None
    t = max(vals)
    cost_now, cost_later = _p(db, "cleaning"), _p(db, "overheat_repair")
    state = tr(f"Самый горячий датчик: {t:.0f} °C", f"Hottest sensor: {t:.0f} °C")
    if t >= 95:
        return Risk("heat", tr("Перегрев", "Overheating"), BAD, state, 0.15,
                    tr("при такой температуре железо сбрасывает частоты, а пайка и чипы изнашиваются быстрее",
                       "at this temperature the hardware throttles, and solder and chips wear out faster"),
                    cost_now, tr("чистка от пыли и замена термопасты", "dust cleaning and new thermal paste"),
                    cost_later, tr("ремонт платы или видеокарты", "motherboard or graphics card repair"),
                    tr("Почистите компьютер от пыли и замените термопасту. Пока — не закрывайте решётки "
                       "охлаждения и не держите ноутбук на мягком.",
                       "Clean out the dust and replace the thermal paste. Meanwhile, keep the vents clear "
                       "and don't use the laptop on soft surfaces."))
    if t >= 85:
        return Risk("heat", tr("Нагрев", "Heat"), WARN, state, 0.05,
                    tr("горячо, но пока в пределах нормы; со временем пыль сделает хуже",
                       "hot but still within limits; dust will make it worse over time"),
                    cost_now, tr("чистка от пыли и замена термопасты", "dust cleaning and new thermal paste"),
                    cost_later, tr("ремонт платы или видеокарты", "motherboard or graphics card repair"),
                    tr("Если компьютеру больше двух лет и его не чистили — самое время.",
                       "If the computer is over two years old and has never been cleaned, now is a good time."))
    return None


# ------------------------------------------------------- резервные копии ---

def backup_status() -> tuple[bool | None, str]:
    """Есть ли резервная копия: True / False / None (не знаю) и что нашлось."""
    try:
        if IS_MAC:
            code, out = run(["tmutil", "latestbackup"], timeout=10)
            m = re.search(r"(\d{4})-(\d{2})-(\d{2})", out) if code == 0 else None
            if m:
                age = (time.time() - time.mktime(time.strptime(m.group(0), "%Y-%m-%d"))) / 86400
                if age <= 30:
                    return True, tr(f"Time Machine: последняя копия {m.group(0)}", f"Time Machine: last backup {m.group(0)}")
                return False, tr(f"Time Machine: последняя копия была {m.group(0)}, больше месяца назад",
                                 f"Time Machine: the last backup was {m.group(0)}, over a month ago")
            code, out = run(["tmutil", "destinationinfo"], timeout=10)
            if code == 0 and "Name" in out:
                return None, tr("Time Machine настроена, но копий пока нет", "Time Machine is set up but has no backups yet")
            return False, tr("Time Machine не настроена", "Time Machine isn't set up")
        if IS_WINDOWS:
            import winreg
            key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
                docs = str(winreg.QueryValueEx(k, "Personal")[0])
            if "onedrive" in docs.lower():
                return True, tr("Документы хранятся в OneDrive", "Documents are kept in OneDrive")
            cfg = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "FileHistory" / "Configuration"
            if any(cfg.glob("Config*.xml")):
                return True, tr("Включена «История файлов»", "File History is on")
            return None, tr("Встроенных резервных копий (OneDrive, «История файлов») не нашлось",
                            "No built-in backups (OneDrive, File History) were found")
    except Exception:  # noqa: BLE001 — нет ответа, значит не знаем
        pass
    return None, ""


# ---------------------------------------------------------------- сборка ---

def assess(disks: list, ps, temps: dict, backup: bool | None, db: dict, mac: bool = IS_MAC) -> Report:
    """Чистая функция: из собранных сведений — список рисков с ценами."""
    rep = Report()
    for d in disks:
        rep.risks.append(_disk(d, backup, db))
    b = _battery(ps, mac, db) if ps is not None else None
    if b:
        rep.risks.append(b)
    h = _heat(temps or {}, db)
    if h:
        rep.risks.append(h)
    if not disks:
        rep.notes.append(tr("Сведений о дисках получить не удалось.", "Couldn't get information about the disks."))
    order = {BAD: 0, WARN: 1, OK: 2}
    rep.risks.sort(key=lambda r: (order.get(r.level, 3), -r.expected))
    return rep


def collect(db: dict) -> Report:
    """Медленно (PowerShell, system_profiler) — звать в фоне."""
    from . import diskhealth, power, sysinfo
    try:
        temps = sysinfo.temperatures(wait=True)
    except Exception:  # noqa: BLE001 — датчики не обязательны
        temps = {}
    backup, about = backup_status()
    rep = assess(diskhealth.physical(), power.state(), temps, backup, db)
    if about:
        rep.notes.insert(0, about)
    if not temps:
        rep.notes.append(tr("Температуры недоступны — перегрев не проверен.", "Temperatures aren't available — overheating wasn't checked."))
    return rep


def headline(rep: Report) -> str:
    todo = rep.todo()
    if not todo:
        return tr("Срочно тратиться не на что: всё исправно.", "Nothing needs money right now: everything is healthy.")
    lo, hi = rep.now_total()
    return tr(f"Сделать всё нужное сейчас — {money(lo, hi)}. Если ничего не делать, ожидаемые потери за год "
              f"≈ {money(rep.expected_total())}, в худшем случае — до {money(rep.worst_total())}.",
              f"Doing what's needed now costs {money(lo, hi)}. Doing nothing means expected losses of about "
              f"{money(rep.expected_total())} a year, up to {money(rep.worst_total())} in the worst case.")


def chance_text(p: float) -> str:
    return f"{p * 100:.0f}%" if p >= 0.01 else "<1%"


def disclaimer() -> str:
    return tr("Вероятности — по статистике отказов, а не предсказание для конкретной детали. Цены примерные, "
              "для сервисов — диапазон по крупным городам.",
              "Chances come from failure statistics, not a prediction for this exact part. Prices are rough; "
              "service prices are a typical range for large cities.")
