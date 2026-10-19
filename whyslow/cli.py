"""Текстовый режим: python -m whyslow --cli"""

from __future__ import annotations

from . import checks, knowledge, online, optimize, settings, updater
from .i18n import tr

ICONS = {checks.OK: "[ OK ]", checks.INFO: "[ i  ]", checks.WARN: "[ !  ]", checks.BAD: "[!!!!]"}


def main(fix: bool = False) -> int:
    kb, msg = knowledge.update()
    print(msg)
    if settings.get("check_updates"):
        try:
            rel, _ = updater.check(timeout=4)
        except updater.UpdateError:
            rel = None
        if rel:
            print(tr(f"Доступна новая версия WhySlow {rel.version}. Обновить: python3 -m whyslow --update",
                     f"WhySlow {rel.version} is available. Update: python3 -m whyslow --update"))
    r = checks.scan(kb, progress=lambda t: print("  " + t))
    print()
    for f in r.findings:
        print(f"{ICONS[f.level]} {f.title}")
        for line in (f.detail, f.advice):
            if line:
                print(f"        {line}")
    print(tr("\nАвтозагрузка:", "\nStartup:"))
    for it in r.startup:
        state = (tr("вкл ", "on  ") if it.enabled else tr("выкл", "off "))
        print(f"  [{state}] {it.name:<32} {it.verdict.label:<16} {it.source}")
    unknown = [it for it in r.startup if it.verdict.verdict == "unknown"][:15]
    if unknown:
        print(tr("\nНет в базе — что нашлось в интернете (не проверено):",
                 "\nNot in the database — what was found online (unverified):"))
        for it in unknown:
            info = online.describe(it.name, it.command, timeout=4)
            text = f"{info.text} [{info.source}]" if info and info.text else tr("описание не нашлось", "no description found")
            print(f"  {it.name}: {text}")
    steps = optimize.plan(r)
    _print_plan(steps)
    if not fix:
        print(tr(f"\nПроверка заняла {r.took:.1f} с. Исправить отмеченное: python3 -m whyslow --optimize "
                 "(или в окне, вкладка «Оптимизация»).",
                 f"\nScan took {r.took:.1f} s. To fix the checked items: python3 -m whyslow --optimize "
                 "(or in the window, Optimize tab)."))
        return 0
    chosen = [s for s in steps if s.checked]
    if not chosen:
        return 0
    if input(tr(f"\nВыполнить отмеченные пункты ({len(chosen)})? [y/N] ",
                f"\nApply the checked items ({len(chosen)})? [y/N] ")).strip().lower() not in ("y", "yes", "д", "да"):
        print(tr("Отменено, ничего не изменено.", "Cancelled, nothing was changed."))
        return 0
    outcomes = optimize.apply(chosen, progress=lambda t: print("  " + t))
    print("\n" + optimize.summary(outcomes))
    return 0 if all(o.ok for o in outcomes) else 1


def _print_plan(steps) -> None:
    """[x] — сделается по --optimize, [ ] — только в окне, если отметить вручную."""
    if not steps:
        print(tr("\nОптимизировать нечего.", "\nNothing to optimize."))
        return
    print(tr("\nПлан оптимизации ([x] — безопасно, [ ] — на ваше усмотрение):",
             "\nOptimization plan ([x] — safe, [ ] — your call):"))
    for s in steps:
        mark = "[x]" if s.checked else "[ ]"
        tail = "" if s.undoable else tr("  (необратимо)", "  (permanent)")
        print(f"  {mark} {s.title}{tail}")


def update() -> int:
    """python -m whyslow --update: проверить и, с согласия, поставить новую версию."""
    try:
        rel, msg = updater.check()
    except updater.UpdateError as e:
        print(e)
        return 1
    print(msg)
    if not rel:
        return 0
    if rel.notes:
        print(tr("\nЧто нового:\n", "\nWhat's new:\n") + rel.notes.strip() + "\n")
    print(updater.mode_hint())
    if updater.mode() == "manual":
        return 1
    if input(tr(f"Установить {rel.version}? [y/N] ", f"Install {rel.version}? [y/N] ")).strip().lower() not in ("y", "yes", "д", "да"):
        print(tr("Отменено.", "Cancelled."))
        return 0
    try:
        print(updater.install(rel, progress=lambda t: print("  " + t)))
    except updater.UpdateError as e:
        print(e)
        return 1
    return 0
