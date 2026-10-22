"""Подписка WhySlow: ключи доступа к платным функциям.

Как это устроено
----------------
* Права на платные функции даёт «лицензия» — строка вида ``WS1.<данные>.<подпись>``.
  В данных записаны тариф, срок и коды устройств, подпись сделана закрытым ключом
  Ed25519 владельца. В программе лежит только открытый ключ (``PUBLIC_KEY``): им можно
  проверить подпись, но нельзя сделать новую, поэтому ключ не подделать, даже прочитав код.
* Лицензия привязана к коду устройства (``device_id()``): на другом компьютере она не работает.
* Покупатель получает её одним из двух способов:
    1. **Код активации** ``WS-XXXX-XXXX-XXXX-XXXX``. Программа отправляет его по HTTPS на
       сервер активации (``SERVER_URL``), сервер проверяет, оплачен ли код, сколько устройств
       уже подключено и не отозван ли он, и возвращает лицензию на этот компьютер на неделю.
       Раз в день программа её продлевает; без интернета лицензия работает до конца недели.
    2. **Готовая лицензия** ``WS1....`` без сервера: покупатель присылает код устройства,
       владелец выпускает лицензию командой ``python owner/keys.py issue`` и отправляет её.

Платные функции проверяют доступ так::

    from whyslow import license
    if license.has_premium("compare"): ...
    license.on_change(callback)   # активировали, сбросили или истёк срок

Всё, что здесь хранится, — подписанный документ; если его изменить, подпись не сойдётся.
"""

from __future__ import annotations

import base64
import datetime
import functools
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import __version__, ed25519
from .i18n import tr
from .util import IS_MAC, IS_WINDOWS, app_dir, read_json, run, write_json

# Открытый ключ владельца (hex, 32 байта). Его вписывает «python owner/keys.py init».
# Пока он пуст, платные функции закрыты у всех.
PUBLIC_KEY = "c8330862f212f43a65db9a709eed28a9b2929341a4c166785f656b6fc9735b25"
# Сервер активации кодов. Пусто — коды не принимаются, работают только готовые лицензии.
SERVER_URL = "https://whyslow-license.whyslow-license.workers.dev"
# Где купить подписку: Telegram-бот с тарифами, оплатой и связью с владельцем
PREMIUM_URL = "https://t.me/WhySlowPCbot"

PLANS = {
    # price — в звёздах Telegram за месяц, оплата в боте (PREMIUM_URL)
    "solo": {"name": ("Соло", "Solo"), "price": 50, "devices": 1},
    "family": {"name": ("3 устройства", "3 devices"), "price": 120, "devices": 3},
}

# Платные функции: id → (название по-русски, по-английски). Доступ даёт любая активная подписка.
FEATURES = {
    "twin": ("Двойник ПК", "PC twin"),
    "health_money": ("Здоровье в деньгах", "Health in money"),
    "xray": ("Рентген фриза", "Freeze X-ray"),
    "gamemode": ("Игровой режим", "Game mode"),
    "compare": ("Сравнение с похожими ПК", "Comparison with similar PCs"),
    "pests": ("Тихие вредители", "Hidden pests"),
    "migrate": ("Переезд на новый ПК", "Moving to a new PC"),
}

REFRESH_EVERY = 86400           # продлевать онлайн-лицензию раз в сутки
CLOCK_SLACK = 2 * 86400         # на сколько можно «откатить» часы, прежде чем это заметим
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # без 0/O и 1/I, чтобы не путали
_CODE_RE = re.compile(r"^(?:WS)?([%s]{16})$" % _CODE_ALPHABET)

_lock = threading.RLock()
_listeners: list = []
_last_active: bool | None = None
_device: str | None = None


class LicenseError(Exception):
    """Ключ не подошёл. Текст — для человека."""


