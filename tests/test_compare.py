from types import SimpleNamespace

from whyslow import settings
from whyslow.premium import compare
from whyslow.knowledge import KnowledgeBase


def test_norm_cpu():
    assert compare.norm_cpu("Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz") == "intel core i5-8250u"
    assert compare.norm_cpu("AMD Ryzen 5 3600 6-Core Processor") == "amd ryzen 5 3600"
    assert compare.norm_cpu("AMD Ryzen 7 5800H with Radeon Graphics") == "amd ryzen 7 5800h"
    assert compare.norm_cpu("Apple M1") == "apple m1"


def test_norm_gpu_and_ram():
    assert compare.norm_gpu("NVIDIA GeForce RTX 3060 Laptop GPU") == "nvidia geforce rtx 3060 laptop"
    assert compare.ram_bucket(int(15.8 * 2**30)) == 16
    assert compare.ram_bucket(int(7.4 * 2**30)) == 8


def test_install_id_is_stable_and_random():
    a = compare.install_id()
    assert len(a) == 32 and a == compare.install_id()
    settings.set("compare_id", "")
    assert compare.install_id() != a


def _verdict(entry_id, verdict="can_disable"):
    return SimpleNamespace(entry_id=entry_id, verdict=verdict)


def test_collect_sends_only_known_program_ids(monkeypatch):
    monkeypatch.setattr(compare, "boot_seconds", lambda: 42.0)
    monkeypatch.setattr(compare.psutil, "cpu_percent", lambda interval=None: 3.0)
    startup = [
        SimpleNamespace(name="Steam", enabled=True, system=False, verdict=_verdict("steam")),
        SimpleNamespace(name="МойСекретныйПроект", enabled=True, system=False, verdict=_verdict("", "unknown")),
        SimpleNamespace(name="Off", enabled=False, system=False, verdict=_verdict("discord")),
    ]
    result = SimpleNamespace(startup=startup, junk=[SimpleNamespace(size=2**30)], procs=[],
                             physical=[SimpleNamespace(system=True, kind="ssd")])
    snap = compare.collect(result, "Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz", [{"name": "Intel UHD 620"}])
    data = snap.to_json()
    assert data["programs"] == ["steam"]
    assert data["unknown_programs"] == 1
    assert data["hw"]["disk"] == "ssd" and data["hw"]["cpu"] == "intel core i5-8250u"
    assert data["metrics"]["boot_s"] == 42.0 and data["metrics"]["startup"] == 2
    assert "Секрет" not in str(data)


def test_explain_names_programs_rare_on_fast_pcs():
    kb = KnowledgeBase({"entries": [
        {"id": "steam", "title": "Steam", "verdict": "can_disable", "note": "", "match": ["steam"]},
        {"id": "edge_update", "title": "Edge Update", "verdict": "can_disable", "note": "обновлялка", "match": ["edge"]},
        {"id": "defender", "title": "Defender", "verdict": "keep", "note": "", "match": ["defender"]},
    ]}, "test")
    snap = compare.Snapshot("0" * 32, {}, {"boot_s": 60, "idle_cpu": 4, "startup": 20}, ["steam", "edge_update", "defender"])
    data = {"peers": 12, "level": "model", "enough": True,
            "metrics": {"boot_s": {"median": 40, "p25": 30}, "idle_cpu": {"median": 4, "p25": 2},
                        "startup": {"median": 10, "p25": 8}},
            "programs": {"steam": {"all": 0.8, "fast": 0.9}, "edge_update": {"all": 0.5, "fast": 0.1},
                         "defender": {"all": 0.1, "fast": 0.0}}}
    r = compare.explain_result(snap, data, kb)
    assert "на 33% быстрее" in r.headline
    assert [s.id for s in r.suspects] == ["edge_update"]
    boot = next(x for x in r.rows if x.key == "boot_s")
    assert boot.text() == "на 50% хуже"


def test_explain_needs_enough_peers():
    snap = compare.Snapshot("0" * 32, {}, {}, [])
    assert compare.explain_result(snap, {"peers": 3}) is None


def test_no_server_means_no_network(monkeypatch):
    import pytest
    monkeypatch.delenv("WHYSLOW_COMPARE_URL", raising=False)
    with pytest.raises(compare.CompareError):
        compare.compare(compare.Snapshot("0" * 32, {}, {}, []))
