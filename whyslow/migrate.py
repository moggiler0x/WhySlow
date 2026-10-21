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

from . import journal, reinstall
from .i18n import tr
from .util import IS_MAC, IS_WINDOWS, backup_dir, human_size, run

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
        from . import license as lic
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


def undo(entry: dict) -> None:
    """Убирает перенесённые настройки и возвращает те, что были до переезда."""
    problems = []
    for dst, old in reversed(entry["data"]["moves"]):
        d = Path(dst)
        try:
            if d.is_dir() and not d.is_symlink():
                shutil.rmtree(d)
            elif d.exists() or d.is_symlink():
                d.unlink()
            if old:
                if Path(old).exists():
                    shutil.move(old, dst)
                else:
                    problems.append(d.name)
        except OSError:
            problems.append(d.name)
    if problems:
        raise OSError(tr("Не вернулось: ", "Not restored: ") + ", ".join(problems))


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

