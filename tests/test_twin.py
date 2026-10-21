import json

import pytest

from whyslow import forecast, i18n
from whyslow.premium import health_money as healthcost
from whyslow.premium import twin
from whyslow.diskhealth import PhysicalDisk
from whyslow.power import PowerState

DB = forecast.load()
NOW = 2026.8
GB = 2**30


@pytest.fixture(autouse=True)
def russian():
    i18n.set_lang("ru", save=False)


def _hw(cpu="AMD Ryzen 5 3600 6-Core Processor", gpu="NVIDIA GeForce GTX 1060 6GB", vram=6, ram=16, disk="ssd",
        laptop=False, mac=False):
    g = {"name": gpu, "vram_mb": vram * 1024, "vram_used_mb": 300} if vram else {"name": gpu}
    return {"cpu": cpu, "threads": 12, "ram": ram * GB * 0.97, "gpus": [g], "disk": disk, "os": "Windows 11",
            "laptop": laptop, "mac": mac, "windows": not mac}


def _use(used=14, total=16, ram_eps=0, cpu_eps=0):
    return twin.Usage(int(used * GB), int(total * GB * 0.97), 0, [("Google Chrome", int(5 * GB))],
                      ram_eps, cpu_eps, ["chrome.exe"] if cpu_eps else [], 97.0 if cpu_eps else 0.0)


def test_bundled_twin_data_is_valid():
    data = json.loads(forecast.BUNDLED.read_text(encoding="utf-8"))
    ids = [u["id"] for u in data["upgrades"]]
    assert len(ids) == len(set(ids))
    for u in data["upgrades"]:
        assert u.get("title_en") and u["replace"], u["id"]
        assert len(u["price"]["rub"]) == 2 and len(u["price"]["usd"]) == 2, u["id"]
    for g in data["games"]:
        assert g["fps_gpu"] > 0 and g["fps_cpu"] > 0
    for key in ("ssd", "recovery", "battery", "cleaning", "backup_drive"):
        assert key in data["health"]["prices"]


def test_reference_parts_are_one():
    assert twin.perf("NVIDIA GeForce RTX 3060", "gpu", DB) == 1.0
    assert twin.perf("NVIDIA GeForce RTX 3060 Ti", "gpu", DB) > 1.0
    assert twin.perf("AMD Ryzen 5 3600 6-Core Processor", "cpu", DB) == 1.0


def test_unknown_part_uses_year():
    old = forecast.Chip("Some GPU", 2013, 0)
    new = forecast.Chip("Some GPU", 2024, 0)
    assert twin.perf("Some GPU", "gpu", DB, old) < 0.3 < 1 < twin.perf("Some GPU", "gpu", DB, new)


def test_fps_is_limited_by_slower_part():
    game = {"fps_gpu": 100, "fps_cpu": 150, "vram_gb": 8, "ram_gb": 8}
    assert twin.game_fps(game, 1.0, 1.0, 12, 16) == (100, "gpu")
    assert twin.game_fps(game, 0.5, 2.0, 12, 16) == (75, "cpu")
    fps, limit = twin.game_fps(game, 1.0, 1.0, 4, 16)
    assert limit == "vram" and fps < 100


def test_gpu_upgrade_raises_fps_on_real_hardware():
    base, sims = twin.simulate_all(_hw(), DB, _use(), "aaa_games", now=NOW)
    gpu = next(s for s in sims if s.up["id"] == "rtx4060")
    cyber = next(g for g in gpu.games if g.id == "cyberpunk")
    assert cyber.after > cyber.now * 2
    assert any("FPS" in e for e in gpu.effects)
    b, a = gpu.get_years("aaa_games")
    assert a > b


def test_measured_fps_scales_estimate():
    _base, sims = twin.simulate_all(_hw(), DB, _use(), "aaa_games", measured={"cyberpunk": 40}, now=NOW)
    gpu = next(s for s in sims if s.up["id"] == "rtx4060")
    cyber = next(g for g in gpu.games if g.id == "cyberpunk")
    assert round(cyber.now) == 40 and cyber.after > 80


def test_ram_effect_names_the_hungry_program():
    _base, sims = twin.simulate_all(_hw(), DB, _use(used=14.5, ram_eps=4), "work", now=NOW)
    ram = next(s for s in sims if s.up["id"] == "ram32")
    text = " ".join(ram.effects)
    assert "Google Chrome" in text and "4 раза" in text
    assert twin.best(sims) is ram


