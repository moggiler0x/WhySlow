"""Диагностика: собирает всё в список понятных человеку «находок»."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import psutil

from . import junk, lhm, network, startup, sysinfo
from .knowledge import KnowledgeBase
from .i18n import tr
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

    progress(tr("Смотрю процессор и память…", "Checking CPU and memory…"))
    mon = ProcessMonitor()
    mon.sample()
    cpu = psutil.cpu_percent(interval=1.5)
    procs = mon.sample(kb)
    _cpu(f, cpu, procs)
    _ram(f, procs)

    progress(tr("Проверяю диски…", "Checking disks…"))
    _disks(f)

    progress(tr("Читаю автозагрузку…", "Reading startup items…"))
    r.startup = startup.list_items(kb)
    _startup(f, r.startup)

    progress(tr("Ищу мусор…", "Looking for junk…"))
    r.junk = [junk.scan(c) for c in junk.categories()]
    total = sum(c.size for c in r.junk)
    if total > 5 * 2**30:
        f.append(Finding(WARN, tr(f"Мусора на {human_size(total)}", f"{human_size(total)} of junk"),
                         _junk_detail(), tr("Очистите — это безопасно.", "Clean it up — it's safe."), "junk"))
    elif total > 500 * 2**20:
        f.append(Finding(INFO, tr(f"Можно освободить {human_size(total)}", f"{human_size(total)} can be freed"),
                         _junk_detail(), tab="junk"))

    progress(tr("Проверяю сеть и прокси…", "Checking network and proxy…"))
    r.proxy = network.proxy_state()
    _proxy(f, r.proxy)

    progress(tr("Температуры и прочее…", "Temperatures and more…"))
    _temps(f)
    _uptime(f)
    if IS_LINUX:
        _failed_units(f)

    order = {BAD: 0, WARN: 1, INFO: 2, OK: 3}
    f.sort(key=lambda x: order[x.level])
    if not any(x.level in (WARN, BAD) for x in f):
        f.insert(0, Finding(OK, tr("Серьёзных проблем не найдено", "No serious problems found"),
                            tr("Если тормозит что-то конкретное — загляните во вкладку «Процессы» во время тормозов.",
                               "If something specific is slow, open the Processes tab while it's lagging.")))
    r.took = time.time() - t0
    return r


def _junk_detail() -> str:
    return tr("Временные файлы, кеши и корзина.", "Temporary files, caches and the trash.")


def _top(procs, key, n=3) -> str:
    top = sorted(procs, key=key, reverse=True)[:n]
    return ", ".join(p.name for p in top)


def _cpu(f, cpu, procs):
    top = sorted(procs, key=lambda p: p.cpu, reverse=True)
    heavy = [p for p in top if p.cpu > 25]
    for p in heavy:
        if p.verdict and p.verdict.verdict == "bad":
            f.append(Finding(BAD, tr(f"{p.name} грузит процессор на {p.cpu:.0f}%", f"{p.name} is using {p.cpu:.0f}% CPU"), p.verdict.note, tab="procs"))
    if cpu > 85:
        f.append(Finding(BAD, tr(f"Процессор загружен на {cpu:.0f}%", f"CPU is at {cpu:.0f}%"),
                         tr(f"Больше всех едят: {_top(procs, lambda p: p.cpu)}.",
                            f"Top consumers: {_top(procs, lambda p: p.cpu)}."),
                         tr("Закройте лишнее во вкладке «Процессы».", "Close what you don't need in the Processes tab."),
                         "procs"))
    elif cpu > 60:
        f.append(Finding(WARN, tr(f"Процессор загружен на {cpu:.0f}%", f"CPU is at {cpu:.0f}%"),
                         tr(f"Больше всех едят: {_top(procs, lambda p: p.cpu)}.",
                            f"Top consumers: {_top(procs, lambda p: p.cpu)}."), tab="procs"))
    else:
        f.append(Finding(OK, tr(f"Процессор: {cpu:.0f}%", f"CPU: {cpu:.0f}%"), tr("Нагрузка в норме.", "Load is normal.")))


def _ram(f, procs):
    used, total, pct = sysinfo.memory()
    swap = psutil.swap_memory()
    top = _top(procs, lambda p: p.mem)
    detail = tr(f"Занято {human_size(used)} из {human_size(total)}. Больше всех: {top}.",
                f"{human_size(used)} of {human_size(total)} in use. Top: {top}.")
    # пороги в процентах, одинаковые для любого объёма памяти
    if pct >= 90:
        f.append(Finding(BAD, tr(f"Оперативка забита на {pct:.0f}%", f"RAM is {pct:.0f}% full"), detail,
                         tr("Система начинает сбрасывать память на диск — отсюда тормоза. "
                            "Закройте лишнее или уберите программы из автозагрузки.",
                            "The system starts swapping memory to disk — that's the slowdown. "
                            "Close what you don't need or remove apps from startup."), "procs"))
    elif pct >= 70:
        f.append(Finding(WARN, tr(f"Оперативка занята на {pct:.0f}%", f"RAM is {pct:.0f}% used"), detail,
                         tr("Пока терпимо, но запаса мало. Закройте то, чем не пользуетесь.",
                            "Bearable for now, but there's little headroom. Close what you aren't using."),
                         "procs"))
    else:
        f.append(Finding(OK, tr(f"Оперативка: {pct:.0f}%", f"RAM: {pct:.0f}%"), detail))
    if total < 6 * 2**30:
        f.append(Finding(INFO, tr(f"Всего {human_size(total)} оперативной памяти", f"Only {human_size(total)} of RAM"),
                         by_os(tr("Для современного браузера и Windows этого маловато.",
                                  "That's tight for a modern browser and Windows."),
                               tr("Для современного браузера и macOS этого маловато.",
                                  "That's tight for a modern browser and macOS."),
                               tr("Для современного браузера и рабочего стола этого маловато.",
                                  "That's tight for a modern browser and desktop.")),
                         by_os(tr("Самый дешёвый апгрейд — докупить планку памяти.",
                                  "The cheapest upgrade is adding a memory stick."),
                               tr("На Mac память не докупить: держите открытыми поменьше вкладок и программ.",
                                  "Mac memory can't be upgraded: keep fewer tabs and apps open."),
                               tr("Самый дешёвый апгрейд — докупить планку памяти. Помогает и zram.",
                                  "The cheapest upgrade is adding a memory stick. zram helps too."))))
    if swap.total and swap.percent > 50 and pct >= 70:
        f.append(Finding(WARN, by_os(tr("Файл подкачки", "Page file"), tr("Своп", "Swap"), tr("Подкачка (swap)", "Swap"))
                         + tr(f" занят на {swap.percent:.0f}%", f" is {swap.percent:.0f}% used"),
                         tr("Памяти не хватает, и система активно пишет её на диск.",
                            "Memory is short and the system is actively writing it to disk.")))


def _disks(f):
    for d in sysinfo.disks():
        free_pct = 100 - d["percent"]
        title = tr(f"Диск {d['mount']}: свободно {human_size(d['free'])} ({free_pct:.0f}%)",
                   f"Disk {d['mount']}: {human_size(d['free'])} free ({free_pct:.0f}%)")
        system = d["mount"] in ("/", "C:\\")
        if free_pct < 10 or d["free"] < 5 * 2**30:
            f.append(Finding(BAD if system else WARN, title,
                             by_os(tr("Когда системный диск почти полон, Windows негде держать файл подкачки "
                                      "и обновления — всё тормозит.",
                                      "When the system disk is almost full, Windows has no room for the page file "
                                      "and updates — everything slows down."),
                                   tr("Когда системный диск почти полон, macOS негде держать своп и снимки "
                                      "Time Machine — всё тормозит.",
                                      "When the system disk is almost full, macOS has no room for swap and "
                                      "Time Machine snapshots — everything slows down."),
                                   tr("Когда системный диск почти полон, системе негде держать подкачку и "
                                      "кеши — всё тормозит.",
                                      "When the system disk is almost full, the system has no room for swap and "
                                      "caches — everything slows down.")) if system else "",
                             by_os(tr("Очистите мусор или перенесите большие файлы. Ещё место покажет "
                                      "«Параметры → Система → Память».",
                                      "Clean up junk or move large files elsewhere. Settings → System → Storage "
                                      "shows what else takes space."),
                                   tr("Очистите мусор или перенесите большие файлы. Ещё место покажет "
                                      "меню  → «Об этом Mac» → «Хранилище».",
                                      "Clean up junk or move large files elsewhere.  menu → About This Mac → "
                                      "Storage shows what else takes space."),
                                   tr("Очистите мусор или перенесите большие файлы. Что занимает место, "
                                      "покажет «Анализатор использования дисков» (baobab) или ncdu.",
                                      "Clean up junk or move large files elsewhere. Disk Usage Analyzer (baobab) "
                                      "or ncdu shows what takes space.")), "junk"))
        elif free_pct < 20 and system:
            f.append(Finding(WARN, title, tr("Лучше держать свободными хотя бы 15–20%.",
                                            "It's best to keep at least 15–20% free."), tab="junk"))


def _startup(f, items):
    on = [i for i in items if i.enabled and not i.system]
    bad = [i for i in items if i.enabled and i.verdict and i.verdict.verdict == "bad"]
    safe = [i for i in on if i.verdict and i.verdict.verdict == "safe"]
    unknown = [i for i in on if i.verdict and i.verdict.verdict == "unknown"]
    if bad:
        names = ", ".join(i.name for i in bad[:4])
        f.append(Finding(BAD, tr(f"Нежелательное в автозагрузке: {names}", f"Unwanted software in startup: {names}"),
                         bad[0].verdict.note, tr("Удалите или отключите во вкладке «Автозагрузка».",
                                                 "Remove or disable it in the Startup tab."), "startup"))
    if len(safe) >= 3:
        f.append(Finding(WARN, tr(f"{len(safe)} программ в автозагрузке можно отключить",
                                 f"{len(safe)} startup apps can be disabled"),
                         ", ".join(i.name for i in safe[:6]) + ("…" if len(safe) > 6 else ""),
                         tr("Каждая из них отнимает время при включении и память в фоне.",
                            "Each one costs boot time and background memory."), "startup"))
    elif safe:
        names = ", ".join(i.name for i in safe)
        f.append(Finding(INFO, tr(f"В автозагрузке есть лишнее: {names}", f"Unneeded startup items: {names}"),
                         tab="startup"))
    if unknown:
        f.append(Finding(INFO, tr(f"Неизвестных программ в автозагрузке: {len(unknown)}",
                                 f"Unknown startup apps: {len(unknown)}"),
                         ", ".join(i.name for i in unknown[:6]),
                         tr("Во вкладке «Автозагрузка» WhySlow покажет, что о них пишут в интернете. "
                            "Если знаете точно — предложите запись в базу.",
                            "The Startup tab shows what the internet says about them. "
                            "If you know for sure, suggest an entry for the database."), "startup"))


def _proxy(f, st):
    if not st.enabled:
        return
    if st.dead:
        f.append(Finding(BAD, tr("Включён прокси, который никто не слушает", "A proxy is on, but nothing is listening"),
                         tr(f"{st.where}: {st.server}. Так бывает, когда VPN-клиент закрылся и не убрал за собой "
                            "настройки — браузер и программы перестают выходить в интернет.",
                            f"{st.where}: {st.server}. This happens when a VPN client quits without cleaning up "
                            "its settings — the browser and apps lose internet access."),
                         tr("Выключите прокси. Если VPN понадобится — он включит его снова.",
                            "Turn the proxy off. If you need the VPN again, it will turn it back on.")
                         if st.fixable else _proxy_manual(st),
                         action="proxy_off" if st.fixable else ""))
    elif st.listener:
        f.append(Finding(INFO, tr(f"Интернет идёт через прокси {st.server}", f"Internet goes through proxy {st.server}"),
                         tr(f"{st.where}. Порт слушает {st.listener} — похоже, это ваш VPN. Если интернет медленный, "
                            "попробуйте без него.",
                            f"{st.where}. {st.listener} is listening on the port — probably your VPN. If the "
                            "internet is slow, try without it.")))
    else:
        f.append(Finding(INFO, tr(f"Включён прокси {st.server}", f"Proxy {st.server} is on"), st.where))


def _proxy_manual(st) -> str:
    """Как убрать прокси, который WhySlow не умеет выключить сам."""
    if st.env_var:
        var = st.env_var
        return by_os(tr(f"Удалите переменную {var}: «Параметры → Система → О системе → Дополнительные параметры "
                        "системы → Переменные среды».",
                        f"Delete the {var} variable: Settings → System → About → Advanced system settings → "
                        "Environment Variables."),
                     tr(f"Уберите строку с {var} из ~/.zshrc, ~/.zprofile или ~/.bash_profile и откройте терминал "
                        "заново.",
                        f"Remove the {var} line from ~/.zshrc, ~/.zprofile or ~/.bash_profile and reopen the terminal."),
                     tr(f"Уберите строку с {var} из ~/.bashrc, ~/.profile или /etc/environment.",
                        f"Remove the {var} line from ~/.bashrc, ~/.profile or /etc/environment."))
    if IS_MAC:
        return tr("Выключите его: «Системные настройки → Сеть → ваша сеть → Подробнее → Прокси».",
                  "Turn it off: System Settings → Network → your network → Details → Proxies.")
    return tr("Выключите его в системных настройках сети.", "Turn it off in the system network settings.")


def _no_temps() -> str:
    return tr("Температуры недоступны", "Temperatures unavailable")


def _temps(f):
    temps = sysinfo.temperatures(wait=True)
    for g in sysinfo.gpus():
        if g.get("temp"):
            temps[g["name"]] = g["temp"]
    if not temps:
        if IS_WINDOWS:
            f.append(Finding(INFO, _no_temps(), lhm.problem(),
                             tr("Кнопка в блоке «Температуры» на вкладке «Система» скачает и запустит "
                                f"LibreHardwareMonitor ({lhm.RELEASES_URL}).",
                                "The button in the Temperatures block on the System tab downloads and starts "
                                f"LibreHardwareMonitor ({lhm.RELEASES_URL}).")))
        elif IS_MAC:
            f.append(Finding(INFO, _no_temps(),
                             tr("Датчики не найдены. На Mac с Apple Silicon они читаются сами, а на Mac с Intel "
                                "macOS не отдаёт их программам без прав администратора.",
                                "No sensors found. On Apple Silicon Macs they are read automatically; on Intel "
                                "Macs, macOS doesn't expose them to apps without administrator rights."),
                             tr("Посмотреть их можно в терминале: sudo powermetrics --samplers smc -n 1",
                                "You can see them in Terminal: sudo powermetrics --samplers smc -n 1")))
        else:
            f.append(Finding(INFO, _no_temps(), tr("Система не отдаёт датчики.", "The system doesn't expose sensors."),
                             tr("Установите lm-sensors и выполните sudo sensors-detect.",
                                "Install lm-sensors and run sudo sensors-detect.")))
        return
    hot = {k: v for k, v in temps.items() if v >= 85}
    if hot:
        name, t = max(hot.items(), key=lambda kv: kv[1])
        dust = tr("Почистите от пыли, замените термопасту, проверьте вентиляторы.",
                  "Clean out the dust, replace the thermal paste, check the fans.")
        f.append(Finding(BAD, tr(f"Перегрев: {name} — {t:.0f}°C", f"Overheating: {name} — {t:.0f}°C"),
                         tr("При перегреве процессор и видеокарта сбрасывают частоты — это и есть тормоза.",
                            "When overheating, the CPU and GPU throttle their clocks — that's the slowdown."),
                         by_os(dust,
                               tr("Не держите Mac на мягком (одеяло, колени) и посмотрите во вкладке «Процессы», "
                                  "кто грузит процессор. Если вентилятор шумит постоянно — почистите от пыли в сервисе.",
                                  "Don't keep the Mac on soft surfaces (blanket, lap) and check the Processes tab for "
                                  "what loads the CPU. If the fan is always loud, have it cleaned at a service center."),
                               dust), "system"))
    else:
        t = max(temps.values())
        f.append(Finding(OK, tr(f"Температуры в норме (максимум {t:.0f}°C)", f"Temperatures are normal (max {t:.0f}°C)")))


def _uptime(f):
    up = sysinfo.uptime()
    if up > 7 * 86400:
        f.append(Finding(WARN, tr(f"Компьютер не перезагружался {human_duration(up)}",
                                 f"No restart for {human_duration(up)}"),
                         tr("За это время накапливаются утечки памяти и зависшие процессы.",
                            "Memory leaks and stuck processes pile up over that time."),
                         by_os(tr("Просто перезагрузитесь (не «Завершение работы» — в Windows оно не сбрасывает "
                                  "систему полностью).",
                                  "Just restart (not Shut down — in Windows that doesn't fully reset the system)."),
                               tr("Перезагрузитесь: меню  → «Перезагрузить». Сон и закрытая крышка память не "
                                  "освобождают.",
                                  "Restart:  menu → Restart. Sleep and closing the lid don't free memory."),
                               tr("Перезагрузитесь. Заодно применятся обновления ядра, если они были.",
                                  "Restart. Any pending kernel updates will be applied too."))))


def _failed_units(f):
    code, out = run(["systemctl", "--failed", "--no-legend", "--plain", "--no-pager"])
    units = [line.split()[0] for line in out.splitlines() if line.strip()] if code == 0 else []
    if units:
        f.append(Finding(WARN, tr(f"Упавшие системные службы: {len(units)}", f"Failed system services: {len(units)}"),
                         ", ".join(units[:5]),
                         tr("Посмотрите причину: journalctl -u <служба>", "See why: journalctl -u <service>")))
