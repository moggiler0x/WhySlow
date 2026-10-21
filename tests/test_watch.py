from whyslow import watch
from whyslow.processes import ProcGroup


def _groups():
    return [ProcGroup("chrome", [1], 70.0, 2 * 2**30), ProcGroup("idle", [2], 1.0, 10)]


def _run(rec, samples):
    """samples: [(секунда, cpu, ram)] — возвращает законченные эпизоды."""
    out = []
    for t, cpu, ram in samples:
        ep = rec.feed(cpu, ram, t)
        if ep:
            out.append(ep)
    return out


def test_long_cpu_spike_is_recorded():
    rec = watch.Recorder()
    rec.feed_procs(_groups())
    high = [(t, 95, 50) for t in range(0, 30)]
    calm = [(t, 20, 50) for t in range(30, 45)]
    eps = _run(rec, high + calm)
    assert len(eps) == 1
    ep = eps[0]
    assert ep.kind == "cpu" and ep.culprits[0][0] == "chrome"
    assert 29 <= ep.duration <= 31


def test_short_spike_is_ignored():
    rec = watch.Recorder()
    rec.feed_procs(_groups())
    eps = _run(rec, [(t, 95, 50) for t in range(0, 5)] + [(t, 10, 50) for t in range(5, 30)])
    assert eps == []


def test_brief_dip_does_not_split_episode():
    rec = watch.Recorder()
    rec.feed_procs(_groups())
    samples = [(t, 95, 50) for t in range(0, 20)] + [(t, 30, 50) for t in range(20, 25)] + \
              [(t, 95, 95) for t in range(25, 40)] + [(t, 10, 40) for t in range(40, 60)]
    eps = _run(rec, samples)
    assert len(eps) == 1 and eps[0].kind == "cpu+ram"


def test_save_and_load():
    ep = watch.Episode(100.0, 160.0, "ram", 40, 95, [["chrome", 10.0, 2**31]])
    watch.save(ep)
    watch.save(watch.Episode(200.0, 230.0, "cpu", 99, 50, []))
    loaded = watch.load()
    assert [e.start for e in loaded] == [200.0, 100.0]
    assert "chrome" in loaded[1].headline() or "Chrome" in loaded[1].headline()
    watch.clear()
    assert watch.load() == []


def test_selfstart_command_is_runnable():
    cmd = watch._command()
    assert cmd and "--tray" in " ".join(cmd)
