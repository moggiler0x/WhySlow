from whyslow import startup
from whyslow.startup import StartupItem


def _it(name, command, enabled=True, can_toggle=True):
    return StartupItem(name, command, "test", enabled, "win_run", can_toggle=can_toggle)


ITEMS = [
    _it("Discord", r'"C:\Users\a\AppData\Local\Discord\Update.exe" --processStart Discord.exe'),
    _it("Steam", r'"C:\Program Files (x86)\Steam\steam.exe" -silent'),
    _it("Telegram", "/Applications/Telegram.app"),
    _it("Off", r"C:\x\off.exe", enabled=False),
]


def test_match_by_exe_path():
    found = startup.find_for_process(ITEMS, "steam.exe", r"C:\Program Files (x86)\Steam\steam.exe")
    assert [i.name for i in found] == ["Steam"]


def test_match_by_name_in_command():
    found = startup.find_for_process(ITEMS, "Discord.exe", r"C:\Users\a\AppData\Local\Discord\app-1.0\Discord.exe")
    assert [i.name for i in found] == ["Discord"]


def test_match_mac_app():
    found = startup.find_for_process(ITEMS, "Telegram", "/Applications/Telegram.app/Contents/MacOS/Telegram")
    assert [i.name for i in found] == ["Telegram"]


def test_disabled_and_unrelated_are_skipped():
    assert startup.find_for_process(ITEMS, "off.exe", r"C:\x\off.exe") == []
    assert startup.find_for_process(ITEMS, "chrome.exe", r"C:\Chrome\chrome.exe") == []
    assert startup.find_for_process(ITEMS, "a", "") == []
