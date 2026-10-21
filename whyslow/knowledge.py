"""База знаний: что можно отключать, а что лучше не трогать.

Встроенная копия лежит в whyslow/data/known.json. При запуске программа
пробует скачать свежую версию из GitHub-репозитория и кеширует её.
Чтобы пополнить базу, достаточно прислать pull request в этот файл.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from . import net
from .i18n import pick, tr
from .util import IS_WINDOWS, app_dir, write_json

DB_URL = os.environ.get(
    "WHYSLOW_DB_URL",
    "https://raw.githubusercontent.com/moggiler0x/WhySlow/main/whyslow/data/known.json",
)
BUNDLED = Path(__file__).resolve().parent / "data" / "known.json"

VERDICTS = {
    "safe": tr("Можно отключить", "Safe to disable"),
    "optional": tr("По желанию", "Optional"),
    "keep": tr("Не трогать", "Keep"),
    "bad": tr("Лучше удалить", "Better remove"),
    "unknown": tr("Неизвестно", "Unknown"),
}

# Системные имена, которые вирусы любят использовать вне System32
_SYSTEM_NAMES = r"(svchost|csrss|lsass|winlogon|services|smss|explorer|rundll32|conhost|dllhost|taskhostw?)\.exe"


@dataclass
class Entry:
    id: str
    title: str
    verdict: str
    note: str
    category: str = ""
    patterns: list[re.Pattern] = field(default_factory=list)


@dataclass
class Verdict:
    verdict: str
    note: str
    title: str = ""
    category: str = ""
    entry_id: str = ""

    @property
    def label(self) -> str:
        return VERDICTS.get(self.verdict, self.verdict)


def _version(data: dict) -> int:
    try:
        return int(data.get("version", 0))
    except (TypeError, ValueError):
        return 0


def _valid(data) -> bool:
    """Похоже ли это на файл базы: словарь со списком записей."""
    return isinstance(data, dict) and isinstance(data.get("entries"), list)


class KnowledgeBase:
    def __init__(self, data: dict, source: str):
        self.version = _version(data)
        self.updated = str(data.get("updated", ""))
        self.source = source
        self.entries: list[Entry] = []
        for raw in data.get("entries", []):
            # Битую запись пропускаем, остальная база должна работать
            if not isinstance(raw, dict) or not isinstance(raw.get("match"), list):
                continue
            try:
                pats = [re.compile(p, re.IGNORECASE) for p in raw["match"]]
            except (TypeError, re.error):
                continue
            self.entries.append(Entry(
                id=raw.get("id", raw.get("title", "?")),
                title=pick(raw, "title"),
                verdict=raw.get("verdict", "unknown"),
                note=pick(raw, "note"),
                category=pick(raw, "category"),
                patterns=pats,
            ))

    def lookup(self, *texts: str) -> Verdict:
        text = " ".join(t for t in texts if t)
        suspicious = _heuristic(text)
        if suspicious:
            return suspicious
        for e in self.entries:
            if any(p.search(text) for p in e.patterns):
                return Verdict(e.verdict, e.note, e.title, e.category, e.id)
        return Verdict("unknown", tr("Нет в базе. WhySlow поищет описание в интернете; если знаете точно — "
                                     "предложите запись в базу, она пополнится для всех.",
                                     "Not in the database. WhySlow will look for a description online; if you know "
                                     "for sure, suggest an entry and the database will grow for everyone."))


def _threat() -> str:
    return tr("Угроза", "Threat")


def _heuristic(text: str) -> Verdict | None:
    if not IS_WINDOWS:
        return None
    low = text.lower().replace("/", "\\")
    if re.search(_SYSTEM_NAMES, low) and "\\windows\\" not in low and "\\appdata\\" in low:
        return Verdict("bad", tr("Системное имя Windows, но файл лежит в AppData — так маскируются вирусы. "
                                 "Проверьте антивирусом.",
                                 "A Windows system name, but the file lives in AppData — that's how malware hides. "
                                 "Scan it with an antivirus."),
                       tr("Подозрительный файл", "Suspicious file"), _threat())
    if "\\appdata\\local\\temp\\" in low and low.rstrip('"').endswith(".exe"):
        return Verdict("bad", tr("Программа стартует из папки временных файлов — нормальные программы так не делают.",
                                 "The program starts from the temp folder — normal programs don't do that."),
                       tr("Подозрительный запуск", "Suspicious launch"), _threat())
    return None


def _cache_path() -> Path:
    return app_dir() / "known.json"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if _valid(data) else None
    except (OSError, ValueError):
        return None


def load() -> KnowledgeBase:
    """Берёт самую свежую из встроенной и закешированной копий."""
    bundled = _read(BUNDLED) or {"entries": []}
    cached = _read(_cache_path())
    if cached and _version(cached) > _version(bundled):
        return KnowledgeBase(cached, tr("кеш с GitHub", "GitHub cache"))
    return KnowledgeBase(bundled, tr("встроенная", "built-in"))


def update(timeout: float = 6) -> tuple[KnowledgeBase, str]:
    """Скачивает базу с GitHub. Возвращает (база, сообщение для пользователя)."""
    current = load()
    try:
        with net.urlopen(DB_URL, timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # сеть, DNS, кривой JSON — всё одно
        why = net.explain(e, tr("файл базы", "database file"))
        return current, tr(f"Не удалось обновить базу: {why}. Продолжаю с имеющейся копией.",
                           f"Couldn't update the database: {why}. Using the existing copy.")
    if not _valid(data):
        return current, tr("Не удалось обновить базу: файл на GitHub повреждён. Продолжаю с имеющейся копией.",
                           "Couldn't update the database: the file on GitHub is corrupted. Using the existing copy.")
    if _version(data) <= current.version:
        return current, tr(f"База актуальна (версия {current.version})",
                           f"Database is up to date (version {current.version})")
    kb = KnowledgeBase(data, "GitHub")
    try:
        write_json(_cache_path(), data)
    except OSError:
        return kb, tr(f"База обновлена до версии {kb.version}, но сохранить её на диск не удалось",
                      f"Database updated to version {kb.version}, but saving it to disk failed")
    return kb, tr(f"База обновлена до версии {kb.version}: {len(kb.entries)} записей",
                  f"Database updated to version {kb.version}: {len(kb.entries)} entries")


def report_url(name: str, command: str, found: str = "", source: str = "") -> str:
    """Ссылка на issue в репозитории с уже заполненными данными.
    found — описание, которое программа нашла в интернете (подставляется как черновик)."""
    from urllib.parse import quote
    repo = "https://github.com/moggiler0x/WhySlow"
    title = tr(f"Добавить в базу: {name}", f"Add to database: {name}")
    body = tr(f"**Имя:** `{name}`\n**Команда/путь:** `{command}`\n\n",
              f"**Name:** `{name}`\n**Command/path:** `{command}`\n\n")
    if found:
        src = source or tr("интернет", "internet")
        body += tr(f"Найдено автоматически ({src}), стоит проверить:\n> {found}\n\n",
                   f"Found automatically ({src}), worth checking:\n> {found}\n\n")
    body += tr("Что это за программа и можно ли её отключать:\n", "What this program is and whether it can be disabled:\n")
    return f"{repo}/issues/new?title={quote(title)}&body={quote(body)}&labels=database"
