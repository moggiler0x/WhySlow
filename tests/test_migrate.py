import json
from pathlib import Path

import pytest

from whyslow import journal, reinstall
from whyslow.premium import migrate


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("HOME", str(h))
    monkeypatch.setattr(migrate, "premium", lambda: True)
    return h


def _write(path, text="x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def test_locked_without_subscription(tmp_path, monkeypatch):
    monkeypatch.setattr(migrate, "premium", lambda: False)
    with pytest.raises(migrate.MigrateError):
        migrate.export(tmp_path, [], [], [])


def test_no_license_module_means_locked(monkeypatch):
    import builtins
    real = builtins.__import__

    def fake(name, globals=None, locals=None, fromlist=(), level=0):
        if level == 2 and fromlist and "license" in fromlist:
            raise ImportError
        return real(name, globals, locals, fromlist, level)
    monkeypatch.setattr(builtins, "__import__", fake)
    assert migrate.premium() is False


def test_gui_does_not_import_the_paid_module():
    """Код вкладки должен приходить только через замок (в релизе он зашифрован)."""
    src = (Path(migrate.__file__).parents[1] / "gui.py").read_text(encoding="utf-8")
    assert "migrate" not in src.split("class MainWindow")[0].split("from .i18n")[0]


def test_junk_rules():
    assert migrate.is_junk("node_modules", True)
    assert migrate.is_junk("Thumbs.db", False)
    assert migrate.is_junk("~$report.docx", False)
    assert migrate.is_junk("film.mkv.part", False)
    assert not migrate.is_junk("report.docx", False)
    assert not migrate.is_junk("Projects", True)
    skip = migrate.make_skip(True)
    dl = reinstall.Folder("Downloads", migrate.Path("/x/Downloads"))
    docs = reinstall.Folder("Documents", migrate.Path("/x/Documents"))
    assert skip(dl, "setup.exe", False)
    assert not skip(docs, "tool.exe", False)
    assert not migrate.make_skip(False)(dl, "setup.exe", False)


def test_match_ids():
    progs = [migrate.Program("Mozilla Firefox (x64 ru)", migrate.MANUAL), migrate.Program("7-Zip 23.01 (x64)", migrate.MANUAL),
             migrate.Program("Some Tool", migrate.MANUAL)]
    out = migrate.match_ids(progs, ["Mozilla.Firefox", "7zip.7zip", "Valve.Steam"], migrate.WINGET)
    by = {p.name: p for p in out}
    assert by["Mozilla Firefox (x64 ru)"].pkg == "Mozilla.Firefox"
    assert by["7-Zip 23.01 (x64)"].pkg == "7zip.7zip"
    assert by["Some Tool"].source == migrate.MANUAL
    assert by["Valve.Steam"].source == migrate.WINGET  # пакет без пары — отдельной строкой

    casks = migrate.match_ids([migrate.Program("Google Chrome", migrate.MANUAL)], ["google-chrome", "rectangle"], migrate.CASK)
    assert casks[0].source == migrate.CASK and casks[0].pkg == "google-chrome"
    assert casks[1].name == "rectangle"


def test_export_and_import_roundtrip(home, tmp_path):
    docs = home / "Documents"
    _write(docs / "a.txt", "hello")
    _write(docs / "proj" / "node_modules" / "big.js")
    _write(docs / "Thumbs.db")
    dl = home / "Downloads"
    _write(dl / "setup.exe")
    _write(dl / "photo.jpg")
    cfg = home / "app" / "settings.json"
    _write(cfg, "{\"old\": 1}")
    usb = tmp_path / "usb"
    usb.mkdir()

    folders = [migrate.measure(reinstall.Folder("Документы", docs)), migrate.measure(reinstall.Folder("Загрузки", dl))]
    assert folders[0].files == 1 and folders[1].files == 1  # мусор и установщик не считаются
    sets = [migrate.AppSettings("vscode", "VS Code", "", [cfg, None, home / "nope.json"], size=10)]
    progs = [migrate.Program("Firefox", migrate.WINGET, "Mozilla.Firefox"), migrate.Program("Old Thing", migrate.MANUAL)]
    pkg = migrate.export(usb, progs, sets, folders)

    root = pkg.path
    assert root.name.startswith("WhySlow-Move-")
    assert pkg.files == 2 and pkg.settings == 1
    assert (root / "files" / "Documents" / "a.txt").read_text() == "hello"
    assert not (root / "files" / "Documents" / "proj" / "node_modules").exists()
    assert not (root / "files" / "Downloads" / "setup.exe").exists()
    assert (root / "files" / reinstall.MANIFEST).exists()  # файлы можно вернуть и без WhySlow
    assert "Old Thing" in (root / "programs.txt").read_text(encoding="utf-8")

    data = migrate.read_package(root)
    assert [p.pkg for p in migrate.package_programs(data)] == ["Mozilla.Firefox", ""]

    # «Новый ПК»: файлов нет, настройки VS Code уже есть свои
    for p in (docs / "a.txt", dl / "photo.jpg"):
        p.unlink()
    _write(home / ".config" / "Code" / "User" / "settings.json", "{\"new\": 1}")
    targets = migrate.settings_targets(data)
    if reinstall.os_name() == "linux":
        assert targets[0]["targets"][0][1] == home / ".config" / "Code" / "User" / "settings.json"
        done, failed = migrate.restore_settings(root, data, {"vscode"})
        assert (done, failed) == (1, [])
        assert json.loads(targets[0]["targets"][0][1].read_text()) == {"old": 1}
        entry = next(e for e in journal.load() if e["kind"] == "move_settings")
        reinstall.undo_moves(entry)
        assert json.loads(targets[0]["targets"][0][1].read_text()) == {"new": 1}

    copied, skipped, errors = migrate.restore_files(root)
    assert (copied, errors) == (2, 0)
    assert (docs / "a.txt").read_text() == "hello"


def test_dest_inside_moved_folder(home):
    docs = home / "Documents"
    docs.mkdir()
    f = reinstall.Folder("Документы", docs, size=0)
    assert migrate.check_dest(docs, [f])
    assert migrate.check_dest(home / "missing", [f])


def test_bad_package(tmp_path, home):
    with pytest.raises(migrate.MigrateError):
        migrate.read_package(tmp_path)
    (tmp_path / migrate.MANIFEST).write_text(json.dumps({"format": 99, "programs": []}))
    with pytest.raises(migrate.MigrateError):
        migrate.read_package(tmp_path)


def test_can_install_needs_tool_and_os(monkeypatch):
    monkeypatch.setattr(migrate.shutil, "which", lambda name: "/bin/" + name)
    winget = migrate.Program("Firefox", migrate.WINGET, "Mozilla.Firefox")
    assert migrate.can_install(winget) == (reinstall.os_name() == "windows")
    assert not migrate.can_install(migrate.Program("X", migrate.MANUAL))
    monkeypatch.setattr(migrate.shutil, "which", lambda name: None)
    assert not migrate.can_install(migrate.Program("ext", migrate.VSCODE, "ms-python.python"))
