"""Обновление программы из GitHub.

Новые версии — это теги вида v0.2.0 в репозитории (и релизы на GitHub, если они есть).
Ставим их одним из двух способов:

* git — программа запущена из клона: подтягиваем тег через git, локальные
  правки не трогаем (если они есть, обновление откажется);
* files — программа скачана архивом: качаем архив тега, сохраняем копию
  текущих файлов и заменяем их.

После установки запускаем новую версию в режиме самопроверки. Если она не
запустилась, всё возвращается как было. Удачное обновление пишется в журнал,
и его можно откатить оттуда же.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import time
import urllib.error
import zipfile
from dataclasses import dataclass
from pathlib import Path

from . import __version__, journal, net
from .i18n import tr
from .util import app_dir, run

REPO = os.environ.get("WHYSLOW_UPDATE_REPO", "moggiler0x/WhySlow")
API = "https://api.github.com/repos/" + REPO
GIT_URL = f"https://github.com/{REPO}.git"
ROOT = Path(__file__).resolve().parent.parent  # папка с run.sh / run.bat


class UpdateError(Exception):
    """Ошибка с текстом, который можно показать человеку."""


@dataclass
class Release:
    version: str
    tag: str
    notes: str = ""
    url: str = ""


def parse(v: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+(?:\.\d+)*)", v.strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else None


def is_newer(v: str, than: str = __version__) -> bool:
    a, b = parse(v), parse(than)
    return bool(a and b and a > b)


def _get(url: str, timeout: float):
    with net.urlopen(url, timeout, {"Accept": "application/vnd.github+json"}) as resp:
        return json.loads(resp.read().decode("utf-8"))


def latest(timeout: float = 6) -> Release | None:
    """Самая свежая версия в репозитории: из релизов, а если их нет — из тегов."""
    try:
        try:
            r = _get(API + "/releases/latest", timeout)
            if parse(r.get("tag_name", "")):
                return Release(r["tag_name"].lstrip("v"), r["tag_name"], r.get("body") or "", r.get("html_url", ""))
        except urllib.error.HTTPError as e:
            if e.code != 404:  # 404 — релизов пока нет, смотрим теги
                raise
        tags = [t["name"] for t in _get(API + "/tags?per_page=100", timeout) if parse(t.get("name", ""))]
    except Exception as e:
        raise UpdateError(tr(f"Не удалось проверить обновления: {net.explain(e)}",
                             f"Could not check for updates: {net.explain(e)}")) from e
    if not tags:
        return None
    tag = max(tags, key=parse)
    return Release(tag.lstrip("v"), tag, "", f"https://github.com/{REPO}/releases/tag/{tag}")


def check(timeout: float = 6) -> tuple[Release | None, str]:
    """(новая версия или None, сообщение для пользователя)."""
    rel = latest(timeout)
    if rel and is_newer(rel.version):
        return rel, tr(f"Доступна новая версия {rel.version} (у вас {__version__})",
                       f"New version {rel.version} is available (you have {__version__})")
    return None, tr(f"У вас последняя версия ({__version__})", f"You have the latest version ({__version__})")


# ------------------------------------------------------------ как ставить ---

def _git(*args: str, timeout: float = 60) -> tuple[int, str]:
    return run(["git", "-C", str(ROOT), *args], timeout=timeout)


def mode() -> str:
    """git, files или manual (собранный .exe / установка через pip — обновлять вручную)."""
    if getattr(sys, "frozen", False) or not (ROOT / "requirements.txt").exists():
        return "manual"
    if (ROOT / ".git").exists() and shutil.which("git"):
        return "git"
    return "files"


def mode_hint() -> str:
    return {
        "git": tr("Программа запущена из git-клона: обновление подтянет новую версию через git.",
                  "Running from a git clone: the update will pull the new version via git."),
        "files": tr("Обновление скачает архив новой версии и заменит файлы программы. Старые сохранятся для отката.",
                    "The update will download the new version's archive and replace the program files. "
                    "The old ones are kept for rollback."),
        "manual": tr("Эту копию нельзя обновить изнутри. Скачайте новую версию со страницы релизов.",
                     "This copy can't update itself. Download the new version from the releases page."),
    }[mode()]


def install(rel: Release, progress=lambda _t: None) -> str:
    """Ставит версию rel. При любой ошибке откатывает изменения и бросает UpdateError."""
    m = mode()
    if m == "manual":
        raise UpdateError(mode_hint())
    old = __version__
    data = _install_git(rel, progress) if m == "git" else _install_files(rel, progress)
    journal.add("update", tr(f"Обновление WhySlow {old} → {rel.version}", f"WhySlow update {old} → {rel.version}"), data)
    return tr(f"WhySlow обновлён до версии {rel.version}. Перезапустите программу.",
              f"WhySlow updated to version {rel.version}. Restart the program.")


def _check_inside(rel: Release, init_py: str) -> None:
    """Версия в коде релиза должна совпадать с тегом, иначе после установки программа
    останется «старой» и будет предлагать то же обновление снова."""
    m = re.search(r"__version__\s*=\s*[\"']([^\"']+)[\"']", init_py)
    inside = m.group(1) if m else "?"
    if inside != rel.version:
        raise UpdateError(tr(
            f"Релиз {rel.tag} выпущен с ошибкой: внутри него версия {inside}, а не {rel.version}. "
            "Ничего не менял. Нужен исправленный релиз с новой версией.",
            f"Release {rel.tag} is broken: it contains version {inside}, not {rel.version}. "
            "Nothing was changed. A fixed release with a new version is needed."))


def _verify(rel: Release, progress, rollback) -> None:
    """Ставит зависимости новой версии и проверяет, что она запускается."""
    progress(tr("Ставлю зависимости…", "Installing dependencies…"))
    code, out = run([sys.executable, "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")],
                    timeout=600, cwd=ROOT)
    pip_failed = code != 0
    progress(tr("Проверяю новую версию…", "Checking the new version…"))
    # Старый байткод с тем же размером и временем файла Python принял бы за свежий
    for cache in (ROOT / "whyslow").rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    code, out = run([sys.executable, "-m", "whyslow", "--selfcheck"], timeout=120, cwd=ROOT)
    lines = out.strip().splitlines()
    if code != 0 or not lines or lines[-1].strip() != rel.version:
        progress(tr("Новая версия не запустилась, возвращаю старую…", "New version failed to start, restoring the old one…"))
        rollback()
        if code == 0 and lines:
            raise UpdateError(tr(f"Новая версия запустилась как {lines[-1].strip()}, а не {rel.version}. "
                                 f"Вернул {__version__}.",
                                 f"The new version reports {lines[-1].strip()}, not {rel.version}. "
                                 f"Restored {__version__}."))
        tail = "\n".join(lines[-5:])
        hint = tr(" Не удалось поставить зависимости (pip).", " Failed to install dependencies (pip).") if pip_failed else ""
        raise UpdateError(tr(f"Новая версия не запустилась, вернул {__version__}.{hint}\n{tail}",
                             f"New version failed to start, restored {__version__}.{hint}\n{tail}").strip())


# ---------------------------------------------------------------- git ---

def _install_git(rel: Release, progress) -> dict:
    progress(tr("Проверяю git-клон…", "Checking the git clone…"))
    code, out = _git("status", "--porcelain", "--untracked-files=no")
    if code != 0:
        raise UpdateError(tr(f"git не смог прочитать папку программы:\n{out.strip()}",
                             f"git could not read the program folder:\n{out.strip()}"))
    if out.strip():
        raise UpdateError(tr("В папке программы есть ваши изменения файлов. Чтобы их не потерять, обновление "
                             "отменено. Сохраните их (git stash или commit) и попробуйте снова.",
                             "The program folder has your local file changes. The update was cancelled so "
                             "they aren't lost. Save them (git stash or commit) and try again."))
    _, old = _git("rev-parse", "HEAD")
    old = old.strip()
    code, branch = _git("symbolic-ref", "-q", "--short", "HEAD")
    branch = branch.strip() if code == 0 else ""

    progress(tr(f"Скачиваю {rel.tag}…", f"Downloading {rel.tag}…"))
    code, out = _git("fetch", "--quiet", "--no-tags", GIT_URL, f"+refs/tags/{rel.tag}:refs/tags/{rel.tag}",
                     timeout=300)
    if code != 0:
        raise UpdateError(tr(f"Не удалось скачать обновление через git:\n{out.strip()}",
                             f"Could not download the update via git:\n{out.strip()}"))
    _, init_py = _git("show", f"{rel.tag}:whyslow/__init__.py")
    _check_inside(rel, init_py)
    if branch:
        code, out = _git("merge", "--ff-only", "--quiet", rel.tag)
    else:
        code, out = _git("checkout", "--quiet", "--detach", rel.tag)
    if code != 0:
        raise UpdateError(tr("Не удалось перейти на новую версию: ветка клона ушла в сторону от "
                             f"основной. Обновите её вручную (git pull).\n{out.strip()}",
                             "Could not switch to the new version: the clone's branch has diverged from "
                             f"the main one. Update it manually (git pull).\n{out.strip()}"))

    def rollback():
        _git_restore(old, branch)

    _verify(rel, progress, rollback)
    _, new = _git("rev-parse", "HEAD")
    return {"mode": "git", "old": old, "new": new.strip(), "branch": branch, "old_version": __version__}


def _git_restore(sha: str, branch: str) -> None:
    if branch:
        code, out = _git("reset", "--hard", "--quiet", sha)
    else:
        code, out = _git("checkout", "--quiet", "--detach", sha)
    if code != 0:
        raise UpdateError(tr(f"git не смог вернуть прошлую версию:\n{out.strip()}",
                             f"git could not restore the previous version:\n{out.strip()}"))


# -------------------------------------------------------------- files ---

def _install_files(rel: Release, progress) -> dict:
    if not os.access(ROOT, os.W_OK):
        raise UpdateError(tr(f"Нет прав на запись в папку программы {ROOT}. Запустите от администратора "
                             "или скачайте новую версию вручную.",
                             f"No write access to the program folder {ROOT}. Run as administrator "
                             "or download the new version manually."))
    tmp = Path(tempfile.mkdtemp(prefix="whyslow-update-"))
    try:
        progress(tr(f"Скачиваю {rel.tag}…", f"Downloading {rel.tag}…"))
        url = f"https://github.com/{REPO}/archive/refs/tags/{rel.tag}.zip"
        archive = tmp / "update.zip"
        try:
            _download(url, archive)
            with zipfile.ZipFile(archive) as z:
                z.extractall(tmp / "x")
        except Exception as e:
            raise UpdateError(tr(f"Не удалось скачать обновление: {net.explain(e)}",
                                 f"Could not download the update: {net.explain(e)}")) from e
        tops = [p for p in (tmp / "x").iterdir() if p.is_dir()]
        src = tops[0] if len(tops) == 1 else tmp / "x"
        if not (src / "whyslow" / "__init__.py").exists():
            raise UpdateError(tr("Архив обновления не похож на WhySlow — ничего не меняю.",
                                 "The update archive doesn't look like WhySlow — nothing changed."))
        _check_inside(rel, (src / "whyslow" / "__init__.py").read_text(encoding="utf-8"))

        progress(tr("Сохраняю текущую версию…", "Backing up the current version…"))
        backup = app_dir() / "updates" / f"{__version__}-{time.strftime('%Y%m%d-%H%M%S')}"
        backup.mkdir(parents=True)
        names = sorted(p.name for p in src.iterdir() if not p.name.startswith("."))
        replaced = [n for n in names if (ROOT / n).exists()]
        added = [n for n in names if n not in replaced]
        for n in replaced:
            _copy(ROOT / n, backup / n)
        manifest = {"root": str(ROOT), "replaced": replaced, "added": added}
        (backup / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

        def rollback():
            _files_restore(backup)
            shutil.rmtree(backup, ignore_errors=True)

        progress(tr("Заменяю файлы…", "Replacing files…"))
        try:
            for n in names:
                _remove(ROOT / n)
                _copy(src / n, ROOT / n)
        except OSError as e:
            rollback()
            raise UpdateError(tr(f"Не удалось заменить файлы ({e}), вернул старую версию.",
                                 f"Could not replace files ({e}), restored the old version.")) from e
        _verify(rel, progress, rollback)
        return {"mode": "files", "backup": str(backup), "old_version": __version__}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _download(url: str, dest: Path) -> None:
    with net.urlopen(url, 60) as resp, dest.open("wb") as f:
        shutil.copyfileobj(resp, f)


def _copy(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    else:
        shutil.copy2(src, dst)


def _remove(p: Path) -> None:
    if p.is_dir() and not p.is_symlink():
        shutil.rmtree(p)
    elif p.exists() or p.is_symlink():
        p.unlink()


def _files_restore(backup: Path) -> None:
    manifest = json.loads((backup / "manifest.json").read_text(encoding="utf-8"))
    root = Path(manifest["root"])
    for n in manifest["added"]:
        _remove(root / n)
    for n in manifest["replaced"]:
        _remove(root / n)
        _copy(backup / n, root / n)


def undo(entry: dict) -> None:
    """Откат обновления из журнала."""
    d = entry["data"]
    if d["mode"] == "git":
        code, out = _git("status", "--porcelain", "--untracked-files=no")
        if code != 0 or out.strip():
            raise UpdateError(tr("В папке программы есть изменения файлов — откат отменён, чтобы их не потерять.",
                                 "The program folder has local file changes — rollback cancelled so they aren't lost."))
        _, head = _git("rev-parse", "HEAD")
        if head.strip() != d["new"]:
            raise UpdateError(tr("После этого обновления код уже менялся, откатывать опасно. "
                                 f"Вернитесь вручную: git checkout {d['old'][:10]}",
                                 "The code has changed since this update, rolling back is unsafe. "
                                 f"Go back manually: git checkout {d['old'][:10]}"))
        _git_restore(d["old"], d["branch"])
    else:
        backup = Path(d["backup"])
        if not (backup / "manifest.json").exists():
            raise UpdateError(tr("Копия прошлой версии не найдена — откатить нельзя.",
                                 "Backup of the previous version not found — can't roll back."))
        _files_restore(backup)


def restart_command() -> list[str]:
    """Команда, которой перезапустить программу с теми же аргументами."""
    return [sys.executable, "-m", "whyslow", *sys.argv[1:]]
