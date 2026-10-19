"""Текстовый режим: python -m whyslow --cli"""

from __future__ import annotations

from . import checks, knowledge, settings, updater

ICONS = {checks.OK: "[ OK ]", checks.INFO: "[ i  ]", checks.WARN: "[ !  ]", checks.BAD: "[!!!!]"}


def main() -> int:
    kb, msg = knowledge.update()
    print(msg)
    if settings.get("check_updates"):
        try:
            rel, _ = updater.check(timeout=4)
        except updater.UpdateError:
            rel = None
        if rel:
            print(f"Доступна новая версия WhySlow {rel.version}. Обновить: python3 -m whyslow --update")
    r = checks.scan(kb, progress=lambda t: print("  " + t))
    print()
    for f in r.findings:
        print(f"{ICONS[f.level]} {f.title}")
        for line in (f.detail, f.advice):
            if line:
                print(f"        {line}")
    print("\nАвтозагрузка:")
    for it in r.startup:
        state = "вкл " if it.enabled else "выкл"
        print(f"  [{state}] {it.name:<32} {it.verdict.label:<16} {it.source}")
    print(f"\nПроверка заняла {r.took:.1f} с. Для исправлений запустите без --cli.")
    return 0


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
        print("\nЧто нового:\n" + rel.notes.strip() + "\n")
    print(updater.mode_hint())
    if updater.mode() == "manual":
        return 1
    if input(f"Установить {rel.version}? [y/N] ").strip().lower() not in ("y", "yes", "д", "да"):
        print("Отменено.")
        return 0
    try:
        print(updater.install(rel, progress=lambda t: print("  " + t)))
    except updater.UpdateError as e:
        print(e)
        return 1
    return 0
