from collections import namedtuple

from whyslow import checks, diskhealth
from whyslow.diskhealth import PhysicalDisk

Io = namedtuple("Io", "read_time write_time")


def test_busy_percent():
    a = (0.0, Io(0, 0), {1: ("a", 0), 2: ("b", 0)})
    b = (2.0, Io(1000, 600), {1: ("a", 4000), 2: ("b", 1000)})
    assert diskhealth.busy_percent(a, b) == 80
    assert diskhealth.top_io(a, b) == [("a", 2000), ("b", 500)]


def test_busy_is_capped():
    a = (0.0, Io(0, 0), {})
    b = (1.0, Io(5000, 5000), {})
    assert diskhealth.busy_percent(a, b) == 100


def test_unknown_counters():
    assert diskhealth.busy_percent((0.0, None, {}), (1.0, None, {})) is None


def test_findings_for_failing_and_hdd():
    f = []
    checks._disk_health(f, [PhysicalDisk("WD Blue", "hdd", "bad", True), PhysicalDisk("Samsung", "ssd", "ok")])
    levels = sorted(x.level for x in f)
    assert levels == [checks.BAD, checks.WARN]


def test_healthy_ssd_is_quiet():
    f = []
    checks._disk_health(f, [PhysicalDisk("Apple SSD", "ssd", "ok", True)])
    checks._disk_busy(f, 40, [])
    assert f == []


def test_busy_disk_names_culprits():
    f = []
    checks._disk_busy(f, 97, [("MsMpEng.exe", 50 * 2**20)])
    assert f and "MsMpEng.exe" in f[0].detail
