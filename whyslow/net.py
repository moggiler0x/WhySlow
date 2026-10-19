"""Загрузка из интернета с проверкой сертификатов, которая работает везде.

Python с python.org на macOS не видит системные сертификаты, пока не запустишь
«Install Certificates.command», и любой запрос к GitHub падает с
CERTIFICATE_VERIFY_FAILED. Поэтому добавляем сертификаты сами: из certifi,
если он установлен, а на Mac ещё и из системной связки ключей.
"""

from __future__ import annotations

import functools
import socket
import ssl
import urllib.error
import urllib.request

from .util import IS_MAC, run

_MAC_KEYCHAINS = ["/System/Library/Keychains/SystemRootCertificates.keychain", "/Library/Keychains/System.keychain"]


@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    try:
        import certifi
        ctx.load_verify_locations(certifi.where())
    except (ImportError, OSError, ssl.SSLError):
        pass
    if IS_MAC:
        code, pem = run(["security", "find-certificate", "-a", "-p", *_MAC_KEYCHAINS], timeout=20)
        if code == 0:
            _load_pem(ctx, pem)
    return ctx


def _load_pem(ctx: ssl.SSLContext, pem: str) -> None:
    try:
        ctx.load_verify_locations(cadata=pem)
        return
    except ssl.SSLError:
        pass
    # Один кривой сертификат не должен ломать остальные
    end = "-----END CERTIFICATE-----"
    for block in pem.split(end):
        if "BEGIN CERTIFICATE" in block:
            try:
                ctx.load_verify_locations(cadata=block.strip() + "\n" + end + "\n")
            except ssl.SSLError:
                pass


def urlopen(url: str | urllib.request.Request, timeout: float, headers: dict | None = None):
    req = url if isinstance(url, urllib.request.Request) else urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", req.get_header("User-agent") or "WhySlow")
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())


def explain(e: Exception, what: str = "файл") -> str:
    """Понятное человеку описание сетевой ошибки, с настоящей причиной в конце."""
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return f"{what} не найден на GitHub"
        if e.code == 403:
            return "GitHub временно ограничил запросы (403), попробуйте позже"
        return f"сервер GitHub ответил ошибкой {e.code}"
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
        return ("не удалось проверить сертификат GitHub. Обычно помогает «pip install certifi» "
                f"или «Install Certificates.command» в папке Python ({reason})")
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return "GitHub не ответил вовремя"
    if isinstance(reason, socket.gaierror):
        return "нет подключения к интернету (не удаётся найти github.com)"
    if isinstance(e, urllib.error.URLError):
        return f"нет подключения к интернету или GitHub недоступен ({reason})"
    if isinstance(e, ValueError):
        return f"{what} на GitHub повреждён"
    return str(e)
