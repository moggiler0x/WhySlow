"""Диагностика: собирает всё в список понятных человеку «находок»."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import psutil

from . import junk, lhm, network, startup, sysinfo
from .knowledge import KnowledgeBase
from .processes import ProcessMonitor
from .util import IS_LINUX, IS_MAC, IS_WINDOWS, by_os, human_duration, human_size, run

OK, INFO, WARN, BAD = "ok", "info", "warn", "bad"


@dataclass
class Finding:
    level: str
    title: str
    detail: str = ""
    advice: str = ""
    tab: str = ""      # на какую вкладку отправить кнопкой «Исправить»
    action: str = ""   # спец-действие, например "proxy_off"


@dataclass
class ScanResult:
    findings: list[Finding] = field(default_factory=list)
    startup: list[startup.StartupItem] = field(default_factory=list)
    junk: list[junk.JunkCategory] = field(default_factory=list)
    proxy: network.ProxyState | None = None
    took: float = 0.0


def scan(kb: KnowledgeBase, progress=lambda text: None) -> ScanResult:
    t0 = time.time()
    r = ScanResult()
    f = r.findings

    progress("Смотрю процессор и память…")
    mon = ProcessMonitor()
    mon.sample()
    cpu = psutil.cpu_percent(interval=1.5)
    procs = mon.sample(kb)
    _cpu(f, cpu, procs)
    _ram(f, procs)

    progress("Проверяю диски…")
    _disks(f)

    progress("Читаю автозагрузку…")
    r.startup = startup.list_items(kb)
    _startup(f, r.startup)

    progress("Ищу мусор…")
    r.junk = [junk.scan(c) for c in junk.categories()]
    total = sum(c.size for c in r.junk)
    if total > 5 * 2**30:
        f.append(Finding(WARN, f"Мусора на {human_size(total)}",
                         "Временные файлы, кеши и корзина.", "Очистите — это безопасно.", "junk"))
    elif total > 500 * 2**20:
        f.append(Finding(INFO, f"Можно освободить {human_size(total)}", "Временные файлы, кеши и корзина.",
                         tab="junk"))

    progress("Проверяю сеть и прокси…")
    r.proxy = network.proxy_state()
    _proxy(f, r.proxy)

    progress("Температуры и прочее…")
    _temps(f)
    _uptime(f)
    if IS_LINUX:
        _failed_units(f)

    order = {BAD: 0, WARN: 1, INFO: 2, OK: 3}
    f.sort(key=lambda x: order[x.level])
    if not any(x.level in (WARN, BAD) for x in f):
        f.insert(0, Finding(OK, "Серьёзных проблем не найдено",
                            "Если тормозит что-то конкретное — загляните во вкладку «Процессы» во время тормозов."))
    r.took = time.time() - t0
    return r


def _top(procs, key, n=3) -> str:
    top = sorted(procs, key=key, reverse=True)[:n]
    return ", ".join(p.name for p in top)


def _cpu(f, cpu, procs):
    top = sorted(procs, key=lambda p: p.cpu, reverse=True)
    heavy = [p for p in top if p.cpu > 25]
    for p in heavy:
        if p.verdict and p.verdict.verdict == "bad":
            f.append(Finding(BAD, f"{p.name} грузит процессор на {p.cpu:.0f}%", p.verdict.note, tab="procs"))
    if cpu > 85:
        f.append(Finding(BAD, f"Процессор загружен на {cpu:.0f}%",
                         f"Больше всех едят: {_top(procs, lambda p: p.cpu)}.",
                         "Закройте лишнее во вкладке «Процессы».", "procs"))
    elif cpu > 60:
        f.append(Finding(WARN, f"Процессор загружен на {cpu:.0f}%",
                         f"Больше всех едят: {_top(procs, lambda p: p.cpu)}.", tab="procs"))
    else:
        f.append(Finding(OK, f"Процессор: {cpu:.0f}%", "Нагрузка в норме."))


def _ram(f, procs):
    used, total, pct = sysinfo.memory()
    swap = psutil.swap_memory()
    top = _top(procs, lambda p: p.mem)
    detail = f"Занято {human_size(used)} из {human_size(total)}. Больше всех: {top}."
    # пороги в процентах, одинаковые для любого объёма памяти
    if pct >= 90:
        f.append(Finding(BAD, f"Оперативка забита на {pct:.0f}%", detail,
                         "Система начинает сбрасывать память на диск — отсюда тормоза. "
                         "Закройте лишнее или уберите программы из автозагрузки.", "procs"))
    elif pct >= 70:
        f.append(Finding(WARN, f"Оперативка занята на {pct:.0f}%", detail,
                         "Пока терпимо, но запаса мало. Закройте то, чем не пользуетесь.", "procs"))
    else:
        f.append(Finding(OK, f"Оперативка: {pct:.0f}%", detail))
    if total < 6 * 2**30:
        f.append(Finding(INFO, f"Всего {human_size(total)} оперативной памяти",
                         by_os("Для современного браузера и Windows этого маловато.",
                               "Для современного браузера и macOS этого маловато.",
                               "Для современного браузера и рабочего стола этого маловато."),
                         by_os("Самый дешёвый апгрейд — докупить планку памяти.",
                               "На Mac память не докупить: держите открытыми поменьше вкладок и программ.",
                               "Самый дешёвый апгрейд — докупить планку памяти. Помогает и zram.")))
    if swap.total and swap.percent > 50 and pct >= 70:
        f.append(Finding(WARN, by_os("Файл подкачки", "Своп", "Подкачка (swap)") + f" занят на {swap.percent:.0f}%",
                         "Памяти не хватает, и система активно пишет её на диск."))


def _disks(f):
    for d in sysinfo.disks():
        free_pct = 100 - d["percent"]
        title = f"Диск {d['mount']}: свободно {human_size(d['free'])} ({free_pct:.0f}%)"
        system = d["mount"] in ("/", "C:\\")
        if free_pct < 10 or d["free"] < 5 * 2**30:
            f.append(Finding(BAD if system else WARN, title,
                             "Когда системный диск почти полон, " + by_os(
                                 "Windows негде держать файл подкачки и обновления — всё тормозит.",
                                 "macOS негде держать своп и снимки Time Machine — всё тормозит.",
                                 "системе негде держать подкачку и кеши — всё тормозит.") if system else "",
                             by_os("Очистите мусор или перенесите большие файлы. Ещё место покажет "
                               "«Параметры → Система → Память».",
                               "Очистите мусор или перенесите большие файлы. Ещё место покажет "
                               "меню  → «Об этом Mac» → «Хранилище».",
                               "Очистите мусор или перенесите большие файлы. Что занимает место, "
                               "покажет «Анализатор использования дисков» (baobab) или ncdu."), "junk"))
        elif free_pct < 20 and system:
            f.append(Finding(WARN, title, "Лучше держать свободными хотя бы 15–20%.", tab="junk"))


def _startup(f, items):
    on = [i for i in items if i.enabled and not i.system]
    bad = [i for i in items if i.enabled and i.verdict and i.verdict.verdict == "bad"]
    safe = [i for i in on if i.verdict and i.verdict.verdict == "safe"]
    unknown = [i for i in on if i.verdict and i.verdict.verdict == "unknown"]
    if bad:
        f.append(Finding(BAD, f"Нежелательное в автозагрузке: {', '.join(i.name for i in bad[:4])}",
                         bad[0].verdict.note, "Удалите или отключите во вкладке «Автозагрузка».", "startup"))
    if len(safe) >= 3:
        f.append(Finding(WARN, f"{len(safe)} программ в автозагрузке можно отключить",
                         ", ".join(i.name for i in safe[:6]) + ("…" if len(safe) > 6 else ""),
                         "Каждая из них отнимает время при включении и память в фоне.", "startup"))
    elif safe:
        f.append(Finding(INFO, f"В автозагрузке есть лишнее: {', '.join(i.name for i in safe)}", tab="startup"))
    if unknown:
        f.append(Finding(INFO, f"Неизвестных программ в автозагрузке: {len(unknown)}",
                         ", ".join(i.name for i in unknown[:6]),
                         "Во вкладке «Автозагрузка» WhySlow покажет, что о них пишут в интернете. "
                         "Если знаете точно — предложите запись в базу.", "startup"))


def _proxy(f, st):
    if not st.enabled:
        return
    if st.dead:
        f.append(Finding(BAD, "Включён прокси, который никто не слушает",
                         f"{st.where}: {st.server}. Так бывает, когда VPN-клиент закрылся и не убрал за собой "
                         "настройки — браузер и программы перестают выходить в интернет.",
                         "Выключите прокси. Если VPN понадобится — он включит его снова." if st.fixable else
                         _proxy_manual(st),
                         action="proxy_off" if st.fixable else ""))
    elif st.listener:
        f.append(Finding(INFO, f"Интернет идёт через прокси {st.server}",
                         f"{st.where}. Порт слушает {st.listener} — похоже, это ваш VPN. Если интернет медленный, "
                         "попробуйте без него."))
    else:
        f.append(Finding(INFO, f"Включён прокси {st.server}", st.where))


def _proxy_manual(st) -> str:
    """Как убрать прокси, который WhySlow не умеет выключить сам."""
    if st.where.startswith("переменная"):
        var = st.where.split()[-1]
        return by_os(f"Удалите переменную {var}: «Параметры → Система → О системе → Дополнительные параметры "
                     "системы → Переменные среды».",
                     f"Уберите строку с {var} из ~/.zshrc, ~/.zprofile или ~/.bash_profile и откройте терминал заново.",
                     f"Уберите строку с {var} из ~/.bashrc, ~/.profile или /etc/environment.")
    if IS_MAC:
        return "Выключите его: «Системные настройки → Сеть → ваша сеть → Подробнее → Прокси»."
    return "Выключите его в системных настройках сети."


def _temps(f):
    temps = sysinfo.temperatures(wait=True)
    for g in sysinfo.gpus():
        if g.get("temp"):
            temps[g["name"]] = g["temp"]
    if not temps:
        if IS_WINDOWS:
            f.append(Finding(INFO, "Температуры недоступны", lhm.problem(),
                             f"LibreHardwareMonitor: {lhm.RELEASES_URL}"))
        elif IS_MAC:
            f.append(Finding(INFO, "Температуры недоступны",
                             "macOS не отдаёт датчики программам без прав администратора.",
                             "Посмотреть их можно в терминале: sudo powermetrics --samplers smc -n 1"))
        else:
            f.append(Finding(INFO, "Температуры недоступны", "Система не отдаёт датчики.",
                             "Установите lm-sensors и выполните sudo sensors-detect."))
        return
    hot = {k: v for k, v in temps.items() if v >= 85}
    if hot:
        name, t = max(hot.items(), key=lambda kv: kv[1])
        f.append(Finding(BAD, f"Перегрев: {name} — {t:.0f}°C",
                         "При перегреве процессор и видеокарта сбрасывают частоты — это и есть тормоза.",
                         by_os("Почистите от пыли, замените термопасту, проверьте вентиляторы.",
                               "Не держите Mac на мягком (одеяло, колени) и посмотрите во вкладке «Процессы», "
                               "кто грузит процессор. Если вентилятор шумит постоянно — почистите от пыли в сервисе.",
                               "Почистите от пыли, замените термопасту, проверьте вентиляторы."), "system"))
    else:
        t = max(temps.values())
        f.append(Finding(OK, f"Температуры в норме (максимум {t:.0f}°C)"))


def _uptime(f):
    up = sysinfo.uptime()
    if up > 7 * 86400:
        f.append(Finding(WARN, f"Компьютер не перезагружался {human_duration(up)}",
                         "За это время накапливаются утечки памяти и зависшие процессы.",
                         by_os("Просто перезагрузитесь (не «Завершение работы» — в Windows оно не сбрасывает "
                               "систему полностью).",
                               "Перезагрузитесь: меню  → «Перезагрузить». Сон и закрытая крышка память не "
                               "освобождают.",
                               "Перезагрузитесь. Заодно применятся обновления ядра, если они были.")))


def _failed_units(f):
    code, out = run(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"])
    units = [line.split()[0] for line in out.splitlines() if line.strip()] if code == 0 else []
    if units:
        f.append(Finding(WARN, f"Упавшие системные службы: {len(units)}", ", ".join(units[:5]),
                         "Посмотрите причину: journalctl -u <служба>"))
