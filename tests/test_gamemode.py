import os
import subprocess
import sys
import time

import psutil
import pytest

from whyslow import gamemode, journal
from whyslow.knowledge import Verdict
from whyslow.processes import ProcGroup


@pytest.mark.parametrize("name,exe", [
    ("cs2.exe", r"C:\Program Files (x86)\Steam\steamapps\common\Counter-Strike Global Offensive\game\bin\win64\cs2.exe"),
    ("Witcher3.exe", r"D:\GOG Games\The Witcher 3\bin\x64\witcher3.exe"),
    ("SomeIndie.exe", r"E:\SteamLibrary\steamapps\common\Some Indie\SomeIndie.exe"),
    ("javaw.exe", r"C:\Users\me\AppData\Roaming\.minecraft\runtime\bin\javaw.exe"),
    ("Factorio", "/Users/me/Library/Application Support/Steam/steamapps/common/Factorio/factorio.app/Contents/MacOS/factorio"),
])
def test_games_are_detected(name, exe):
    assert gamemode.is_game(name, exe)


@pytest.mark.parametrize("name,exe", [
    ("steam.exe", r"C:\Program Files (x86)\Steam\steam.exe"),
    ("EasyAntiCheat_EOS.exe", r"C:\Program Files (x86)\EasyAntiCheat_EOS\EasyAntiCheat_EOS.exe"),
    ("UnityCrashHandler64.exe", r"E:\SteamLibrary\steamapps\common\Some Indie\UnityCrashHandler64.exe"),
    ("javaw.exe", r"C:\Program Files\Java\bin\javaw.exe"),
    ("chrome.exe", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
])
def test_not_games(name, exe):
    assert not gamemode.is_game(name, exe)


def test_user_list_and_ignore():
    procs = [(10, "mygame.exe", r"C:\Games\mygame.exe"), (11, "cs2.exe", "")]
    assert gamemode.detect(procs, extra=["mygame"]).name == "mygame.exe"
    assert gamemode.detect(procs, ignore=["cs2.exe"]) is None


def _v(verdict):
    return Verdict(verdict, "", "")


def test_plan_spares_keep_system_and_voice_chat(monkeypatch):
    game = gamemode.Game("cs2.exe", 100)
    groups = [
        ProcGroup("cs2.exe", [100], 60.0, 3 * 2**30),
        ProcGroup("OneDrive.exe", [1, 2], 8.0, 400 * 2**20, verdict=_v("safe")),
        ProcGroup("chrome.exe", [3], 5.0, 2 * 2**30, verdict=_v("optional")),
        ProcGroup("MsMpEng.exe", [4], 15.0, 300 * 2**20, verdict=_v("keep")),
        ProcGroup("Discord.exe", [5], 4.0, 600 * 2**20, verdict=_v("optional")),
        ProcGroup("svchost.exe", [6], 3.0, 400 * 2**20, system=True),
        ProcGroup("tiny.exe", [7], 0.1, 10 * 2**20, verdict=_v("safe")),
    ]
    monkeypatch.setattr(gamemode, "can_lower", lambda: True)
    acts = gamemode.plan(game, groups)
    names = {a.name for a in acts}
    assert "cs2.exe" in names  # приоритет самой игры
    assert {"OneDrive.exe", "chrome.exe"} <= names
    assert not names & {"MsMpEng.exe", "Discord.exe", "svchost.exe", "tiny.exe"}
    suspend = [a for a in acts if a.kind == "suspend"]
    assert [a.name for a in suspend] == ["OneDrive.exe"] and not suspend[0].checked  # пауза — только если отметить


def test_no_lowering_when_it_cannot_be_undone(monkeypatch):
    monkeypatch.setattr(gamemode, "can_lower", lambda: False)
    groups = [ProcGroup("OneDrive.exe", [1], 8.0, 400 * 2**20, verdict=_v("safe"))]
    acts = gamemode.plan(gamemode.Game("cs2.exe", 100), groups)
    assert [a.kind for a in acts if a.name == "OneDrive.exe"] == ["suspend"]


def _bg_action(pid):
    """Понижение приоритета там, где его можно откатить, иначе пауза."""
    kind = "lower" if gamemode.can_lower() else "suspend"
    return gamemode.Action(kind, kind, name="bg", pids=[pid])


@pytest.fixture
def child():
    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    time.sleep(0.2)
    if sys.platform == "win32":  # раннер GitHub сам запускает всё с пониженным приоритетом
        psutil.Process(p.pid).nice(psutil.NORMAL_PRIORITY_CLASS)
    yield p
    p.kill()
    p.wait()


def test_apply_and_restore_roundtrip(child, monkeypatch):
    monkeypatch.setattr(gamemode, "power_action", lambda: None)
    proc = psutil.Process(child.pid)
    before = proc.nice()
    game = gamemode.Game("fake.exe", os.getpid())
    acts = [_bg_action(child.pid), gamemode.Action("suspend", "pause", name="bg", pids=[child.pid])]
    s, errors = gamemode.apply(game, acts, gamemode.start(game))
    assert errors == []
    assert proc.nice() != before or not gamemode.can_lower()
    assert proc.status() == psutil.STATUS_STOPPED or sys.platform == "win32"
    assert gamemode._state_path().exists()
    assert any(e["kind"] == "gamemode" for e in journal.load())
    assert gamemode.restore(s) == []
    assert proc.nice() == before
    assert proc.status() != psutil.STATUS_STOPPED
    assert not gamemode._state_path().exists()


def test_recover_after_crash(child):
    proc = psutil.Process(child.pid)
    before = proc.nice()
    game = gamemode.Game("fake.exe", os.getpid())
    gamemode.apply(game, [_bg_action(child.pid)], gamemode.start(game))
    s = gamemode.recover()  # как будто WhySlow упал и запустился снова
    assert s and s.game["name"] == "fake.exe"
    assert proc.nice() == before
    assert proc.status() != psutil.STATUS_STOPPED
    assert gamemode.recover() is None


def test_restore_skips_reused_pid(child):
    s = gamemode.Session({"name": "x", "pid": 1}, time.time())
    s.undo = [{"kind": "nice", "pid": child.pid, "created": 1.0, "old": 0}]  # другое время запуска
    assert gamemode.restore(s) == []


def test_game_alive():
    s = gamemode.start(gamemode.Game("me", os.getpid()))
    assert gamemode.game_alive(s)
    s.game["created"] = 1.0
    assert not gamemode.game_alive(s)


def test_report_compares_before_and_after():
    s = gamemode.Session({"name": "cs2.exe", "pid": 1}, time.time())
    g = [ProcGroup("cs2.exe", [1], 40.0, 0)]
    for _ in range(3):
        s.feed(90.0, g, 2 * 2**30)
    s.applied_at = time.time()
    for _ in range(3):
        s.feed(60.0, [ProcGroup("cs2.exe", [1], 50.0, 0)], 3 * 2**30)
    s.fps_before, s.fps_after = {"avg": 60.0, "low": 30.0}, {"avg": 72.0, "low": 45.0}
    text = "\n".join(gamemode.report(s))
    assert "50% процессора до → 10%" in text
    assert "60 → 72 (+20%)" in text


def test_parse_presentmon_csv():
    v1 = "Application,ProcessID,MsBetweenPresents\n" + "".join(f"g.exe,1,{16.6 if i % 50 else 50}\n" for i in range(200))
    r = gamemode.parse_frames(v1)
    assert 50 < r["avg"] < 61 and r["low"] == 20.0
    v2 = "Application,FrameTime\n" + "g.exe,10\n" * 20
    assert gamemode.parse_frames(v2)["avg"] == 100.0
    assert gamemode.parse_frames("a,b\n1,2\n") == {}
