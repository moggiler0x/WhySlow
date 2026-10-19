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

from .i18n import tr
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


def explain(e: Exception, what: str = "") -> str:
    """Понятное человеку описание сетевой ошибки, с настоящей причиной в конце."""
    what = what or tr("файл", "file")
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            return tr(f"{what} не найден на GitHub", f"{what} not found on GitHub")
        if e.code == 403:
            return tr("GitHub временно ограничил запросы (403), попробуйте позже",
                      "GitHub is temporarily rate-limiting requests (403), try again later")
        return tr(f"сервер GitHub ответил ошибкой {e.code}", f"GitHub server returned error {e.code}")
    reason = e.reason if isinstance(e, urllib.error.URLError) else e
    if isinstance(reason, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(reason):
        return tr("не удалось проверить сертификат GitHub. Обычно помогает «pip install certifi» "
                  f"или «Install Certificates.command» в папке Python ({reason})",
                  "could not verify GitHub's certificate. Usually \"pip install certifi\" or "
                  f"\"Install Certificates.command\" in the Python folder helps ({reason})")
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return tr("GitHub не ответил вовремя", "GitHub did not respond in time")
    if isinstance(reason, socket.gaierror):
        return tr("нет подключения к интернету (не удаётся найти github.com)",
                  "no internet connection (cannot resolve github.com)")
    if isinstance(e, urllib.error.URLError):
        return tr(f"нет подключения к интернету или GitHub недоступен ({reason})",
                  f"no internet connection or GitHub is unavailable ({reason})")
    if isinstance(e, ValueError):
        return tr(f"{what} на GitHub повреждён", f"{what} on GitHub is corrupted")
    return str(e)
