from whyslow import checks, optimize, processes, startup
from whyslow.knowledge import Verdict


def _item(name, verdict, **kw):
    return startup.StartupItem(name, name, "test", True, "xdg", verdict=Verdict(verdict, "note"), **kw)


def _proc(name, verdict, mem=0, cpu=0.0, system=False):
    return processes.ProcGroup(name, [1], cpu, mem, system=system, verdict=Verdict(verdict, "note"))


def test_keep_is_never_offered():
    r = checks.ScanResult(startup=[_item("a", "keep"), _item("b", "safe"), _item("c", "optional")],
                          procs=[_proc("kernel_task", "keep", mem=2**31, cpu=90),
                                 _proc("miner", "bad"), _proc("chat", "safe", mem=2**30)])
    steps = optimize.plan(r)
    names = {s.target.name: s for s in steps}
    assert "a" not in names and "kernel_task" not in names
    assert names["b"].checked and not names["c"].checked
    assert names["miner"].checked and not names["chat"].checked


def test_system_and_admin_items_are_not_prechecked():
    r = checks.ScanResult(startup=[_item("svc", "safe", needs_admin=True), _item("sys", "safe", system=True)])
    assert not any(s.checked for s in optimize.plan(r))
