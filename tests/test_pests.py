import json
import time

import pytest

from whyslow import knowledge, pests


@pytest.fixture
def kb():
    return knowledge.load()


@pytest.mark.parametrize("cmd, hit", [
    ("xmrig.exe -o stratum+tcp://pool.minexmr.com:4444 -u 4A...", True),
    ("svc.exe --donate-level 1 --algo rx/0", True),
    ("t.exe -a kawpow -o rvn.2miners.com:6060 -u RXyz.worker", True),
    ("chrome.exe --type=renderer --lang=ru", False),
    ("python -m http.server 3333", False),
])
def test_miner_args(cmd, hit):
    assert bool(pests.MINER_ARGS.search(cmd)) is hit


def test_public_addresses():
    assert pests.is_public("8.8.8.8")
    assert not pests.is_public("192.168.1.10")
    assert not pests.is_public("127.0.0.1")
    assert not pests.is_public("fe80::1%eth0")
    assert not pests.is_public("::ffff:10.0.0.1")
    assert not pests.is_public("garbage")


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


def test_chromium_extensions(tmp_path, monkeypatch, kb):
    root = tmp_path / "Chrome"
    prof = root / "Default"
    ext_id, good_id, comp_id = "a" * 32, "b" * 32, "c" * 32
    d = prof / "Extensions" / ext_id / "1.0_0"
    _write(d / "manifest.json", {"name": "__MSG_appName__", "default_locale": "en", "version": "1.0",
                                 "permissions": ["webRequest", "cookies", "storage"],
                                 "content_scripts": [{"matches": ["<all_urls>"], "js": ["x.js"]}]})
    _write(d / "_locales" / "en" / "messages.json", {"appname": {"message": "Shady Coupons"}})
    _write(prof / "Extensions" / good_id / "2.0" / "manifest.json", {"name": "Dark Mode", "version": "2.0",
                                                                     "permissions": ["storage"]})
    _write(prof / "Extensions" / comp_id / "1" / "manifest.json", {"name": "Built-in"})
    _write(prof / "Preferences", {"profile": {"name": "Me"}, "extensions": {"settings": {
        ext_id: {"location": 3, "from_webstore": False},
        good_id: {"location": 1, "from_webstore": True, "disable_reasons": [1]},
        comp_id: {"location": 5}}}})
    monkeypatch.setattr(pests, "_chromium_roots", lambda: [("Chrome", root, "chrome")])
    monkeypatch.setattr(pests, "_chromium_policy_ids", lambda: {"d" * 32: [{"file": "/etc/x.json"}]})
    exts = {e.id: e for e in pests._chromium_extensions()}
    assert set(exts) == {ext_id, good_id, "d" * 32}  # встроенное в браузер не показываем
    shady = exts[ext_id]
    assert shady.name == "Shady Coupons" and shady.profile == "Me"
    assert shady.all_sites and shady.sideloaded and shady.from_store is False
    assert shady.permissions == ["webRequest", "cookies"]
    assert shady.manage_url == f"chrome://extensions/?id={ext_id}"
    assert not exts[good_id].enabled
    assert exts["d" * 32].forced
    pests.rate(shady, kb)
    pests.rate(exts[good_id], kb)
    assert shady.level == pests.WARN
    assert exts[good_id].level == pests.OK


def test_firefox_extensions(tmp_path, monkeypatch, kb):
    prof = tmp_path / "Profiles" / "abc.default-release"
    _write(prof / "extensions.json", {"addons": [
        {"id": "ublock@x", "type": "extension", "location": "app-profile", "active": True, "version": "1",
         "defaultLocale": {"name": "uBlock Origin"}, "sourceURI": "https://addons.mozilla.org/x.xpi",
         "userPermissions": {"permissions": ["webRequest", "webRequestBlocking"], "origins": ["<all_urls>"]},
         "signedState": 2},
        {"id": "evil@x", "type": "extension", "location": "app-profile", "foreignInstall": True,
         "defaultLocale": {"name": "Hola Free VPN"}, "signedState": 0, "userPermissions": {}},
        {"id": "sys@mozilla", "type": "extension", "location": "app-system-defaults"},
        {"id": "dict", "type": "dictionary", "location": "app-profile"}]})
    monkeypatch.setattr(pests, "_firefox_roots", lambda: [tmp_path / "Profiles"])
    exts = {e.id: e for e in pests._firefox_extensions()}
    assert set(exts) == {"ublock@x", "evil@x"}
    assert exts["ublock@x"].from_store and exts["ublock@x"].all_sites
    for e in exts.values():
        pests.rate(e, kb)
    assert exts["evil@x"].level == pests.BAD  # Hola есть в базе как нежелательное
    assert exts["ublock@x"].level in (pests.INFO, pests.WARN)


def test_safari_parse(monkeypatch):
    out = ("+    com.example.Ext.Extension(1.4)\n"
           "\t            Path = /Applications/Ext.app/Contents/PlugIns/Ext Extension.appex\n"
           "\t    Display Name = Ext Helper\n"
           "-    com.other.Blocker(2.0)\n")
    monkeypatch.setattr(pests, "IS_MAC", True)
    monkeypatch.setattr(pests, "run", lambda cmd, timeout=0: (0, out) if cmd[-1].endswith("web-extension") else (1, ""))
    exts = pests._safari_extensions()
    assert [(e.name, e.enabled) for e in exts] == [("Ext Helper", True), ("com.other.Blocker", False)]
    assert exts[0].path.endswith(".appex")


def _at(hour):
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hour, 30, 0, 0, 0, -1))


def test_night_log(kb):
    miner = pests.Conn(1, "helper.exe", "C:/x/helper.exe", "45.1.2.3", 3333)
    unknown = pests.Conn(2, "zzqx_agent", "/opt/zzqx", "93.184.216.34", 443)
    sync = pests.Conn(3, "OneDrive.exe", "", "13.107.42.12", 443)
    pests.record(now=_at(14), idle=0, conns=[unknown])      # днём не считается
    pests.record(now=_at(3), idle=5, conns=[unknown])       # ночью, но человек за компьютером
    log = pests.record(now=_at(3), idle=3600, conns=[miner, unknown, sync])
    assert log["procs"]["zzqx_agent"]["samples"] == 3
    assert log["procs"]["zzqx_agent"]["night"] == 1
    assert log["night_samples"] == 1
    rep = {a.name: a for a in pests.night_report(kb, log)}
    assert rep["helper.exe"].level == pests.BAD
    assert rep["zzqx_agent"].level == pests.WARN
    assert rep["OneDrive.exe"].level == pests.INFO
    pests.clear_log()
    assert pests.load_log() == {"procs": {}}


def test_quarantine_and_undo(tmp_path):
    from whyslow import journal
    f = tmp_path / "miner.exe"
    f.write_bytes(b"x")
    dst = pests.quarantine(str(f))
    assert not f.exists() and dst.exists()
    entry = journal.load()[-1]
    assert entry["kind"] == "pest_quarantine" and entry["undoable"]
    pests.undo(entry)
    assert f.exists() and not dst.exists()


def test_locked_without_license(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "whyslow.license", None)  # модуля нет → закрыто
    assert pests.unlocked() is False
