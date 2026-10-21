from whyslow import xray
from whyslow.processes import ProcGroup


def _stats(cpu=10.0, ram=40.0, disk=0.0, dt=1.0, temps=None, per=None):
    return {"cpu": cpu, "per": per or [cpu], "ram": (0, 0, ram), "swap": (0, 0, 0), "disk_r": disk, "disk_w": 0.0,
            "net_rx": 0.0, "net_tx": 0.0, "gpu": None, "temps": temps or {}, "freq": None, "dt": dt}


def _feed(buf, samples, groups=None):
    for t, st in enumerate(samples):
        buf.feed_stats(st, t=1000 + t)
        if groups and t % 2:
            buf.feed_procs(groups, t=1000 + t)
    return 1000 + len(samples)


def test_cpu_hog_is_named():
    buf = xray.Buffer()
    now = _feed(buf, [_stats(20)] * 10 + [_stats(97)] * 5,
                [ProcGroup("chrome", [1], 80.0, 2**30), ProcGroup("idle", [2], 1.0, 10)])
    shot = xray.capture(buf, "hotkey", live={}, now=now)
    assert shot.suspects[0]["kind"] == "cpu"
    assert shot.suspects[0]["who"] == "chrome"
    assert "chrome" in shot.headline()


def test_whole_system_stall_is_reported():
    buf = xray.Buffer()
    now = _feed(buf, [_stats()] * 5 + [_stats(dt=4.2)] + [_stats()] * 2)
    shot = xray.capture(buf, "auto", live={}, now=now)
    assert shot.suspects[0]["kind"] == "stall"


def test_sleep_gap_is_not_a_freeze():
    buf = xray.Buffer()
    _feed(buf, [_stats()] * 3 + [_stats(dt=600)])
    assert buf.stalled() == 0
    assert not buf.want_auto(now=2000)


def test_auto_shot_is_rate_limited():
    buf = xray.Buffer()
    _feed(buf, [_stats(dt=3.0)])
    assert buf.want_auto(now=5000)
    assert not buf.want_auto(now=5010)
    assert buf.want_auto(now=5000 + xray.AUTO_GAP)


def test_driver_and_disk_from_live_probe():
    buf = xray.Buffer()
    now = _feed(buf, [_stats(disk=200 * 2**20)] * 5)
    shot = xray.capture(buf, "button", now=now,
                        live={"interrupt": 6.0, "dpc": 9.0, "disk_busy": 100.0, "disk_procs": [["MsMpEng.exe", 9e7]]})
    kinds = [s["kind"] for s in shot.suspects]
    assert kinds[0] == "driver"
    assert "disk" in kinds
    disk = next(s for s in shot.suspects if s["kind"] == "disk")
    assert disk["who"] == "MsMpEng.exe"


def test_single_core_bottleneck():
    buf = xray.Buffer()
    now = _feed(buf, [_stats(30, per=[100, 5, 5, 5])] * 5, [ProcGroup("game.exe", [7], 25.0, 2**30)])
    shot = xray.capture(buf, "hotkey", live={}, now=now)
    assert shot.suspects[0]["kind"] == "core"


def test_nothing_found_still_explains():
    buf = xray.Buffer()
    now = _feed(buf, [_stats()] * 10)
    shot = xray.capture(buf, "hotkey", live={}, now=now)
    assert [s["kind"] for s in shot.suspects] == ["none"]


def test_window_keeps_only_recent_seconds():
    buf = xray.Buffer()
    now = _feed(buf, [_stats()] * 50)
    frames, _ = buf.window(now)
    assert len(frames) <= xray.LOOKBACK + 3


def test_save_load_clear():
    buf = xray.Buffer()
    now = _feed(buf, [_stats(95)] * 5)
    xray.save(xray.capture(buf, "hotkey", live={}, now=now))
    xray.save(xray.capture(buf, "auto", live={}, now=now + 5))
    shots = xray.load()
    assert [s.reason for s in shots] == ["auto", "hotkey"]
    xray.clear()
    assert xray.load() == []


def test_probe_runs_here():
    live = xray.probe(0.2)
    assert isinstance(live, dict) and "disk_procs" in live
