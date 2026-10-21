from whyslow import checks, power
from whyslow.power import PowerState


def _levels(st):
    f = []
    checks._power(f, st)
    return [x.level for x in f]


def test_desktop_is_quiet():
    assert _levels(PowerState()) == []


def test_power_saver():
    assert _levels(PowerState(saver=True, saver_name="Экономия энергии")) == [checks.WARN]


def test_low_battery_unplugged():
    assert _levels(PowerState(battery=True, plugged=False, percent=12)) == [checks.INFO]


def test_worn_battery():
    assert _levels(PowerState(battery=True, health=70, cycles=900)) == [checks.INFO]
    assert _levels(PowerState(battery=True, health=50)) == [checks.WARN]
    assert _levels(PowerState(battery=True, condition="Service Recommended")) == [checks.WARN]


def test_ratio():
    assert power._ratio("45000", "50000") == 90
    assert power._ratio(None, "1") is None
    assert power._ratio("60", "50") == 100


def test_state_does_not_crash():
    assert isinstance(power.state(), PowerState)
