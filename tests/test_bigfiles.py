from whyslow import bigfiles


def _make(path, size):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.truncate(size)  # разреженный файл: размер есть, места почти не занимает


def test_finds_largest_and_skips_app_data(tmp_path):
    _make(tmp_path / "Downloads" / "big.iso", 3000)
    _make(tmp_path / "Videos" / "mid.mp4", 2000)
    _make(tmp_path / "small.txt", 10)
    _make(tmp_path / "AppData" / "Local" / "huge.bin", 9000)
    _make(tmp_path / ".cache" / "huge2.bin", 9000)
    found = bigfiles.find(tmp_path, min_size=1000)
    assert [f.name for f in found] == ["big.iso", "mid.mp4"]
    assert found[0].size == 3000


def test_limit(tmp_path):
    for i in range(10):
        _make(tmp_path / f"f{i}", 1000 + i)
    found = bigfiles.find(tmp_path, min_size=1000, limit=3)
    assert [f.size for f in found] == [1009, 1008, 1007]