def test_options_fit_the_computer():
    _b, sims = twin.simulate_all(_hw(disk="hdd"), DB, None, "everyday", now=NOW)
    assert "ssd" in {s.up["id"] for s in sims}
    _b, sims = twin.simulate_all(_hw(ram=32), DB, None, "everyday", now=NOW)
    ids = {s.up["id"] for s in sims}
    assert "ssd" not in ids and "ram16" not in ids and "ram32" not in ids
    laptop = _hw(cpu="Intel(R) Core(TM) i5-8250U CPU", gpu="Intel(R) UHD Graphics 620", vram=0, ram=8, laptop=True)
    _b, sims = twin.simulate_all(laptop, DB, None, "everyday", now=NOW)
    assert {s.up["kind"] for s in sims} <= {"ram", "ssd"}
    mac = _hw(cpu="Apple M1", gpu="Apple M1", vram=0, ram=8, laptop=True, mac=True)
    _b, sims = twin.simulate_all(mac, DB, None, "work", now=NOW)
    assert sims and all(s.up.get("platform") == "mac" for s in sims)


def test_hdd_limit_is_explained():
    _b, sims = twin.simulate_all(_hw(disk="hdd"), DB, None, "work", now=NOW)
    ram = next(s for s in sims if s.up["id"] == "ram32")
    assert any("мешает" in e for e in ram.effects)


def test_money_format():
    assert forecast.money(15000, 50000) == "15 000–50 000 ₽"
    assert forecast.money(0, 0) == "0 ₽"
    i18n.set_lang("en", save=False)
    assert forecast.money(300, 1500) == "$300–1,500"


# ------------------------------------------------------- здоровье в деньгах ---

def test_failing_disk_without_backup_costs_recovery():
    rep = healthcost.assess([PhysicalDisk("WD Blue", "hdd", "warn", True)], PowerState(), {}, False, DB, mac=False)
    disk = rep.risks[0]
    assert disk.level == "warn" and disk.act_now
    assert disk.later[1] > disk.now[1]
    assert "полгода" in disk.when
    assert "₽" in healthcost.headline(rep)


def test_backup_makes_failure_cheap():
    no = healthcost.assess([PhysicalDisk("X", "ssd", "bad", True)], None, {}, False, DB, mac=False).risks[0]
    yes = healthcost.assess([PhysicalDisk("X", "ssd", "bad", True)], None, {}, True, DB, mac=False).risks[0]
    assert yes.later[1] < no.later[1]


def test_healthy_with_backup_needs_nothing():
    rep = healthcost.assess([PhysicalDisk("X", "ssd", "ok", True)], PowerState(), {}, True, DB, mac=False)
    assert not rep.todo()
    assert "Срочно" in healthcost.headline(rep)


def test_backup_counted_once():
    disks = [PhysicalDisk("A", "ssd", "ok", True), PhysicalDisk("B", "hdd", "", False)]
    rep = healthcost.assess(disks, None, {}, False, DB, mac=False)
    assert rep.now_total() == (0, DB["health"]["prices"]["backup_drive"]["rub"][1])


def test_battery_levels():
    worn = healthcost.assess([], PowerState(battery=True, health=55, cycles=900), {}, None, DB, mac=True)
    bat = next(r for r in worn.risks if r.id == "battery")
    assert bat.level == "bad" and bat.act_now and bat.later[1] > bat.now[1]
    tired = healthcost.assess([], PowerState(battery=True, health=75, cycles=500), {}, None, DB, mac=False)
    bat = next(r for r in tired.risks if r.id == "battery")
    assert bat.level == "warn" and not bat.act_now  # ждать можно: замена не подорожает
    fresh = healthcost.assess([], PowerState(battery=True, health=95, cycles=100), {}, None, DB, mac=False)
    assert next(r for r in fresh.risks if r.id == "battery").level == "ok"
    assert not any(r.id == "battery" for r in healthcost.assess([], PowerState(), {}, None, DB).risks)


def test_overheat():
    hot = healthcost.assess([], None, {"CPU Package": 97, "NVMe": 60}, None, DB)
    assert hot.risks[0].id == "heat" and hot.risks[0].level == "bad"
    assert not healthcost.assess([], None, {"CPU": 70}, None, DB).risks
    assert not healthcost.assess([], None, {"NVMe drive": 99}, None, DB).risks
