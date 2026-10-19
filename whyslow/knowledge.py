"""База знаний: что можно отключать, а что лучше не трогать.

Встроенная копия лежит в whyslow/data/known.json. При запуске программа
пробует скачать свежую версию из GitHub-репозитория и кеширует её.
Чтобы пополнить базу, достаточно прислать pull request в этот файл.
"""

from __future__ import annotations

import json
import os
import re
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from .util import IS_WINDOWS, app_dir

DB_URL = os.environ.get(
    "WHYSLOW_DB_URL",
    "https://raw.githubusercontent.com/moggiler0x/WhySlow/main/whyslow/data/known.json",
)
BUNDLED = Path(__file__).resolve().parent / "data" / "known.json"

VERDICTS = {
    "safe": "Можно отключить",
    "optional": "По желанию",
    "keep": "Не трогать",
    "bad": "Лучше удалить",
    "unknown": "Неизвестно",
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


class KnowledgeBase:
    def __init__(self, data: dict, source: str):
        self.version = int(data.get("version", 0))
        self.updated = str(data.get("updated", ""))
        self.source = source
        self.entries: list[Entry] = []
        for raw in data.get("entries", []):
            try:
                pats = [re.compile(p, re.IGNORECASE) for p in raw["match"]]
            except (KeyError, re.error):
                continue
            self.entries.append(Entry(
                id=raw.get("id", raw.get("title", "?")),
                title=raw.get("title", ""),
                verdict=raw.get("verdict", "unknown"),
                note=raw.get("note", ""),
                category=raw.get("category", ""),
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
        return Verdict("unknown", "Нет в базе. Если знаете, что это, — пришлите в репозиторий, база пополнится.")


def _heuristic(text: str) -> Verdict | None:
    if not IS_WINDOWS:
        return None
    low = text.lower().replace("/", "\\")
    if re.search(_SYSTEM_NAMES, low) and "\\windows\\" not in low and "\\appdata\\" in low:
        return Verdict("bad", "Системное имя Windows, но файл лежит в AppData — так маскируются вирусы. "
                              "Проверьте антивирусом.", "Подозрительный файл", "Угроза")
    if "\\appdata\\local\\temp\\" in low and low.rstrip('"').endswith(".exe"):
        return Verdict("bad", "Программа стартует из папки временных файлов — нормальные программы так не делают.",
                       "Подозрительный запуск", "Угроза")
    return None


def _cache_path() -> Path:
    return app_dir() / "known.json"


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) and "entries" in data else None
    except (OSError, ValueError):
        return None


def load() -> KnowledgeBase:
    """Берёт самую свежую из встроенной и закешированной копий."""
    bundled = _read(BUNDLED) or {"entries": []}
    cached = _read(_cache_path())
    if cached and int(cached.get("version", 0)) > int(bundled.get("version", 0)):
        return KnowledgeBase(cached, "кеш с GitHub")
    return KnowledgeBase(bundled, "встроенная")


def update(timeout: float = 6) -> tuple[KnowledgeBase, str]:
    """Скачивает базу с GitHub. Возвращает (база, сообщение для пользователя)."""
    current = load()
    try:
        req = urllib.request.Request(DB_URL, headers={"User-Agent": "WhySlow"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # сеть, DNS, кривой JSON — всё одно
        return current, f"Не удалось обновить базу: {e}"
    if not isinstance(data, dict) or "entries" not in data:
        return current, "Не удалось обновить базу: неверный формат"
    if int(data.get("version", 0)) <= current.version:
        return current, f"База актуальна (версия {current.version})"
    _cache_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    kb = KnowledgeBase(data, "GitHub")
    return kb, f"База обновлена до версии {kb.version}: {len(kb.entries)} записей"


def report_url(name: str, command: str) -> str:
    """Ссылка на issue в репозитории с уже заполненными данными."""
    from urllib.parse import quote
    repo = "https://github.com/moggiler0x/WhySlow"
    title = f"Добавить в базу: {name}"
    body = f"**Имя:** `{name}`\n**Команда/путь:** `{command}`\n\nЧто это за программа и можно ли её отключать:\n"
    return f"{repo}/issues/new?title={quote(title)}&body={quote(body)}&labels=database"
