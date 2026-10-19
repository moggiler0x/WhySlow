"""Оптимизация в один клик: из результатов проверки собирается план исправлений.

Ничего не делается само: план показывается списком, безопасные пункты отмечены,
сомнительные — нет, а то, что база велит не трогать, не предлагается вовсе.
Выполняются только пункты, которые человек оставил отмеченными. Каждое действие
пишется в журнал теми же функциями, что и на вкладках, — отключение автозагрузки
и прокси откатывается, очистка и закрытие программ необратимы и так и подписаны.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import checks, junk, network, processes, startup
from .i18n import tr
from .util import ADMIN_HINT, human_size, is_admin

# Что считать «тяжёлой» фоновой программой
HEAVY_CPU = 10.0       # % всего процессора
HEAVY_MEM = 500 * 2**20
MIN_JUNK = 1 * 2**20   # меньше мегабайта — не стоит строчки в плане


@dataclass
class Step:
    kind: str           # proxy / startup / process / junk
    title: str
    detail: str = ""
    checked: bool = False
    undoable: bool = True
    target: object = None
    size: int = 0       # сколько места освободит (для мусора)


@dataclass
class Outcome:
    step: Step
    ok: bool
    message: str = ""
    freed: int = 0


def plan(r: checks.ScanResult) -> list[Step]:
    """План по последней проверке. Ничего не меняет и ничего заново не сканирует."""
    steps: list[Step] = []
    st = r.proxy
    if st and st.dead and st.fixable:
        steps.append(Step("proxy", tr(f"Выключить зависший прокси {st.server}", f"Turn off the stuck proxy {st.server}"),
                          tr("Порт никто не слушает, поэтому программы не выходят в интернет. "
                             "Так бывает, когда программа с прокси закрылась и не убрала за собой настройки.",
                             "Nothing listens on the port, so apps can't reach the internet. "
                             "This happens when an app that set the proxy quit without cleaning up."),
                          True, True, st))
    steps += _startup_steps(r.startup)
    steps += _process_steps(r.procs)
    steps += _junk_steps(r.junk)
    return steps


def _startup_steps(items: list[startup.StartupItem]) -> list[Step]:
    order = {"bad": 0, "safe": 1, "optional": 2}
    found = [i for i in items if i.enabled and i.can_toggle and i.verdict and i.verdict.verdict in order]
    found.sort(key=lambda i: (order[i.verdict.verdict], i.name.lower()))
    steps = []
    for it in found:
        v = it.verdict
        detail = v.note
        # Без прав администратора отключение не пройдёт (Windows) или спросит пароль на каждый пункт (macOS)
        if it.needs_admin:
            detail += tr(" Нужны права администратора.", " Administrator rights required.")
        checked = v.verdict in ("bad", "safe") and not it.system and not it.needs_admin
        title = tr(f"Отключить в автозагрузке: {it.name}", f"Disable at startup: {it.name}")
        steps.append(Step("startup", title, detail.strip(), checked, True, it))
    return steps


def _process_steps(groups: list[processes.ProcGroup]) -> list[Step]:
    steps = []
    for g in sorted(groups, key=lambda g: g.mem, reverse=True):
        v = g.verdict
        if g.system or not v or v.verdict not in ("bad", "safe", "optional"):
            continue
        heavy = g.cpu >= HEAVY_CPU or g.mem >= HEAVY_MEM
        if v.verdict != "bad" and not heavy:
            continue
        usage = tr(f"{human_size(g.mem)} памяти, {g.cpu:.0f}% процессора", f"{human_size(g.mem)} of memory, {g.cpu:.0f}% CPU")
        detail = f"{usage}. {v.note}".strip()
        detail += tr(" Несохранённые данные в ней пропадут.", " Any unsaved data in it will be lost.")
        # Закрывать чужую работу сами не предлагаем — только то, что база считает вредным
        steps.append(Step("process", tr(f"Закрыть {g.name}", f"Close {g.name}"), detail,
                          v.verdict == "bad", False, g))
    return steps


def _junk_steps(cats: list[junk.JunkCategory]) -> list[Step]:
    steps = []
    for c in cats:
        if c.size < MIN_JUNK:
            continue
        detail = c.note
        checked = c.key not in ("trash", "recycle")  # корзину человек мог оставить нарочно
        if c.key == "wu" and not is_admin():
            detail += f" ({ADMIN_HINT})"
            checked = False
        files = tr(f", {c.files} файлов", f", {c.files} files") if c.files else ""
        steps.append(Step("junk", tr(f"Очистить «{c.title}» — {human_size(c.size)}", f"Clean “{c.title}” — {human_size(c.size)}"),
                          detail + files, checked, False, c, c.size))
    return steps


def manual(r: checks.ScanResult) -> list[checks.Finding]:
    """Что WhySlow сам не исправит, но человеку стоит сделать: перезагрузка, перегрев, упавшие службы."""
    return [f for f in r.findings if f.level in (checks.WARN, checks.BAD) and f.advice
            and f.tab in ("", "system") and not f.action]


def apply(steps: list[Step], progress=lambda text: None) -> list[Outcome]:
    """Выполняет переданные пункты по очереди. Ошибка в одном не останавливает остальные."""
    out = []
    for i, s in enumerate(steps, 1):
        progress(f"{i}/{len(steps)}: {s.title}")
        try:
            out.append(_run(s))
        except Exception as e:  # noqa: BLE001 — показать человеку, что именно не вышло
            out.append(Outcome(s, False, str(e) or e.__class__.__name__))
    return out


def _run(s: Step) -> Outcome:
    if s.kind == "proxy":
        network.disable_proxy(s.target)
        return Outcome(s, True)
    if s.kind == "startup":
        startup.set_enabled(s.target, False)
        return Outcome(s, True)
    if s.kind == "junk":
        freed, failed = junk.clean(s.target)
        msg = tr(f"освобождено {human_size(freed)}", f"freed {human_size(freed)}")
        if failed:
            msg += tr(f", пропущено файлов: {failed}", f", files skipped: {failed}")
        return Outcome(s, True, msg, freed)
    if s.kind == "process":
        g = s.target
        # С момента проверки программа могла перезапуститься: берём её процессы заново
        g.pids = processes.pids_by_name(g.name)
        if not g.pids:
            return Outcome(s, True, tr("уже закрыта", "already closed"))
        closed, failed = processes.kill(g)
        if failed:
            return Outcome(s, False, tr(f"не закрылось процессов: {failed} ({ADMIN_HINT})",
                                        f"processes not closed: {failed} ({ADMIN_HINT})"))
        return Outcome(s, True)
    raise ValueError(s.kind)


def summary(outcomes: list[Outcome]) -> str:
    done = [o for o in outcomes if o.ok]
    freed = sum(o.freed for o in outcomes)
    text = tr(f"Выполнено: {len(done)} из {len(outcomes)}.", f"Done: {len(done)} of {len(outcomes)}.")
    if freed:
        text += tr(f" Освобождено {human_size(freed)}.", f" Freed {human_size(freed)}.")
    failed = [o for o in outcomes if not o.ok]
    if failed:
        text += tr("\n\nНе получилось:\n", "\n\nFailed:\n") + "\n".join(f"• {o.step.title}: {o.message}" for o in failed)
    if any(o.ok and o.step.undoable for o in outcomes):
        text += tr("\n\nОтключение автозагрузки и прокси можно откатить во вкладке «Журнал».",
                   "\n\nStartup and proxy changes can be undone in the Journal tab.")
    return text

