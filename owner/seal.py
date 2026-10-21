"""Шифрование платных модулей перед сборкой. Только для владельца.

Каждый ``whyslow/premium/<функция>.py`` зашифровывается в ``<функция>.wsl`` ключом функции,
выведенным из закрытого ключа владельца. В релиз кладутся только ``.wsl`` (это делает
packaging/build.py), поэтому кода платных функций в сборке нет — лишь шифртекст.
Тот же ключ функции программа получает внутри ключа подписки и расшифровывает модуль у себя.

  python owner/seal.py            зашифровать все платные модули → .wsl
  python owner/seal.py --list     что есть и что зашифровано
  python owner/seal.py --clean    удалить .wsl (вернуться к разработке)

Ключ берётся из ~/.whyslow-owner/private.key (как у owner/keys.py) или из переменной
окружения WHYSLOW_SEAL_SEED (hex) — это удобно в CI для подписанной сборки.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from whyslow import seal  # noqa: E402

PREMIUM = ROOT / "whyslow" / "premium"
SEAL_FILE = Path(os.environ.get("WHYSLOW_OWNER_DIR") or Path.home() / ".whyslow-owner") / "seal.key"


def seed() -> bytes:
    # Секрет шифрования, отдельный от ключа подписи. В CI задаётся переменной WHYSLOW_SEAL_SEED.
    env = os.environ.get("WHYSLOW_SEAL_SEED")
    if env:
        return bytes.fromhex(env.strip())
    try:
        return bytes.fromhex(SEAL_FILE.read_text().strip())
    except FileNotFoundError:
        sys.exit(f"Нет секрета шифрования {SEAL_FILE} и переменной WHYSLOW_SEAL_SEED. "
                 "Сначала: python owner/keys.py init")


def modules() -> list[Path]:
    return sorted(p for p in PREMIUM.glob("*.py") if p.stem != "__init__")


def cmd_seal() -> None:
    key_seed = seed()
    found = modules()
    if not found:
        print("В whyslow/premium нет модулей функций (*.py). Нечего шифровать.")
        return
    for src in found:
        feature = src.stem
        blob = seal.seal_bytes(src.read_bytes(), seal.feature_key(key_seed, feature))
        out = src.with_suffix(".wsl")
        out.write_bytes(blob)
        # Проверяем, что расшифровывается тем же ключом
        assert seal.open_sealed(out.read_bytes(), seal.feature_key(key_seed, feature)) == src.read_bytes()
        print(f"  {feature}: {src.name} → {out.name} ({len(blob)} байт)")
    print(f"Зашифровано модулей: {len(found)}. В релиз пойдут только .wsl, исходники .py — нет.")


def cmd_list() -> None:
    for src in modules():
        sealed = src.with_suffix(".wsl").exists()
        print(f"  {src.stem:<16} {'зашифрован (.wsl есть)' if sealed else 'открыт (только .py)'}")
    if not modules():
        print("Модулей функций нет.")


def cmd_clean() -> None:
    n = 0
    for p in PREMIUM.glob("*.wsl"):
        p.unlink()
        n += 1
    print(f"Удалено .wsl: {n}.")


def main() -> None:
    p = argparse.ArgumentParser(description="Шифрование платных модулей WhySlow")
    p.add_argument("--list", action="store_true", help="показать состояние")
    p.add_argument("--clean", action="store_true", help="удалить .wsl")
    a = p.parse_args()
    if a.list:
        cmd_list()
    elif a.clean:
        cmd_clean()
    else:
        cmd_seal()


if __name__ == "__main__":
    main()
