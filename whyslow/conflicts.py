"""Конфликты между фоновыми программами: два антивируса сразу, несколько VPN,
DNS или сетевой адаптер, оставшиеся от закрытого VPN, гора оверлеев поверх игр.
Каждая программа по отдельности нормальная — тормозит именно их сочетание."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass

from .i18n import tr
from .util import IS_LINUX, IS_MAC, IS_WINDOWS, by_os, ps_json, run

OK, INFO, WARN, BAD = "ok", "info", "warn", "bad"


@dataclass
class Conflict:
    level: str
    title: str
    detail: str = ""
    advice: str = ""
    tab: str = ""


# Антивирусы с защитой в реальном времени: по имени процесса. Встроенный Защитник
# Windows сам уступает место стороннему антивирусу, поэтому его здесь нет.
# Malwarebytes и подобные «вторые мнения» рассчитаны на работу рядом — их тоже нет.
ANTIVIRUSES = {
    "Kaspersky": r"^(avp|avpui|kavfs\w*)(\.exe)?$|^kav$",
    "ESET": r"^(ekrn|egui|esets?_?daemon|esets_proxy)(\.exe)?$",
    "Avast": r"^(avastsvc|avastui|aswidsagent)(\.exe)?$|^com\.avast\.",
    "AVG": r"^(avgsvc|avgui|avgsvca)(\.exe)?$",
    "Dr.Web": r"^(dwservice|spideragent|dwengine|drweb\w*)(\.exe)?$",
    "Bitdefender": r"^(vsserv|bdagent|bdservicehost|bdredline)(\.exe)?$",
    "Norton": r"^(nortonsecurity|ns|nsservice)(\.exe)?$",
    "McAfee": r"^(mcshield|mfemms|mfevtps|mcapexe|masvc)(\.exe)?$",
    "360 Total Security": r"^(qhactivedefense|qhsafetray|360tray|360sd)(\.exe)?$",
    "Comodo": r"^(cmdagent|cis)(\.exe)?$",
    "Sophos": r"^(sophoshealth|savservice|sophosui)(\.exe)?$",
    "Trend Micro": r"^(coreserviceshell|uiseagnt|pccntmon)(\.exe)?$",
}

# VPN-клиенты: каждый ставит свои маршруты и DNS, два сразу дерутся за трафик
VPNS = {
    "NordVPN": r"nordvpn",
    "ProtonVPN": r"protonvpn",
    "ExpressVPN": r"expressvpn",
    "Surfshark": r"surfshark",
    "Windscribe": r"windscribe",
    "Hotspot Shield": r"hotspotshield|hsscp",
    "AmneziaVPN": r"amnezia",
    "Hiddify": r"hiddify",
    "v2rayN": r"^v2rayn",
    "Nekoray": r"neko(ray|box)",
    "Clash": r"clash|mihomo",
    "Outline": r"^outline",
    "OpenVPN": r"^openvpn(-gui|serv)?(\.exe)?$",
    "WireGuard": r"^wireguard(\.exe)?$",
    "Psiphon": r"psiphon",
    "AdGuard VPN": r"adguardvpn",
    "Kaspersky VPN": r"ksde|kaspersky.*vpn",
    "Cisco AnyConnect": r"vpnagent|vpnui|anyconnect|cisco secure client",
    "FortiClient": r"forticlient|fortitray",
    "Tailscale": r"^tailscaled?(\.exe|-ipn)?$",
    "ZeroTier": r"zerotier",
    "Radmin VPN": r"radminvpn|rvcontrolsvc",
    "Hamachi": r"hamachi",
}

# Оверлеи поверх игр: каждый перехватывает вывод кадра, вместе они дают статтеры
OVERLAYS = {
    "RivaTuner (MSI Afterburner)": r"^rtss(\.exe)?$",
    "NVIDIA Overlay": r"^nvidia overlay(\.exe)?$",
    "Xbox Game Bar": r"^gamebar(ftserver)?(\.exe)?$",
    "Discord": r"^discord(\.exe)?$",
    "Steam": r"^steam(\.exe)?$",
    "Overwolf": r"^overwolf(\.exe)?$",
    "AMD Software": r"^(radeonsoftware|amdrsserv)(\.exe)?$",
}

# Сетевые адаптеры, которые ставят VPN и программы «виртуальной локалки»
_VIRTUAL_ADAPTER = re.compile(r"tap-windows|tap-?\w*\s*adapter|wintun|wireguard|tailscale|zerotier|hamachi|radmin|"
                              r"openvpn|nordlynx|proton|surfshark|express|amnezia|fortinet|anyconnect|cisco", re.I)


def _running(names: list[str], table: dict[str, str]) -> list[str]:
    found = []
    for product, pattern in table.items():
        rx = re.compile(pattern, re.I)
        if any(rx.search(n) for n in names):
            found.append(product)
    return found


def detect(procs) -> list[Conflict]:
    """procs — группы процессов из ProcessMonitor (нужны только имена)."""
    names = [p.name for p in procs]
    out: list[Conflict] = []
    out += _antivirus(names)
    vpns = _running(names, VPNS)
    out += _vpns(vpns)
    out += _adapters(vpns)
    out += _dns()
    out += _overlays(names)
    return out


def _antivirus(names: list[str]) -> list[Conflict]:
    found = _running(names, ANTIVIRUSES)
    if IS_WINDOWS:
        for name in _win_registered_av():
            if name not in found and not any(name.lower().startswith(f.lower()) or f.lower() in name.lower()
                                             for f in found):
                found.append(name)
    if len(found) < 2:
        return []
    return [Conflict(BAD, tr(f"Работают сразу два антивируса: {', '.join(found)}",
                             f"Two antiviruses are running at once: {', '.join(found)}"),
                     tr("Каждый проверяет каждый открываемый файл, а заодно и друг друга. Отсюда медленное открытие "
                        "программ, высокая нагрузка на диск и иногда зависания.",
                        "Each one scans every file you open, and each other too. That's why programs open slowly, "
                        "the disk is busy and the system sometimes freezes."),
                     tr("Оставьте один антивирус, второй удалите через его родной деинсталлятор.",
                        "Keep one antivirus and remove the other with its own uninstaller."))]


def _win_registered_av() -> list[str]:
    """Сторонние антивирусы, которые сейчас включены по данным Центра безопасности Windows."""
    out = []
    for p in ps_json("Get-CimInstance -Namespace root/SecurityCenter2 -ClassName AntiVirusProduct | "
                     "Select-Object displayName, productState | ConvertTo-Json", timeout=20):
        name = str(p.get("displayName") or "")
        try:
            state = int(p.get("productState") or 0)
        except (TypeError, ValueError):
            continue
        # Второй байт productState: 0x10 — защита в реальном времени включена
        if name and "defender" not in name.lower() and (state >> 8) & 0x10:
            out.append(name)
    return out


def _vpns(vpns: list[str]) -> list[Conflict]:
    # Tailscale/ZeroTier — частные сети, рядом с обычным VPN обычно живут мирно
    real = [v for v in vpns if v not in ("Tailscale", "ZeroTier", "Radmin VPN", "Hamachi")]
    if len(real) < 2:
        return []
    return [Conflict(WARN, tr(f"Запущено несколько VPN: {', '.join(real)}", f"Several VPNs are running: {', '.join(real)}"),
                     tr("Каждый VPN перенастраивает маршруты и DNS под себя. Вместе они мешают друг другу: интернет "
                        "то пропадает, то становится медленным, сайты открываются через раз.",
                        "Each VPN reroutes traffic and DNS its own way. Together they get in each other's way: the "
                        "internet drops out or slows down, sites load every other time."),
                     tr("Закройте лишние VPN и уберите их из автозагрузки, оставьте один.",
                        "Close the extra VPNs and remove them from startup; keep one."), "startup")]


def _adapters(vpns: list[str]) -> list[Conflict]:
    """Включённый VPN-адаптер, когда ни один VPN не запущен: трафик может уходить в никуда."""
    if not IS_WINDOWS or vpns:
        return []
    up = []
    for a in ps_json("Get-NetAdapter | Where-Object Status -eq 'Up' | "
                     "Select-Object Name, InterfaceDescription | ConvertTo-Json", timeout=20):
        desc = f"{a.get('InterfaceDescription') or ''} {a.get('Name') or ''}"
        if _VIRTUAL_ADAPTER.search(desc):
            up.append(str(a.get("InterfaceDescription") or a.get("Name")))
    if not up:
        return []
    return [Conflict(WARN, tr(f"Включён VPN-адаптер без VPN: {', '.join(up[:3])}",
                              f"A VPN adapter is up with no VPN running: {', '.join(up[:3])}"),
                     tr("Адаптер остался от VPN-программы, которая сейчас не запущена. Windows может отправлять через "
                        "него часть трафика — тогда интернет медленный или пропадает.",
                        "The adapter is left over from a VPN app that isn't running now. Windows may route some "
                        "traffic through it — then the internet is slow or drops out."),
                     tr("Отключите его: «Параметры → Сеть и Интернет → Дополнительные сетевые параметры» → адаптер → "
                        "«Отключить». Если VPN больше не нужен — удалите программу.",
                        "Disable it: Settings → Network & Internet → Advanced network settings → the adapter → "
                        "Disable. If you no longer need the VPN, uninstall it."))]


def dns_servers() -> list[str]:
    """DNS-серверы, которыми система пользуется сейчас."""
    servers: list[str] = []
    if IS_WINDOWS:
        code, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                         "(Get-DnsClientServerAddress -AddressFamily IPv4).ServerAddresses"], timeout=20)
        if code == 0:
            servers = [line.strip() for line in out.splitlines() if line.strip()]
    elif IS_MAC:
        code, out = run(["scutil", "--dns"])
        if code == 0:
            # первый resolver — тот, что работает для всех имён
            first = out.split("resolver #2")[0]
            servers = re.findall(r"nameserver\[\d+\]\s*:\s*(\S+)", first)
    else:
        try:
            with open("/etc/resolv.conf", encoding="utf-8") as f:
                servers = re.findall(r"^\s*nameserver\s+(\S+)", f.read(), re.M)
        except OSError:
            pass
    seen = []
    for s in servers:
        if s not in seen:
            seen.append(s)
    return seen


def dns_alive(server: str, timeout: float = 1.5) -> bool | None:
    """Отвечает ли DNS-сервер. Шлём один запрос (A example.com) по UDP.
    False — порт точно закрыт (система вернула «отказ»), None — не ответил вовремя."""
    import socket
    import struct
    query = struct.pack(">HHHHHH", 0x5753, 0x0100, 1, 0, 0, 0) + b"\x07example\x03com\x00" + struct.pack(">HH", 1, 1)
    family = socket.AF_INET6 if ":" in server else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.connect((server.split("%")[0], 53))
            sock.send(query)
            return len(sock.recv(512)) > 0
    except (ConnectionRefusedError, ConnectionResetError):
        return False
    except OSError:
        return None


def _is_loopback(addr: str) -> bool:
    try:
        return ipaddress.ip_address(addr.split("%")[0]).is_loopback
    except ValueError:
        return False


def _dns() -> list[Conflict]:
    servers = dns_servers()
    local = [s for s in servers if _is_loopback(s)]
    if not local or len(local) < len(servers):
        return []  # есть и обычный сервер — имена всё равно разрешаются
    if IS_LINUX and set(local) <= {"127.0.0.53", "127.0.0.54"}:
        return []  # systemd-resolved, штатная схема
    if any(dns_alive(s) is not False for s in local):
        return []
    return [Conflict(BAD, tr(f"DNS указывает на {local[0]}, но там никого нет",
                             f"DNS points to {local[0]}, but nothing is there"),
                     tr("Так бывает, когда VPN или «блокировщик рекламы» подменил DNS на себя и закрылся. "
                        "Сайты перестают открываться, хотя интернет вроде бы есть.",
                        "This happens when a VPN or ad blocker pointed DNS at itself and then quit. "
                        "Sites stop loading even though the internet seems to be connected."),
                     by_os(tr("Верните DNS «автоматически»: «Параметры → Сеть и Интернет → ваша сеть → "
                              "Назначение DNS-сервера → Изменить → Автоматически (DHCP)».",
                              "Set DNS back to automatic: Settings → Network & Internet → your network → "
                              "DNS server assignment → Edit → Automatic (DHCP)."),
                           tr("Уберите адрес из «Системные настройки → Сеть → ваша сеть → Подробнее → DNS».",
                              "Remove the address in System Settings → Network → your network → Details → DNS."),
                           tr("Верните DNS «автоматически» в настройках сети или в /etc/resolv.conf.",
                              "Set DNS back to automatic in network settings or in /etc/resolv.conf.")))]


def _overlays(names: list[str]) -> list[Conflict]:
    if IS_MAC:
        return []
    found = _running(names, OVERLAYS)
    if len(found) < 3:
        return []
    return [Conflict(INFO, tr(f"Сразу несколько оверлеев поверх игр: {', '.join(found)}",
                              f"Several game overlays at once: {', '.join(found)}"),
                     tr("Каждый оверлей встраивается в вывод кадра. Когда их много, в играх появляются статтеры и "
                        "просадки FPS, а иногда игра вылетает.",
                        "Each overlay hooks into frame output. With many of them, games stutter, FPS drops, and "
                        "sometimes the game crashes."),
                     tr("Выключите оверлей в настройках тех программ, где он не нужен (Discord, Steam, Game Bar).",
                        "Turn the overlay off in the apps where you don't need it (Discord, Steam, Game Bar)."))]
