import time

import pytest

from whyslow import license, premium, seal

SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
DEV = "AAAAA-BBBBB"
SRC = b"VALUE = 42\n\n\ndef build(main):\n    return ('widget', main, VALUE)\n"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    premium._cache.clear()
    monkeypatch.setattr(license, "PUBLIC_KEY", PUBLIC)
    monkeypatch.setattr(license, "_device", DEV)
    monkeypatch.setattr(license, "_listeners", [])
    yield
    premium._cache.clear()


def test_sealed_module_needs_the_key(tmp_path, monkeypatch):
    monkeypatch.setattr(premium, "_DIR", tmp_path)
    key = seal.feature_key(SECRET, "compare")
    (tmp_path / "compare.wsl").write_bytes(seal.seal_bytes(SRC, key))

    # Нет подписки — нет ключа — модуль не открыть, даже зная, что он есть
    monkeypatch.setattr(license, "active_keys", lambda: {})
    assert premium.available("compare") and premium.is_sealed("compare")
    with pytest.raises(premium.PremiumLocked):
        premium.widget("compare", "MAIN")

    # Неверный ключ (например, подделка) — тоже замок
    premium._cache.clear()
    monkeypatch.setattr(license, "active_keys", lambda: {"compare": b"\x00" * 32})
    with pytest.raises(premium.PremiumLocked):
        premium.widget("compare", "MAIN")

    # Правильный ключ из подписки — модуль расшифрован и запущен
    premium._cache.clear()
    monkeypatch.setattr(license, "active_keys", lambda: {"compare": key})
    assert premium.widget("compare", "MAIN") == ("widget", "MAIN", 42)


def test_no_module_is_locked(tmp_path, monkeypatch):
    monkeypatch.setattr(premium, "_DIR", tmp_path)
    monkeypatch.setattr(license, "active_keys", lambda: {"ghost": b"\x01" * 32})
    assert not premium.available("ghost")
    with pytest.raises(premium.PremiumLocked):
        premium.widget("ghost", "MAIN")


def test_plaintext_module_in_dev(monkeypatch):
    # В разработке исходник открыт, ключ не нужен
    f = premium._DIR / "_tmptest.py"
    f.write_text(SRC.decode(), encoding="utf-8")
    try:
        premium._cache.clear()
        assert premium.is_sealed("_tmptest") is False
        assert premium.widget("_tmptest", "M") == ("widget", "M", 42)
    finally:
        f.unlink(missing_ok=True)
        import sys
        sys.modules.pop("whyslow.premium._tmptest", None)


def test_end_to_end_subscription_unlocks_sealed_module(tmp_path, monkeypatch):
    """Полный путь: только действующая подписка (ключ с нужными keys) открывает модуль.
    Вырезать проверку бессмысленно — кода в .wsl нет без ключа."""
    monkeypatch.setattr(premium, "_DIR", tmp_path)
    key = seal.feature_key(SECRET, "compare")
    (tmp_path / "compare.wsl").write_bytes(seal.seal_bytes(SRC, key))

    # Пока ключ не введён — замок
    license.deactivate()
    with pytest.raises(premium.PremiumLocked):
        premium.widget("compare", "MAIN")

    # Подписанный ключ подписки несёт ключи функций внутри себя
    now = int(time.time())
    keys = {f: seal.feature_key(SECRET, f).hex() for f in license.FEATURES}
    token = license.make_token(SECRET, {"v": 1, "kid": "x", "plan": "solo", "iat": now,
                                        "exp": now + 3600, "dev": [DEV], "keys": keys})
    license.enter_key(token)
    premium._cache.clear()
    assert premium.widget("compare", "MAIN") == ("widget", "MAIN", 42)

    # Подписку убрали — снова замок
    license.deactivate()
    premium._cache.clear()
    assert license.active_keys() == {}
    with pytest.raises(premium.PremiumLocked):
        premium.widget("compare", "MAIN")
