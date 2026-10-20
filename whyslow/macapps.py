"""Данные программ на Mac по отдельным программам.

В ~/Library/Application Support, Containers и Group Containers лежат настройки, базы и
скачанное программами. Здесь эти папки собираются в группы «одна программа — одна строка»
и сверяются с установленными приложениями: что используется, что давно не запускали
и что осталось от уже удалённых программ.
"""

from __future__ import annotations

import calendar
import os
import plistlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .i18n import tr
from .util import run

SYSTEM, USED, UNKNOWN, OLD, LEFTOVER, APP = "system", "used", "unknown", "old", "leftover", "app"
OLD_DAYS = 180     # программу не запускали полгода — кандидат на удаление
STALE_DAYS = 30    # папку чужой программы никто не трогал месяц — похоже на остатки

# Папки самой macOS в Application Support, не похожие на bundle id
APPLE_NAMES = {
    "addressbook", "callhistorydb", "callhistorytransactions", "clouddocs", "cloudkit", "crashreporter",
    "dock", "fileprovider", "icloud", "knowledge", "mobilesync", "networkserviceproxy", "quick look",
    "syncservices", "accounts", "animoji", "app store", "familycircle", "icdd", "idleassetsd", "locationaccessstored",
    "photos", "stickers", "videoconference", "voicetrigger", "audiocomponentregistrar", "contextstoreagent",
    "homeenergyd", "maps", "siri", "spotlight", "tipsd", "transparencyd", "cachedelete", "dmd", "dmd.daemon",
    "com.apple.sharedfilelist", "com.apple.touristd", "com.apple.backgroundtaskmanagementagent",
}
_TEAM = re.compile(r"^[A-Z0-9]{10}\.")
_SKIP = {"com.docker.docker"}  # у Docker своя категория
_ALIASES = {"ms": "microsoft", "office": "microsoft"}  # общие папки Microsoft Office: UBF8T346G9.ms


@dataclass
class App:
    name: str
    bundle_id: str
    path: str
    names: set[str] = field(default_factory=set)
    last_used: float = 0  # 0 — macOS не знает


@dataclass
class Group:
    key: str
    title: str
    paths: list[str] = field(default_factory=list)
    app: App | None = None
    related: App | None = None   # не сама программа, но того же разработчика
    bundle_id: str = ""
    system: bool = False


def read_plist(path) -> dict:
    """plist как словарь; битый или чужой формат — пустой словарь. Программы кладут в бандлы
    что угодно: plistlib на кривом XML бросает ExpatError, которая не наследует ValueError."""
    try:
        with open(path, "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _info(app: Path) -> dict:
    """Info.plist приложения, в том числе программ для iPhone и iPad, запущенных на Mac."""
    cands = [app / "Contents/Info.plist", app / "WrappedBundle/Info.plist"]
    wrapper = app / "Wrapper"
    if wrapper.is_dir():
        cands += [p / "Info.plist" for p in wrapper.glob("*.app")]
    for p in cands:
        info = read_plist(p)
        if info:
            return info
    return {}


def installed() -> list[App]:
    roots = [Path("/Applications"), Path("/Applications/Utilities"), Path.home() / "Applications",
             Path("/System/Applications"), Path("/System/Applications/Utilities")]
    seen, apps = set(), []
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for p in entries:
            # программы бывают и в своей папке: /Applications/Adobe Photoshop/Adobe Photoshop.app
            for app in [p] if p.suffix == ".app" else (list(p.glob("*.app")) if p.is_dir() and not p.is_symlink() else []):
                if str(app) in seen:
                    continue
                seen.add(str(app))
                info = _info(app)
                bid = str(info.get("CFBundleIdentifier") or "").lower()
                names = {app.stem.lower()} | {str(info.get(k)).lower() for k in
                                              ("CFBundleName", "CFBundleDisplayName", "CFBundleExecutable") if info.get(k)}
                apps.append(App(info.get("CFBundleDisplayName") or info.get("CFBundleName") or app.stem, bid, str(app), names))
    _last_used([a for a in apps if not a.path.startswith("/System/")])
    return apps


def _last_used(apps: list[App]) -> None:
    """Когда программу последний раз открывали — то же, что «Дата последнего открытия» в Finder."""
    for i in range(0, len(apps), 100):
        chunk = apps[i:i + 100]
        code, out = run(["mdls", "-name", "kMDItemLastUsedDate", "-raw", *[a.path for a in chunk]], timeout=30)
        if code != 0:
            return
        for a, val in zip(chunk, out.split("\0")):
            try:
                a.last_used = calendar.timegm(time.strptime(val.strip()[:19], "%Y-%m-%d %H:%M:%S"))  # mdls пишет в UTC
            except ValueError:
                pass


def _container_id(path: str) -> str:
    """Контейнеры программ для iPhone и iPad названы UUID; настоящий id — в служебном файле."""
    return str(read_plist(os.path.join(path, ".com.apple.containermanagerd.metadata.plist")).get("MCMMetadataIdentifier") or "")


def _vendor(bid: str) -> str:
    parts = bid.split(".")
    return ".".join(parts[:2]) if len(parts) >= 3 else ""


def _is_bundle_id(name: str) -> bool:
    return bool(re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9_-]+){2,}", name))


