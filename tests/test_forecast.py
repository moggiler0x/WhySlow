import json

import pytest

from whyslow import forecast

DB = forecast.load()
NOW = 2026.8


def _hw(cpu, gpus=(), ram=16, disk="ssd", os="Windows 11 (сборка 26100)", laptop=False, mac=False):
    return {"cpu": cpu, "threads": 8, "ram": ram * 2**30 * 0.97, "gpus": [{"name": g} if isinstance(g, str) else g for g in gpus],
            "disk": disk, "os": os, "laptop": laptop, "mac": mac, "windows": os.startswith("Windows")}


def test_bundled_file_is_valid():
    data = json.loads(forecast.BUNDLED.read_text(encoding="utf-8"))
    assert forecast._valid(data)
    ids = [w["id"] for w in data["workloads"]]
    assert "everyday" in ids and len(ids) == len(set(ids))
    for w in data["workloads"]:
        assert w.get("title_en"), w["id"]


@pytest.mark.parametrize("name, year, tier", [
    ("Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz", 2017, -1),
    ("Intel(R) Core(TM) i7-10750H CPU @ 2.60GHz", 2020, 1),
    ("Intel(R) Core(TM) i7-1065G7 CPU @ 1.30GHz", 2020, 1),
    ("13th Gen Intel(R) Core(TM) i9-13900K", 2022, 2),
    ("Intel(R) Core(TM) Ultra 7 155H", 2023, 1),
    ("AMD Ryzen 5 3600 6-Core Processor", 2019, 0),
    ("AMD Ryzen 7 5700U with Radeon Graphics", 2020, 0),
    ("AMD Ryzen AI 9 HX 370 w/ Radeon 890M", 2024, 2),
    ("Apple M1", 2020, 1),
    ("Apple M3 Pro", 2023, 2),
])
def test_cpu_rules(name, year, tier):
    chip = forecast.identify(name, DB["cpu"])
    assert chip and (chip.year, chip.tier) == (year, tier)


def test_win11_support():
    assert forecast.identify("Intel(R) Core(TM) i7-8700 CPU", DB["cpu"]).win11 is True
    assert forecast.identify("Intel(R) Core(TM) i7-7700 CPU", DB["cpu"]).win11 is False
    assert forecast.identify("AMD Ryzen 5 1600 Six-Core", DB["cpu"]).win11 is False
    assert forecast.identify("AMD Ryzen 5 2600 Six-Core", DB["cpu"]).win11 is True


@pytest.mark.parametrize("name, year, tier", [
    ("NVIDIA GeForce RTX 3060", 2020, 0),
    ("NVIDIA GeForce RTX 4090", 2022, 4),
    ("NVIDIA GeForce RTX 4060 Laptop GPU", 2022, -1),
    ("NVIDIA GeForce GTX 1060 6GB", 2016, 0),
    ("NVIDIA GeForce GTX 1650", 2019, -2),
    ("AMD Radeon RX 580 Series", 2017, 0),
    ("AMD Radeon RX 6700 XT", 2020, 1),
    ("AMD Radeon RX 9070 XT", 2025, 2),
    ("Apple M2", 2022, -2),
])
def test_gpu_rules(name, year, tier):
    chip = forecast.identify(name, DB["gpu"], 2020)
    assert chip and (chip.year, chip.tier) == (year, tier)


def test_integrated_gpu_takes_cpu_year_and_virtual_is_skipped():
    chip = forecast.identify("Intel(R) UHD Graphics 630", DB["gpu"], 2018)
    assert chip.year == 2018 and chip.integrated
    assert forecast.identify("Microsoft Basic Display Adapter", DB["gpu"]) is False


def test_new_pc_lasts_long_old_pc_is_done():
    new = forecast.estimate(_hw("AMD Ryzen 7 9700X", ["NVIDIA GeForce RTX 5070"], ram=32), DB, NOW)
    old = forecast.estimate(_hw("Intel(R) Core(TM) i5-3470 CPU", ["Intel(R) HD Graphics 2500"], ram=4,
                                disk="hdd", os="Windows 10 (сборка 19045)"), DB, NOW)
    assert new.get("everyday").years >= 8
    assert new.get("aaa_games").years >= 3
    assert old.get("everyday").years == 0
    assert old.os_text and old.os_action


def test_hdd_is_the_first_thing_to_fix():
    fc = forecast.estimate(_hw("AMD Ryzen 5 5600X", ["NVIDIA GeForce RTX 3060"], disk="hdd"), DB, NOW)
    assert fc.get("everyday").by == "disk"
    tips = forecast.advice(fc, "everyday", DB)
    assert tips[0].kind == "disk"


def test_more_ram_means_more_years():
    a = forecast.estimate(_hw("AMD Ryzen 5 5600X", ram=8), DB, NOW).get("work").parts["ram"]
    b = forecast.estimate(_hw("AMD Ryzen 5 5600X", ram=32), DB, NOW).get("work").parts["ram"]
    assert b > a


def test_mac_advice_does_not_suggest_parts():
    fc = forecast.estimate(_hw("Apple M1", ["Apple M1"], ram=8, os="macOS 15.1", laptop=True, mac=True), DB, NOW)
    texts = " ".join(a.text for a in forecast.advice(fc, "aaa_games", DB))
    assert "Mac" in texts and "RTX" not in texts
    assert fc.os_until


def test_unknown_cpu_is_flagged():
    fc = forecast.estimate(_hw("Some Future CPU 9000"), DB, NOW)
    assert not fc.cpu.known and fc.notes


def test_texts():
    assert forecast.span(1) == "год"
    assert forecast.span(3) == "3 года"
    assert forecast.span(5) == "5 лет"
    assert forecast.years_text(0) == "уже не хватает"
    assert forecast.headline(forecast.estimate(_hw("Apple M4", ["Apple M4"], mac=True), DB, NOW), "everyday")


def test_ram_rounding():
    assert forecast._ram_gb(15.7 * 2**30) == 16
    assert forecast._ram_gb(7.8 * 2**30) == 8
