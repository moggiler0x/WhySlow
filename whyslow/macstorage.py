"""Из чего состоят «Системные данные» в настройках хранилища macOS.

Apple показывает их одной серой полосой без подробностей. Здесь они разложены по категориям:
кеши, логи, локальные снимки Time Machine, копии iPhone и iPad, установщики macOS, Xcode,
Docker и так далее. У каждой категории пометка: важное (не трогать), можно удалить или
macOS удалит сама. Удаление только после подтверждения: файлы уходят в Корзину и
записываются в журнал, откуда их можно вернуть на место.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import journal, junk, settings
from .i18n import tr
from .util import IS_MAC, human_size, run

KEEP, SAFE, AUTO, MIXED = "keep", "safe", "auto", "mixed"


def level_label(level: str) -> str:
    return {KEEP: tr("Важное, не трогать", "Important, keep"),
            SAFE: tr("Можно удалить", "Safe to delete"),
            AUTO: tr("Удалится само", "Removed automatically"),
            MIXED: tr("Смотря что", "Depends")}[level]


@dataclass
class Item:
    path: str
    name: str
    size: int = 0
    mtime: float = 0
    note: str = ""
    paths: list[str] = field(default_factory=list)  # что удалять; пусто — сам path
    level: str = ""          # своя пометка пункта (данные программ), иначе как у категории
    tag: str = ""            # её текст
    deletable: bool = True

    @property
    def targets(self) -> list[str]:
        return self.paths or [self.path]


@dataclass
class Category:
    key: str
    title: str
    note: str                    # что это такое
    level: str                   # KEEP / SAFE / AUTO
    paths: list[Path] = field(default_factory=list)
    method: str = "trash"        # trash — в Корзину, empty — навсегда, snapshots — tmutil, "" — только показать
    advice: str = ""             # как освободить место, если WhySlow сам не удаляет
    split: bool = True           # пункты — содержимое папок (иначе сами папки)
    fda: bool = False            # без «Полного доступа к диску» не прочитать
    auto: bool = False           # можно включить в автоочистку
    exclude: list[Path] = field(default_factory=list)
    items: list[Item] = field(default_factory=list)
    size: int = 0
    denied: bool = False         # macOS не дала прочитать
    size_known: bool = True

    @property
    def deletable(self) -> bool:
        return bool(self.method)


def categories() -> list[Category]:
    home = Path.home()
    lib = home / "Library"
    dev = lib / "Developer"
    docker = lib / "Containers/com.docker.docker"
    mail_dl = lib / "Containers/com.apple.mail/Data/Library/Mail Downloads"
    backups = lib / "Application Support/MobileSync/Backup"
    return [
        Category("caches", tr("Кеши программ", "App caches"),
                 tr("Временные копии, которые программы держат для скорости. Пересоздаются сами, "
                    "первый запуск программы после удаления может быть чуть медленнее.",
                    "Temporary copies apps keep to run faster. They are recreated on their own; "
                    "the first launch after deleting may be a little slower."),
                 SAFE, [lib / "Caches"], auto=True),
        Category("sys_caches", tr("Системные кеши", "System caches"),
                 tr("Кеши macOS и служб для всех пользователей. Система чистит их сама.",
                    "Caches of macOS and its services for all users. The system cleans them itself."),
                 AUTO, [Path("/Library/Caches")], method=""),
        Category("logs", tr("Логи программ", "App logs"),
                 tr("Журналы работы и отчёты о сбоях. Нужны только для поиска ошибок.",
                    "Activity logs and crash reports. Only needed for troubleshooting."),
                 SAFE, [lib / "Logs"], auto=True),
        Category("sys_logs", tr("Системные логи", "System logs"),
                 tr("Журналы macOS. Старые записи система удаляет сама.",
                    "macOS logs. The system removes old entries itself."),
                 AUTO, [Path("/Library/Logs"), Path("/private/var/log")], method=""),
        Category("snapshots", tr("Локальные снимки Time Machine", "Local Time Machine snapshots"),
                 tr("Копии диска на самом Mac на случай, если внешний диск Time Machine не подключён. "
                    "macOS удаляет их сама через сутки или когда нужно место. Размер снимков macOS не сообщает.",
                    "Copies of the disk on the Mac itself, for when the Time Machine drive isn't connected. "
                    "macOS removes them after a day or when space is needed. macOS doesn't report their size."),
                 AUTO, method="snapshots", split=False),
        Category("ios_backups", tr("Резервные копии iPhone и iPad", "iPhone and iPad backups"),
                 tr("Полные копии устройств. Удаляйте только старые копии устройств, которых у вас больше нет, "
                    "или если копия есть в iCloud.",
                    "Full device backups. Only delete old backups of devices you no longer have, "
                    "or if the backup is also in iCloud."),
                 KEEP, [backups], fda=True),
        Category("installers", tr("Установщики macOS", "macOS installers"),
                 tr("Программы «Установка macOS…». Нужны только для переустановки, их можно скачать заново из App Store.",
                    "“Install macOS…” apps. Only needed to reinstall; they can be downloaded again from the App Store."),
                 SAFE, [Path("/Applications")], split=False),
        Category("updates", tr("Загруженные обновления macOS", "Downloaded macOS updates"),
                 tr("Обновления, которые ждут установки. После установки macOS удаляет их сама.",
                    "Updates waiting to be installed. macOS removes them after installing."),
                 AUTO, [Path("/Library/Updates")], method=""),
        Category("derived", tr("Xcode: промежуточные сборки", "Xcode: DerivedData"),
                 tr("Результаты сборки проектов. Xcode соберёт заново, когда понадобится.",
                    "Build products of projects. Xcode rebuilds them when needed."),
                 SAFE, [dev / "Xcode/DerivedData"], auto=True),
        Category("device_support", tr("Xcode: поддержка устройств", "Xcode: device support"),
                 tr("Символы для отладки на iPhone и iPad. Для старых версий iOS не нужны, Xcode скачает заново.",
                    "Debug symbols for iPhone and iPad. Not needed for old iOS versions; Xcode downloads them again."),
                 SAFE, [dev / "Xcode/iOS DeviceSupport", dev / "Xcode/watchOS DeviceSupport"]),
        Category("archives", tr("Xcode: архивы приложений", "Xcode: archives"),
                 tr("Собранные версии ваших приложений для App Store. Нужны для отладки отчётов о сбоях.",
                    "Built versions of your apps for the App Store. Needed to symbolicate crash reports."),
                 KEEP, [dev / "Xcode/Archives"]),
        Category("simulators", tr("Симуляторы iOS", "iOS simulators"),
                 tr("Данные симуляторов: установленные приложения и их файлы.",
                    "Simulator data: installed apps and their files."),
                 KEEP, [dev / "CoreSimulator/Devices"], method="",
                 advice=tr("Ненужные симуляторы удаляйте в Xcode → Window → Devices and Simulators "
                           "или командой xcrun simctl delete unavailable.",
                           "Remove unneeded simulators in Xcode → Window → Devices and Simulators "
                           "or with xcrun simctl delete unavailable.")),
        Category("sim_caches", tr("Кеши симуляторов", "Simulator caches"),
                 tr("Временные файлы симуляторов, пересоздаются сами.", "Simulator temporary files, recreated on their own."),
                 SAFE, [dev / "CoreSimulator/Caches"], auto=True),
        Category("docker", tr("Docker", "Docker"),
                 tr("Образ диска с контейнерами, образами и томами Docker.",
                    "The disk image with Docker containers, images and volumes."),
                 KEEP, [docker], method="", split=False, fda=True,
                 advice=tr("Освободите место в Docker Desktop → Troubleshoot → Clean / Purge data "
                           "или командой docker system prune.",
                           "Free space in Docker Desktop → Troubleshoot → Clean / Purge data "
                           "or with docker system prune.")),
        Category("mail", tr("Вложения Почты", "Mail attachments"),
                 tr("Копии вложений, которые вы открывали. Сами письма и вложения остаются в Почте.",
                    "Copies of attachments you opened. The emails and attachments stay in Mail."),
                 SAFE, [mail_dl], fda=True),
        Category("apps", tr("Данные программ", "App data"),
                 tr("Настройки, базы и скачанное программами, по программам. Данные удалённых и давно не запускавшихся "
                    "программ можно убрать; у используемых программ после удаления сбросятся настройки и вход. "
                    "Кеши программ — в своей категории.",
                    "Settings, databases and downloads, grouped by app. Data of removed apps and apps you haven't "
                    "opened in a long time can go; apps you use will lose their settings and sign-in. "
                    "App caches are a separate category."),
                 MIXED, [lib], fda=True, exclude=[backups.parent, docker, mail_dl]),
        Category("trash", tr("Корзина", "Trash"),
                 tr("Файлы, которые вы уже удалили. Место освобождается только после очистки Корзины.",
                    "Files you already deleted. The space is only freed once the Trash is emptied."),
                 SAFE, [home / ".Trash"], method="empty", fda=True),
        Category("vm", tr("Файл подкачки и сна", "Swap and sleep image"),
                 tr("Память, которую macOS сбросила на диск. Управляется системой.",
                    "Memory macOS moved to disk. Managed by the system."),
                 AUTO, [Path("/private/var/vm")], method=""),
    ]


# ---------------------------------------------------------------- размеры ---

def _usage(st: os.stat_result) -> int:
    """Сколько файл занимает на диске: разреженные образы (Docker.raw) меньше своего размера."""
    blocks = getattr(st, "st_blocks", None)
    return blocks * 512 if blocks is not None else st.st_size


def du(path: str, exclude: set[str] = frozenset()) -> tuple[int, float, bool]:
    """Размер папки или файла: (байт, время последнего изменения, был ли отказ в доступе)."""
    try:
        st = os.lstat(path)
    except PermissionError:
        return 0, 0, True
    except OSError:
        return 0, 0, False
    if not stat.S_ISDIR(st.st_mode):
        return _usage(st), st.st_mtime, False
    total, newest, denied = 0, st.st_mtime, False
    stack = [path]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                for e in it:
                    try:
                        s = e.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(s.st_mode):
                        if e.path not in exclude:
                            stack.append(e.path)
                    else:
                        total += _usage(s)
                        newest = max(newest, s.st_mtime)
        except PermissionError:
            denied = True
        except OSError:
            pass
    return total, newest, denied


def _children(root: Path) -> list[str] | None:
    """Содержимое папки; None — нет доступа."""
    try:
        return [str(root / n) for n in os.listdir(root) if n != ".DS_Store"]
    except PermissionError:
        return None
    except OSError:
        return []


def scan(cat: Category) -> Category:
    cat.items, cat.denied, cat.size_known = [], False, True
    if cat.method == "snapshots":
        cat.items = snapshots()
        cat.size_known = False
        cat.size = 0
        return cat
    exclude = {str(e) for e in cat.exclude}
    if cat.key == "apps":
        return _scan_apps(cat, exclude)
    for root in cat.paths:
        if cat.key == "installers":
            names = _children(root) or []
            paths = [p for p in names if os.path.basename(p).startswith(("Install macOS", "Установка macOS"))]
        elif cat.split:
            paths = _children(root)
            if paths is None:
                cat.denied = True
                continue
        else:
            paths = [str(root)] if root.exists() else []
        for p in paths:
            if p in exclude:
                continue
            size, mtime, denied = du(p, exclude)
            cat.denied |= denied
            if size or cat.key in ("installers", "ios_backups"):
                cat.items.append(Item(p, os.path.basename(p), size, mtime, _item_note(cat, p)))
    cat.items.sort(key=lambda i: -i.size)
    cat.size = sum(i.size for i in cat.items)
    return cat


def _scan_apps(cat: Category, exclude: set[str]) -> Category:
    """Данные программ: строка на программу, с пометкой по тому, установлена ли она и давно ли её открывали."""
    from . import macapps
    lib = cat.paths[0]
    if _children(lib / "Containers") is None:
        cat.denied = True
    for g in macapps.groups(lib, exclude):
        size = newest = 0
        for p in g.paths:
            sz, mt, denied = du(p, exclude)
            size += sz
            newest = max(newest, mt)
            cat.denied |= denied
        if not size:
            continue
        level, tag = macapps.classify(g, newest)
        used = g.app.last_used if g.app else 0
        cat.items.append(Item(g.paths[0], g.title, size, used or newest, "", list(g.paths),
                              level, tag, deletable=level != macapps.SYSTEM))
        if level == macapps.OLD and g.app and not g.app.path.startswith("/System/"):
            # давно не запускали — можно убрать и саму программу
            sz, mt, _ = du(g.app.path)
            cat.items.append(Item(g.app.path, tr(f"{g.title} — сама программа", f"{g.title}: the app itself"), sz,
                                  used, "", [], macapps.APP, tr("Удалить и программу", "Remove the app too")))
    cat.items.sort(key=lambda i: -i.size)
    cat.size = sum(i.size for i in cat.items if i.level != macapps.APP)  # программы не входят в «Системные данные»
    return cat


def _item_note(cat: Category, path: str) -> str:
    if cat.key == "ios_backups":
        try:
            with open(os.path.join(path, "Info.plist"), "rb") as f:
                info = plistlib.load(f)
            name = info.get("Device Name") or info.get("Display Name") or ""
            when = info.get("Last Backup Date")
            return f"{name} · {when:%d.%m.%Y}" if when else name
        except (OSError, ValueError, plistlib.InvalidFileException):
            return ""
    return ""


def scan_all(progress=lambda _t: None) -> list[Category]:
    cats = []
    for c in categories():
        progress(tr(f"Считаю: {c.title}…", f"Measuring: {c.title}…"))
        cats.append(scan(c))
    cats.sort(key=lambda c: (not c.size_known, -c.size))
    return cats


# ------------------------------------------------- снимки Time Machine ---

def snapshots() -> list[Item]:
    code, out = run(["tmutil", "listlocalsnapshots", "/"], timeout=15)
    if code != 0:
        return []
    items = []
    for line in out.splitlines():
        line = line.strip()
        if not line.startswith("com.apple.TimeMachine."):
            continue
        date = line.removeprefix("com.apple.TimeMachine.").removesuffix(".local")
        items.append(Item(date, line, note=date))
    return items


def _delete_snapshots(dates: list[str]) -> tuple[int, int]:
    """macOS спросит пароль администратора один раз на все снимки."""
    safe = [d for d in dates if all(ch.isdigit() or ch == "-" for ch in d)]
    if not safe:
        return 0, len(dates)
    script = "; ".join(f"tmutil deletelocalsnapshots {d}" for d in safe)
    code, _ = run(["osascript", "-e", f'do shell script "{script}" with administrator privileges'], timeout=300)
    return (len(safe), len(dates) - len(safe)) if code == 0 else (0, len(dates))


# ------------------------------------------------------- полный доступ ---

def has_full_disk_access() -> bool:
    """Без «Полного доступа к диску» macOS прячет Почту, Safari, копии iPhone, Корзину и часть Library."""
    if not IS_MAC:
        return True
    for p in (Path.home() / "Library/Application Support/com.apple.TCC/TCC.db", Path.home() / "Library/Safari"):
        try:
            if p.is_dir():
                os.listdir(p)
            else:
                with open(p, "rb"):
                    pass
            return True
        except PermissionError:
            return False
        except OSError:
            continue
    return True


def fda_hint() -> str:
    from .updater import FROZEN
    who = "WhySlow" if FROZEN else tr("Терминал (или ту программу, из которой запускаете WhySlow)",
                                      "Terminal (or whichever app you start WhySlow from)")
    return tr(f"Откройте Системные настройки → Конфиденциальность и безопасность → Полный доступ к диску, "
              f"включите {who} (кнопка «+», если его нет в списке) и перезапустите WhySlow. "
              "Без этого часть категорий не посчитать: копии iPhone, Почта, Корзина, данные программ.",
              f"Open System Settings → Privacy & Security → Full Disk Access, turn on {who} "
              "(the “+” button if it's not listed) and restart WhySlow. Without it some categories "
              "can't be measured: iPhone backups, Mail, Trash, app data.")


def open_fda_settings() -> None:
    run(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"])


# ---------------------------------------------------------------- удаление ---

def _trash_dest(path: str) -> Path:
    trash = Path.home() / ".Trash"
    dest = trash / os.path.basename(path)
    if dest.exists():
        dest = trash / f"{dest.stem} {time.strftime('%H.%M.%S')}{dest.suffix}"
    return dest


_FINDER = ["-e", "on run argv", "-e", "tell application \"Finder\" to set r to delete (POSIX file (item 1 of argv) as alias)",
           "-e", "return POSIX path of (r as alias)", "-e", "end run"]


def _to_trash(path: str) -> str | None:
    """Перемещает в Корзину, возвращает новый путь. Сначала сами, а если macOS не пускает
    в ~/.Trash (нет полного доступа) или файл на другом диске — через Finder, как «Переместить в Корзину»."""
    dest = _trash_dest(path)
    try:
        if os.lstat(path).st_dev == os.lstat(dest.parent).st_dev:
            os.rename(path, dest)
            return str(dest)
    except OSError:
        pass
    code, out = run(["osascript", *_FINDER, path], timeout=60)
    return out.strip().rstrip("/") if code == 0 and out.strip() else None


def delete(chosen: list[tuple[Category, Item]], progress=lambda _t: None) -> tuple[int, int]:
    """Удаляет выбранное: в Корзину, из Корзины навсегда или снимки через tmutil.
    Возвращает (сколько пунктов удалено, сколько не удалось) и пишет всё в журнал."""
    moved: list[list[str]] = []
    done = failed = freed = 0
    erased = 0
    snaps = [it.path for c, it in chosen if c.method == "snapshots"]
    for cat, it in chosen:
        if cat.method == "trash":
            progress(tr(f"В Корзину: {it.name}", f"To Trash: {it.name}"))
            ok = True
            for path in it.targets:
                dest = _to_trash(path)
                if dest:
                    moved.append([path, dest])
                else:
                    ok = False
            if ok:
                done += 1
                freed += it.size
            else:
                failed += 1
        elif cat.method == "empty":
            progress(tr(f"Удаляю навсегда: {it.name}", f"Deleting permanently: {it.name}"))
            try:
                if os.path.isdir(it.path) and not os.path.islink(it.path):
                    shutil.rmtree(it.path)
                else:
                    os.remove(it.path)
                done += 1
                erased += it.size
            except OSError:
                failed += 1
    if snaps:
        progress(tr("Удаляю снимки Time Machine (macOS спросит пароль)…",
                    "Deleting Time Machine snapshots (macOS will ask for a password)…"))
        ok, bad = _delete_snapshots(snaps)
        done += ok
        failed += bad
        if ok:
            journal.add("macdata_snapshots", tr(f"Удалено снимков Time Machine: {ok}", f"Deleted Time Machine snapshots: {ok}"),
                        {"dates": snaps}, undoable=False)
    if moved:
        journal.add("macdata_trash",
                    tr(f"Системные данные: в Корзину перемещено {len(moved)} ({human_size(freed)})",
                       f"System Data: moved {len(moved)} items to Trash ({human_size(freed)})"),
                    {"moves": moved})
    if erased:
        journal.add("macdata_empty", tr(f"Очищена Корзина: {human_size(erased)}", f"Emptied the Trash: {human_size(erased)}"),
                    {"freed": erased}, undoable=False)
    return done, failed


def undo(entry: dict) -> None:
    """Возвращает из Корзины на старое место то, что ещё там лежит."""
    missing = []
    for orig, trashed in entry["data"]["moves"]:
        if os.path.exists(orig) or not os.path.exists(trashed):
            missing.append(os.path.basename(orig))
            continue
        os.makedirs(os.path.dirname(orig), exist_ok=True)
        os.rename(trashed, orig)
    if missing:
        raise OSError(tr("Не вернулось (Корзину уже очистили или на месте новый файл): ",
                         "Not restored (the Trash was emptied or a new file is in place): ") + ", ".join(missing))


# ------------------------------------------------------------- автоочистка ---

AUTO_AGE_DAYS = 7  # автоочистка трогает только файлы, которые не менялись неделю
INTERVALS = (1, 7, 30)


def auto_categories() -> list[Category]:
    return [c for c in categories() if c.auto]


def autoclean_due() -> bool:
    if not IS_MAC or not settings.get("mac_autoclean"):
        return False
    return time.time() - settings.get("mac_autoclean_last") >= settings.get("mac_autoclean_days") * 86400


def autoclean(progress=lambda _t: None) -> tuple[int, int]:
    """Удаляет из безопасных категорий файлы старше недели. Сразу, без Корзины: смысл
    автоочистки в том, чтобы место освобождалось само. Возвращает (байт, пропущено файлов)."""
    freed = failed = 0
    names = []
    for c in auto_categories():
        progress(tr(f"Автоочистка: {c.title}…", f"Auto-clean: {c.title}…"))
        f, bad = junk.remove_old(c.paths, AUTO_AGE_DAYS * 24, c.exclude)
        freed += f
        failed += bad
        if f:
            names.append(c.title)
    settings.set("mac_autoclean_last", time.time())
    journal.add("macdata_auto",
                tr(f"Автоочистка системных данных: освобождено {human_size(freed)}", f"System Data auto-clean: freed {human_size(freed)}")
                + (f" ({', '.join(names)})" if names else ""),
                {"freed": freed}, undoable=False)
    return freed, failed


def autoclean_text() -> str:
    """Что делает автоочистка — одной строкой для окна и текстового режима."""
    titles = ", ".join(c.title for c in auto_categories())
    return tr(f"Удаляет файлы старше {AUTO_AGE_DAYS} дней из категорий: {titles}. Важное не трогает.",
              f"Deletes files older than {AUTO_AGE_DAYS} days from: {titles}. Never touches anything important.")
