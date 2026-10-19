"""Текстовый режим: python -m whyslow --cli"""

from __future__ import annotations

from . import checks, knowledge

ICONS = {checks.OK: "[ OK ]", checks.INFO: "[ i  ]", checks.WARN: "[ !  ]", checks.BAD: "[!!!!]"}


def main() -> int:
    kb, msg = knowledge.update()
    print(msg)
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
