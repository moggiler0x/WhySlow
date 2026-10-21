"""Шифрование платных модулей: без ключа их код нельзя ни прочитать, ни выполнить.

Зачем
-----
Проверку подписки (``license.has_premium``) в открытом коде можно вырезать. Чтобы это
ничего не давало, сам код платных функций лежит в сборке **зашифрованным** (файлы
``whyslow/premium/<функция>.wsl``). Расшифровать его можно только ключом функции, а ключ
приходит внутри подписанного ключа подписки (его нельзя подделать) и только после оплаты.
Вырезали проверку — функция всё равно не запускается, потому что расшифровать нечем.

Ключ каждой функции выводится из секрета владельца: ``feature_key(seed, "compare")``.
В программе секрета нет — туда попадают только готовые ключи функций внутри токена, поэтому
по коду программы вычислить ключ другой функции нельзя.

Формат ``.wsl``: ``WSEAL1`` + nonce(16) + тег HMAC-SHA256(32) + шифртекст. Шифр — потоковый
на SHA-256 в режиме счётчика, аутентификация — HMAC-SHA256 (encrypt-then-MAC). Нужен только
``hashlib`` и ``hmac`` из стандартной библиотеки, сторонних зависимостей нет.

Честная граница: оплативший подписку получает ключи и в принципе может достать расшифрованный
код. От этого не защищён никто. Шифрование не пускает тех, кто не платил, и делает бесполезной
правку проверки в коде.
"""

from __future__ import annotations

import hashlib
import hmac
import os

MAGIC = b"WSEAL1"
_NONCE = 16
_TAG = 32


class SealError(Exception):
    """Блок повреждён или ключ не подходит."""


def _hkdf(ikm: bytes, salt: bytes, info: bytes, length: int = 32) -> bytes:
    """HKDF-SHA256 (RFC 5869). Один вывод одинаков в Python, на сервере и у владельца."""
    prk = hmac.new(salt, ikm, hashlib.sha256).digest()
    out, block, counter = b"", b"", 1
    while len(out) < length:
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
        counter += 1
    return out[:length]


def feature_key(seed: bytes, feature: str) -> bytes:
    """Ключ функции из 32-байтного секрета владельца (того же, что подписывает ключи)."""
    return _hkdf(seed, b"whyslow-seal-key", feature.encode("utf-8"))


def _keystream(key: bytes, nonce: bytes, n: int) -> bytes:
    ek = _hkdf(key, nonce, b"whyslow-seal-enc")
    out = bytearray()
    counter = 0
    while len(out) < n:
        out += hashlib.sha256(ek + counter.to_bytes(8, "big")).digest()
        counter += 1
    return bytes(out[:n])


def seal_bytes(data: bytes, key: bytes) -> bytes:
    nonce = os.urandom(_NONCE)
    ct = bytes(a ^ b for a, b in zip(data, _keystream(key, nonce, len(data))))
    mk = _hkdf(key, nonce, b"whyslow-seal-mac")
    tag = hmac.new(mk, MAGIC + nonce + ct, hashlib.sha256).digest()
    return MAGIC + nonce + tag + ct


def open_sealed(blob: bytes, key: bytes) -> bytes:
    if len(blob) < len(MAGIC) + _NONCE + _TAG or blob[: len(MAGIC)] != MAGIC:
        raise SealError("это не защищённый модуль WhySlow")
    nonce = blob[len(MAGIC): len(MAGIC) + _NONCE]
    tag = blob[len(MAGIC) + _NONCE: len(MAGIC) + _NONCE + _TAG]
    ct = blob[len(MAGIC) + _NONCE + _TAG:]
    mk = _hkdf(key, nonce, b"whyslow-seal-mac")
    if not hmac.compare_digest(tag, hmac.new(mk, MAGIC + nonce + ct, hashlib.sha256).digest()):
        raise SealError("ключ не подходит или модуль повреждён")
    return bytes(a ^ b for a, b in zip(ct, _keystream(key, nonce, len(ct))))
