"""Загрузчик платных функций: расшифровывает и запускает модуль только по ключу подписки.

Каждая платная функция — это отдельный модуль ``whyslow/premium/<функция>.py`` с функцией
``build(main) -> QWidget`` (виджет вкладки). В релизной сборке исходника ``.py`` нет — вместо
него лежит зашифрованный ``<функция>.wsl``. Чтобы его открыть, нужен ключ функции, а он есть
только у действующей подписки (приходит внутри подписанного токена). Поэтому:

* нет подписки — ключа нет — модуль не расшифровать и не запустить (``PremiumLocked``);
* вырезали проверку ``has_premium`` — толку ноль: кода функции в сборке нет, только шифртекст.

При разработке исходники ``.py`` лежат открыто, ключ не нужен — загрузчик просто импортирует их.
Так другие ветки пишут и тестируют свои функции как обычно, а шифрование включается при сборке.

    from whyslow import premium
    tab = premium.widget("compare", main)   # PremiumLocked, если подписки нет
"""

from __future__ import annotations

import importlib
import sys
import types
from pathlib import Path

_PKG = __name__  # "whyslow.premium"
_DIR = Path(__file__).resolve().parent
_cache: dict[str, types.ModuleType] = {}


class PremiumLocked(Exception):
    """Модуль функции закрыт: нет подписки, нет ключа или модуля нет в сборке."""


def features() -> list[str]:
    """Платные функции, доступные в этой сборке (по файлам .wsl и .py)."""
    names = set()
    for p in _DIR.iterdir():
        if p.suffix in (".wsl", ".py") and p.stem != "__init__":
            names.add(p.stem)
    return sorted(names)


def is_sealed(feature: str) -> bool:
    return (_DIR / f"{feature}.wsl").exists()


def available(feature: str) -> bool:
    return is_sealed(feature) or (_DIR / f"{feature}.py").exists()


def _content_key(feature: str) -> bytes | None:
    from .. import license
    return license.active_keys().get(feature)


def load(feature: str) -> types.ModuleType:
    """Возвращает модуль функции. PremiumLocked, если его нельзя открыть."""
    if feature in _cache:
        return _cache[feature]
    name = f"{_PKG}.{feature}"
    sealed = _DIR / f"{feature}.wsl"
    plain = _DIR / f"{feature}.py"
    if sealed.exists():
        from .. import seal
        key = _content_key(feature)
        if key is None:
            raise PremiumLocked(feature)
        try:
            source = seal.open_sealed(sealed.read_bytes(), key).decode("utf-8")
        except seal.SealError as e:
            raise PremiumLocked(f"{feature}: {e}") from e
        mod = _exec(name, source, str(sealed))
    elif plain.exists():
        # Разработка: исходник открыт, импортируем обычным образом
        mod = importlib.import_module(name)
    else:
        raise PremiumLocked(feature)
    _cache[feature] = mod
    return mod


def _exec(name: str, source: str, origin: str) -> types.ModuleType:
    mod = types.ModuleType(name)
    mod.__file__ = origin
    mod.__package__ = _PKG
    sys.modules[name] = mod  # чтобы относительные импорты внутри модуля работали
    try:
        exec(compile(source, origin, "exec"), mod.__dict__)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return mod


def widget(feature: str, main):
    """Строит виджет вкладки функции. PremiumLocked, если подписки/ключа/модуля нет."""
    mod = load(feature)
    builder = getattr(mod, "build", None)
    if builder is None:
        raise PremiumLocked(f"{feature}: в модуле нет build(main)")
    return builder(main)
