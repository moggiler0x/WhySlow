import time

import pytest

from whyslow import ed25519, license

# Тестовая пара ключей (RFC 8032, пример 1) — не настоящая
SECRET = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
PUBLIC = "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a"
DEV = "AAAAA-BBBBB"


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    monkeypatch.setattr(license, "PUBLIC_KEY", PUBLIC)
    monkeypatch.setattr(license, "_device", DEV)
    monkeypatch.setattr(license, "_listeners", [])
    monkeypatch.setattr(license, "_last_active", None)
    monkeypatch.delenv("WHYSLOW_LICENSE_SERVER", raising=False)
    monkeypatch.setattr(license, "SERVER_URL", "")  # тесты не ходят на настоящий сервер


def token(**kw):
    now = int(time.time())
    payload = {"v": 1, "kid": "off-1", "plan": "solo", "iat": now, "exp": now + 3600, "dev": [DEV], **kw}
    return license.make_token(SECRET, payload)


def test_ed25519_rfc8032_vector():
    assert ed25519.public_key(SECRET).hex() == PUBLIC
    sig = ed25519.sign(SECRET, b"")
    assert sig.hex().startswith("e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e0652249015")
    assert ed25519.verify(bytes.fromhex(PUBLIC), b"", sig)
    assert not ed25519.verify(bytes.fromhex(PUBLIC), b"x", sig)


def test_no_key_no_premium():
    assert not license.has_premium("compare")
    assert license.auth_headers() == {}


def test_offline_key_unlocks_and_notifies():
    calls = []
    license.on_change(lambda: calls.append(1))
    st = license.enter_key(token())
    assert st.active and st.plan == "solo"
    assert license.has_premium("compare") and license.has_premium()
    assert calls == [1]
    assert license.auth_headers()["Authorization"].startswith("Bearer WS1.")
    license.deactivate()
    assert not license.has_premium()
    assert calls == [1, 1]


@pytest.mark.parametrize("kw,err", [
    ({"exp": 1}, "закончился"),
    ({"dev": ["CCCCC-DDDDD"]}, "другого компьютера"),
    ({"plan": "gold"}, "тариф"),
])
def test_rejected_keys(kw, err):
    with pytest.raises(license.LicenseError, match=err):
        license.enter_key(token(**kw))
    assert not license.has_premium()


def test_forged_key():
    t = token()
    head, body, sig = t.split(".")
    other = license.make_token(bytes(32), {"plan": "family", "exp": time.time() + 9999, "dev": []})
    with pytest.raises(license.LicenseError, match="подделан"):
        license.enter_key(other)
    with pytest.raises(license.LicenseError, match="подделан"):
        license.enter_key(f"{head}.{other.split('.')[1]}.{sig}")


def test_not_configured(monkeypatch):
    monkeypatch.setattr(license, "PUBLIC_KEY", "")
    with pytest.raises(license.LicenseError, match="не настроена"):
        license.enter_key(token())


def test_expiry_noticed_by_tick():
    calls = []
    license.enter_key(token(exp=int(time.time()) + 2))
    license.on_change(lambda: calls.append(1))
    license.tick()
    assert calls == []
    d = license._load()
    st = license.status(now=time.time() + 10)
    assert not st.active and "закончился" in st.error
    assert d == license._load()


def test_clock_rollback():
    license.enter_key(token(exp=int(time.time()) + 30 * 86400))
    d = license._load()
    d["seen"] = time.time() + 10 * 86400
    license._save(d)
    st = license.status()
    assert not st.active and "часы" in st.error


def test_code_format():
    assert license.normalize_code("ws-abcd-efgh-jklm-npqr") == "ABCDEFGHJKLMNPQR"
    assert license.normalize_code("WSABCDEFGHJKLMNPQR") == "ABCDEFGHJKLMNPQR"
    assert license.normalize_code("WS-0000-0000-0000-0000") is None


def test_code_without_server():
    with pytest.raises(license.LicenseError, match=DEV):
        license.enter_key("WS-ABCD-EFGH-JKLM-NPQR")