def groups(lib: Path, exclude: set[str]) -> list[Group]:
    apps = installed()
    by_id = {a.bundle_id: a for a in apps if a.bundle_id}
    by_name = {n: a for a in apps for n in a.names}
    vendors = {}
    for a in apps:
        v = _vendor(a.bundle_id)
        if v and not v.startswith("com.apple"):
            vendors.setdefault(v, a)
            vendors.setdefault(v.split(".")[1], a)  # «google» → Google Chrome
    out: dict[str, Group] = {}

    def add(path: str, name: str):
        low = _TEAM.sub("", name).lower().removeprefix("group.")  # UBF8T346G9.ms, group.com.x
        app = by_id.get(low) or by_name.get(low)
        if not app and _is_bundle_id(low):  # помощник программы: com.microsoft.teams2.helper → com.microsoft.teams2
            app = next((by_id[p] for p in _prefixes(low) if p in by_id), None)
        key = app.bundle_id or app.path if app else low
        g = out.get(key)
        if not g:
            system = low.startswith("com.apple.") or low in APPLE_NAMES or bool(app and app.path.startswith("/System/"))
            related = None
            if not app and not system:
                related = vendors.get(_vendor(low)) if _is_bundle_id(low) else \
                    vendors.get(_ALIASES.get(low, low.replace(" ", ""))) or next((a for n, a in by_name.items()
                                                                if len(low) > 3 and n.startswith(low + " ")), None)
            title = app.name if app else (name if not _is_bundle_id(low) else _pretty(name))
            g = out[key] = Group(key, title, app=app, related=related, system=system,
                                 bundle_id=(app.bundle_id if app else low if _is_bundle_id(low) else ""))
        g.paths.append(path)

    for sub in ("Application Support", "Containers", "Group Containers"):
        root = lib / sub
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            p = str(root / n)
            if n.startswith(".") or p in exclude or n.lower() in _SKIP or not os.path.isdir(p):
                continue
            ident = _container_id(p) if sub == "Containers" else ""
            add(p, ident or n)
    # Мелочи той же программы: настройки, сохранённые окна, cookies
    for g in out.values():
        if not g.bundle_id or g.system:
            continue
        for extra in (lib / "Preferences" / f"{g.bundle_id}.plist", lib / "Saved Application State" / f"{g.bundle_id}.savedState",
                      lib / "HTTPStorages" / g.bundle_id, lib / "WebKit" / g.bundle_id):
            if extra.exists() and str(extra) not in g.paths:
                g.paths.append(str(extra))
    return list(out.values())


def _prefixes(bid: str) -> list[str]:
    parts = bid.split(".")
    return [".".join(parts[:i]) for i in range(len(parts) - 1, 2, -1)]


def _pretty(bid: str) -> str:
    """com.miHoYo.GenshinImpact → GenshinImpact (com.miHoYo.GenshinImpact)"""
    last = bid.split(".")[-1]
    return f"{last} ({bid})" if last else bid


def classify(g: Group, newest: float, now: float | None = None) -> tuple[str, str]:
    """(уровень, пометка для человека)."""
    now = now or time.time()
    if g.system:
        return SYSTEM, tr("Системное", "System")
    app = g.app or g.related
    if app:
        used = app.last_used or 0
        if used and now - used > OLD_DAYS * 86400:
            months = int((now - used) // (30 * 86400))
            return OLD, tr(f"Не открывали {months} мес.", f"Unused for {months} months")
        if g.app:
            return USED, tr("Используется", "In use")
        vendor = (app.bundle_id.split(".") + ["", ""])[1].capitalize() or app.name
        return USED, tr(f"Программы {vendor}", f"{vendor} apps")
    if newest and now - newest > STALE_DAYS * 86400:
        return LEFTOVER, tr("Остатки удалённой", "Removed app leftovers")
    return UNKNOWN, tr("Программа не найдена", "App not found")
