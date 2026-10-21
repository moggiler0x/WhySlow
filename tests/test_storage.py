from whyslow import journal, settings
from whyslow.util import app_dir, read_json, write_json


def test_write_and_read_json(tmp_path):
    p = tmp_path / "a.json"
    write_json(p, {"x": "привет"})
    assert read_json(p, None) == {"x": "привет"}
    assert not (tmp_path / "a.json.tmp").exists()


def test_broken_file_is_kept_aside(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("{oops", encoding="utf-8")
    assert read_json(p, []) == []
    assert (tmp_path / "a.json.broken").read_text(encoding="utf-8") == "{oops"


def test_journal_roundtrip():
    e = journal.add("test", "Проверка", {"a": 1})
    assert journal.load()[-1]["id"] == e["id"]
    journal.mark_undone(e["id"])
    assert journal.load()[-1]["undone"] is True


def test_broken_journal_is_not_overwritten():
    (app_dir() / "journal.json").write_text("[{", encoding="utf-8")
    journal.add("test", "после сбоя", {})
    assert (app_dir() / "journal.json.broken").exists()
    assert len(journal.load()) == 1


def test_settings_ignore_unknown_keys():
    settings.set("auto_update", True)
    write_json(app_dir() / "settings.json", dict(read_json(app_dir() / "settings.json", {}), junk=1))
    data = settings.load()
    assert data["auto_update"] is True and "junk" not in data