def test_online_activation_and_refresh(monkeypatch):
    monkeypatch.setenv("WHYSLOW_LICENSE_SERVER", "https://lic.example")
    sent = []
    replies = {"activate": (200, {"token": token(src="online", sub=int(time.time()) + 31 * 86400)})}

    def post(url, body):
        sent.append((url, body))
        return replies[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(license, "_post", post)
    st = license.enter_key("WS-ABCD-EFGH-JKLM-NPQR")
    assert st.active and st.online
    assert sent[0][0] == "https://lic.example/v1/activate"
    assert sent[0][1]["code"] == "ABCDEFGHJKLMNPQR" and sent[0][1]["device"] == DEV
    assert st.expires > time.time() + 30 * 86400  # показываем срок подписки, а не недельной лицензии

    replies["refresh"] = (403, {"error": "revoked"})
    assert not license.refresh()          # рано: продлеваем раз в сутки
    assert license.refresh(force=True)
    st = license.status()
    assert not st.active and "отозван" in st.error


def test_server_cannot_hand_out_foreign_tokens(monkeypatch):
    monkeypatch.setenv("WHYSLOW_LICENSE_SERVER", "https://evil.example")
    fake = license.make_token(bytes(32), {"plan": "family", "exp": time.time() + 9999, "dev": []})
    monkeypatch.setattr(license, "_post", lambda url, body: (200, {"token": fake}))
    with pytest.raises(license.LicenseError):
        license.enter_key("WS-ABCD-EFGH-JKLM-NPQR")
    assert not license.has_premium()


def test_limit_message(monkeypatch):
    monkeypatch.setenv("WHYSLOW_LICENSE_SERVER", "https://lic.example")
    monkeypatch.setattr(license, "_post", lambda url, body: (409, {"error": "limit"}))
    with pytest.raises(license.LicenseError, match="максимальном числе устройств"):
        license.enter_key("WS-ABCD-EFGH-JKLM-NPQR")


def test_device_id_format(monkeypatch):
    monkeypatch.setattr(license, "_device", None)
    d = license.device_id()
    assert len(d) == 11 and d[5] == "-" and d == license.device_id()


def test_owner_keys_issue(tmp_path, monkeypatch, capsys):
    import importlib.util
    from pathlib import Path
    spec = importlib.util.spec_from_file_location("owner_keys", Path(__file__).parents[1] / "owner" / "keys.py")
    keys = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(keys)
    monkeypatch.setattr(keys, "KEY_FILE", tmp_path / "private.key")
    monkeypatch.setattr(keys, "SEAL_FILE", tmp_path / "seal.key")
    monkeypatch.setattr(keys, "LOG_FILE", tmp_path / "issued.jsonl")
    monkeypatch.setattr(keys, "HOME", tmp_path)
    (tmp_path / "private.key").write_text(SECRET.hex())
    (tmp_path / "seal.key").write_text(("11" * 32))  # отдельный секрет шифрования
    monkeypatch.setattr("sys.argv", ["keys.py", "issue", "family", "1", "aaaaabbbbb", "CCCCC-DDDDD"])
    keys.main()
    tok = capsys.readouterr().out.strip().splitlines()[-1]
    st = license.enter_key(tok)
    assert st.active and st.plan == "family"
    with pytest.raises(SystemExit):
        monkeypatch.setattr("sys.argv", ["keys.py", "issue", "solo", "1", "AAAAA-BBBBB", "CCCCC-DDDDD"])
        keys.main()


def test_owner_init_makes_separate_secrets(tmp_path, monkeypatch):
    import importlib.util
    from pathlib import Path
    from whyslow import seal
    spec = importlib.util.spec_from_file_location("owner_keys2", Path(__file__).parents[1] / "owner" / "keys.py")
    keys = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(keys)
    monkeypatch.setattr(keys, "KEY_FILE", tmp_path / "private.key")
    monkeypatch.setattr(keys, "SEAL_FILE", tmp_path / "seal.key")
    monkeypatch.setattr(keys, "HOME", tmp_path)
    monkeypatch.setattr(keys, "ROOT", tmp_path)
    (tmp_path / "whyslow").mkdir()
    (tmp_path / "whyslow" / "license.py").write_text('PUBLIC_KEY = ""\n')
    monkeypatch.delenv("WHYSLOW_SEAL_SEED", raising=False)
    keys.cmd_init(None)
    signing = bytes.fromhex((tmp_path / "private.key").read_text().strip())
    sealing = keys.seal_secret()
    assert signing != sealing and len(sealing) == 32   # секрет шифрования отдельный
    # feature_keys берутся из секрета шифрования, а не из ключа подписи
    fk = keys.feature_keys()
    assert fk["compare"] == seal.feature_key(sealing, "compare").hex()
    assert fk["compare"] != seal.feature_key(signing, "compare").hex()


def _trial_server(monkeypatch, replies):
    monkeypatch.setenv("WHYSLOW_LICENSE_SERVER", "https://lic.example")
    monkeypatch.setattr(license, "_hardware_raw", lambda: {"board": "MB123456", "disk": "WD-XYZ9876"})
    sent = []

    def post(url, body):
        sent.append((url.rsplit("/", 1)[1], body))
        return replies[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(license, "_post", post)
    return sent


def test_trial_starts_once_and_sends_only_hashes(monkeypatch):
    sub = int(time.time()) + 7 * 86400
    t = token(src="online", trial=1, sub=sub, exp=sub)
    sent = _trial_server(monkeypatch, {"trial": (200, {"code": "WS-ABCD-EFGH-JKLM-NPQR", "token": t}),
                                       "deactivate": (200, {"ok": True})})
    assert license.trial_available()
    st = license.start_trial()
    assert st.active and st.trial and st.online
    assert "Пробный период" in license.status_text(st) and st.plan_name == "Пробный период"
    action, body = sent[0]
    assert action == "trial" and body["device"] == DEV
    # Серийники не уходят с компьютера — только SHA-256 по каждой детали
    assert len(body["hw"]) == 3 and all(len(h) == 64 for h in body["hw"])
    assert "MB123456" not in str(body) and "WD-XYZ9876" not in str(body)
    assert not license.trial_available()
    # Отметка остаётся и после отключения
    license.deactivate()
    assert not license.trial_available()


def test_trial_refused_by_server(monkeypatch):
    _trial_server(monkeypatch, {"trial": (409, {"error": "trial_used"})})
    with pytest.raises(license.LicenseError, match="пробный период уже был"):
        license.start_trial()
    assert not license.has_premium() and not license.trial_available()


def test_trial_over_message(monkeypatch):
    sub = int(time.time()) + 7 * 86400
    t = token(src="online", trial=1, sub=sub, exp=sub)
    _trial_server(monkeypatch, {"trial": (200, {"code": "WS-ABCD-EFGH-JKLM-NPQR", "token": t}),
                                "refresh": (403, {"error": "trial_over"})})
    license.start_trial()
    st = license.status(now=sub + 3600)
    assert not st.active and "пробный период закончился" in st.error
    assert license.refresh(force=True)
    assert "пробный период закончился" in license.status().error


def test_junk_serials_ignored():
    for junk in ("To be filled by O.E.M.", "Default string", "0", "00000000-0000-0000-0000-000000000000",
                 "System Serial Number", "None", "03000200-0400-0500-0006-000700080009"):
        assert license._real_serial(junk) == "", junk
    assert license._real_serial(" C02XK1JHJGH5 ") == "C02XK1JHJGH5"
