"""Собирает WhySlow в готовое приложение для текущей ОС.

* Windows — один файл WhySlow-Windows.exe;
* macOS — WhySlow.app в образе WhySlow-macOS-AppleSilicon.dmg или WhySlow-macOS-Intel.dmg
  (под процессор той машины, где идёт сборка);
* Linux — WhySlow-Linux-x86_64.AppImage.

Готовый файл кладётся в dist/ и проверяется запуском с --selfcheck.
Запуск: pip install -r requirements.txt pyinstaller pillow && python packaging/build.py
"""

from __future__ import annotations

import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
DIST = ROOT / "dist"
WORK = ROOT / "build"
sys.path.insert(0, str(ROOT))

from whyslow.updater import asset_name  # noqa: E402 — имена файлов те же, что ищет обновление

VERSION = re.search(r'__version__ = "(.+)"', (ROOT / "whyslow" / "__init__.py").read_text(encoding="utf-8")).group(1)
SYSTEM = platform.system()
APPIMAGETOOL = ("https://github.com/AppImage/appimagetool/releases/download/continuous/"
                "appimagetool-x86_64.AppImage")


def sh(*cmd: str, **kw) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def pyinstaller(onefile: bool) -> None:
    launcher = WORK / "WhySlow.py"
    WORK.mkdir(exist_ok=True)
    launcher.write_text("import sys\nfrom whyslow.__main__ import main\nsys.exit(main())\n", encoding="utf-8")
    sep = os.pathsep
    args = [
        sys.executable, "-m", "PyInstaller", str(launcher),
        "--name", "WhySlow", "--windowed", "--noconfirm", "--clean",
        "--distpath", str(DIST), "--workpath", str(WORK / "pyi"), "--specpath", str(WORK),
        "--paths", str(ROOT),
        "--icon", str(HERE / "icon.png"),
        "--add-data", f"{ROOT / 'whyslow' / 'data'}{sep}whyslow/data",
        # Модули самопроверки грузятся по имени, PyInstaller их сам не заметит
        "--collect-submodules", "whyslow",
        # Окну нужны только QtCore, QtGui и QtWidgets — остальное только раздувает сборку
        *[a for m in ("QtNetwork", "QtQml", "QtQuick", "QtPdf", "QtWebEngineCore", "QtMultimedia", "Qt3DCore")
          for a in ("--exclude-module", f"PySide6.{m}")],
        "--exclude-module", "tkinter",
    ]
    if onefile:
        args.append("--onefile")
    if SYSTEM == "Darwin":
        args += ["--osx-bundle-identifier", "io.github.moggiler0x.whyslow"]
    sh(*args, cwd=ROOT)


def selfcheck(binary: Path) -> None:
    """Собранное приложение должно запуститься и назвать свою версию."""
    out = Path(tempfile.mkdtemp()) / "version.txt"
    env = dict(os.environ, WHYSLOW_SELFCHECK_OUT=str(out), QT_QPA_PLATFORM="offscreen")
    print("+", binary, "--selfcheck", flush=True)
    subprocess.run([str(binary), "--selfcheck"], check=True, env=env, timeout=300)
    got = out.read_text(encoding="utf-8").strip()
    if got != VERSION:
        sys.exit(f"Сборка называет себя {got!r}, а должна {VERSION!r}")
    print(f"Самопроверка пройдена: {got}")


def build_windows(name: str) -> Path:
    pyinstaller(onefile=True)
    result = DIST / name
    os.replace(DIST / "WhySlow.exe", result)
    selfcheck(result)
    return result


def build_mac(name: str) -> Path:
    pyinstaller(onefile=False)
    app = DIST / "WhySlow.app"
    plist = app / "Contents" / "Info.plist"
    for key, value in (("CFBundleShortVersionString", VERSION), ("CFBundleVersion", VERSION),
                       ("NSHighResolutionCapable", True)):
        kind = "-bool" if isinstance(value, bool) else "-string"
        sh("plutil", "-replace", key, kind, str(value).lower() if isinstance(value, bool) else value, str(plist))
    # После правки Info.plist подпись бандла сломана — подписываем заново «без разработчика»
    sh("codesign", "--force", "--deep", "--sign", "-", str(app))
    selfcheck(app / "Contents" / "MacOS" / "WhySlow")
    stage = WORK / "dmg"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    sh("ditto", str(app), str(stage / "WhySlow.app"))
    (stage / "Applications").symlink_to("/Applications")  # перетащить в «Программы» прямо в окне образа
    result = DIST / name
    result.unlink(missing_ok=True)
    sh("hdiutil", "create", "-volname", "WhySlow", "-srcfolder", str(stage), "-fs", "HFS+",
       "-format", "UDZO", "-ov", str(result))
    return result


def build_linux(name: str) -> Path:
    pyinstaller(onefile=False)
    appdir = WORK / "WhySlow.AppDir"
    shutil.rmtree(appdir, ignore_errors=True)
    shutil.copytree(DIST / "WhySlow", appdir / "usr" / "lib" / "whyslow", symlinks=True)
    shutil.copy(HERE / "icon.png", appdir / "whyslow.png")
    (appdir / "whyslow.desktop").write_text(
        "[Desktop Entry]\nType=Application\nName=WhySlow\n"
        "Comment=Explains why your computer is slow\nComment[ru]=Объясняет, почему тормозит компьютер\n"
        "Exec=WhySlow\nIcon=whyslow\nCategories=System;Monitor;\nTerminal=false\n", encoding="utf-8")
    run = appdir / "AppRun"
    run.write_text('#!/bin/sh\nHERE="$(dirname "$(readlink -f "$0")")"\n'
                   'exec "$HERE/usr/lib/whyslow/WhySlow" "$@"\n', encoding="utf-8")
    run.chmod(0o755)
    selfcheck(appdir / "usr" / "lib" / "whyslow" / "WhySlow")

    tool = WORK / "appimagetool"
    if not tool.exists():
        urllib.request.urlretrieve(APPIMAGETOOL, tool)
        tool.chmod(tool.stat().st_mode | stat.S_IEXEC)
    result = DIST / name
    result.unlink(missing_ok=True)
    # В CI нет FUSE: appimagetool сам распаковывается и работает без монтирования
    env = dict(os.environ, ARCH="x86_64", APPIMAGE_EXTRACT_AND_RUN="1")
    sh(str(tool), "--no-appstream", str(appdir), str(result), env=env)
    result.chmod(0o755)
    selfcheck(result)  # тот же APPIMAGE_EXTRACT_AND_RUN из окружения CI, если FUSE нет
    return result


def main() -> None:
    name = asset_name()
    if not name:
        sys.exit(f"Для {SYSTEM} {platform.machine()} сборка не предусмотрена")
    shutil.rmtree(DIST, ignore_errors=True)
    builder = {"Windows": build_windows, "Darwin": build_mac, "Linux": build_linux}[SYSTEM]
    result = builder(name)
    print(f"Готово: {result} ({result.stat().st_size / 1e6:.0f} МБ)")
    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"file={result}\nname={name}\n")


if __name__ == "__main__":
    main()
