"""Проверка паролей из менеджера паролей по базе утечек и замена утёкших.

Менеджеры паролей (Chrome, Edge, Firefox, Safari и «Пароли» macOS, Bitwarden, 1Password, KeePass,
LastPass) умеют выгружать пароли в CSV и загружать их обратно. WhySlow читает такую выгрузку,
проверяет каждый пароль по базе Have I Been Pwned и для утёкших предлагает новый случайный пароль.

Сам пароль никуда не отправляется: от него считается SHA-1, и на сервер уходят только первые
5 символов хеша (k-анонимность). Сервер возвращает все хеши с таким началом, сравнение — у нас.

Пароль в менеджере нельзя менять в обход сайта: иначе в менеджере окажется пароль, который сайт
не знает. Поэтому новый пароль попадает в файл для импорта только после того, как пользователь
подтвердил, что сменил его на самом сайте.
"""

from __future__ import annotations

import csv
import hashlib
import io
import secrets
import string
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import net

RANGE_URL = "https://api.pwnedpasswords.com/range/"

# Названия колонок в выгрузках разных менеджеров (в нижнем регистре)
_COLUMNS = {
    "password": ("password", "login_password", "pass"),
    "username": ("username", "login_username", "login name", "user name", "login", "email"),
    "url": ("url", "login_uri", "web site", "website", "origin", "hostname"),
    "title": ("name", "title", "account"),
}
# Firefox при импорте оставляет более свежий пароль — время смены надо обновить
_CHANGED_AT = "timepasswordchanged"

SYMBOLS = "!#$%&*+-=?@_"


class VaultError(Exception):
    pass


@dataclass
class Entry:
    row: int                   # номер строки в выгрузке (без заголовка)
    title: str
    url: str
    username: str
    password: str = field(repr=False)
    leaks: int | None = None   # сколько раз встречается в утечках; None — ещё не проверен
    new_password: str = field(default="", repr=False)
    changed: bool = False      # пользователь подтвердил, что сменил пароль на сайте

    @property
    def site(self) -> str:
        host = urlsplit(self.url).hostname if "://" in self.url else ""
        return host or self.title or self.url

    @property
    def leaked(self) -> bool:
        return bool(self.leaks)


@dataclass
class Vault:
    path: Path
    header: list[str]
    rows: list[list[str]] = field(repr=False)
    cols: dict[str, int]
    entries: list[Entry]

    @property
    def leaked(self) -> list[Entry]:
        return [e for e in self.entries if e.leaked]

    @property
    def changed(self) -> list[Entry]:
        return [e for e in self.entries if e.changed]


def _find_columns(header: list[str]) -> dict[str, int]:
    names = [h.strip().lower() for h in header]
    cols = {}
    for key, variants in _COLUMNS.items():
        for v in variants:
            if v in names:
                cols[key] = names.index(v)
                break
    return cols


def parse(text: str, path: Path | str = "") -> Vault:
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    if not rows:
        raise VaultError("empty")
    header, body = rows[0], rows[1:]
    cols = _find_columns(header)
    if "password" not in cols:
        raise VaultError("no password column")

    def cell(r: list[str], key: str) -> str:
        i = cols.get(key)
        return r[i].strip() if i is not None and i < len(r) else ""

    entries = []
    for n, r in enumerate(body):
        pw = r[cols["password"]] if cols["password"] < len(r) else ""
        if not pw:  # заметки, карты и прочие записи без пароля
            continue
        url = cell(r, "url").split(",")[0].strip()  # у Bitwarden может быть несколько адресов через запятую
        entries.append(Entry(n, cell(r, "title"), url, cell(r, "username"), pw))
    return Vault(Path(path), header, body, cols, entries)


def load(path: Path | str) -> Vault:
    p = Path(path)
    try:
        raw = p.read_bytes()
    except OSError as e:
        raise VaultError(str(e)) from e
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return parse(raw.decode(enc), p)
        except UnicodeDecodeError:
            continue
    raise VaultError("unknown encoding")


# ------------------------------------------------------------- проверка ---

def _sha1(password: str) -> str:
    return hashlib.sha1(password.encode("utf-8")).hexdigest().upper()


def fetch_range(prefix: str, timeout: float = 15) -> dict[str, int]:
    """Все суффиксы хешей с этим началом и сколько раз каждый встречался в утечках.
    Add-Padding добавляет в ответ фальшивые строки с нулём, чтобы по размеру ответа нельзя было
    ничего понять; их отбрасываем."""
    with net.urlopen(RANGE_URL + prefix, timeout=timeout, headers={"Add-Padding": "true"}) as r:
        text = r.read().decode("utf-8", "replace")
    out = {}
    for line in text.splitlines():
        suffix, _, count = line.strip().partition(":")
        if count.isdigit() and int(count):
            out[suffix.upper()] = int(count)
    return out


def check(vault: Vault, progress=lambda _t: None, fetch=fetch_range) -> None:
    """Заполняет leaks у всех записей. Одинаковые пароли и одинаковые начала хешей
    запрашиваются один раз."""
    hashes = {e.row: _sha1(e.password) for e in vault.entries}
    prefixes = sorted({h[:5] for h in hashes.values()})
    ranges: dict[str, dict[str, int]] = {}
    for i, prefix in enumerate(prefixes, 1):
        progress(f"{i}/{len(prefixes)}")
        ranges[prefix] = fetch(prefix)
    for e in vault.entries:
        h = hashes[e.row]
        e.leaks = ranges[h[:5]].get(h[5:], 0)


# ------------------------------------------------------- новый пароль ---

def generate(length: int = 20, symbols: bool = True) -> str:
    """Случайный пароль, где есть и строчные, и заглавные буквы, и цифры (и символы, если можно)."""
    groups = [string.ascii_lowercase, string.ascii_uppercase, string.digits] + ([SYMBOLS] if symbols else [])
    alphabet = "".join(groups)
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if all(any(c in g for c in pw) for g in groups):
            return pw


def change_url(url: str) -> str:
    """Адрес страницы смены пароля. /.well-known/change-password — стандарт, по которому сайт сам
    перенаправляет на свою страницу смены; если сайт его не поддерживает, откроется главная."""
    parts = urlsplit(url if "://" in url else "https://" + url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        return ""
    return f"https://{parts.netloc}/.well-known/change-password"


# ---------------------------------------------------- файл для импорта ---

def export(vault: Vault, path: Path | str) -> int:
    """Пишет файл для импорта в тот же менеджер: тот же формат, только записи со сменённым
    паролем, и в них только новый пароль. Возвращает число записей."""
    changed = vault.changed
    i_pw = vault.cols["password"]
    i_time = next((i for i, h in enumerate(vault.header) if h.strip().lower() == _CHANGED_AT), None)
    now_ms = str(int(time.time() * 1000))
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(vault.header)
        for e in changed:
            row = list(vault.rows[e.row])
            row += [""] * (len(vault.header) - len(row))
            row[i_pw] = e.new_password
            if i_time is not None:
                row[i_time] = now_ms
            w.writerow(row)
    return len(changed)


def wipe(path: Path | str) -> bool:
    """Удаляет файл с паролями, предварительно затерев содержимое. True — файла больше нет."""
    p = Path(path)
    try:
        size = p.stat().st_size
        with open(p, "r+b") as f:
            f.write(secrets.token_bytes(size))
            f.flush()
        p.unlink()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True
