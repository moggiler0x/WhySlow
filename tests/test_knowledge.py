import json
import re

import pytest

from whyslow import knowledge

RAW = json.loads(knowledge.BUNDLED.read_text(encoding="utf-8"))


def test_file_is_valid():
    assert knowledge._valid(RAW)
    assert isinstance(RAW["version"], int)


def test_every_entry_is_complete():
    ids = set()
    for e in RAW["entries"]:
        assert e["id"] not in ids, f"повтор id {e['id']}"
        ids.add(e["id"])
        assert e["verdict"] in knowledge.VERDICTS, e["id"]
        for key in ("title", "title_en", "note", "note_en"):
            assert e.get(key, "").strip(), f"{e['id']}: пусто {key}"
        assert e["match"], e["id"]
        for p in e["match"]:
            re.compile(p, re.IGNORECASE)


def test_nothing_is_dropped_on_load():
    """Запись с битым шаблоном молча пропала бы из базы — ловим это здесь."""
    assert len(knowledge.load().entries) == len(RAW["entries"])


@pytest.mark.parametrize("name, exe, verdict, entry", [
    ("svchost.exe", r"C:\Windows\System32\svchost.exe", "keep", "win_core"),
    ("System", "", "keep", "win_core"),
    ("explorer.exe", r"C:\Windows\explorer.exe", "keep", "win_shell"),
    ("MsMpEng.exe", r"C:\ProgramData\Microsoft\Windows Defender\MsMpEng.exe", "keep", ""),
    ("TiWorker.exe", r"C:\Windows\WinSxS\x\TiWorker.exe", "keep", "win_tiworker"),
    ("kernel_task", "", "keep", "mac_kernel_task"),
    ("WindowServer", "/System/Library/PrivateFrameworks/SkyLight.framework/Resources/WindowServer", "keep",
     "mac_windowserver"),
    ("mds_stores", "/System/Library/Frameworks/CoreServices.framework/mds_stores", "keep", "mac_spotlight"),
    ("photoanalysisd", "/System/Library/PrivateFrameworks/PhotoAnalysis.framework/photoanalysisd", "keep",
     "mac_photos"),
    ("com.apple.WebKit.WebContent", "/System/Library/Frameworks/WebKit.framework/x/com.apple.WebKit.WebContent",
     "optional", "mac_webkit"),
    ("rapportd", "/usr/libexec/rapportd", "keep", "mac_system_path"),
    ("Telegram", "/Applications/Telegram.app/Contents/MacOS/Telegram", "safe", "telegram"),
    ("System Settings", "/System/Applications/System Settings.app/Contents/MacOS/System Settings", "unknown", ""),
    ("xmrig.exe", r"C:\Users\a\xmrig.exe", "bad", "miner"),
])
def test_lookup(name, exe, verdict, entry):
    v = knowledge.load().lookup(name, exe)
    assert v.verdict == verdict
    if entry:
        assert v.entry_id == entry


def test_cache_newer_version_wins(data_dir):
    newer = dict(RAW, version=RAW["version"] + 1, entries=RAW["entries"][:1])
    knowledge._cache_path().write_text(json.dumps(newer), encoding="utf-8")
    assert knowledge.load().version == newer["version"]
