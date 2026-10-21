from whyslow import conflicts
from whyslow.processes import ProcGroup


def _procs(*names):
    return [ProcGroup(n) for n in names]


def test_two_antiviruses(monkeypatch):
    monkeypatch.setattr(conflicts, "_win_registered_av", lambda: [])
    found = conflicts._antivirus(["avp.exe", "ekrn.exe", "chrome.exe"])
    assert found and found[0].level == conflicts.BAD
    assert "Kaspersky" in found[0].title and "ESET" in found[0].title


def test_one_antivirus_is_fine(monkeypatch):
    monkeypatch.setattr(conflicts, "_win_registered_av", lambda: [])
    assert conflicts._antivirus(["avp.exe", "MsMpEng.exe"]) == []


def test_several_vpns():
    vpns = conflicts._running(["NordVPN.exe", "AmneziaVPN.exe", "Tailscale.exe"], conflicts.VPNS)
    assert set(vpns) == {"NordVPN", "AmneziaVPN", "Tailscale"}
    assert conflicts._vpns(vpns)[0].level == conflicts.WARN
    assert conflicts._vpns(["NordVPN", "Tailscale"]) == []


def test_dead_local_dns(monkeypatch):
    monkeypatch.setattr(conflicts, "dns_servers", lambda: ["127.0.0.1"])
    monkeypatch.setattr(conflicts, "dns_alive", lambda s: False)
    assert conflicts._dns()[0].level == conflicts.BAD
    monkeypatch.setattr(conflicts, "dns_alive", lambda s: True)
    assert conflicts._dns() == []


def test_regular_dns_is_not_flagged(monkeypatch):
    monkeypatch.setattr(conflicts, "dns_servers", lambda: ["127.0.0.1", "8.8.8.8"])
    monkeypatch.setattr(conflicts, "dns_alive", lambda s: False)
    assert conflicts._dns() == []


def test_closed_port_is_detected():
    # на loopback закрытый UDP-порт отвечает отказом сразу
    assert conflicts.dns_alive("127.0.0.1") in (False, None)


def test_detect_runs_without_errors(monkeypatch):
    monkeypatch.setattr(conflicts, "dns_servers", lambda: [])
    assert isinstance(conflicts.detect(_procs("python")), list)
