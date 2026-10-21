import os
import time

from whyslow import junk


def _file(path, size, age_hours):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    t = time.time() - age_hours * 3600
    os.utime(path, (t, t))


def test_only_old_files_are_removed(tmp_path):
    _file(tmp_path / "old.tmp", 100, 48)
    _file(tmp_path / "sub" / "old2.tmp", 50, 48)
    _file(tmp_path / "new.tmp", 10, 1)
    cat = junk.JunkCategory("t", "t", "t", [tmp_path])
    junk.scan(cat)
    assert (cat.size, cat.files) == (150, 2)
    freed, failed = junk.remove_old([tmp_path], 24)
    assert (freed, failed) == (150, 0)
    assert (tmp_path / "new.tmp").exists() and not (tmp_path / "old.tmp").exists()


def test_excluded_folder_is_untouched(tmp_path):
    _file(tmp_path / "keep" / "a", 10, 48)
    _file(tmp_path / "b", 10, 48)
    junk.remove_old([tmp_path], 24, [tmp_path / "keep"])
    assert (tmp_path / "keep" / "a").exists() and not (tmp_path / "b").exists()


def test_categories_have_titles():
    for c in junk.categories():
        assert c.key and c.title and c.note


def test_all_browser_profiles(tmp_path):
    data = tmp_path / r"Google\Chrome" / "User Data"
    (data / "Profile 2").mkdir(parents=True)
    (tmp_path / r"Mozilla\Firefox\Profiles" / "abc.default").mkdir(parents=True)
    paths = junk._win_browser_caches(tmp_path)
    assert data / "Default" / "Cache" in paths
    assert data / "Profile 2" / "Cache" in paths
    assert tmp_path / r"Mozilla\Firefox\Profiles" / "abc.default" / "cache2" in paths
