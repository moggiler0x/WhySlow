"""Переезд на новый ПК одной кнопкой (функция подписки).

На старом компьютере WhySlow собирает «пакет переезда» в папку на флешке, внешнем
или сетевом диске: список установленных программ (с командами winget / Homebrew /
Mac App Store / Flatpak, чтобы поставить их заново), настройки нескольких
известных программ и выбранные папки пользователя — без кешей, временных файлов
и прочего мусора. На новом компьютере WhySlow открывает пакет, ставит отмеченные
программы и раскладывает настройки и файлы по местам.

Пакет работает и без WhySlow: рядом лежат install.bat / install.command (программы)
и files/restore.bat / restore.command (файлы). Файлы на новом компьютере не
перезаписываются, а настройки, которые там уже есть, откладываются в резервную
копию — их можно вернуть из «Журнала».
"""

from __future__ import annotations

import json
import os
import platform
import plistlib
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import journal, reinstall
from ..i18n import tr
from ..util import IS_MAC, IS_WINDOWS, backup_dir, human_size, run

FEATURE = "migrate"   # ключ функции в подписке
MANIFEST = "whyslow-move.json"
FORMAT = 1

WINGET, CASK, BREW, MAS, APPSTORE, FLATPAK, VSCODE, MANUAL = (
    "winget", "cask", "brew", "mas", "appstore", "flatpak", "vscode", "manual")
# Чем ставится программа и на какой ОС это работает
SOURCE_OS = {WINGET: {"windows"}, CASK: {"mac"}, BREW: {"mac", "linux"}, MAS: {"mac"}, FLATPAK: {"linux"},
             VSCODE: {"windows", "mac", "linux"}}
TOOL = {WINGET: "winget", CASK: "brew", BREW: "brew", MAS: "mas", FLATPAK: "flatpak", VSCODE: "code"}


class MigrateError(Exception):
    pass


def premium() -> bool:
    """Открыта ли функция. Без модуля подписки — закрыта, чтобы сборка без него не раздавала её даром."""
    try:
        from .. import license as lic
    except ImportError:
        return False
    try:
        return bool(lic.has_premium(FEATURE))
    except Exception:
        return False


def _require() -> None:
    if not premium():
        raise MigrateError(tr("Переезд на новый ПК доступен по подписке WhySlow. Введите ключ, чтобы открыть его.",
                              "Moving to a new PC is part of the WhySlow subscription. Enter a key to unlock it."))


# ------------------------------------------------------------------ программы ---

@dataclass
class Program:
    name: str
    source: str           # winget / cask / brew / mas / appstore / flatpak / vscode / manual
    pkg: str = ""         # идентификатор пакета для source
    version: str = ""
    publisher: str = ""

    @property
    def installable(self) -> bool:
        return self.source in TOOL

    def to_json(self) -> dict:
        return {"name": self.name, "source": self.source, "pkg": self.pkg, "version": self.version,
                "publisher": self.publisher}

    @staticmethod
    def from_json(d: dict) -> "Program":
        return Program(str(d.get("name") or d.get("pkg") or "?"), str(d.get("source") or MANUAL), str(d.get("pkg") or ""),
                       str(d.get("version") or ""), str(d.get("publisher") or ""))


def source_label(source: str) -> str:
    return {WINGET: "winget", CASK: "Homebrew", BREW: "Homebrew", MAS: "App Store", FLATPAK: "Flatpak",
            VSCODE: tr("расширение VS Code", "VS Code extension"),
            APPSTORE: tr("App Store, вручную", "App Store, by hand"),
            MANUAL: tr("вручную", "by hand")}.get(source, source)


def _norm(s: str) -> str:
    return re.sub(r"[^0-9a-zа-яё]+", "", s.lower())


# Библиотеки и обновления: их ставят сами программы, переносить их отдельно незачем
_RUNTIME = re.compile(r"redistributable|runtime|^microsoft visual c\+\+|^update for|^security update|^hotfix|"
                      r"^microsoft \.net|^windows sdk|^microsoft edge webview|driver|драйвер", re.I)


