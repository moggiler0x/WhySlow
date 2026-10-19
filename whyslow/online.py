"""Описание неизвестных программ из открытых источников.

Если программы нет в базе, WhySlow сам выясняет, что это:
  1. читает сведения из самого файла (издатель, описание, название продукта);
  2. ищет статью в Википедии (на русском интерфейсе — сначала русской, потом английской;
     на английском — только английской).
Ключи API не нужны. Найденное кешируется на диске (отдельно для каждого языка),
чтобы не ходить в сеть снова.
Вердикт «можно отключить» по таким данным не ставится — это только пояснение.
"""

from __future__ import annotations

import json
import os
import plistlib
import re
import threading
import time
import urllib.parse
from dataclasses import asdict, dataclass
from pathlib import Path

from . import i18n, net
from .i18n import tr
from .util import IS_MAC, IS_WINDOWS, app_dir

TTL_FOUND = 30 * 86400    # найденное описание перепроверяем раз в месяц
TTL_MISS = 3 * 86400      # «ничего не нашлось» — раз в три дня
_UA = "WhySlow (https://github.com/moggiler0x/WhySlow)"
_lock = threading.Lock()
_offline_until = 0.0      # после сетевой ошибки минуту не стучимся, чтобы не ждать таймаут на каждой строке

# Части имён, которые ничего не говорят о программе
_NOISE = {"exe", "app", "helper", "service", "svc", "agent", "daemon", "updater", "update", "launcher",
          "tray", "x64", "x86", "win32", "win64", "64", "32", "bin", "plugin", "renderer", "gpu", "crashpad",
          "handler", "host", "broker", "server", "client", "background", "task", "the", "inc", "ltd", "llc",
          "corporation", "corp", "company", "gmbh", "co"}


@dataclass
class Info:
    summary: str = ""        # пара предложений: что это за программа
    title: str = ""          # название статьи или продукта
    url: str = ""            # откуда взято
    source: str = ""         # «Википедия», «сведения о файле»
    publisher: str = ""      # издатель из самого файла

    @property
    def text(self) -> str:
        parts = []
        if self.summary:
            parts.append(self.summary)
        if self.publisher and self.publisher.lower() not in self.summary.lower():
            parts.append(tr(f"Издатель: {self.publisher}.", f"Publisher: {self.publisher}."))
        return " ".join(parts)


# ----------------------------------------------------------- сведения о файле ---

def _exe_from_command(command: str) -> str:
    """Путь к исполняемому файлу из командной строки автозапуска."""
    command = (command or "").strip()
    if command.startswith('"'):
        return command[1:].split('"', 1)[0]
    m = re.match(r"(.+?\.(?:exe|com|bat|cmd|app))(?:\s|$)", command, re.IGNORECASE)
    return m.group(1) if m else command.split(" ", 1)[0]