@dataclass
class Status:
    active: bool = False
    plan: str = ""
    expires: float = 0.0          # до когда оплачено (для онлайн-кода — срок подписки)
    devices: int = 0              # сколько устройств разрешает тариф
    device: str = ""              # код этого компьютера
    online: bool = False          # получено через код активации
    error: str = ""               # почему не активна, если ключ введён
    kid: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def plan_name(self) -> str:
        p = PLANS.get(self.plan)
        return tr(*p["name"]) if p else self.plan


# ------------------------------------------------------------ код устройства ---

def _machine_raw() -> str:
    if IS_WINDOWS:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                                winreg.KEY_READ | 0x0100) as k:  # KEY_WOW64_64KEY
                return winreg.QueryValueEx(k, "MachineGuid")[0]
        except OSError:
            pass
    elif IS_MAC:
        code, out = run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], timeout=5)
        m = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"', out)
        if code == 0 and m:
            return m.group(1)
    else:
        for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
            try:
                s = open(p, encoding="ascii").read().strip()
                if s:
                    return s
            except OSError:
                pass
    import uuid
    return f"mac:{uuid.getnode():012x}"


def device_id() -> str:
    """Короткий код этого компьютера, например «K7Q2M-9XWPA». По нему нельзя узнать
    ничего о компьютере: это хеш системного идентификатора."""
    global _device
    if _device is None:
        h = hashlib.sha256(("whyslow-device:" + _machine_raw()).encode()).digest()
        n = int.from_bytes(h[:7], "big") >> 6  # 50 бит
        chars = "".join(_CODE_ALPHABET[(n >> (5 * i)) & 31] for i in range(9, -1, -1))
        _device = chars[:5] + "-" + chars[5:]
    return _device


# ------------------------------------------------------------------ токены ---