def _win_registry_programs() -> list[Program]:
    try:
        import winreg
    except ImportError:
        return []
    keys = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
            (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")]
    out, seen = [], set()
    for hive, path in keys:
        try:
            root = winreg.OpenKey(hive, path)
        except OSError:
            continue
        for i in range(winreg.QueryInfoKey(root)[0]):
            try:
                sub = winreg.OpenKey(root, winreg.EnumKey(root, i))
            except OSError:
                continue

            def val(name, default=""):
                try:
                    return winreg.QueryValueEx(sub, name)[0]
                except OSError:
                    return default
            name = str(val("DisplayName")).strip()
            if not name or val("SystemComponent", 0) == 1 or val("ParentKeyName") or _RUNTIME.search(name):
                continue
            if _norm(name) in seen:
                continue
            seen.add(_norm(name))
            out.append(Program(name, MANUAL, version=str(val("DisplayVersion")), publisher=str(val("Publisher"))))
    return out


def _winget_ids() -> list[str]:
    """Пакеты, которые winget знает и может поставить заново (winget export)."""
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "winget.json"
        run(["winget", "export", "-o", str(out), "--accept-source-agreements", "--disable-interactivity"], timeout=180)
        try:
            data = json.loads(out.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            return []
    ids = []
    for src in data.get("Sources") or []:
        for p in src.get("Packages") or []:
            if p.get("PackageIdentifier"):
                ids.append(p["PackageIdentifier"])
    return ids


def match_ids(programs: list[Program], ids: list[str], source: str) -> list[Program]:
    """Сопоставляет идентификаторы пакетов с найденными программами по имени.
    «Mozilla.Firefox» → «Mozilla Firefox (x64 ru)», «google-chrome» → «Google Chrome».
    Не нашедшие пары пакеты добавляются отдельными строками."""
    extra = []
    for pid in ids:
        tail = pid.split(".", 1)[1] if "." in pid and source == WINGET else pid
        key, whole = _norm(tail), _norm(pid)
        hit = None
        if len(key) >= 3:
            hit = next((p for p in programs if p.source == MANUAL and (key in _norm(p.name) or _norm(p.name) == whole)), None)
        if hit:
            hit.source, hit.pkg = source, pid
        else:
            extra.append(Program(tail.replace("-", " ").replace(".", " ") if source != WINGET else pid, source, pid))
    return programs + extra


def _mac_apps() -> list[tuple[str, Path, str]]:
    """(имя, путь, bundle id) программ в /Applications и ~/Applications, кроме системных Apple."""
    out = []
    for base in (Path("/Applications"), Path.home() / "Applications"):
        for app in sorted(base.glob("*.app")):
            try:
                with open(app / "Contents" / "Info.plist", "rb") as f:
                    info = plistlib.load(f)
            except (OSError, ValueError, plistlib.InvalidFileException):
                info = {}
            bid = str(info.get("CFBundleIdentifier") or "")
            if bid.startswith("com.apple.") and not (app / "Contents" / "_MASReceipt").exists():
                continue
            out.append((app.stem, app, bid))
    return out


def _lines(cmd: list[str], timeout: float = 60) -> list[str]:
    code, out = run(cmd, timeout=timeout)
    return [ln.strip() for ln in out.splitlines() if ln.strip()] if code == 0 else []


def _mac_programs() -> list[Program]:
    progs, mas_ids = [], {}
    for ln in _lines(["mas", "list"]):  # «497799835  Xcode  (15.0)»
        m = re.match(r"(\d+)\s+(.+?)\s+\(", ln)
        if m:
            mas_ids[_norm(m.group(2))] = m.group(1)
    for name, path, _bid in _mac_apps():
        if (path / "Contents" / "_MASReceipt").exists():
            pid = mas_ids.get(_norm(name), "")
            progs.append(Program(name, MAS if pid else APPSTORE, pid))
        else:
            progs.append(Program(name, MANUAL))
    progs = match_ids(progs, _lines(["brew", "list", "--cask", "-1"]), CASK)
    return match_ids(progs, _lines(["brew", "leaves"]), BREW)


def _linux_programs() -> list[Program]:
    progs = []
    for ln in _lines(["flatpak", "list", "--app", "--columns=application,name"]):
        parts = ln.split("\t")
        progs.append(Program(parts[1] if len(parts) > 1 else parts[0], FLATPAK, parts[0]))
    return progs


def _vscode_extensions() -> list[Program]:
    return [Program(ext, VSCODE, ext) for ext in _lines(["code", "--list-extensions"]) if "." in ext]


def installed_programs() -> list[Program]:
    """Что установлено на этом компьютере и чем это можно поставить заново."""
    if IS_WINDOWS:
        progs = match_ids(_win_registry_programs(), _winget_ids(), WINGET)
    elif IS_MAC:
        progs = _mac_programs()
    else:
        progs = _linux_programs()
    progs.sort(key=lambda p: (not p.installable, p.name.lower()))
    return progs + _vscode_extensions()


def can_install(p: Program) -> bool:
    """Поставится ли программа здесь: подходящая ОС и есть нужный инструмент."""
    return p.installable and reinstall.os_name() in SOURCE_OS.get(p.source, set()) and bool(shutil.which(TOOL[p.source]))


def install_cmd(p: Program) -> list[str]:
    return {WINGET: ["winget", "install", "--id", p.pkg, "-e", "--silent", "--accept-package-agreements",
                     "--accept-source-agreements", "--disable-interactivity"],
            CASK: ["brew", "install", "--cask", p.pkg],
            BREW: ["brew", "install", p.pkg],
            MAS: ["mas", "install", p.pkg],
            FLATPAK: ["flatpak", "install", "-y", "--noninteractive", "flathub", p.pkg],
            VSCODE: ["code", "--install-extension", p.pkg]}[p.source]


def missing_tool_hint(p: Program) -> str:
    """Почему программа не ставится сама и что сделать."""
    if p.source == APPSTORE:
        return tr("Установите из App Store (вкладка «Покупки»).", "Install it from the App Store (Purchased).")
    if p.source == MANUAL or reinstall.os_name() not in SOURCE_OS.get(p.source, set()):
        return tr("Скачайте с сайта программы.", "Download it from the app's website.")
    return {WINGET: tr("Нужен winget — «Установщик приложений» из Microsoft Store.",
                       "winget is needed: “App Installer” from the Microsoft Store."),
            CASK: tr("Нужен Homebrew: brew.sh.", "Homebrew is needed: brew.sh."),
            BREW: tr("Нужен Homebrew: brew.sh.", "Homebrew is needed: brew.sh."),
            MAS: tr("Нужен mas: brew install mas, или установите из App Store.",
                    "mas is needed: brew install mas, or install from the App Store."),
            FLATPAK: tr("Нужен Flatpak с подключённым Flathub.", "Flatpak with Flathub is needed."),
            VSCODE: tr("Сначала установите VS Code и команду code.", "Install VS Code and its code command first.")}[p.source]


def install(p: Program) -> tuple[bool, str]:
    if not can_install(p):
        return False, missing_tool_hint(p)
    code, out = run(install_cmd(p), timeout=1800)
    if code == 0 or re.search(r"already installed|уже установлен", out, re.I):
        return True, ""
    tail = [ln for ln in out.strip().splitlines() if ln.strip()][-1:] or [str(code)]
    return False, tail[0][:200]


# ------------------------------------------------------------------ настройки ---

@dataclass
class AppSettings:
    key: str
    title: str
    note: str
    paths: list[Path]          # для текущей ОС; порядок совпадает между ОС
    preset: bool = True        # отмечено по умолчанию
    size: int = 0

    @property
    def found(self) -> list[tuple[int, Path]]:
        return [(i, p) for i, p in enumerate(self.paths) if p and p.exists()]


# (Windows, macOS, Linux) для каждого файла или папки. None — у этой ОС такого нет.
# Строки с одинаковым номером означают одно и то же на разных ОС: так настройки VS Code
# переезжают и с Windows на Mac.
_SUPPORT = "~/Library/Application Support"
CATALOG = [
    ("vscode", "Visual Studio Code", ("настройки, горячие клавиши, сниппеты", "settings, keybindings, snippets"), True, [
        ("%APPDATA%/Code/User/settings.json", f"{_SUPPORT}/Code/User/settings.json", "~/.config/Code/User/settings.json"),
        ("%APPDATA%/Code/User/keybindings.json", f"{_SUPPORT}/Code/User/keybindings.json", "~/.config/Code/User/keybindings.json"),
        ("%APPDATA%/Code/User/snippets", f"{_SUPPORT}/Code/User/snippets", "~/.config/Code/User/snippets")]),
    ("git", "Git", ("имя, почта, псевдонимы", "name, email, aliases"), True, [
        ("~/.gitconfig", "~/.gitconfig", "~/.gitconfig")]),
    ("chrome", "Google Chrome", ("закладки", "bookmarks"), True, [
        ("%LOCALAPPDATA%/Google/Chrome/User Data/Default/Bookmarks", f"{_SUPPORT}/Google/Chrome/Default/Bookmarks",
         "~/.config/google-chrome/Default/Bookmarks")]),
    ("yandex", tr("Яндекс Браузер", "Yandex Browser"), ("закладки", "bookmarks"), True, [
        ("%LOCALAPPDATA%/Yandex/YandexBrowser/User Data/Default/Bookmarks",
         f"{_SUPPORT}/Yandex/YandexBrowser/Default/Bookmarks", "~/.config/yandex-browser/Default/Bookmarks")]),
    ("edge", "Microsoft Edge", ("закладки", "bookmarks"), True, [
        ("%LOCALAPPDATA%/Microsoft/Edge/User Data/Default/Bookmarks", f"{_SUPPORT}/Microsoft Edge/Default/Bookmarks",
         "~/.config/microsoft-edge/Default/Bookmarks")]),
    ("obs", "OBS Studio", ("сцены и профили", "scenes and profiles"), True, [
        ("%APPDATA%/obs-studio/basic", f"{_SUPPORT}/obs-studio/basic", "~/.config/obs-studio/basic")]),
    ("npp", "Notepad++", ("настройки и горячие клавиши", "settings and shortcuts"), True, [
        ("%APPDATA%/Notepad++/config.xml", None, None), ("%APPDATA%/Notepad++/shortcuts.xml", None, None)]),
    ("winterm", "Windows Terminal", ("профили и оформление", "profiles and look"), True, [
        ("%LOCALAPPDATA%/Packages/Microsoft.WindowsTerminal_8wekyb3d8bbwe/LocalState/settings.json", None, None)]),
    ("powershell", "PowerShell", ("профиль", "profile"), True, [
        ("~/Documents/PowerShell/Microsoft.PowerShell_profile.ps1", None, None),
        ("~/Documents/WindowsPowerShell/Microsoft.PowerShell_profile.ps1", None, None)]),
    ("zsh", "zsh", ("~/.zshrc, ~/.zprofile", "~/.zshrc, ~/.zprofile"), True, [
        (None, "~/.zshrc", "~/.zshrc"), (None, "~/.zprofile", "~/.zprofile")]),
    ("bash", "bash", ("~/.bashrc, ~/.bash_profile", "~/.bashrc, ~/.bash_profile"), True, [
        (None, "~/.bashrc", "~/.bashrc"), (None, "~/.bash_profile", "~/.bash_profile")]),
    ("iterm", "iTerm2", ("настройки", "settings"), True, [
        (None, "~/Library/Preferences/com.googlecode.iterm2.plist", None)]),
    ("fonts", tr("Шрифты", "Fonts"), ("установленные вами шрифты", "fonts you installed"), True, [
        (None, "~/Library/Fonts", "~/.local/share/fonts")]),
    ("ssh", tr("SSH-ключи", "SSH keys"),
     ("ключи попадут в пакет незашифрованными — храните флешку бережно",
      "the keys go into the package unencrypted: keep the drive safe"), False, [
        ("~/.ssh", "~/.ssh", "~/.ssh")]),
]


def _os_index() -> int:
    return 0 if IS_WINDOWS else 1 if IS_MAC else 2


def _expand(s: str | None) -> Path | None:
    if not s:
        return None
    return Path(os.path.expanduser(os.path.expandvars(s)))


def catalog() -> list[AppSettings]:
    """Все известные настройки для этой ОС (и найденные, и нет)."""
    i = _os_index()
    out = []
    for key, title, note, preset, rows in CATALOG:
        paths = [_expand(r[i]) for r in rows]
        out.append(AppSettings(key, title, tr(*note), paths, preset))
    return out


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    total = 0
    for root, _dirs, names in os.walk(p):
        for n in names:
            try:
                total += os.lstat(os.path.join(root, n)).st_size
            except OSError:
                pass
    return total


def found_settings() -> list[AppSettings]:
    """Настройки, которые есть на этом компьютере."""
    out = []
    for s in catalog():
        if s.found:
            s.size = sum(_size(p) for _i, p in s.found)
            out.append(s)
    return out


# ------------------------------------------------------------------ мусор ---

JUNK_DIRS = {"node_modules", "__pycache__", ".cache", "cache", "caches", ".npm", ".yarn", ".gradle", ".m2",
             ".venv", "venv", ".tox", ".pytest_cache", ".mypy_cache", ".next", ".parcel-cache", "$recycle.bin",
             ".trash", ".trashes", ".spotlight-v100", ".fseventsd", ".temporaryitems", "system volume information"}
JUNK_FILES = {"thumbs.db", "ehthumbs.db", "desktop.ini", ".ds_store", ".localized", "iconcache.db"}
JUNK_EXT = (".tmp", ".temp", ".crdownload", ".part", ".partial", ".download", ".swp", ".~tmp")
INSTALLER_EXT = (".exe", ".msi", ".msix", ".dmg", ".pkg", ".deb", ".rpm", ".appimage")


def is_junk(name: str, is_dir: bool) -> bool:
    n = name.lower()
    if is_dir:
        return n in JUNK_DIRS
    return n in JUNK_FILES or n.endswith(JUNK_EXT) or n.startswith("~$") or n.startswith(".~lock.")


def make_skip(skip_installers: bool = True):
    """Что не брать в пакет: мусор везде, установщики — только из «Загрузок»
    (программы всё равно поставятся заново, а старые установщики — это гигабайты)."""
    def skip(folder, name: str, is_dir: bool) -> bool:
        if is_junk(name, is_dir):
            return True
        return (skip_installers and not is_dir and folder.path.name.lower() == "downloads"
                and name.lower().endswith(INSTALLER_EXT))
    return skip


def measure(folder: reinstall.Folder, skip_installers: bool = True, stop=lambda: False) -> reinstall.Folder:
    """Размер папки без мусора — сколько она займёт в пакете."""
    skip = make_skip(skip_installers)
    return reinstall.measure(folder, stop, skip=lambda n, d: skip(folder, n, d))


# ------------------------------------------------------------------ сборка пакета ---

@dataclass
class Package:
    path: Path
    programs: int = 0
    settings: int = 0
    files: int = 0
    size: int = 0
    failed: list[str] = field(default_factory=list)


def check_dest(dest: Path, folders: list[reinstall.Folder], extra: int = 0) -> str:
    """Почему сюда нельзя сложить пакет. Пустая строка — можно."""
    if not dest.is_dir():
        return tr("Папка для пакета не найдена.", "The folder for the package was not found.")
    d = dest.resolve()
    for f in folders:
        s = f.path.resolve()
        if d == s or s in d.parents or d in s.parents:
            return tr(f"Пакет нельзя класть в переносимую папку «{f.title}» и наоборот.",
                      f"The package can't go inside the moved folder “{f.title}” or the other way round.")
    need = sum(max(f.size, 0) for f in folders) + extra
    try:
        free = shutil.disk_usage(dest).free
    except OSError:
        free = 0
    if need and free < need * 1.02 + 2**24:
        return tr(f"Не хватит места: нужно {human_size(need)}, свободно {human_size(free)}.",
                  f"Not enough space: {human_size(need)} needed, {human_size(free)} free.")
    return ""


def _copy_any(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    else:
        shutil.copy2(src, dst, follow_symlinks=False)


def export(dest: Path, programs: list[Program], settings: list[AppSettings], folders: list[reinstall.Folder],
           skip_installers: bool = True, progress=lambda done, total, name: None, stop=lambda: False) -> Package:
    """Собирает пакет переезда в dest/WhySlow-Move-<дата>. На этом компьютере ничего не меняет."""
    _require()
    why = check_dest(dest, folders, sum(s.size for s in settings))
    if why:
        raise MigrateError(why)
    root = reinstall._unique(dest, time.strftime("WhySlow-Move-%Y-%m-%d-%H%M"))
    root.mkdir(parents=True)
    pkg = Package(root, programs=len(programs))

    saved = []
    for s in settings:
        if stop():
            raise MigrateError(tr("Сборка остановлена.", "Packing stopped."))
        entries = []
        for i, p in s.found:
            try:
                _copy_any(p, root / "settings" / s.key / str(i) / p.name)
            except OSError as e:
                pkg.failed.append(f"{p}: {e}")
                continue
            entries.append({"i": i, "name": p.name})
        if entries:
            saved.append({"key": s.key, "title": s.title, "entries": entries, "size": s.size})
            pkg.settings += 1
        progress(0, 0, s.title)

    files = None
    if folders:
        (root / "files").mkdir()
        res = reinstall.copy_folders(folders, root / "files", progress, stop, skip=make_skip(skip_installers))
        pkg.files, pkg.size = res.copied, res.size
        pkg.failed += res.failed
        files = {"files": res.copied, "size": res.size, "folders": [f.title for f in folders]}

    _write_install_script(root, programs)
    (root / "programs.txt").write_text(programs_text(programs), encoding="utf-8")
    # Манифест пишется последним: если его нет, пакет недособран
    (root / MANIFEST).write_text(json.dumps({
        "format": FORMAT, "created": time.strftime("%Y-%m-%d %H:%M:%S"), "os": reinstall.os_name(),
        "computer": platform.node(),
        "programs": [p.to_json() for p in programs], "settings": saved, "files": files,
        "failed": pkg.failed[:500]}, ensure_ascii=False, indent=1), encoding="utf-8")
    return pkg


def programs_text(programs: list[Program]) -> str:
    lines = [tr("Программы со старого компьютера", "Programs from the old computer"), ""]
    for p in programs:
        how = f"{source_label(p.source)}: {p.pkg}" if p.installable else source_label(p.source)
        ver = f" {p.version}" if p.version else ""
        lines.append(f"- {p.name}{ver}  [{how}]")
    return "\n".join(lines) + "\n"


def _write_install_script(root: Path, programs: list[Program]) -> None:
    """Скрипт установки программ рядом с пакетом — работает и без WhySlow."""
    own = [p for p in programs if p.installable and reinstall.os_name() in SOURCE_OS[p.source]]
    if not own:
        return
    if IS_WINDOWS:
        lines = ["@echo off", "chcp 65001 >nul"]
        lines += [" ".join(f'"{a}"' if " " in a else a for a in install_cmd(p)) for p in own]
        lines += ["echo.", "echo Done / Готово", "pause"]
        (root / "install.bat").write_text("\r\n".join(lines) + "\r\n", encoding="utf-8")
        return
    lines = ["#!/bin/sh"]
    lines += [" ".join(install_cmd(p)) + " || true" for p in own]
    lines.append('echo "Done / Готово"')
    path = root / ("install.command" if IS_MAC else "install.sh")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(0o755)


# ------------------------------------------------------------------ разворачивание ---

def read_package(root: Path) -> dict:
    try:
        data = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MigrateError(tr("В этой папке нет пакета переезда WhySlow (или он собран не до конца).",
                              "This folder has no WhySlow move package (or it was not finished)."))
    if not isinstance(data, dict) or not isinstance(data.get("programs"), list):
        raise MigrateError(tr("Файл пакета повреждён.", "The package file is damaged."))
    if int(data.get("format") or 1) > FORMAT:
        raise MigrateError(tr("Пакет собран более новой WhySlow — обновите программу.",
                              "The package was made by a newer WhySlow: update the app."))
    return data


def package_programs(data: dict) -> list[Program]:
    return [Program.from_json(d) for d in data.get("programs") or [] if isinstance(d, dict)]


def settings_targets(data: dict) -> list[dict]:
    """Настройки из пакета и куда они лягут на этом компьютере.
    Каждой записи добавляется "targets": [(src, dst)]; пустой список — на этой ОС им некуда лечь."""
    rows = {key: r for key, _t, _n, _p, r in CATALOG}
    i = _os_index()
    out = []
    for s in data.get("settings") or []:
        targets = []
        for e in s.get("entries") or []:
            row = rows.get(s.get("key"), [])
            idx = int(e.get("i", -1))
            dst = _expand(row[idx][i]) if 0 <= idx < len(row) else None
            if dst:
                targets.append((Path("settings") / s["key"] / str(idx) / e["name"], dst))
        out.append({**s, "targets": targets})
    return out


def restore_settings(root: Path, data: dict, keys: set[str]) -> tuple[int, list[str]]:
    """Раскладывает настройки. То, что уже есть на месте, откладывается в резервную копию
    (откат — в «Журнале»). Возвращает (сколько программ настроено, ошибки)."""
    _require()
    stash = backup_dir() / time.strftime("move-%Y%m%d-%H%M%S")
    moves, failed, done = [], [], 0
    for s in settings_targets(data):
        if s["key"] not in keys or not s["targets"]:
            continue
        ok = False
        for rel, dst in s["targets"]:
            src = root / rel
            if not src.exists():
                continue
            old = ""
            try:
                if dst.exists() or dst.is_symlink():
                    keep = stash / rel.relative_to("settings")
                    keep.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(dst), str(keep))
                    old = str(keep)
                _copy_any(src, dst)
            except OSError as e:
                failed.append(f"{s.get('title')}: {e}")
                if old and not dst.exists():
                    shutil.move(old, str(dst))
                continue
            moves.append([str(dst), old])
            ok = True
        done += ok
    if moves:
        journal.add("move_settings", tr(f"Переезд: перенесены настройки программ ({done})",
                                        f"Move: app settings brought over ({done})"), {"moves": moves})
    return done, failed


def restore_files(root: Path, progress=lambda done, total, name: None) -> tuple[int, int, int]:
    """Файлы из пакета — по местам. Существующие файлы не перезаписываются."""
    _require()
    if not (root / "files").is_dir():
        return 0, 0, 0
    try:
        return reinstall.restore(root / "files", progress)
    except reinstall.ReinstallError as e:
        raise MigrateError(str(e))


def other_os(data: dict) -> bool:
    return data.get("os") not in (None, reinstall.os_name())


def os_title(code: str) -> str:
    return {"windows": "Windows", "mac": "macOS", "linux": "Linux"}.get(code, code or "?")


# ------------------------------------------------------------------ вкладка ---
# Qt нужен только окну: логика переезда работает и без него (тесты, сервер)

try:
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from PySide6.QtWidgets import (
        QCheckBox, QComboBox, QFileDialog, QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
        QProgressBar, QPushButton, QRadioButton, QScrollArea, QTableWidgetItem, QVBoxLayout, QWidget,
    )

    from ..gui import C, SystemTab, _esc, accent, confirm, make_table, muted, px, run_async
except ImportError:
    QWidget = object


def build(main):
    """Вкладка «Переезд». Её строит замок подписки (license_ui.PremiumGate)."""
    return MoveTab(main)


class MoveTab(QWidget):
    """Переезд на новый ПК: на старом собираем пакет (программы, настройки, файлы без мусора),
    на новом — ставим программы и раскладываем всё по местам. Функция подписки."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.started = False
        self.busy = False
        self.ex_programs: list[Program] | None = None   # None — ещё ищем
        self.ex_settings: list[AppSettings] = []
        self.ex_folders: list[reinstall.Folder] = []
        self.pkg_root: Path | None = None
        self.pkg_data: dict | None = None
        self.im_programs: list[Program] = []
        self.im_settings: list[dict] = []
        self.set_boxes: list[QCheckBox] = []
        self.folder_boxes: list[QCheckBox] = []
        self.folder_sizes: list[QLabel] = []
        self.files_box: QCheckBox | None = None

        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))
        title = QLabel(accent(tr("Переезд на новый ПК", "Move to a new PC")))
        title.setObjectName("h1")
        lay.addWidget(title)
        lay.addWidget(muted(tr("На старом компьютере WhySlow соберёт пакет на флешку или внешний диск: список программ, "
                               "их настройки и ваши папки — без кешей, временных файлов и старых установщиков. "
                               "На новом откройте пакет: программы поставятся сами, настройки и файлы лягут по местам. "
                               "Ничего не перезаписывается без резервной копии.",
                               "On the old computer WhySlow packs a USB or external drive: your program list, their "
                               "settings and your folders, without caches, temporary files or old installers. On the new "
                               "one open the package: programs install themselves, settings and files go where they "
                               "belong. Nothing is overwritten without a backup.")))
        mode_row = QHBoxLayout()
        self.r_export = QRadioButton(tr("Это старый ПК — собрать пакет", "This is the old PC: pack"))
        self.r_import = QRadioButton(tr("Это новый ПК — развернуть пакет", "This is the new PC: unpack"))
        self.r_export.setChecked(True)
        self.r_export.toggled.connect(self.render)
        mode_row.addWidget(self.r_export)
        mode_row.addWidget(self.r_import)
        mode_row.addStretch()
        self.b_open = QPushButton(tr("Открыть пакет…", "Open package…"))
        self.b_open.clicked.connect(self.open_package)
        mode_row.addWidget(self.b_open)
        lay.addLayout(mode_row)
        self.pkg_note = muted()
        lay.addWidget(self.pkg_note)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        body = QWidget()
        bl = QVBoxLayout(body)
        bl.setContentsMargins(0, 0, px(6), 0)
        bl.setSpacing(px(10))
        scroll.setWidget(body)
        lay.addWidget(scroll, 1)

        self.g_progs = QGroupBox(tr("Программы", "Programs"))
        pl = QVBoxLayout(self.g_progs)
        self.table = make_table([tr("Программа", "Program"), tr("Как поставить", "How it installs")])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.table.setMinimumHeight(px(220))
        self.table.itemChanged.connect(lambda _i: self.update_state())
        pl.addWidget(self.table)
        prow = QHBoxLayout()
        self.progs_note = muted()
        prow.addWidget(self.progs_note, 1)
        b_all = QPushButton(tr("Все", "All"))
        b_all.clicked.connect(lambda: self.check_all(True))
        b_none = QPushButton(tr("Ничего", "None"))
        b_none.clicked.connect(lambda: self.check_all(False))
        prow.addWidget(b_all)
        prow.addWidget(b_none)
        pl.addLayout(prow)
        bl.addWidget(self.g_progs)

        self.g_set = QGroupBox(tr("Настройки программ", "App settings"))
        self.set_lay = QGridLayout(self.g_set)
        self.set_lay.setHorizontalSpacing(px(14))
        bl.addWidget(self.g_set)

        self.g_files = QGroupBox(tr("Файлы", "Files"))
        fl = QVBoxLayout(self.g_files)
        self.files_lay = QGridLayout()
        self.files_lay.setHorizontalSpacing(px(14))
        fl.addLayout(self.files_lay)
        self.files_tools = QWidget()
        ft = QHBoxLayout(self.files_tools)
        ft.setContentsMargins(0, 0, 0, 0)
        b_add = QPushButton(tr("Добавить папку…", "Add folder…"))
        b_add.clicked.connect(self.add_folder)
        ft.addWidget(b_add)
        self.cb_installers = QCheckBox(tr("Не брать установщики из «Загрузок»", "Skip installers in Downloads"))
        self.cb_installers.setChecked(True)
        self.cb_installers.toggled.connect(lambda _c: self.measure_folders(self.ex_folders))
        ft.addWidget(self.cb_installers)
        ft.addStretch()
        fl.addWidget(self.files_tools)
        bl.addWidget(self.g_files)

        self.g_dest = QGroupBox(tr("Куда сложить пакет", "Where to put the package"))
        dl = QVBoxLayout(self.g_dest)
        drow = QHBoxLayout()
        self.dest_box = QComboBox()
        self.dest_box.currentIndexChanged.connect(self.update_state)
        drow.addWidget(self.dest_box, 1)
        b_pick = QPushButton(tr("Выбрать папку…", "Choose folder…"))
        b_pick.clicked.connect(self.pick_dest)
        drow.addWidget(b_pick)
        b_ref = QPushButton(tr("Обновить", "Refresh"))
        b_ref.clicked.connect(self.load_drives)
        drow.addWidget(b_ref)
        dl.addLayout(drow)
        self.dest_note = muted()
        dl.addWidget(self.dest_note)
        bl.addWidget(self.g_dest)
        bl.addStretch()

        self.bar = QProgressBar()
        self.bar.setTextVisible(False)
        self.bar.hide()
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        self.status = muted()
        row.addWidget(self.status, 1)
        self.b_go = QPushButton()
        self.b_go.setObjectName("primary")
        self.b_go.setCursor(Qt.PointingHandCursor)
        self.b_go.clicked.connect(self.go)
        row.addWidget(self.b_go)
        lay.addLayout(row)
        self.render()

    def showEvent(self, e):
        super().showEvent(e)
        if self.started:
            return
        self.started = True
        self.ex_settings = found_settings()
        self.ex_folders = reinstall.default_folders()
        self.load_drives()
        self.measure_folders(self.ex_folders)
        run_async(lambda _p: installed_programs(), self.got_programs, lambda e: self.got_programs([]))
        self.render()

    def exporting(self) -> bool:
        return self.r_export.isChecked()

    # --- что показать ---

    def render(self):
        exp = self.exporting()
        self.b_open.setVisible(not exp)
        self.g_dest.setVisible(exp)
        self.files_tools.setVisible(exp)
        if exp:
            self.pkg_note.setText("")
            self.fill_programs(self.ex_programs, export=True)
            self.fill_settings([(s.title, s.note, human_size(s.size), s.preset, True) for s in self.ex_settings])
            self.fill_folders()
        else:
            if self.pkg_data is None:
                self.pkg_note.setText(tr("Откройте папку WhySlow-Move-… с флешки или диска.",
                                         "Open the WhySlow-Move-… folder from the drive."))
            self.fill_programs(self.im_programs if self.pkg_data else [], export=False)
            self.fill_settings([(s.get("title", s["key"]), "" if s["targets"] else
                                 tr(f"на {os_title(reinstall.os_name())} им некуда лечь",
                                    f"no place for them on {os_title(reinstall.os_name())}"),
                                 human_size(int(s.get("size") or 0)), bool(s["targets"]), bool(s["targets"]))
                                for s in self.im_settings])
            self.fill_import_files()
        self.update_state()

    def fill_programs(self, progs, export: bool):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        if progs is None:
            self.progs_note.setText(tr("Ищу установленные программы…", "Looking for installed programs…"))
            self.table.blockSignals(False)
            return
        for p in progs:
            r = self.table.rowCount()
            self.table.insertRow(r)
            name = QTableWidgetItem(p.name + (f"  {p.version}" if p.version else ""))
            name.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
            ok = p.installable if export else can_install(p)
            name.setCheckState(Qt.Checked if ok or export else Qt.Unchecked)
            if not export and not ok:
                name.setFlags(Qt.ItemIsEnabled)
                name.setToolTip(missing_tool_hint(p))
            how = QTableWidgetItem(f"{source_label(p.source)}" + (f" · {p.pkg}" if p.installable and p.source != VSCODE else "")
                                   if export or ok else missing_tool_hint(p))
            how.setForeground(QColor(C["text"] if ok else C["muted"]))
            self.table.setItem(r, 0, name)
            self.table.setItem(r, 1, how)
        self.table.blockSignals(False)

    def fill_settings(self, rows):
        SystemTab._clear(self.set_lay)
        self.set_boxes = []
        for i, (title, note, size, checked, enabled) in enumerate(rows):
            cb = QCheckBox(title)
            cb.setChecked(checked)
            cb.setEnabled(enabled)
            cb.toggled.connect(self.update_state)
            self.set_boxes.append(cb)
            self.set_lay.addWidget(cb, i, 0)
            self.set_lay.addWidget(muted(note), i, 1)
            lab = QLabel(size)
            lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.set_lay.addWidget(lab, i, 2)
        self.set_lay.setColumnStretch(1, 1)
        if not rows:
            self.set_lay.addWidget(muted(tr("Настроек известных программ не нашлось.", "No settings of known apps found.")
                                         if self.exporting() or self.pkg_data else "—"), 0, 0)
        if self.exporting():  # SSH-ключи и прочее чувствительное не отмечено — пусть человек решит сам
            for s, cb in zip(self.ex_settings, self.set_boxes):
                cb.setChecked(s.preset)

    def fill_folders(self):
        SystemTab._clear(self.files_lay)
        self.folder_boxes, self.folder_sizes = [], []
        for i, f in enumerate(self.ex_folders):
            cb = QCheckBox(f.title)
            cb.setChecked(f.preset)
            cb.toggled.connect(self.update_state)
            size = QLabel(tr("считаю…", "measuring…") if f.size < 0 else
                          human_size(f.size) + tr(f"  ·  {f.files} файлов", f"  ·  {f.files} files"))
            size.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            self.folder_boxes.append(cb)
            self.folder_sizes.append(size)
            self.files_lay.addWidget(cb, i, 0)
            self.files_lay.addWidget(muted(reinstall.short_path(f.path)), i, 1)
            self.files_lay.addWidget(size, i, 2)
        self.files_lay.setColumnStretch(1, 1)
        if not self.ex_folders:
            self.files_lay.addWidget(muted(tr("Папок не найдено — добавьте свои.", "No folders found: add your own.")), 0, 0)

    def fill_import_files(self):
        SystemTab._clear(self.files_lay)
        self.files_box = None
        files = (self.pkg_data or {}).get("files")
        if not files:
            self.files_lay.addWidget(muted(tr("В пакете нет файлов.", "No files in the package.") if self.pkg_data else "—"), 0, 0)
            return
        self.files_box = QCheckBox(tr("Вернуть папки: ", "Bring back folders: ") + ", ".join(files.get("folders") or []))
        self.files_box.setChecked(True)
        self.files_box.toggled.connect(self.update_state)
        self.files_lay.addWidget(self.files_box, 0, 0)
        lab = QLabel(human_size(int(files.get("size") or 0)) + tr(f"  ·  {files.get('files')} файлов",
                                                                   f"  ·  {files.get('files')} files"))
        lab.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.files_lay.addWidget(lab, 0, 1)
        self.files_lay.setColumnStretch(0, 1)

    def got_programs(self, progs):
        self.ex_programs = progs
        if self.exporting():
            self.fill_programs(progs, export=True)
        self.update_state()

    def measure_folders(self, folders):
        skip = self.cb_installers.isChecked()
        for f in folders:
            f.size = -1
        if self.exporting():
            self.fill_folders()

        def done(_r):
            if self.exporting():
                checked = [b.isChecked() for b in self.folder_boxes]
                self.fill_folders()
                for b, c in zip(self.folder_boxes, checked):
                    b.setChecked(c)
            self.update_state()
        run_async(lambda _p: [measure(f, skip) for f in folders], done, lambda _e: None)

    def check_all(self, on: bool):
        for r in range(self.table.rowCount()):
            it = self.table.item(r, 0)
            if it.flags() & Qt.ItemIsUserCheckable:
                it.setCheckState(Qt.Checked if on else Qt.Unchecked)

    # --- выбор ---

    def chosen_programs(self) -> list[Program]:
        progs = (self.ex_programs or []) if self.exporting() else self.im_programs
        return [p for r, p in enumerate(progs)
                if r < self.table.rowCount() and self.table.item(r, 0).checkState() == Qt.Checked]

    def chosen_settings(self):
        src = self.ex_settings if self.exporting() else self.im_settings
        return [s for s, b in zip(src, self.set_boxes) if b.isChecked() and b.isEnabled()]

    def chosen_folders(self):
        return [f for f, b in zip(self.ex_folders, self.folder_boxes) if b.isChecked()]

    def add_folder(self):
        d = QFileDialog.getExistingDirectory(self, tr("Какую папку перенести", "Folder to move"), str(Path.home()))
        if not d or any(f.path == Path(d) for f in self.ex_folders):
            return
        f = reinstall.Folder(Path(d).name or d, Path(d), preset=True)
        self.ex_folders.append(f)
        self.measure_folders([f])

    def load_drives(self):
        cur = self.dest_box.currentData()
        self.dest_box.blockSignals(True)
        self.dest_box.clear()
        for d in reinstall.drives():
            self.dest_box.addItem(d.title, str(d.path))
        if cur and self.dest_box.findData(cur) < 0:
            self.dest_box.addItem(cur, cur)
        if not self.dest_box.count():
            self.dest_box.addItem(tr("Подключите флешку или внешний диск — или выберите папку",
                                     "Connect a USB or external drive, or choose a folder"), "")
        self.dest_box.setCurrentIndex(max(self.dest_box.findData(cur), 0) if cur else 0)
        self.dest_box.blockSignals(False)
        self.update_state()

    def pick_dest(self):
        d = QFileDialog.getExistingDirectory(self, tr("Куда сложить пакет", "Where to put the package"), "")
        if not d:
            return
        if self.dest_box.findData(d) < 0:
            self.dest_box.addItem(d, d)
        self.dest_box.setCurrentIndex(self.dest_box.findData(d))

    def update_state(self):
        if self.exporting():
            self.b_go.setText(tr("Собрать пакет", "Pack"))
            progs, sets, folders = self.chosen_programs(), self.chosen_settings(), self.chosen_folders()
            if self.ex_programs is not None:
                auto = sum(p.installable for p in self.ex_programs)
                self.progs_note.setText(tr(f"Найдено {len(self.ex_programs)}, из них {auto} поставятся на новом ПК сами. "
                                           "Остальные попадут в список «скачать вручную».",
                                           f"Found {len(self.ex_programs)}; {auto} will install themselves on the new PC. "
                                           "The rest go to the “download by hand” list."))
            need = sum(max(f.size, 0) for f in folders) + sum(s.size for s in sets)
            dest = self.dest_box.currentData()
            problem = (check_dest(Path(dest), folders, sum(s.size for s in sets)) if dest else
                       tr("Нужна папка для пакета: флешка, внешний или сетевой диск.",
                          "A folder for the package is needed: USB, external or network drive."))
            self.dest_note.setText(f"<span style='color:{C['warn']}'>{_esc(problem)}</span>" if problem else
                                   tr(f"В пакет уйдёт {human_size(need)}.", f"{human_size(need)} goes into the package."))
            measuring = any(f.size < 0 for f in folders) or self.ex_programs is None
            self.b_go.setEnabled(not self.busy and not problem and not measuring and bool(progs or sets or folders))
        else:
            self.b_go.setText(tr("Перенести на этот ПК", "Bring it to this PC"))
            if self.pkg_data:
                n = sum(can_install(p) for p in self.im_programs)
                self.progs_note.setText(tr(f"Сами поставятся {n} из {len(self.im_programs)}. "
                                           "Остальные — по подсказке во второй колонке.",
                                           f"{n} of {len(self.im_programs)} install themselves. "
                                           "For the rest, see the hint in the second column."))
            else:
                self.progs_note.setText("")
            any_chosen = bool(self.chosen_programs() or self.chosen_settings() or
                              (self.files_box and self.files_box.isChecked()))
            self.b_go.setEnabled(not self.busy and bool(self.pkg_data) and any_chosen)
        self.b_open.setEnabled(not self.busy)

    # --- действия ---

    def open_package(self):
        d = QFileDialog.getExistingDirectory(self, tr("Папка пакета WhySlow-Move-…", "WhySlow-Move-… package folder"), "")
        if not d:
            return
        try:
            data = read_package(Path(d))
        except MigrateError as e:
            QMessageBox.warning(self, tr("Не получилось", "Failed"), str(e))
            return
        self.pkg_root, self.pkg_data = Path(d), data
        self.im_programs = package_programs(data)
        self.im_settings = settings_targets(data)
        note = tr(f"Пакет от {data.get('created')} с компьютера {data.get('computer') or '?'} "
                  f"({os_title(data.get('os'))}).",
                  f"Package from {data.get('created')}, computer {data.get('computer') or '?'} "
                  f"({os_title(data.get('os'))}).")
        if other_os(data):
            note += tr(" Он с другой ОС: программы оттуда сами не поставятся, а настройки перенесутся там, где это возможно.",
                       " It comes from another OS: its programs won't install themselves, settings move where possible.")
        self.pkg_note.setText(note)
        self.render()

    def go(self):
        if self.busy:
            return
        self.do_export() if self.exporting() else self.do_import()

    def do_export(self):
        progs, sets, folders = self.chosen_programs(), self.chosen_settings(), self.chosen_folders()
        dest = Path(self.dest_box.currentData())
        lines = [tr(f"• Программ в списке: {len(progs)}", f"• Programs on the list: {len(progs)}")]
        if sets:
            lines.append(tr("• Настройки: ", "• Settings: ") + ", ".join(s.title for s in sets))
        lines += [f"• {f.title} — {human_size(max(f.size, 0))}" for f in folders]
        if any(s.key == "ssh" for s in sets):
            lines.append(tr("\nSSH-ключи лягут в пакет незашифрованными.", "\nSSH keys go into the package unencrypted."))
        if not confirm(self, tr("Собрать пакет?", "Pack?"),
                       tr(f"В {dest} будет создана папка WhySlow-Move-…:\n", f"A WhySlow-Move-… folder is created in {dest}:\n")
                       + "\n".join(lines) + tr("\n\nНа этом компьютере ничего не меняется.", "\n\nNothing changes on this computer.")):
            return
        self.set_busy(True)
        skip = self.cb_installers.isChecked()
        run_async(lambda p: export(dest, progs, sets, folders, skip, self._stepper(p)),
                  self.exported, self.failed, self.on_progress)

    def exported(self, pkg: Package):
        self.set_busy(False)
        journal.add("move_pack", tr(f"Переезд: собран пакет {pkg.path}", f"Move: package made at {pkg.path}"),
                    {"path": str(pkg.path)}, undoable=False)
        self.main.journal_tab.reload()
        msg = tr(f"Пакет готов: {pkg.path}\nПрограмм: {pkg.programs}, настроек: {pkg.settings}, "
                 f"файлов: {pkg.files} ({human_size(pkg.size)}).\n\nНа новом компьютере откройте WhySlow → "
                 "«Переезд» → «Это новый ПК» и выберите эту папку. Без WhySlow — запустите install и files/restore.",
                 f"The package is ready: {pkg.path}\nPrograms: {pkg.programs}, settings: {pkg.settings}, "
                 f"files: {pkg.files} ({human_size(pkg.size)}).\n\nOn the new computer open WhySlow → Move → "
                 "“This is the new PC” and pick this folder. Without WhySlow, run install and files/restore.")
        if pkg.failed:
            msg += tr(f"\n\nНе скопировалось: {len(pkg.failed)} (нет доступа или файл занят). Список — в {MANIFEST}.",
                      f"\n\nNot copied: {len(pkg.failed)} (no access or in use). The list is in {MANIFEST}.")
        self.status.setText(msg.split("\n")[0])
        QMessageBox.information(self, tr("Готово", "Done"), msg)

    def do_import(self):
        progs, sets = self.chosen_programs(), self.chosen_settings()
        files = bool(self.files_box and self.files_box.isChecked())
        lines = []
        if progs:
            lines.append(tr(f"• Поставить программ: {len(progs)}", f"• Install programs: {len(progs)}"))
        if sets:
            lines.append(tr("• Настройки: ", "• Settings: ") + ", ".join(s.get("title", s["key"]) for s in sets) +
                         tr(" (нынешние отложатся в резервную копию, откат — в «Журнале»)",
                            " (the current ones are backed up; undo in Journal)"))
        if files:
            lines.append(tr("• Вернуть файлы (существующие не перезаписываются)", "• Bring back files (existing ones are kept)"))
        tip = tr("\n\nЗакройте браузеры и программы, чьи настройки переносятся, — иначе они перезапишут их при выходе.",
                 "\n\nClose the browsers and apps whose settings are moved, or they overwrite them on exit.") if sets else ""
        if not confirm(self, tr("Перенести на этот ПК?", "Bring it to this PC?"), "\n".join(lines) + tip):
            return
        self.set_busy(True)
        root, data, keys = self.pkg_root, self.pkg_data, {s["key"] for s in sets}

        def work(p):
            out = {"installed": [], "failed": [], "settings": 0, "set_failed": [], "files": None}
            for i, prog in enumerate(progs):
                p(tr(f"Ставлю {prog.name} ({i + 1} из {len(progs)})…", f"Installing {prog.name} ({i + 1} of {len(progs)})…"))
                ok, why = install(prog)
                (out["installed"] if ok else out["failed"]).append(prog.name if ok else f"{prog.name}: {why}")
            if keys:
                p(tr("Раскладываю настройки…", "Putting settings in place…"))
                out["settings"], out["set_failed"] = restore_settings(root, data, keys)
            if files:
                out["files"] = restore_files(root, self._stepper(p))
            return out

        run_async(work, self.imported, self.failed, self.on_progress)

    def imported(self, out: dict):
        self.set_busy(False)
        self.main.journal_tab.reload()
        lines = []
        if out["installed"] or out["failed"]:
            lines.append(tr(f"Программ поставлено: {len(out['installed'])}.", f"Programs installed: {len(out['installed'])}."))
        if out["settings"] or out["set_failed"]:
            lines.append(tr(f"Настройки перенесены: {out['settings']}.", f"Settings moved: {out['settings']}."))
        if out["files"]:
            c, s, f = out["files"]
            lines.append(tr(f"Файлов возвращено: {c}, уже были на месте: {s}" + (f", ошибок: {f}" if f else "") + ".",
                            f"Files restored: {c}, already in place: {s}" + (f", errors: {f}" if f else "") + "."))
        bad = out["failed"] + out["set_failed"]
        if bad:
            lines.append(tr("\nНе получилось:", "\nFailed:"))
            lines += [f"• {b}" for b in bad[:15]] + ([f"… +{len(bad) - 15}"] if len(bad) > 15 else [])
        manual = [p.name for p in self.im_programs if not can_install(p)]
        if manual:
            lines.append(tr("\nСкачать вручную: ", "\nDownload by hand: ") + ", ".join(manual[:20]) +
                         (f" … +{len(manual) - 20}" if len(manual) > 20 else ""))
        msg = "\n".join(lines) or tr("Готово.", "Done.")
        self.status.setText(lines[0] if lines else "")
        QMessageBox.information(self, tr("Переезд закончен", "Move finished"), msg)

    def _stepper(self, p):
        last = [0.0]

        def step(done, total, name):
            if time.monotonic() - last[0] > 0.2:
                last[0] = time.monotonic()
                p(f"{done}\t{total}\t{name}")
        return step

    def on_progress(self, text: str):
        parts = text.split("\t")
        if len(parts) != 3:
            self.status.setText(text)
            return
        done, total, name = int(parts[0]), int(parts[1]), parts[2]
        if total:
            self.bar.setRange(0, 1000)
            self.bar.setValue(int(done * 1000 / total))
        self.status.setText(tr(f"Копирую «{name}»: {human_size(done)} из {human_size(total)}",
                               f"Copying “{name}”: {human_size(done)} of {human_size(total)}") if total else
                            tr(f"Копирую «{name}»…", f"Copying “{name}”…"))

    def failed(self, msg: str):
        self.set_busy(False)
        self.status.setText(msg)
        QMessageBox.warning(self, tr("Не получилось", "Failed"), msg)

    def set_busy(self, busy: bool):
        self.busy = busy
        self.bar.setVisible(busy)
        if busy:
            self.bar.setRange(0, 0)
        self.update_state()