def _win_version_info(path: str) -> dict:
    import ctypes
    from ctypes import wintypes
    ver = ctypes.windll.version
    size = ver.GetFileVersionInfoSizeW(path, None)
    if not size:
        return {}
    buf = ctypes.create_string_buffer(size)
    if not ver.GetFileVersionInfoW(path, 0, size, buf):
        return {}
    ptr, n = ctypes.c_void_p(), wintypes.UINT()
    if not ver.VerQueryValueW(buf, r"\VarFileInfo\Translation", ctypes.byref(ptr), ctypes.byref(n)) or not n.value:
        langs = ["040904B0", "040904E4", "041904B0"]
    else:
        pairs = ctypes.cast(ptr, ctypes.POINTER(wintypes.WORD * (n.value // 2))).contents
        langs = [f"{pairs[i]:04X}{pairs[i + 1]:04X}" for i in range(0, len(pairs) - 1, 2)]
    out = {}
    for key, name in (("description", "FileDescription"), ("company", "CompanyName"), ("product", "ProductName")):
        for lang in langs:
            if ver.VerQueryValueW(buf, f"\\StringFileInfo\\{lang}\\{name}", ctypes.byref(ptr), ctypes.byref(n)) and n.value:
                value = ctypes.wstring_at(ptr.value, n.value).strip("\x00 ")
                if value:
                    out[key] = value
                    break
    return out


def _mac_bundle_info(path: str) -> dict:
    p = Path(path)
    for parent in [p, *p.parents]:
        if parent.suffix == ".app":
            try:
                with open(parent / "Contents" / "Info.plist", "rb") as f:
                    pl = plistlib.load(f)
            except (OSError, ValueError, plistlib.InvalidFileException):
                return {}
            out = {"product": pl.get("CFBundleDisplayName") or pl.get("CFBundleName") or parent.stem}
            ident = str(pl.get("CFBundleIdentifier", ""))
            if ident.count(".") >= 2:  # com.yandex.music → Yandex
                out["company"] = ident.split(".")[1].capitalize()
            copy = str(pl.get("NSHumanReadableCopyright", "")).strip()
            if copy:
                out["copyright"] = copy
            return out
    return {}


def file_info(path: str) -> dict:
    """Описание, издатель и продукт из самого файла. Пусто, если файла нет или он молчит."""
    path = _exe_from_command(path)
    if not path or not os.path.exists(path):
        return {}
    try:
        if IS_WINDOWS:
            return _win_version_info(path)
        if IS_MAC:
            return _mac_bundle_info(path)
    except Exception:  # ctypes, кривые ресурсы файла — не повод падать
        return {}
    return {}


# ------------------------------------------------------------------ Википедия ---

def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-zа-яё0-9]{3,}", text.lower()) if w not in _NOISE}


def query_for(name: str, info: dict | None = None) -> str:
    """Что искать: название продукта из файла или очищенное имя процесса."""
    info = info or {}
    for key in ("product", "description"):
        value = info.get(key, "")
        if value and _words(value):
            return value
    base = re.sub(r"\.(exe|app|plist|lnk|desktop|service)$", "", name.strip(), flags=re.IGNORECASE)
    base = re.sub(r"^(com|org|net|io)\.", "", base)        # com.adobe.acc → adobe acc
    base = re.sub(r"[._\-]+", " ", base)
    base = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", base)       # OneDriveSetup → One Drive Setup
    kept = [w for w in base.split() if w.lower() not in _NOISE]
    return " ".join(kept) or base


def _get_json(url: str, timeout: float) -> dict:
    with net.urlopen(url, timeout, {"User-Agent": _UA}) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _relevant(title: str, query: str) -> bool:
    """Статья про то, что искали? Хоть одно значимое слово должно совпасть."""
    t, q = _words(title), _words(query)
    if not q:
        return False
    if t & q:
        return True
    joined = "".join(re.findall(r"[a-zа-яё0-9]+", query.lower()))   # «One Drive Setup» ~ «OneDrive»
    return any(len(w) >= 5 and (w in joined or joined in w) for w in t)


def _first_sentences(text: str, n: int = 2, limit: int = 420) -> str:
    text = re.sub(r"\s+", " ", re.sub(r"\([^()]*\)", "", text)).strip()   # убираем скобки с транскрипцией
    text = re.sub(r"\s+([,.])", r"\1", text)
    parts = re.split(r"(?<=[.!?])\s+(?=[A-ZА-ЯЁ])", text)
    out = " ".join(parts[:n])
    return out if len(out) <= limit else out[:limit].rsplit(" ", 1)[0] + "…"


def wikipedia(query: str, timeout: float = 6) -> Info | None:
    if not query.strip():
        return None
    for lang in (("en",) if i18n.lang() == "en" else ("ru", "en")):
        params = {
            "action": "query", "format": "json", "formatversion": "2", "redirects": "1",
            "generator": "search", "gsrsearch": query, "gsrlimit": "3", "gsrnamespace": "0",
            "prop": "extracts|info|pageprops", "exintro": "1", "explaintext": "1", "exsentences": "3",
            "inprop": "url", "ppprop": "disambiguation",
        }
        data = _get_json(f"https://{lang}.wikipedia.org/w/api.php?" + urllib.parse.urlencode(params), timeout)
        pages = sorted(data.get("query", {}).get("pages", []), key=lambda p: p.get("index", 99))
        for page in pages:
            title, extract = page.get("title", ""), page.get("extract", "")
            if "disambiguation" in page.get("pageprops", {}) or not extract or not _relevant(title, query):
                continue
            return Info(summary=_first_sentences(extract), title=title, url=page.get("fullurl", ""),
                        source=tr("Википедия", "Wikipedia"))
    return None


# ----------------------------------------------------------------------- кеш ---

def _cache_path() -> Path:
    return app_dir() / "online.json"


def _load_cache() -> dict:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _key(name: str) -> str:
    """Ключ кеша с языком: описания на русском и английском хранятся отдельно."""
    return f"{i18n.lang()}:{name.strip().lower()}"


def cached(name: str) -> tuple[bool, Info | None]:
    """(есть ли свежая запись в кеше, описание). Без сети — можно звать из окна."""
    with _lock:
        rec = _load_cache().get(_key(name))
    if not isinstance(rec, dict):
        return False, None
    info = rec.get("info")
    ttl = TTL_FOUND if info else TTL_MISS
    if time.time() - rec.get("t", 0) > ttl:
        return False, None
    try:
        return True, Info(**info) if info else None
    except TypeError:
        return False, None


def _store(name: str, info: Info | None) -> None:
    with _lock:
        data = _load_cache()
        data[_key(name)] = {"t": int(time.time()), "info": asdict(info) if info else None}
        if len(data) > 2000:  # не даём файлу расти бесконечно: выкидываем самые старые
            for k in sorted(data, key=lambda k: data[k].get("t", 0))[:len(data) - 2000]:
                del data[k]
        try:
            _cache_path().write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass


def describe(name: str, path: str = "", timeout: float = 6) -> Info | None:
    """Что это за программа. Сначала кеш, потом файл и Википедия. None — ничего не нашлось.
    Сетевые ошибки не кешируются: в следующий раз попробуем снова."""
    hit, info = cached(name)
    if hit:
        return info
    finfo = file_info(path)
    publisher = finfo.get("company", "")
    global _offline_until
    offline = time.time() < _offline_until
    try:
        info = None if offline else wikipedia(query_for(name, finfo), timeout)
    except Exception:
        info, offline = None, True
        _offline_until = time.time() + 60
    if info is None and finfo.get("description"):
        product = finfo.get("product", "")
        desc = finfo["description"]
        summary = desc if not product or product.lower() in desc.lower() else f"{desc} ({product})"
        info = Info(summary=summary.rstrip(".") + ".", title=product or desc, source=tr("сведения о файле", "file details"))
    if info is not None:
        info.publisher = publisher
    elif publisher:
        info = Info(publisher=publisher, source=tr("сведения о файле", "file details"))
    if not offline:  # без сети не запоминаем: в следующий раз спросим Википедию снова
        _store(name, info)
    return info


def as_html(info: Info | None, muted: str) -> str:
    """Короткий блок для окна с пометкой, что данные не проверены."""
    if info is None:
        return f"<span style='color:{muted}'>{tr('В интернете описание не нашлось.', 'No description found online.')}</span>"
    link = f" <a href='{info.url}'>{tr('подробнее', 'more')}</a>" if info.url else ""
    note = tr(f"Источник: {info.source}. Из интернета, не проверено базой WhySlow.",
              f"Source: {info.source}. From the internet, not verified by the WhySlow database.")
    return f"{_esc(info.text)}{link}<br><span style='color:{muted}'>{note}</span>"


def _esc(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
