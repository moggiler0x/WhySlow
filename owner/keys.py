"""Выпуск ключей подписки WhySlow. Только для владельца, в программу не входит.

Закрытый ключ лежит в ~/.whyslow-owner/private.key и никогда не попадает в репозиторий.
Потеряете его — придётся выпустить новую версию с новым открытым ключом, и все старые
ключи перестанут работать. Сделайте резервную копию файла (флешка, менеджер паролей).

Команды:
  python owner/keys.py init                       создать пару ключей и вписать открытый в whyslow/license.py
  python owner/keys.py issue solo 1 K7Q2M-9XWPA   лицензия «Соло» на 1 месяц для кода устройства
  python owner/keys.py issue family 12 A..., B... «Все устройства» на год для нескольких кодов
  python owner/keys.py server-secret              закрытый ключ для сервера (wrangler secret put PRIVATE_KEY)

Через сервер активации (адрес и пароль администратора — в ~/.whyslow-owner/server.json):
  python owner/keys.py code solo 1 "Иван, @ivan"  новый код активации WS-XXXX-XXXX-XXXX-XXXX
  python owner/keys.py extend WS-... 1            продлить ещё на месяц
  python owner/keys.py revoke WS-...              отозвать (перестанет работать в течение суток)
  python owner/keys.py unlink WS-... K7Q2M-9XWPA  отвязать устройство, чтобы человек мог перенести ключ
  python owner/keys.py list                       все коды
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whyslow import ed25519, license  # noqa: E402

HOME = Path(os.environ.get("WHYSLOW_OWNER_DIR") or Path.home() / ".whyslow-owner")
KEY_FILE = HOME / "private.key"
LOG_FILE = HOME / "issued.jsonl"
SERVER_FILE = HOME / "server.json"
MONTH = 31 * 86400


def secret() -> bytes:
    try:
        return bytes.fromhex(KEY_FILE.read_text().strip())
    except FileNotFoundError:
        sys.exit(f"Нет закрытого ключа {KEY_FILE}. Сначала: python owner/keys.py init")


def cmd_init(a):
    if KEY_FILE.exists():
        sys.exit(f"{KEY_FILE} уже есть. Новый ключ сделает все выданные ключи недействительными; "
                 "если это точно нужно, удалите файл вручную.")
    HOME.mkdir(parents=True, exist_ok=True)
    seed = secrets.token_bytes(32)
    KEY_FILE.write_text(seed.hex() + "\n")
    try:
        os.chmod(KEY_FILE, 0o600)
    except OSError:
        pass
    pub = ed25519.public_key(seed).hex()
    src = ROOT / "whyslow" / "license.py"
    text = src.read_text(encoding="utf-8")
    text = re.sub(r'^PUBLIC_KEY = ".*"$', f'PUBLIC_KEY = "{pub}"', text, count=1, flags=re.M)
    src.write_text(text, encoding="utf-8")
    print(f"Закрытый ключ: {KEY_FILE}  — сделайте его резервную копию и никому не показывайте.")
    print(f"Открытый ключ: {pub}  — вписан в whyslow/license.py, его можно публиковать.")


def cmd_issue(a):
    devices = [license.normalize_device(d) for d in re.split(r"[,\s]+", " ".join(a.devices)) if d]
    limit = license.PLANS[a.plan]["devices"]
    if not devices:
        sys.exit("Укажите код устройства (его видно в окне «Подписка» у покупателя).")
    if len(devices) > limit:
        sys.exit(f"Тариф {a.plan} — не больше {limit} устройств.")
    now = int(time.time())
    payload = {"v": 1, "kid": "off-" + secrets.token_hex(4), "plan": a.plan, "iat": now,
               "exp": now + int(a.months * MONTH), "dev": devices}
    token = license.make_token(secret(), payload)
    license.read_token(token, public=ed25519.public_key(secret()).hex(), device=devices[0])
    HOME.mkdir(parents=True, exist_ok=True)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({**payload, "note": a.note}, ensure_ascii=False) + "\n")
    until = datetime.datetime.fromtimestamp(payload["exp"]).strftime("%d.%m.%Y")
    print(f"Лицензия «{license.PLANS[a.plan]['name'][0]}» до {until} для {', '.join(devices)}:\n")
    print(token)


def cmd_server_secret(a):
    print(secret().hex())


# ---------------------------------------------------------------- сервер ---

def server() -> tuple[str, str]:
    url, token = os.environ.get("WHYSLOW_LICENSE_SERVER"), os.environ.get("WHYSLOW_ADMIN_TOKEN")
    try:
        cfg = json.loads(SERVER_FILE.read_text())
        url, token = url or cfg.get("url"), token or cfg.get("admin_token")
    except (OSError, ValueError):
        pass
    if not url or not token:
        sys.exit(f'Заполните {SERVER_FILE}: {{"url": "https://....workers.dev", "admin_token": "..."}}')
    return url.rstrip("/"), token


def admin(path: str, body: dict | None = None) -> dict:
    url, token = server()
    req = urllib.request.Request(url + path, data=json.dumps(body or {}).encode(), method="POST",
                                 headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json",
                                          "User-Agent": "WhySlow-owner"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        sys.exit(f"Сервер ответил {e.code}: {e.read().decode(errors='replace')}")


def show(rec: dict):
    until = datetime.datetime.fromtimestamp(rec["exp"]).strftime("%d.%m.%Y")
    state = "ОТОЗВАН" if rec.get("revoked") else ("истёк" if rec["exp"] < time.time() else f"до {until}")
    devs = ", ".join(d["id"] for d in rec.get("devices", [])) or "—"
    print(f"{rec.get('code') or rec['kid']}  {rec['plan']:<6}  {state:<14}  устройства: {devs}  {rec.get('note', '')}")


def cmd_code(a):
    rec = admin("/admin/create", {"plan": a.plan, "days": round(a.months * 31), "note": a.note})
    show(rec)
    print(f"\nОтправьте покупателю: {rec['code']}")


def cmd_extend(a):
    show(admin("/admin/extend", {"code": a.code, "days": round(a.months * 31)}))


def cmd_revoke(a):
    show(admin("/admin/revoke", {"code": a.code}))


def cmd_unlink(a):
    show(admin("/admin/unlink", {"code": a.code, "device": license.normalize_device(a.device)}))


def cmd_list(a):
    for rec in admin("/admin/list")["items"]:
        show(rec)


def main():
    p = argparse.ArgumentParser(description="Ключи подписки WhySlow", formatter_class=argparse.RawDescriptionHelpFormatter,
                                epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init").set_defaults(fn=cmd_init)
    s = sub.add_parser("issue")
    s.add_argument("plan", choices=license.PLANS)
    s.add_argument("months", type=float)
    s.add_argument("devices", nargs="+")
    s.add_argument("--note", default="")
    s.set_defaults(fn=cmd_issue)
    sub.add_parser("server-secret").set_defaults(fn=cmd_server_secret)
    s = sub.add_parser("code")
    s.add_argument("plan", choices=license.PLANS)
    s.add_argument("months", type=float)
    s.add_argument("note", nargs="?", default="")
    s.set_defaults(fn=cmd_code)
    s = sub.add_parser("extend")
    s.add_argument("code")
    s.add_argument("months", type=float)
    s.set_defaults(fn=cmd_extend)
    s = sub.add_parser("revoke")
    s.add_argument("code")
    s.set_defaults(fn=cmd_revoke)
    s = sub.add_parser("unlink")
    s.add_argument("code")
    s.add_argument("device")
    s.set_defaults(fn=cmd_unlink)
    sub.add_parser("list").set_defaults(fn=cmd_list)
    a = p.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
