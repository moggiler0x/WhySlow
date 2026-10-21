"""Текстовый режим: python -m whyslow --cli"""

from __future__ import annotations

from . import checks, knowledge, macapps, macstorage, online, optimize, passwords, premium, settings, updater
from .i18n import tr
from .util import IS_MAC, human_size

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
    if IS_MAC:
        print(tr("\nИз чего состоят «Системные данные» macOS: python3 -m whyslow --storage",
                 "\nWhat macOS “System Data” is made of: python3 -m whyslow --storage"))
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


def storage() -> int:
    """python -m whyslow --storage: «Системные данные» macOS по категориям. Только показывает,
    удалять — в окне, вкладка «Хранилище»."""
    if not IS_MAC:
        print(tr("«Системные данные» есть только в macOS.", "“System Data” only exists on macOS."))
        return 0
    if not macstorage.has_full_disk_access():
        print(tr("Нет полного доступа к диску, часть категорий не посчитать.\n",
                 "No Full Disk Access, some categories can't be measured.\n") + macstorage.fda_hint() + "\n")
    cats = macstorage.scan_all(progress=lambda t: print("  " + t))
    print()
    for c in cats:
        size = human_size(c.size) if c.size_known else tr(f"{len(c.items)} шт.", f"{len(c.items)} items")
        mark = tr("  (не всё доступно)", "  (partly measured)") if c.denied and c.fda else ""
        print(f"  {size:>10}  {c.title:<36} {macstorage.level_label(c.level)}{mark}")
    apps = [it for c in cats if c.key == "apps" for it in c.items if it.level in (macapps.OLD, macapps.LEFTOVER)]
    if apps:
        print(tr("\nДанные программ, которые можно удалить:", "\nApp data that can be removed:"))
        for it in apps:
            print(f"  {human_size(it.size):>10}  {it.name:<36} {it.tag}")
    print(tr(f"\nВсего: {human_size(sum(c.size for c in cats))}. Удалить выбранное можно в окне, вкладка «Хранилище».",
             f"\nTotal: {human_size(sum(c.size for c in cats))}. Delete selected items in the window, Storage tab."))
    print(tr("Автоочистка: ", "Auto-clean: ") + (tr("включена. ", "on. ") if settings.get("mac_autoclean") else tr("выключена. ", "off. "))
          + macstorage.autoclean_text())
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


def check_passwords(path: str) -> int:
    """Проверка выгрузки менеджера паролей по базе утечек. Сами пароли не печатаются."""
    try:
        vault = passwords.load(path)
    except passwords.VaultError:
        print(tr("Это не похоже на выгрузку менеджера паролей: в файле нет колонки с паролями.",
                 "This doesn't look like a password manager export: there is no password column."))
        return 1
    print(tr(f"Паролей в файле: {len(vault.entries)}. Проверяю по базе утечек (на сервер уходят только 5 символов хеша)…",
             f"Passwords in the file: {len(vault.entries)}. Checking against the breach database "
             "(only 5 hash characters are sent)…"))
    try:
        passwords.check(vault)
    except OSError as e:
        print(tr(f"Не удалось связаться с api.pwnedpasswords.com: {e}", f"Could not reach api.pwnedpasswords.com: {e}"))
        return 1
    leaked = sorted(vault.leaked, key=lambda e: -(e.leaks or 0))
    if not leaked:
        print(tr("Утёкших паролей нет.", "No leaked passwords."))
        return 0
    print(tr(f"\nВ утечках: {len(leaked)} из {len(vault.entries)}. Смените эти пароли на сайтах:",
             f"\nLeaked: {len(leaked)} of {len(vault.entries)}. Change these passwords on the sites:"))
    for e in leaked:
        print(f"  {e.leaks:>10}  {e.site}  {e.username}")
    print(tr("\nНовые пароли и файл для импорта в менеджер — в окне, вкладка «Пароли».",
             "\nNew passwords and a file to import into the manager are in the window, Passwords tab."))
    return 0


def find_pests() -> int:
    """python -m whyslow --pests: скрытые майнеры, расширения браузеров, ночной выход в сеть.
    Только показывает; закрыть, убрать в карантин или снять политику — в окне, после подтверждения."""
    from . import license
    try:
        pests = premium.load("pests") if license.has_premium("pests") else None
    except premium.PremiumLocked:
        pests = None
    if pests is None:
        print(tr("«Тихие вредители» доступны по подписке. Ключ вводится в окне WhySlow.",
                 "“Hidden pests” is part of the subscription. Enter your key in the WhySlow window."))
        return 2
    kb = knowledge.load()
    r = pests.scan(kb, progress=lambda t: print("  " + t))
    icons = {pests.OK: "[ OK ]", pests.INFO: "[ i  ]", pests.WARN: "[ !  ]", pests.BAD: "[!!!!]"}
    print("\n" + r.headline())
    print(tr("\nПохоже на майнеры:", "\nLooks like miners:") if r.miners else tr("\nМайнеров не видно.", "\nNo miners seen."))
    for s in r.miners:
        print(f"{icons[s.level]} {s.name}  {tr('ЦП', 'CPU')} {s.cpu:.0f}%  GPU {s.gpu:.0f}%")
        for why in s.reasons:
            print(f"        {why}")
    print(tr(f"\nРасширения браузеров: {len(r.extensions)}", f"\nBrowser extensions: {len(r.extensions)}"))
    for e in r.extensions:
        if e.level in (pests.WARN, pests.BAD):
            print(f"{icons[e.level]} {e.name} ({e.browser})")
            for why in e.reasons:
                print(f"        {why}")
    if r.night:
        print(tr("\nВыходили в сеть ночью, пока за компьютером никого не было:",
                 "\nWent online at night while nobody was at the computer:"))
        for n in r.night:
            print(f"{icons[n.level]} {n.name}  {tr('ночей', 'nights')}: {n.nights}  {n.note}")
    print(tr("\nЗакрыть, убрать в карантин или удалить расширение — в окне, вкладка «Вредители».",
             "\nClose, quarantine or remove an extension in the window, Pests tab."))
    return 0


def license_cmd(key: str = "") -> int:
    """Состояние подписки; с ключом — активирует его."""
    from . import license
    if key:
        try:
            license.enter_key(key)
        except license.LicenseError as e:
            print(tr(f"Ключ не принят: {e}", f"Key not accepted: {e}"))
            return 1
    else:
        license.refresh()
    st = license.status()
    print(license.status_text(st))
    if st.error:
        print(st.error)
    print(tr(f"Код этого компьютера: {st.device}", f"This computer's code: {st.device}"))
    return 0 if st.active else 1