def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _b64e(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def make_token(secret: bytes, payload: dict) -> str:
    """Подписывает лицензию. Нужен закрытый ключ — его есть только у владельца."""
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode()
    return "WS1." + _b64e(body) + "." + _b64e(ed25519.sign(secret, body))


@functools.lru_cache(maxsize=16)
def _verified(token: str, pub: str) -> dict | None:
    """Подпись проверяется один раз на строку: has_premium() зовут часто, а проверка небыстрая."""
    _, b, sg = token.split(".")
    try:
        body = _b64d(b)
        if not ed25519.verify(bytes.fromhex(pub), body, _b64d(sg)):
            return None
        data = json.loads(body)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def normalize_device(d: str) -> str:
    s = re.sub(r"[^A-Z0-9]", "", d.upper())
    return s[:5] + "-" + s[5:] if len(s) == 10 else d.strip().upper()


def read_token(token: str, public: str | None = None, device: str | None = None, now: float | None = None) -> dict:
    """Проверяет подпись, срок и привязку к устройству. Возвращает данные лицензии."""
    pub = public if public is not None else PUBLIC_KEY
    if not pub:
        raise LicenseError(tr("в этой сборке подписка ещё не настроена", "subscriptions are not set up in this build"))
    token = token.strip()
    if not token.startswith("WS1.") or token.count(".") != 2:
        raise LicenseError(tr("это не ключ WhySlow", "this is not a WhySlow key"))
    data = _verified(token, pub)
    if data is None:
        raise LicenseError(tr("ключ повреждён или подделан", "the key is damaged or forged"))
    data = dict(data)
    if data.get("plan") not in PLANS:
        raise LicenseError(tr("неизвестный тариф", "unknown plan"))
    now = time.time() if now is None else now
    if float(data.get("exp", 0)) < now:
        raise LicenseError(tr("срок действия ключа закончился", "the key has expired"))
    devs = [normalize_device(d) for d in data.get("dev") or []]
    if devs and (device or device_id()) not in devs:
        raise LicenseError(tr("ключ выпущен для другого компьютера", "the key was issued for another computer"))
    return data


# --------------------------------------------------------------- хранилище ---

def _path():
    return app_dir() / "license.json"


def _load() -> dict:
    d = read_json(_path(), {})
    return d if isinstance(d, dict) else {}


def _save(d: dict) -> None:
    try:
        write_json(_path(), d)
    except OSError:
        pass


def status(now: float | None = None) -> Status:
    """Текущее состояние подписки на этом компьютере."""
    now = time.time() if now is None else now
    with _lock:
        d = _load()
    st = Status(device=device_id())
    token = d.get("token")
    if not token:
        st.error = d.get("error", "")
        return st
    st.online = bool(d.get("code"))
    if now + CLOCK_SLACK < float(d.get("seen", 0)):
        st.error = tr("часы компьютера переведены назад — поправьте дату и время",
                      "the computer clock was set back — fix the date and time")
        return st
    try:
        data = read_token(token, now=now)
    except LicenseError as e:
        st.error = str(e)
        if st.online:
            st.error += tr(" — подключитесь к интернету, чтобы продлить", " — connect to the internet to renew")
        return st
    st.active = True
    st.plan = data["plan"]
    st.expires = float(data.get("sub") or data["exp"])
    st.devices = int(data.get("max") or PLANS[st.plan]["devices"])
    st.kid = data.get("kid", "")
    st.extra = data
    if now > float(d.get("seen", 0)) + 3600:
        d["seen"] = now
        with _lock:
            _save(d)
    return st


def has_premium(feature: str | None = None) -> bool:
    """Открыта ли платная функция. Любая действующая подписка открывает все функции."""
    try:
        return status().active
    except Exception:
        return False


def auth_headers() -> dict:
    """Заголовок для запросов к серверу WhySlow: сервер сам проверит подпись и срок."""
    token = _load().get("token")
    return {"Authorization": f"Bearer {token}", "X-WhySlow-Device": device_id()} if token else {}


def active_keys() -> dict[str, bytes]:
    """Ключи расшифровки платных модулей из действующей подписки (из подписанного токена).
    Нет подписки — пустой словарь, и зашифрованный модуль не открыть."""
    st = status()
    if not st.active:
        return {}
    out = {}
    for feature, hexkey in (st.extra.get("keys") or {}).items():
        try:
            out[feature] = bytes.fromhex(hexkey)
        except (ValueError, TypeError):
            pass
    return out


def _date(ts: float) -> str:
    return datetime.datetime.fromtimestamp(ts).strftime("%d.%m.%Y")


def status_text(st: Status | None = None) -> str:
    st = st or status()
    if not st.active:
        return tr("Подписка не активна", "No active subscription")
    left = max(0, int((st.expires - time.time()) // 86400))
    return tr(f"«{st.plan_name}» до {_date(st.expires)} (осталось {left} дн.)",
              f"“{st.plan_name}” until {_date(st.expires)} ({left} days left)")


# -------------------------------------------------------------- подписчики ---

def on_change(callback) -> None:
    """callback() вызывается без аргументов, когда подписка включилась или выключилась.
    Может прийти не из потока интерфейса."""
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)


def _notify() -> None:
    global _last_active
    active = has_premium()
    with _lock:
        _last_active = active
        listeners = list(_listeners)
    for cb in listeners:
        try:
            cb()
        except Exception:
            pass


def tick() -> None:
    """Сообщает подписчикам, если подписка истекла сама по себе. Зовётся по таймеру."""
    if has_premium() != _last_active:
        _notify()


# ------------------------------------------------------------- ввод ключа ---

def normalize_code(text: str) -> str | None:
    s = re.sub(r"[\s\-_]", "", text.upper())
    m = _CODE_RE.match(s)
    return m.group(1) if m else None


def code_kid(code: str) -> str:
    return hashlib.sha256(("whyslow-code:" + code).encode()).hexdigest()[:16]


def enter_key(text: str) -> Status:
    """Принимает код активации или готовую лицензию. Бросает LicenseError с понятной причиной."""
    text = text.strip()
    if not text:
        raise LicenseError(tr("введите ключ", "enter a key"))
    if text.startswith("WS1."):
        read_token(text)
        with _lock:
            _save({"token": text, "seen": time.time()})
    else:
        code = normalize_code(text)
        if not code:
            raise LicenseError(tr("ключ выглядит как WS-XXXX-XXXX-XXXX-XXXX или начинается с WS1.",
                                  "a key looks like WS-XXXX-XXXX-XXXX-XXXX or starts with WS1."))
        token = _server("activate", code)
        with _lock:
            _save({"token": token, "code": code, "seen": time.time(), "refreshed": time.time()})
    _notify()
    return status()


def refresh(force: bool = False) -> bool:
    """Продлевает лицензию, полученную по коду. True — если что-то поменялось.
    Сеть недоступна — молча оставляем как есть: лицензия ещё поработает без интернета."""
    with _lock:
        d = _load()
    code = d.get("code")
    if not code or (not force and time.time() - float(d.get("refreshed", 0)) < REFRESH_EVERY):
        return False
    before = has_premium()
    try:
        token = _server("refresh", code)
    except _Rejected as e:  # отозван, закончился или устройство отвязали
        with _lock:
            d = _load()
            d.pop("token", None)
            d["error"] = str(e)
            _save(d)
    except LicenseError:
        return False
    else:
        with _lock:
            d = _load()
            d.update(token=token, refreshed=time.time())
            _save(d)
    if has_premium() != before:
        _notify()
    return True


def refresh_async() -> None:
    threading.Thread(target=refresh, daemon=True, name="license-refresh").start()


def deactivate() -> None:
    """Убирает ключ с этого компьютера. Для кода активации ещё и освобождает место на сервере."""
    with _lock:
        d = _load()
    if d.get("code"):
        try:
            _server("deactivate", d["code"])
        except LicenseError:
            pass
    with _lock:
        _save({})
    _notify()


# ------------------------------------------------------------------ сервер ---

class _Rejected(LicenseError):
    """Сервер ответил, что ключ не годится (а не просто не ответил)."""


def server_url() -> str:
    return (os.environ.get("WHYSLOW_LICENSE_SERVER") or SERVER_URL).rstrip("/")


def _post(url: str, body: dict) -> tuple[int, dict]:
    from . import net
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with net.urlopen(req, timeout=15) as r:
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


_ERRORS = {
    "unknown": ("такого ключа нет — проверьте, что он введён без ошибок", "no such key — check it is typed correctly"),
    "revoked": ("ключ отозван", "the key was revoked"),
    "expired": ("срок подписки закончился", "the subscription has expired"),
    "limit": ("ключ уже активирован на максимальном числе устройств. Отключите его на другом компьютере "
              "(кнопка «Отключить на этом компьютере») или возьмите тариф «3 устройства»",
              "the key is already active on the maximum number of devices. Turn it off on another computer "
              "or switch to the 3 devices plan"),
    "device": ("этот компьютер отвязан от ключа — введите ключ снова", "this computer was unlinked from the key — enter it again"),
}


def _server(action: str, code: str) -> str:
    base = server_url()
    if not base:
        raise LicenseError(tr("сервер активации ещё не подключён — попросите готовую лицензию для кода устройства "
                              f"{device_id()}",
                              f"the activation server is not set up yet — ask for a license for device code {device_id()}"))
    import platform
    body = {"code": code, "device": device_id(), "name": platform.node()[:40], "version": __version__}
    try:
        status_code, data = _post(f"{base}/v1/{action}", body)
    except (OSError, ValueError) as e:
        from . import net
        raise LicenseError(tr("не удалось связаться с сервером активации: ", "could not reach the activation server: ")
                           + net.explain(e, tr("сервер", "server")))
    if action == "deactivate":
        return ""
    if status_code == 200 and data.get("token"):
        read_token(data["token"])  # сервер тоже не может выдать то, что не подписано нашим ключом
        return data["token"]
    err = data.get("error", "")
    if err in _ERRORS:
        raise _Rejected(tr(*_ERRORS[err]))
    raise LicenseError(tr(f"сервер активации ответил ошибкой {status_code}", f"the activation server returned error {status_code}"))


def ask_key(parent=None) -> None:
    """Открывает окно ввода ключа (для кнопок «Нужна подписка» в платных вкладках)."""
    from .license_ui import show_license_dialog
    show_license_dialog(parent)
