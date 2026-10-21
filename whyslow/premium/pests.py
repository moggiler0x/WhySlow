"""Тихие вредители: скрытые майнеры, расширения браузеров, которые видят все сайты,
и программы, которые выходят в интернет по ночам, пока компьютер никто не трогает.

Платная функция: модуль грузит whyslow.premium (в релизе он зашифрован), вкладку строит
build(main). Закрыть процесс, убрать файл в карантин или снять политику браузера можно лишь
после подтверждения в окне. Всё обратимое пишется в журнал, а откатывается через открытый
whyslow.restore — вернуть файл из карантина можно и после окончания подписки."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import plistlib
import re
import shutil
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

from .. import journal
from ..i18n import tr
from ..knowledge import KnowledgeBase, Verdict
from ..util import IS_LINUX, IS_MAC, IS_WINDOWS, app_dir, by_os, read_json, run, write_json

FEATURE = "pests"
OK, INFO, WARN, BAD = "ok", "info", "warn", "bad"
RANK = {OK: 0, INFO: 1, WARN: 2, BAD: 3}

NIGHT_HOURS = range(0, 6)      # 00:00–05:59 по местному времени
IDLE_FOR_NIGHT = 10 * 60       # ночью считаем выход в сеть, только если за компьютером никого нет столько секунд
IDLE_FOR_MINER = 5 * 60        # «грузит процессор, пока вы отошли»
RECORD_EVERY = 5 * 60          # как часто записывать соединения в фоне
KEEP_DAYS = 30

# Порты, на которых обычно сидят майнинг-пулы (stratum)
POOL_PORTS = {3333, 4444, 5555, 6666, 7777, 9999, 14433, 14444, 20535, 45560, 45700}
# Признаки майнера в командной строке: адрес пула, алгоритм, «чаевые» автору майнера
MINER_ARGS = re.compile(
    r"stratum\+(tcp|ssl|tls)://|--donate-level|\bcryptonight\b|\brandomx\b|\bkawpow\b|\bethash\b|\bautolykos|"
    r"--algo[= ]|--coin[= ]|\bnicehash\b|\bmoneroocean\b|\bminexmr\b|\b(xmr|eth|rvn|etc)\.?pool\b|"
    r"\b-o\s+\S+\.\S+:\d{4,5}\b.*\b-u\s", re.IGNORECASE)


def unlocked() -> bool:
    from .. import license
    return license.has_premium(FEATURE)


# ------------------------------------------------------------- простой ---

def idle_seconds() -> float | None:
    """Сколько секунд никто не трогал мышь и клавиатуру. None — система не говорит."""
    if IS_WINDOWS:
        try:
            import ctypes

            class LastInput(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            li = LastInput()
            li.cbSize = ctypes.sizeof(li)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(li)):
                return None
            return ((ctypes.windll.kernel32.GetTickCount() - li.dwTime) & 0xFFFFFFFF) / 1000
        except (AttributeError, OSError):
            return None
    if IS_MAC:
        code, out = run(["ioreg", "-c", "IOHIDSystem", "-d", "4"], timeout=3)
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', out) if code == 0 else None
        return int(m.group(1)) / 1e9 if m else None
    if shutil.which("xprintidle"):
        code, out = run(["xprintidle"], timeout=2)
        if code == 0 and out.strip().isdigit():
            return int(out.strip()) / 1000
    return None


def is_public(ip: str) -> bool:
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    if a.version == 6 and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def _text(s: str) -> str:
    return s.encode("utf-8", "surrogateescape").decode("utf-8", "replace") if s else ""


# ------------------------------------------------------------- соединения ---

@dataclass
class Conn:
    pid: int
    name: str
    exe: str
    ip: str
    port: int
    host: str = ""        # обратное имя (может быть пустым)

    @property
    def pool_port(self) -> bool:
        return self.port in POOL_PORTS


def _proc_conns(p: psutil.Process):
    fn = getattr(p, "net_connections", None) or p.connections
    return fn(kind="inet")


def _raw_connections() -> list[tuple[int, str, int]]:
    """(pid, удалённый ip, порт) для установленных соединений. В macOS общий список
    требует root — тогда спрашиваем каждый процесс по отдельности (свои видны)."""
    out = []
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.raddr and c.status == psutil.CONN_ESTABLISHED and c.pid:
                out.append((c.pid, c.raddr.ip, c.raddr.port))
        return out
    except (psutil.AccessDenied, PermissionError, OSError):
        pass
    for p in psutil.process_iter():
        try:
            for c in _proc_conns(p):
                if c.raddr and c.status == psutil.CONN_ESTABLISHED:
                    out.append((p.pid, c.raddr.ip, c.raddr.port))
        except (psutil.Error, OSError):
            continue
    return out


_dns: dict[str, str] = {}


def resolve(ips: list[str], timeout: float = 3.0) -> dict[str, str]:
    """Обратные имена адресов (a.b.c.d → server.example.com), с кешем и общим лимитом времени."""
    # Потоки, а не concurrent.futures: модуль грузится в сборке по ключу, и его импорты должны
    # уже быть в программе — PyInstaller видит только открытый код
    todo = [ip for ip in dict.fromkeys(ips) if ip not in _dns][:64]

    def one(ip):
        try:
            _dns[ip] = socket.gethostbyaddr(ip)[0]
        except (OSError, UnicodeError):
            _dns[ip] = ""

    threads = [threading.Thread(target=one, args=(ip,), daemon=True) for ip in todo]
    for t in threads:
        t.start()
    deadline = time.time() + timeout
    for t in threads:
        t.join(max(0.0, deadline - time.time()))
    return {ip: _dns.get(ip, "") for ip in ips}


def connections(with_hosts: bool = True) -> list[Conn]:
    """Текущие соединения программ с интернетом (локальная сеть и сам WhySlow не в счёт)."""
    me = os.getpid()
    names: dict[int, tuple[str, str]] = {}
    result = []
    for pid, ip, port in _raw_connections():
        if pid == me or not is_public(ip):
            continue
        if pid not in names:
            try:
                p = psutil.Process(pid)
                name = _text(p.name())
                try:
                    exe = _text(p.exe())
                except (psutil.Error, OSError):
                    exe = ""
                names[pid] = (name, exe)
            except psutil.Error:
                names[pid] = ("?", "")
        name, exe = names[pid]
        result.append(Conn(pid, name, exe, ip, port))
    if with_hosts and result:
        hosts = resolve([c.ip for c in result])
        for c in result:
            c.host = hosts.get(c.ip, "")
    return result


# ------------------------------------------------------- ночной журнал сети ---

def _log_path() -> Path:
    return app_dir() / "netlog.json"


def load_log() -> dict:
    data = read_json(_log_path(), {})
    if not isinstance(data, dict) or not isinstance(data.get("procs"), dict):
        data = {"procs": {}}
    return data


def record(now: float | None = None, idle: float | None = -1.0, conns: list[Conn] | None = None) -> dict:
    """Один замер для ночного журнала: кто сейчас держит соединения с интернетом.
    Зовётся в фоне раз в несколько минут, пока WhySlow открыт или сидит в трее."""
    now = time.time() if now is None else now
    if idle == -1.0:
        idle = idle_seconds()
    conns = connections(with_hosts=False) if conns is None else conns
    lt = time.localtime(now)
    night = lt.tm_hour in NIGHT_HOURS and (idle is None or idle >= IDLE_FOR_NIGHT)
    date = time.strftime("%Y-%m-%d", lt)
    data = load_log()
    procs = data["procs"]
    by_name: dict[str, list[Conn]] = {}
    for c in conns:
        by_name.setdefault(c.name, []).append(c)
    for name, cs in by_name.items():
        e = procs.setdefault(name, {"exe": "", "samples": 0, "night": 0, "dates": [], "hosts": {},
                                    "first": now, "last": now})
        e["exe"] = cs[0].exe or e.get("exe", "")
        e["samples"] = e.get("samples", 0) + 1
        e["last"] = now
        if night:
            e["night"] = e.get("night", 0) + 1
            dates = e.setdefault("dates", [])
            if date not in dates:
                dates.append(date)
                del dates[:-KEEP_DAYS]
            hosts = e.setdefault("hosts", {})
            for c in cs:
                key = f"{c.ip}:{c.port}"
                hosts[key] = hosts.get(key, 0) + 1
            if len(hosts) > 40:  # держим самые частые, файл не должен расти без конца
                e["hosts"] = dict(sorted(hosts.items(), key=lambda kv: -kv[1])[:40])
    cutoff = now - KEEP_DAYS * 86400
    data["procs"] = {k: v for k, v in procs.items() if v.get("last", 0) >= cutoff}
    data["updated"] = now
    data["samples"] = data.get("samples", 0) + 1
    data["night_samples"] = data.get("night_samples", 0) + (1 if night else 0)
    try:
        write_json(_log_path(), data)
    except OSError:
        pass
    return data


def clear_log() -> None:
    try:
        _log_path().unlink()
    except OSError:
        pass


@dataclass
class NightActivity:
    name: str
    exe: str
    nights: int           # в сколько разных ночей выходила в сеть
    samples: int          # сколько ночных замеров её застали
    hosts: list[tuple[str, int]]   # ("ip:порт", сколько раз)
    last: float
    level: str = INFO
    verdict: Verdict | None = None
    note: str = ""


# Программы, которым ходить в сеть ночью нормально: синхронизация, обновления, браузер с открытыми вкладками
_NIGHT_NORMAL = re.compile(
    r"onedrive|dropbox|googledrive|google drive|yandex\.?disk|icloud|bird|cloudd|nsurlsessiond|"
    r"softwareupdate|wuauclt|usoclient|musnotification|trustedinstaller|msmpeng|mrt\.exe|"
    r"update|updater|chrome|msedge|firefox|safari|opera|brave|yandex|vivaldi|telegram|whatsapp|"
    r"discord|slack|teams|zoom|skype|steam|epicgames|battle\.net|spotify|svchost|systemd|"
    r"mdnsresponder|apsd|trustd|ocspd|timed|ntpd|chronyd", re.IGNORECASE)


def night_report(kb: KnowledgeBase, log: dict | None = None) -> list[NightActivity]:
    """Кто выходил в интернет ночью, пока компьютер стоял без дела."""
    log = load_log() if log is None else log
    out = []
    for name, e in log.get("procs", {}).items():
        if not e.get("night"):
            continue
        v = kb.lookup(name, e.get("exe", ""))
        if v.verdict == "keep":
            continue
        hosts = sorted(e.get("hosts", {}).items(), key=lambda kv: -kv[1])
        nights = len(e.get("dates", []))
        a = NightActivity(name, e.get("exe", ""), nights, e.get("night", 0), hosts, e.get("last", 0), verdict=v)
        pool = any(int(h.rsplit(":", 1)[-1]) in POOL_PORTS for h, _ in hosts if h.rsplit(":", 1)[-1].isdigit())
        if v.verdict == "bad" or pool:
            a.level = BAD
            a.note = v.note if v.verdict == "bad" else tr(
                "Ночью держит соединение на порт майнинг-пула — похоже на скрытый майнер.",
                "Keeps a connection to a mining-pool port at night — looks like a hidden miner.")
        elif v.verdict in ("safe", "optional") or _NIGHT_NORMAL.search(f"{name} {a.exe}"):
            a.level = INFO
            a.note = v.note if v.verdict != "unknown" else tr(
                "Синхронизация, обновления или открытый браузер — ночью это обычно нормально.",
                "Sync, updates or an open browser — usually fine at night.")
        else:
            a.level = WARN
            a.note = tr("Неизвестная программа выходит в интернет, пока за компьютером никого нет. "
                        "Если вы её не узнаёте — посмотрите, куда она подключается, и проверьте антивирусом.",
                        "An unknown program goes online while nobody is at the computer. "
                        "If you don't recognise it, check where it connects and scan it with an antivirus.")
        out.append(a)
    out.sort(key=lambda a: (-RANK[a.level], -a.nights, -a.samples))
    return out


# --------------------------------------------------------- скрытые майнеры ---

@dataclass
class Suspect:
    name: str
    pids: list[int]
    exe: str = ""
    cmdline: str = ""
    cpu: float = 0.0           # % от всего процессора
    gpu: float = 0.0           # % видеокарты (если система отдаёт по процессам)
    reasons: list[str] = field(default_factory=list)
    level: str = WARN
    verdict: Verdict | None = None
    remotes: list[str] = field(default_factory=list)

    def raise_to(self, level: str) -> None:
        if RANK[level] > RANK[self.level]:
            self.level = level


def gpu_by_pid(sample: float = 1.0) -> dict[int, float]:
    """Загрузка видеокарты по процессам. Windows — счётчики «GPU Engine» (как Диспетчер задач),
    NVIDIA — список вычислительных задач nvidia-smi (майнеры как раз такие)."""
    result: dict[int, float] = {}
    if IS_WINDOWS:
        from ..sysinfo import _WinGpuLoad
        g = _WinGpuLoad()
        g.items()
        time.sleep(sample)
        for name, value in g.items() or []:
            m = re.search(r"pid_(\d+)_", name)
            if m:
                pid = int(m.group(1))
                result[pid] = max(result.get(pid, 0.0), value)  # самый загруженный движок
    if shutil.which("nvidia-smi"):
        code, out = run(["nvidia-smi", "--query-compute-apps=pid,used_memory", "--format=csv,noheader,nounits"],
                        timeout=4)
        if code == 0:
            for line in out.splitlines():
                pid = line.split(",")[0].strip()
                if pid.isdigit():
                    result.setdefault(int(pid), 0.0)
                    result[int(pid)] = max(result[int(pid)], 1.0)  # «пользуется вычислениями на GPU»
    return {pid: min(v, 100.0) for pid, v in result.items()}


def _suspicious_place(exe: str) -> bool:
    low = exe.lower().replace("\\", "/")
    return any(s in low for s in ("/appdata/local/temp/", "/appdata/roaming/", "/programdata/", "/tmp/",
                                  "/users/public/", "/var/tmp/", "/dev/shm/"))


def find_miners(kb: KnowledgeBase, sample: float = 3.0, idle: float | None = -1.0,
                progress=lambda _t: None) -> list[Suspect]:
    """Кто похож на майнер: адрес пула в командной строке, соединение на порт пула,
    запись «майнер» в базе, или нагрузка на процессор/видеокарту, пока вы не за компьютером."""
    if idle == -1.0:
        idle = idle_seconds()
    away = idle is not None and idle >= IDLE_FOR_MINER
    me = os.getpid()
    ncpu = psutil.cpu_count() or 1
    procs = []
    for p in psutil.process_iter(["name", "exe", "cmdline"]):
        if p.pid in (0, me):
            continue
        try:
            p.cpu_percent(None)
            procs.append(p)
        except psutil.Error:
            continue
    progress(tr("Замеряю нагрузку процессов…", "Measuring process load…"))
    gpu = gpu_by_pid(min(sample, 1.0)) if sample else {}
    time.sleep(max(sample - (1.0 if IS_WINDOWS else 0.0), 0))
    remotes: dict[int, list[tuple[str, int]]] = {}
    for pid, ip, port in _raw_connections():
        if is_public(ip):
            remotes.setdefault(pid, []).append((ip, port))

    groups: dict[str, Suspect] = {}
    for p in procs:
        try:
            cpu = p.cpu_percent(None) / ncpu
        except psutil.Error:
            continue
        info = p.info
        name = _text(info.get("name") or "?")
        exe = _text(info.get("exe") or "")
        cmd = _text(" ".join(info.get("cmdline") or []))
        s = groups.setdefault(name.lower(), Suspect(name, [], exe, cmd))
        s.pids.append(p.pid)
        s.cpu += cpu
        s.gpu = max(s.gpu, gpu.get(p.pid, 0.0))
        s.exe = s.exe or exe
        if MINER_ARGS.search(cmd) and not MINER_ARGS.search(s.cmdline):
            s.cmdline = cmd
        s.remotes += [f"{ip}:{port}" for ip, port in remotes.get(p.pid, [])]

    out = []
    for s in groups.values():
        v = kb.lookup(s.name, s.exe)
        s.verdict = v
        s.level = INFO
        if v.entry_id == "miner":
            s.reasons.append(tr("Есть в базе как майнер.", "Listed in the database as a miner."))
            s.raise_to(BAD)
        if MINER_ARGS.search(s.cmdline):
            s.reasons.append(tr("В командной строке адрес майнинг-пула или алгоритм майнинга.",
                                "The command line has a mining-pool address or a mining algorithm."))
            s.raise_to(BAD)
        pool = [r for r in s.remotes if int(r.rsplit(":", 1)[1]) in POOL_PORTS]
        if pool:
            s.reasons.append(tr(f"Подключена к порту, на котором обычно работают майнинг-пулы: {pool[0]}.",
                                f"Connected to a port mining pools usually use: {pool[0]}."))
            s.raise_to(BAD if s.cpu >= 15 or s.gpu >= 20 else WARN)
        trusted = v.verdict == "keep"
        if away and s.cpu >= 25 and not trusted:
            mins = int(idle // 60)
            s.reasons.append(tr(f"Грузит процессор на {s.cpu:.0f}%, хотя за компьютером никого нет уже {mins} мин.",
                                f"Loads the CPU at {s.cpu:.0f}% although nobody has used the computer for {mins} min."))
            s.raise_to(WARN)
        if s.gpu >= 30 and not trusted and (away or v.verdict in ("unknown", "bad")):
            s.reasons.append(tr(f"Нагружает видеокарту на {s.gpu:.0f}%" + (", пока вы не за компьютером." if away else
                                " — и это не известная игра или программа."),
                                f"Loads the GPU at {s.gpu:.0f}%" + (" while you're away." if away else
                                " — and it isn't a known game or app.")))
            s.raise_to(WARN)
        if s.reasons and s.exe and _suspicious_place(s.exe) and (s.cpu >= 10 or s.gpu >= 10):
            s.reasons.append(tr("Файл лежит в папке, откуда нормальные программы не запускаются: ",
                                "The file sits in a folder normal programs don't run from: ") + s.exe)
            s.raise_to(BAD)
        if s.reasons:
            out.append(s)
    out.sort(key=lambda s: (-RANK[s.level], -(s.cpu + s.gpu)))
    return out


# ------------------------------------------------------ расширения браузеров ---

@dataclass
class Extension:
    browser: str
    profile: str
    id: str
    name: str
    version: str = ""
    path: str = ""
    enabled: bool = True
    all_sites: bool = False
    permissions: list[str] = field(default_factory=list)
    from_store: bool | None = None   # None — неизвестно
    forced: bool = False             # поставлено принудительно политикой
    sideloaded: bool = False         # поставлено другой программой / вне магазина
    manage_url: str = ""
    level: str = OK
    reasons: list[str] = field(default_factory=list)
    verdict: Verdict | None = None
    policy: list[dict] = field(default_factory=list)  # где записана принудительная установка


# Разрешения, которые дают расширению заметную власть над браузером
RISKY = {
    "webRequest": tr("перехватывать запросы", "intercept requests"),
    "webRequestBlocking": tr("подменять запросы", "rewrite requests"),
    "cookies": tr("читать cookie (входы на сайты)", "read cookies (site logins)"),
    "history": tr("читать историю", "read history"),
    "management": tr("управлять другими расширениями", "manage other extensions"),
    "proxy": tr("пускать трафик через свой прокси", "route traffic through its proxy"),
    "nativeMessaging": tr("общаться с программой на компьютере", "talk to a program on the computer"),
    "debugger": tr("управлять вкладками как отладчик", "control tabs as a debugger"),
    "clipboardRead": tr("читать буфер обмена", "read the clipboard"),
    "downloads": tr("скачивать файлы", "download files"),
    "privacy": tr("менять настройки приватности", "change privacy settings"),
    "searchProvider": tr("менять поиск", "change the search engine"),
}
_ALL_SITES = re.compile(r"^(<all_urls>|\*://\*/\*|https?://\*/\*|\*://\*/|https?://\*/|file:///\*)$")

# Хром и его родственники: (название, папка данных в Windows, в macOS, в Linux, схема адреса настроек)
CHROMIUM = [
    ("Chrome", ("LOCALAPPDATA", "Google/Chrome/User Data"), "Google/Chrome", "google-chrome", "chrome"),
    ("Chromium", ("LOCALAPPDATA", "Chromium/User Data"), "Chromium", "chromium", "chrome"),
    ("Edge", ("LOCALAPPDATA", "Microsoft/Edge/User Data"), "Microsoft Edge", "microsoft-edge", "edge"),
    ("Brave", ("LOCALAPPDATA", "BraveSoftware/Brave-Browser/User Data"), "BraveSoftware/Brave-Browser",
     "BraveSoftware/Brave-Browser", "brave"),
    ("Yandex", ("LOCALAPPDATA", "Yandex/YandexBrowser/User Data"), "Yandex/YandexBrowser", "yandex-browser",
     "browser"),
    ("Vivaldi", ("LOCALAPPDATA", "Vivaldi/User Data"), "Vivaldi", "vivaldi", "vivaldi"),
    ("Opera", ("APPDATA", "Opera Software/Opera Stable"), "com.operasoftware.Opera", "opera", "opera"),
    ("Opera GX", ("APPDATA", "Opera Software/Opera GX Stable"), "com.operasoftware.OperaGX", "", "opera"),
]
# Ключи политик Windows, через которые рекламные программы ставят расширения принудительно
POLICY_KEYS = {
    "Chrome": r"Software\Policies\Google\Chrome",
    "Chromium": r"Software\Policies\Chromium",
    "Edge": r"Software\Policies\Microsoft\Edge",
    "Brave": r"Software\Policies\BraveSoftware\Brave",
    "Yandex": r"Software\Policies\YandexBrowser",
}


def _home() -> Path:
    return Path.home()


def _chromium_roots() -> list[tuple[str, Path, str]]:
    out = []
    for name, (env, win), mac, linux, scheme in CHROMIUM:
        if IS_WINDOWS:
            base = os.environ.get(env)
            root = Path(base) / win if base else None
        elif IS_MAC:
            root = _home() / "Library/Application Support" / mac
        else:
            root = _home() / ".config" / linux if linux else None
        if root and root.is_dir():
            out.append((name, root, scheme))
    return out


def _profiles(root: Path) -> list[Path]:
    if (root / "Extensions").is_dir():  # Opera: профиль прямо в корне
        return [root]
    return sorted(p for p in root.iterdir() if p.is_dir() and (p.name == "Default" or p.name.startswith("Profile "))
                  and (p / "Extensions").is_dir()) if root.is_dir() else []


def _json(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _msg(value: str, ext_dir: Path, manifest: dict) -> str:
    """Название вида __MSG_appName__ берётся из _locales."""
    m = re.fullmatch(r"__MSG_(\w+)__", value or "")
    if not m:
        return value or ""
    key = m.group(1).lower()
    locales = [manifest.get("default_locale", "en"), "en", "en_US", "ru"]
    for loc in dict.fromkeys(locales):
        msgs = _json(ext_dir / "_locales" / str(loc) / "messages.json")
        for k, v in msgs.items():
            if k.lower() == key and isinstance(v, dict) and v.get("message"):
                return str(v["message"])
    return value


def _perms(manifest: dict) -> tuple[list[str], bool]:
    raw = [p for p in manifest.get("permissions", []) + manifest.get("host_permissions", []) if isinstance(p, str)]
    for cs in manifest.get("content_scripts", []) or []:
        if isinstance(cs, dict):
            raw += [m for m in cs.get("matches", []) if isinstance(m, str)]
    all_sites = any(_ALL_SITES.match(p) for p in raw)
    return [p for p in dict.fromkeys(raw) if p in RISKY], all_sites


def _chromium_policy_ids() -> dict[str, list[dict]]:
    """Принудительно установленные расширения: id → где записано (для снятия)."""
    found: dict[str, list[dict]] = {}

    def add(ext_id: str, where: dict):
        if re.fullmatch(r"[a-p]{32}", ext_id):
            found.setdefault(ext_id, []).append(where)

    if IS_WINDOWS:
        import winreg
        for browser, key in POLICY_KEYS.items():
            for hive_name, hive in (("HKCU", winreg.HKEY_CURRENT_USER), ("HKLM", winreg.HKEY_LOCAL_MACHINE)):
                sub = key + r"\ExtensionInstallForcelist"
                try:
                    with winreg.OpenKey(hive, sub) as k:
                        i = 0
                        while True:
                            try:
                                vname, value, vtype = winreg.EnumValue(k, i)
                            except OSError:
                                break
                            i += 1
                            add(str(value).split(";")[0].strip(), {"hive": hive_name, "key": sub, "name": vname,
                                                                    "value": str(value), "type": vtype,
                                                                    "browser": browser})
                except OSError:
                    continue
    elif IS_LINUX:
        for d in ("/etc/opt/chrome/policies/managed", "/etc/chromium/policies/managed",
                  "/etc/opt/edge/policies/managed", "/etc/brave/policies/managed"):
            for f in Path(d).glob("*.json") if Path(d).is_dir() else []:
                for item in _json(f).get("ExtensionInstallForcelist", []) or []:
                    add(str(item).split(";")[0].strip(), {"file": str(f)})
    elif IS_MAC:
        for f in list(Path("/Library/Managed Preferences").glob("*.plist")) + \
                 list(Path("/Library/Managed Preferences").glob("*/*.plist")):
            try:
                data = plistlib.loads(f.read_bytes())
            except (OSError, plistlib.InvalidFileException, ValueError):
                continue
            for item in data.get("ExtensionInstallForcelist", []) or []:
                add(str(item).split(";")[0].strip(), {"file": str(f)})
    return found


# location в Preferences: 1 — магазин, 2/3/6 — поставлено другой программой, 4 — распакованное,
# 5/10 — встроенное в браузер, 7/9 — политика
_LOC_EXTERNAL = {2, 3, 6}
_LOC_POLICY = {7, 9}
_LOC_COMPONENT = {5, 10}


def _chromium_extensions() -> list[Extension]:
    out = []
    policy = _chromium_policy_ids()
    for browser, root, scheme in _chromium_roots():
        for prof in _profiles(root):
            settings: dict = {}
            for fname in ("Preferences", "Secure Preferences"):
                s = _json(prof / fname).get("extensions", {}).get("settings", {})
                if isinstance(s, dict):
                    for k, v in s.items():
                        if isinstance(v, dict):
                            settings.setdefault(k, {}).update(v)
            pname = _json(prof / "Preferences").get("profile", {}).get("name") or prof.name
            for ext_root in (prof / "Extensions").iterdir():
                if not ext_root.is_dir():
                    continue
                versions = sorted((d for d in ext_root.iterdir() if (d / "manifest.json").is_file()),
                                  key=lambda d: d.stat().st_mtime, reverse=True)
                if not versions:
                    continue
                d = versions[0]
                manifest = _json(d / "manifest.json")
                st = settings.get(ext_root.name, {})
                loc = st.get("location")
                if loc in _LOC_COMPONENT or "theme" in manifest or not manifest.get("name"):
                    continue
                perms, all_sites = _perms(manifest)
                enabled = not st.get("disable_reasons") and st.get("state", 1) != 0
                ext = Extension(browser, str(pname), ext_root.name, _msg(manifest["name"], d, manifest),
                                str(manifest.get("version", "")), str(d), enabled, all_sites, perms,
                                from_store=st.get("from_webstore") if "from_webstore" in st else None,
                                forced=loc in _LOC_POLICY or ext_root.name in policy,
                                sideloaded=loc in _LOC_EXTERNAL or loc == 4,
                                manage_url=f"{scheme}://extensions/?id={ext_root.name}",
                                policy=policy.get(ext_root.name, []))
                out.append(ext)
    # Политика есть, а самого расширения (ещё) нет — тоже показываем: оно поставится при следующем запуске
    seen = {e.id for e in out}
    for ext_id, where in policy.items():
        if ext_id not in seen:
            browser = where[0].get("browser", "Chrome")
            out.append(Extension(browser, "", ext_id, tr("(ещё не скачано) ", "(not downloaded yet) ") + ext_id,
                                 forced=True, policy=where, manage_url=f"chrome://extensions/?id={ext_id}"))
    return out


def _firefox_roots() -> list[Path]:
    if IS_WINDOWS:
        base = os.environ.get("APPDATA")
        return [Path(base) / "Mozilla/Firefox/Profiles"] if base else []
    if IS_MAC:
        return [_home() / "Library/Application Support/Firefox/Profiles"]
    return [_home() / ".mozilla/firefox", _home() / "snap/firefox/common/.mozilla/firefox"]


def _firefox_extensions() -> list[Extension]:
    out = []
    for root in _firefox_roots():
        if not root.is_dir():
            continue
        for prof in root.iterdir():
            data = _json(prof / "extensions.json")
            for a in data.get("addons", []) or []:
                if not isinstance(a, dict) or a.get("type") != "extension":
                    continue
                if a.get("location") in ("app-builtin", "app-system-defaults", "app-system-addons") or \
                        a.get("isSystem") or a.get("isBuiltin"):
                    continue
                loc = a.get("defaultLocale") or {}
                up = a.get("userPermissions") or {}
                perms_raw = [p for p in (up.get("permissions") or []) + (up.get("origins") or []) if isinstance(p, str)]
                all_sites = any(_ALL_SITES.match(p) for p in perms_raw)
                src = str(a.get("sourceURI") or "")
                ext = Extension("Firefox", prof.name.split(".", 1)[-1], str(a.get("id", "")),
                                str(loc.get("name") or a.get("id", "?")), str(a.get("version", "")),
                                str(a.get("path", "")), bool(a.get("active", True)), all_sites,
                                [p for p in dict.fromkeys(perms_raw) if p in RISKY],
                                from_store=("addons.mozilla.org" in src) if src else None,
                                sideloaded=bool(a.get("foreignInstall")),
                                manage_url="about:addons")
                if isinstance(a.get("signedState"), int) and a["signedState"] <= 0:
                    ext.reasons.append(tr("Не подписано Mozilla.", "Not signed by Mozilla."))
                out.append(ext)
    return out


def _safari_extensions() -> list[Extension]:
    if not IS_MAC:
        return []
    out = []
    for proto in ("com.apple.Safari.web-extension", "com.apple.Safari.extension",
                  "com.apple.Safari.content-blocker"):
        code, text = run(["pluginkit", "-mAvvv", "-p", proto], timeout=8)
        if code != 0:
            continue
        cur: Extension | None = None
        for line in text.splitlines():
            m = re.match(r"^([+\-!=? ])\s+(\S+)\((.*)\)\s*$", line)
            if m:
                cur = Extension("Safari", "", m.group(2), m.group(2), m.group(3),
                                enabled=m.group(1) != "-", manage_url="safari-settings")
                if proto.endswith("content-blocker"):
                    cur.level = OK  # блокировщики контента не видят страницы
                out.append(cur)
                continue
            kv = re.match(r"^\s+([\w ]+?)\s=\s(.*)$", line)
            if cur and kv:
                key, val = kv.group(1).strip(), kv.group(2).strip()
                if key == "Display Name" and val:
                    cur.name = val
                elif key == "Path":
                    cur.path = val
    return out


def find_extensions(kb: KnowledgeBase) -> list[Extension]:
    exts = []
    for fn in (_chromium_extensions, _firefox_extensions, _safari_extensions):
        try:
            exts += fn()
        except OSError:
            continue
    for e in exts:
        rate(e, kb)
    exts.sort(key=lambda e: (-RANK[e.level], e.browser, e.name.lower()))
    return exts


def rate(e: Extension, kb: KnowledgeBase) -> None:
    """Оценка опасности расширения и объяснение простыми словами."""
    v = kb.lookup(e.name, e.id)
    e.verdict = v
    level = OK
    if v.verdict == "bad":
        e.reasons.insert(0, v.note)
        level = BAD
    if e.forced:
        e.reasons.append(tr("Поставлено принудительно через политику браузера — удалить его из браузера нельзя. "
                            "На рабочем компьютере так делает администратор, на домашнем — обычно рекламная программа.",
                            "Force-installed through a browser policy, so the browser won't let you remove it. "
                            "On a work computer that's the admin; on a home computer it's usually adware."))
        level = max(level, WARN, key=RANK.get)
    if e.sideloaded:
        e.reasons.append(tr("Поставлено не вами из магазина, а другой программой.",
                            "Installed by another program, not by you from the store."))
        level = max(level, WARN if e.all_sites or e.permissions else INFO, key=RANK.get)
    if e.from_store is False and not e.forced and e.browser != "Safari":
        e.reasons.append(tr("Не из магазина расширений.", "Not from the extension store."))
        level = max(level, INFO, key=RANK.get)
    if e.all_sites:
        what = ", ".join(RISKY[p] for p in e.permissions)
        if what:
            e.reasons.append(tr(f"Видит все сайты и может: {what}.", f"Sees every site and can: {what}."))
            level = max(level, WARN if (e.sideloaded or e.forced or v.verdict == "unknown") else INFO, key=RANK.get)
        else:
            e.reasons.append(tr("Видит содержимое всех сайтов. Нормально для блокировщиков рекламы, переводчиков и "
                                "менеджеров паролей, но больше никому это не нужно.",
                                "Sees the contents of every site. Normal for ad blockers, translators and password "
                                "managers, but nothing else needs it."))
            level = max(level, INFO, key=RANK.get)
    elif e.permissions:
        e.reasons.append(tr("Может: ", "Can: ") + ", ".join(RISKY[p] for p in e.permissions) + ".")
        level = max(level, INFO, key=RANK.get)
    if v.verdict in ("keep", "safe") and level != BAD:
        level = OK if not e.forced else level
        e.reasons.insert(0, v.note)
    if any("Mozilla" in r for r in e.reasons):
        level = max(level, WARN, key=RANK.get)
    e.level = level


# ---------------------------------------------------------------- отчёт ---

@dataclass
class Report:
    miners: list[Suspect]
    extensions: list[Extension]
    night: list[NightActivity]
    conns: list[Conn]
    idle: float | None
    took: float
    log_samples: int = 0
    log_night_samples: int = 0

    @property
    def worst(self) -> str:
        levels = [s.level for s in self.miners] + [e.level for e in self.extensions] + [n.level for n in self.night]
        return max(levels, key=RANK.get, default=OK)

    def headline(self) -> str:
        bad = sum(1 for x in self.miners + self.extensions + self.night if x.level == BAD)
        warn = sum(1 for x in self.miners + self.extensions + self.night if x.level == WARN)
        if bad:
            return tr(f"Найдено опасное: {bad}, подозрительное: {warn}", f"Dangerous: {bad}, suspicious: {warn}")
        if warn:
            return tr(f"Подозрительное: {warn} — стоит посмотреть", f"Suspicious: {warn} — worth a look")
        return tr("Тихих вредителей не найдено", "No hidden pests found")


def scan(kb: KnowledgeBase, progress=lambda _t: None, sample: float = 3.0) -> Report:
    t0 = time.time()
    idle = idle_seconds()
    progress(tr("Ищу скрытые майнеры…", "Looking for hidden miners…"))
    miners = find_miners(kb, sample=sample, idle=idle, progress=progress)
    progress(tr("Проверяю расширения браузеров…", "Checking browser extensions…"))
    exts = find_extensions(kb)
    progress(tr("Смотрю, кто сейчас в сети…", "Checking who is online right now…"))
    conns = connections()
    log = record(idle=idle, conns=conns)
    night = night_report(kb, log)
    return Report(miners, exts, night, conns, idle, time.time() - t0,
                  log.get("samples", 0), log.get("night_samples", 0))


# -------------------------------------------------------------- действия ---

class ActionError(Exception):
    pass


def _quarantine_dir() -> Path:
    d = app_dir() / "quarantine"
    d.mkdir(parents=True, exist_ok=True)
    return d


def quarantine(path: str) -> Path:
    """Убирает файл программы в карантин WhySlow (не удаляет). Вернуть — из «Журнала».
    Процесс перед этим должен быть закрыт."""
    src = Path(path)
    if not src.is_file():
        raise ActionError(tr("Файл не найден", "File not found") + f": {path}")
    dst = _quarantine_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}_{src.name}.quarantine"
    try:
        shutil.move(str(src), str(dst))
    except OSError as e:
        raise ActionError(tr(f"Не удалось переместить файл: {e}", f"Couldn't move the file: {e}")) from e
    journal.add("pest_quarantine", tr(f"В карантин: {src.name}", f"Quarantined: {src.name}"),
                {"src": str(src), "dst": str(dst)})
    return dst


def remove_policy(ext: Extension) -> int:
    """Снимает принудительную установку расширения (Windows, ключ политики). Возвращает число снятых записей."""
    if not IS_WINDOWS:
        raise ActionError(by_os("", tr("В macOS политику ставит профиль: Системные настройки → Основные → "
                                       "Управление устройством (Профили) — удалите там незнакомый профиль.",
                                       "On macOS the policy comes from a profile: System Settings → General → "
                                       "Device Management (Profiles) — remove the unfamiliar profile there."),
                                tr("Удалите файл политики (нужны права root): ",
                                   "Delete the policy file (root required): ")
                                + ", ".join(w.get("file", "") for w in ext.policy)))
    import winreg
    done = 0
    for w in ext.policy:
        hive = winreg.HKEY_CURRENT_USER if w["hive"] == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        try:
            with winreg.OpenKey(hive, w["key"], 0, winreg.KEY_SET_VALUE) as k:
                winreg.DeleteValue(k, w["name"])
        except PermissionError as e:
            raise ActionError(tr("Политика записана для всего компьютера — перезапустите WhySlow от администратора.",
                                 "The policy is set machine-wide — restart WhySlow as administrator.")) from e
        except OSError:
            continue
        done += 1
        journal.add("pest_policy", tr(f"Снята принудительная установка расширения {ext.name} ({ext.browser})",
                                      f"Removed forced install of extension {ext.name} ({ext.browser})"), dict(w))
    return done


def _browser_cmd(browser: str) -> list[str]:
    if IS_MAC:
        app = {"Chrome": "Google Chrome", "Edge": "Microsoft Edge", "Brave": "Brave Browser",
               "Yandex": "Yandex", "Opera GX": "Opera GX"}.get(browser, browser)
        return ["open", "-a", app, "--args"]
    if IS_WINDOWS:
        rel = {"Chrome": r"Google\Chrome\Application\chrome.exe", "Edge": r"Microsoft\Edge\Application\msedge.exe",
               "Brave": r"BraveSoftware\Brave-Browser\Application\brave.exe",
               "Yandex": r"Yandex\YandexBrowser\Application\browser.exe", "Vivaldi": r"Vivaldi\Application\vivaldi.exe",
               "Firefox": r"Mozilla Firefox\firefox.exe", "Chromium": r"Chromium\Application\chrome.exe"}.get(browser)
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            if rel and base and Path(base, rel).is_file():
                return [str(Path(base, rel))]
        if browser.startswith("Opera") and os.environ.get("LOCALAPPDATA"):
            exe = Path(os.environ["LOCALAPPDATA"], "Programs", "Opera GX" if "GX" in browser else "Opera", "opera.exe")
            if exe.is_file():
                return [str(exe)]
        return []
    for name in {"Chrome": ["google-chrome", "google-chrome-stable"], "Chromium": ["chromium", "chromium-browser"],
                 "Edge": ["microsoft-edge"], "Brave": ["brave-browser", "brave"], "Yandex": ["yandex-browser"],
                 "Vivaldi": ["vivaldi"], "Opera": ["opera"], "Firefox": ["firefox"]}.get(browser, []):
        if shutil.which(name):
            return [name]
    return []


def open_manager(ext: Extension) -> bool:
    """Открывает страницу расширения в его браузере, где его можно удалить.
    Возвращает False, если браузер запустить не удалось (адрес тогда вставляют вручную)."""
    if ext.browser == "Safari":
        run(["open", "-a", "Safari"], timeout=5)
        return False  # настройки расширений Safari открываются только из меню
    cmd = _browser_cmd(ext.browser)
    if not cmd:
        return False
    try:
        kwargs = {"creationflags": 0x08000000} if IS_WINDOWS else {}
        subprocess.Popen(cmd + [ext.manage_url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kwargs)
    except OSError:
        return False
    return True


def manager_hint(ext: Extension) -> str:
    if ext.browser == "Safari":
        return tr("Safari → Настройки → Расширения: снимите галочку или удалите программу, которая его принесла "
                  f"({ext.path.split('/Contents/')[0] if ext.path else ext.id}).",
                  "Safari → Settings → Extensions: untick it, or delete the app that brought it "
                  f"({ext.path.split('/Contents/')[0] if ext.path else ext.id}).")
    return tr(f"Откройте в {ext.browser} адрес {ext.manage_url} и нажмите «Удалить».",
              f"Open {ext.manage_url} in {ext.browser} and click “Remove”.")


# ------------------------------------------------------------------ окно ---

try:
    from PySide6.QtCore import QPointF, QRectF, Qt
    from PySide6.QtGui import QColor, QFontMetrics, QPainter, QPen
    from PySide6.QtWidgets import (
        QApplication, QFrame, QHBoxLayout, QHeaderView, QLabel, QMessageBox, QPushButton, QTableWidget,
        QTableWidgetItem, QTabWidget, QVBoxLayout, QWidget,
    )

    from .. import bigfiles, startup
    from ..gui import C, _esc, accent, confirm, make_table, muted, online_note, pt, px, run_async
    from ..processes import ProcGroup, kill
    from ..util import ADMIN_HINT, human_duration
except ImportError:  # текстовый режим без Qt: нужна только проверка, вкладка не строится
    QWidget = object
    C = {OK: "", INFO: "", WARN: "", BAD: ""}


def build(main) -> "PestsTab":
    """Вкладка «Вредители» для whyslow.premium / PremiumGate."""
    return PestsTab(main)


PEST_COLORS = {OK: C["ok"], INFO: C["info"], WARN: C["warn"], BAD: C["bad"]}
PEST_LABELS = {OK: tr("Порядок", "Fine"), INFO: tr("Для сведения", "FYI"),
               WARN: tr("Подозрительно", "Suspicious"), BAD: tr("Опасно", "Dangerous")}


def _level_item(level: str) -> QTableWidgetItem:
    it = QTableWidgetItem(PEST_LABELS[level])
    it.setForeground(QColor(PEST_COLORS[level]))
    return it


class ConnMap(QWidget):
    """Карта соединений: в центре компьютер, вокруг — программы, снаружи — куда они подключены.
    Красным — то, что похоже на майнер или вредителя, жёлтым — выходившее в сеть ночью."""

    MAX_PROCS = 14
    MAX_HOSTS = 6

    def __init__(self, on_pick):
        super().__init__()
        self.on_pick = on_pick
        self.conns: list[Conn] = []
        self.marks: dict[str, str] = {}   # имя программы → уровень
        self.nodes: list[tuple[QPointF, float, str, object]] = []
        self.setMinimumHeight(px(320))
        self.setCursor(Qt.PointingHandCursor)

    def set_data(self, conns: list[Conn], marks: dict[str, str]):
        self.conns, self.marks = conns, marks
        self.update()

    def _level(self, name: str, conn: Conn | None = None) -> str:
        if conn is not None and conn.pool_port:
            return BAD
        return self.marks.get(name.lower(), OK)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        center = QPointF(w / 2, h / 2)
        r_out = min(w, h) / 2 - px(46)
        r_in = r_out * 0.48
        self.nodes = []
        by_proc: dict[str, list[Conn]] = {}
        for c in self.conns:
            by_proc.setdefault(c.name, []).append(c)
        procs = sorted(by_proc.items(), key=lambda kv: (-RANK[self._level(kv[0])], -len(kv[1])))[:self.MAX_PROCS]
        f = p.font()
        f.setPointSizeF(f.pointSizeF() * 0.85)
        p.setFont(f)
        if not procs:
            p.setPen(QColor(C["muted"]))
            p.drawText(self.rect(), Qt.AlignCenter, tr("Нет соединений с интернетом", "No internet connections"))
            return
        sector = 2 * math.pi / len(procs)
        for i, (name, cs) in enumerate(procs):
            a = -math.pi / 2 + i * sector
            pp = QPointF(center.x() + r_in * math.cos(a), center.y() + r_in * math.sin(a))
            plevel = max([self._level(name)] + [self._level(name, c) for c in cs], key=RANK.get)
            hosts: dict[str, Conn] = {}
            for c in cs:
                hosts.setdefault(c.host or c.ip, c)
            shown = list(hosts.items())[:self.MAX_HOSTS]
            for j, (label, c) in enumerate(shown):
                b = a + (j - (len(shown) - 1) / 2) * min(sector / max(len(shown), 1), 0.22)
                hp = QPointF(center.x() + r_out * math.cos(b), center.y() + r_out * math.sin(b))
                color = QColor(PEST_COLORS[self._level(name, c)] if self._level(name, c) != OK else C["accent2"])
                color.setAlpha(110)
                p.setPen(QPen(color, 1.2))
                p.drawLine(pp, hp)
                p.setPen(Qt.NoPen)
                color.setAlpha(230)
                p.setBrush(color)
                p.drawEllipse(hp, px(4), px(4))
                self.nodes.append((hp, px(8), "host", (name, label, c)))
                short = label if len(label) <= 26 else "…" + label[-25:]
                p.setPen(QColor(C["muted"]))
                tw = QFontMetrics(p.font()).horizontalAdvance(short)
                tx = hp.x() + px(7) if math.cos(b) >= 0 else hp.x() - px(7) - tw
                p.drawText(QPointF(max(2, min(tx, w - tw - 2)), hp.y() + px(4)), short)
            if len(hosts) > self.MAX_HOSTS:
                p.setPen(QColor(C["muted"]))
                op = QPointF(center.x() + (r_out + px(22)) * math.cos(a), center.y() + (r_out + px(22)) * math.sin(a))
                p.drawText(op, f"+{len(hosts) - self.MAX_HOSTS}")
            p.setPen(QPen(QColor(255, 255, 255, 40), 1.5))
            p.drawLine(center, pp)
            col = QColor(PEST_COLORS[plevel] if plevel != OK else C["accent"])
            p.setPen(QPen(QColor("white"), 1))
            p.setBrush(col)
            rad = px(7) + min(len(cs), 10) * 0.6
            p.drawEllipse(pp, rad, rad)
            self.nodes.append((pp, rad + px(4), "proc", (name, cs)))
            p.setPen(QColor(C["text"]))
            tw = QFontMetrics(p.font()).horizontalAdvance(name)
            p.drawText(QPointF(pp.x() - tw / 2, pp.y() - rad - px(4)), name)
        p.setPen(QPen(QColor("white"), 1.5))
        p.setBrush(QColor(C["panel2"]))
        p.drawEllipse(center, px(18), px(18))
        p.setPen(QColor("white"))
        p.drawText(QRectF(center.x() - px(40), center.y() + px(20), px(80), px(20)), Qt.AlignCenter,
                   tr("Этот ПК", "This PC"))

    def mousePressEvent(self, e):
        pos = e.position() if hasattr(e, "position") else QPointF(e.pos())
        for pt_, rad, kind, data in reversed(self.nodes):
            if (pt_.x() - pos.x()) ** 2 + (pt_.y() - pos.y()) ** 2 <= rad * rad:
                self.on_pick(kind, data)
                return


class PestsTab(QWidget):
    """Скрытые майнеры, опасные расширения браузеров и программы, которые выходят в сеть по ночам."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.report: Report | None = None
        self._picked = None  # точка, выбранная на карте
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))

        head = QHBoxLayout()
        text = QVBoxLayout()
        text.setSpacing(px(4))
        title = QLabel(accent(tr("Тихие вредители", "Hidden pests")))
        title.setObjectName("h1")
        text.addWidget(title)
        text.addWidget(muted(tr(
            "Ищет то, что не бросается в глаза: майнеры, которые грузят процессор и видеокарту, пока вы отошли; "
            "расширения браузера, которые видят все сайты; программы, которые выходят в интернет по ночам. "
            "Ничего не удаляется без вашего подтверждения.",
            "Finds what doesn't catch the eye: miners that load the CPU and GPU while you're away, browser "
            "extensions that see every site, programs that go online at night. Nothing is removed without "
            "your confirmation.")))
        head.addLayout(text, 1)
        self.b_scan = QPushButton(tr("Проверить", "Scan"))
        self.b_scan.setObjectName("primary")
        self.b_scan.setCursor(Qt.PointingHandCursor)
        self.b_scan.clicked.connect(self.scan)
        head.addWidget(self.b_scan, 0, Qt.AlignVCenter)
        lay.addLayout(head)
        self.summary = muted()
        self.summary.setTextFormat(Qt.RichText)
        lay.addWidget(self.summary)

        body = QHBoxLayout()
        body.setSpacing(px(14))
        self.pages = QTabWidget()
        self.t_miners = make_table([tr("Программа", "Program"), tr("ЦП", "CPU"), "GPU", tr("Оценка", "Rating")])
        self.t_ext = make_table([tr("Расширение", "Extension"), tr("Браузер", "Browser"), tr("Профиль", "Profile"),
                                 tr("Оценка", "Rating")])
        self.t_night = make_table([tr("Программа", "Program"), tr("Ночей", "Nights"), tr("Куда", "Where to"),
                                   tr("Оценка", "Rating")])
        for t in (self.t_miners, self.t_ext, self.t_night):
            hh = t.horizontalHeader()
            hh.setSectionResizeMode(QHeaderView.ResizeToContents)
            hh.setSectionResizeMode(0, QHeaderView.Stretch)
            t.itemSelectionChanged.connect(self.on_select)
        self.t_night.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.map = ConnMap(self.on_map_pick)
        self.pages.addTab(self.t_miners, tr("Майнеры", "Miners"))
        self.pages.addTab(self.t_ext, tr("Расширения", "Extensions"))
        self.pages.addTab(self.t_night, tr("Ночью в сети", "Online at night"))
        self.pages.addTab(self.map, tr("Карта соединений", "Connection map"))
        self.pages.currentChanged.connect(lambda _i: self.on_select())
        body.addWidget(self.pages, 5)

        side = QFrame()
        side.setObjectName("card")
        sl = QVBoxLayout(side)
        sl.setContentsMargins(px(14), px(12), px(14), px(12))
        sl.setSpacing(px(8))
        self.d_title = QLabel()
        self.d_title.setTextFormat(Qt.PlainText)
        self.d_title.setWordWrap(True)
        self.d_title.setStyleSheet(f"font-size: {pt(12)}; font-weight: 600;")
        sl.addWidget(self.d_title)
        self.d_text = muted()
        self.d_text.setTextFormat(Qt.RichText)
        self.d_text.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sl.addWidget(self.d_text)
        self.b_kill = QPushButton(tr("Закрыть программу", "Close program"))
        self.b_kill.setObjectName("danger")
        self.b_kill.clicked.connect(self.kill)
        self.b_autostart = QPushButton(tr("Убрать из автозагрузки", "Remove from startup"))
        self.b_autostart.clicked.connect(self.disable_autostart)
        self.b_quarantine = QPushButton(tr("Закрыть и убрать файл в карантин", "Close and quarantine the file"))
        self.b_quarantine.setObjectName("danger")
        self.b_quarantine.clicked.connect(self.quarantine)
        self.b_manage = QPushButton(tr("Открыть в браузере, чтобы удалить", "Open in the browser to remove"))
        self.b_manage.clicked.connect(self.open_manager)
        self.b_policy = QPushButton(tr("Снять принудительную установку", "Remove the forced install"))
        self.b_policy.setObjectName("danger")
        self.b_policy.clicked.connect(self.remove_policy)
        self.b_reveal = QPushButton(tr("Показать файл", "Show file"))
        self.b_reveal.clicked.connect(self.reveal)
        self.b_clear = QPushButton(tr("Очистить ночной журнал", "Clear the night log"))
        self.b_clear.clicked.connect(self.clear_log)
        self.actions = [self.b_kill, self.b_autostart, self.b_quarantine, self.b_manage, self.b_policy, self.b_reveal,
                        self.b_clear]
        for b in self.actions:
            sl.addWidget(b)
        sl.addStretch()
        body.addWidget(side, 3)
        lay.addLayout(body, 1)
        self.refill()

    def activate(self):
        if self.report is None:
            self.scan()

    # --- проверка ---

    def scan(self):
        if not unlocked():
            return
        self.b_scan.setEnabled(False)
        self.summary.setText(tr("Проверяю… это займёт несколько секунд.", "Scanning… this takes a few seconds."))

        def done(r):
            self.report = r
            self.b_scan.setEnabled(True)
            self.refill()

        def failed(e):
            self.b_scan.setEnabled(True)
            self.summary.setText(tr("Проверка не удалась: ", "The scan failed: ") + _esc(e))

        run_async(lambda p: scan(self.main.kb, p), done, failed, self.summary.setText)

    def refill(self):
        r = self.report
        if r is None:
            self.summary.setText(tr("Нажмите «Проверить».", "Click “Scan”."))
            self.on_select()
            return
        color = PEST_COLORS[r.worst]
        parts = [tr(f"соединений сейчас: {len(r.conns)}", f"connections now: {len(r.conns)}")]
        if r.idle and r.idle >= 60:
            parts.append(tr(f"вы не трогали компьютер {human_duration(r.idle)}", f"idle for {human_duration(r.idle)}"))
        parts.append(tr(f"ночной журнал: {r.log_night_samples} ночных замеров из {r.log_samples}",
                        f"night log: {r.log_night_samples} night samples of {r.log_samples}")
                     if r.log_night_samples else
                     tr("ночных замеров пока нет — оставьте WhySlow на ночь в трее, и здесь появится, кто выходит "
                        "в сеть, пока вы спите",
                        "no night samples yet — leave WhySlow in the tray overnight to see who goes online while "
                        "you sleep"))
        self.summary.setText(f"<b style='color:{color}'>{_esc(r.headline())}</b> · " + _esc(" · ".join(parts)))
        self._fill(self.t_miners, [[s.name, f"{s.cpu:.0f}%", f"{s.gpu:.0f}%" if s.gpu else "—", s.level]
                                   for s in r.miners])
        self._fill(self.t_ext, [[e.name + ("" if e.enabled else tr(" (выключено)", " (disabled)")), e.browser,
                                 e.profile, e.level] for e in r.extensions])
        self._fill(self.t_night, [[n.name, str(n.nights), ", ".join(h for h, _ in n.hosts[:3]), n.level]
                                  for n in r.night])
        self.pages.setTabText(0, tr("Майнеры", "Miners") + (f" · {len(r.miners)}" if r.miners else ""))
        risky = sum(e.level in (WARN, BAD) for e in r.extensions)
        self.pages.setTabText(1, tr("Расширения", "Extensions") + f" · {len(r.extensions)}"
                              + (f" ({risky}!)" if risky else ""))
        self.pages.setTabText(2, tr("Ночью в сети", "Online at night") + (f" · {len(r.night)}" if r.night else ""))
        marks = {}
        for s in r.miners:
            marks[s.name.lower()] = s.level
        for n in r.night:
            if RANK[n.level] >= RANK[WARN]:
                marks[n.name.lower()] = max(n.level, marks.get(n.name.lower(), OK), key=RANK.get)
        self.map.set_data(r.conns, marks)
        self.on_select()

    @staticmethod
    def _fill(table: QTableWidget, rows: list[list[str]]):
        table.setRowCount(len(rows))
        for i, row in enumerate(rows):
            for c, text in enumerate(row[:-1]):
                table.setItem(i, c, QTableWidgetItem(text))
            table.setItem(i, len(row) - 1, _level_item(row[-1]))

    # --- выбор и подробности ---

    def _current(self):
        if not self.report:
            return None, None
        page = self.pages.currentIndex()
        table, items = {0: (self.t_miners, self.report.miners), 1: (self.t_ext, self.report.extensions),
                        2: (self.t_night, self.report.night)}.get(page, (None, None))
        if table is None:
            return "map", self._picked
        rows = table.selectionModel().selectedRows()
        if not rows or rows[0].row() >= len(items):
            return None, None
        return ("miner", "ext", "night")[page], items[rows[0].row()]

    def _autostart(self, name: str, exe: str) -> list:
        return startup.find_for_process(self.main.startup_tab.items, name, exe)

    def on_select(self):
        kind, obj = self._current()
        for b in self.actions:
            b.setVisible(False)
        self.b_clear.setVisible(self.pages.currentIndex() == 2)
        if obj is None:
            self.d_title.setText("")
            hint = {0: tr("Майнеры прячутся: работают, когда вы отошли, и замолкают, когда открываете Диспетчер "
                          "задач. Проверьте ещё раз, вернувшись к компьютеру через 5–10 минут — WhySlow учтёт, "
                          "сколько он простоял без дела.",
                          "Miners hide: they work when you're away and go quiet when you open Task Manager. "
                          "Scan again after leaving the computer alone for 5–10 minutes — WhySlow takes idle time "
                          "into account."),
                    1: tr("Выберите расширение, чтобы увидеть, что оно может.",
                          "Pick an extension to see what it can do."),
                    2: tr("Пока WhySlow в трее, он раз в 5 минут записывает, кто держит соединения с интернетом. "
                          "Ночью (с 0 до 6) это засчитывается, только если за компьютером никого нет.",
                          "While WhySlow is in the tray, it notes every 5 minutes who holds internet connections. "
                          "At night (0–6 am) this only counts if nobody is at the computer."),
                    3: tr("Нажмите на точку: в центре — компьютер, вокруг — программы, снаружи — куда они подключены. "
                          "Красное похоже на майнер или вредителя, жёлтое — выходило в сеть ночью.",
                          "Click a dot: the computer is in the middle, programs around it, and where they connect on "
                          "the outside. Red looks like a miner or pest, yellow went online at night.")}
            self.d_text.setText(hint.get(self.pages.currentIndex(), ""))
            return
        if kind == "miner":
            self._show_proc(obj.name, obj.exe, obj.level, obj.reasons, obj.verdict, obj.remotes)
            self.b_kill.setVisible(True)
            self.b_quarantine.setVisible(bool(obj.exe) and obj.level == BAD)
        elif kind == "night":
            hosts = [f"{h} ×{n}" for h, n in obj.hosts[:8]]
            self._show_proc(obj.name, obj.exe, obj.level, [obj.note], obj.verdict, hosts,
                            extra=tr(f"Выходила в сеть ночью: {obj.nights} ноч., замеров: {obj.samples}.",
                                     f"Went online at night: {obj.nights} night(s), samples: {obj.samples}."))
            self.b_kill.setVisible(self._running(obj.name))
        elif kind == "ext":
            self.d_title.setText(f"{obj.name} — {obj.browser}")
            lines = [f"<b style='color:{PEST_COLORS[obj.level]}'>{_esc(PEST_LABELS[obj.level])}</b>"]
            lines += [_esc(r) for r in obj.reasons if r]
            if not obj.reasons:
                lines.append(tr("Обычных прав, ничего подозрительного.", "Ordinary permissions, nothing suspicious."))
            meta = [f"ID: {obj.id}"] + ([tr("версия ", "version ") + obj.version] if obj.version else [])
            lines.append(f"<span style='color:{C['muted']}'>{_esc(' · '.join(meta))}</span>")
            self.d_text.setText("<br>".join(lines))
            self.b_manage.setVisible(bool(obj.manage_url))
            self.b_policy.setVisible(bool(obj.policy))
            self.b_reveal.setVisible(bool(obj.path))
        elif kind == "map":
            self._show_map(obj)

    def _show_proc(self, name, exe, level, reasons, verdict, remotes, extra=""):
        self.d_title.setText(name)
        lines = [f"<b style='color:{PEST_COLORS[level]}'>{_esc(PEST_LABELS[level])}</b>"]
        lines += [_esc(r) for r in reasons if r]
        if extra:
            lines.append(_esc(extra))
        if verdict and verdict.verdict != "unknown" and verdict.note not in reasons:
            lines.append(f"<i>{_esc(verdict.title or name)}</i>: {_esc(verdict.note)}")
        elif verdict and verdict.verdict == "unknown":
            lines.append(online_note(name, exe, self.on_select))
        if remotes:
            lines.append(tr("Подключения: ", "Connections: ") + _esc(", ".join(remotes[:8])))
        if exe:
            lines.append(f"<span style='color:{C['muted']}'>{_esc(exe)}</span>")
        self.d_text.setText("<br>".join(lines))
        self.b_autostart.setVisible(bool(self._autostart(name, exe)))
        self.b_reveal.setVisible(bool(exe))

    def _show_map(self, picked):
        if not picked:
            return
        kind, data = picked
        if kind == "proc":
            name, cs = data
            hosts = sorted({c.host or c.ip for c in cs})
            v = self.main.kb.lookup(name, cs[0].exe)
            self.d_title.setText(name)
            lines = [tr(f"Соединений: {len(cs)}", f"Connections: {len(cs)}")]
            if v.verdict != "unknown":
                lines.append(f"<i>{_esc(v.title or name)}</i>: {_esc(v.note)}")
            lines.append(_esc(", ".join(hosts[:12])) + (" …" if len(hosts) > 12 else ""))
            if cs[0].exe:
                lines.append(f"<span style='color:{C['muted']}'>{_esc(cs[0].exe)}</span>")
            self.d_text.setText("<br>".join(lines))
            self.b_kill.setVisible(True)
        else:
            name, label, c = data
            self.d_title.setText(label)
            lines = [f"{_esc(name)} → {_esc(c.ip)}:{c.port}"]
            if c.host:
                lines.append(_esc(c.host))
            if c.pool_port:
                lines.append(f"<b style='color:{C['bad']}'>" + tr("Порт, на котором обычно работают майнинг-пулы.",
                                                                 "A port mining pools usually use.") + "</b>")
            self.d_text.setText("<br>".join(lines))

    def on_map_pick(self, kind, data):
        self._picked = (kind, data)
        self.on_select()

    @staticmethod
    def _running(name: str) -> bool:
        return any((p.info.get("name") or "").lower() == name.lower() for p in psutil.process_iter(["name"]))

    def _target(self) -> tuple[str, str]:
        kind, obj = self._current()
        if kind == "map" and obj and obj[0] == "proc":
            return obj[1][0], obj[1][1][0].exe
        if kind in ("miner", "night"):
            return obj.name, obj.exe
        if kind == "ext":
            return obj.name, obj.path
        return "", ""

    # --- действия: всё только после подтверждения ---

    def _group(self, name: str):
        g = ProcGroup(name)
        for p in psutil.process_iter(["name"]):
            if (p.info.get("name") or "").lower() == name.lower():
                g.pids.append(p.pid)
        return g

    def kill(self):
        name, _exe = self._target()
        if not name or not confirm(self, tr("Закрыть программу?", "Close program?"),
                                   tr(f"Закрыть «{name}»? Несохранённые данные в ней будут потеряны.",
                                      f"Close “{name}”? Any unsaved data in it will be lost."), danger=True):
            return

        def done(r):
            closed, failed = r
            msg = tr(f"«{name}»: закрыто процессов — {closed}", f"“{name}”: processes closed — {closed}")
            if failed:
                msg += tr(f", не удалось — {failed} ({ADMIN_HINT})", f", failed — {failed} ({ADMIN_HINT})")
            self.main.status(msg)
            self.main.journal_tab.reload()

        run_async(lambda _p: kill(self._group(name)), done)

    def disable_autostart(self):
        name, exe = self._target()
        items = self._autostart(name, exe)
        if items:
            self.main.startup_tab.toggle(items[0], False)  # подтверждение и журнал — как на вкладке «Автозагрузка»
            self.on_select()

    def quarantine(self):
        name, exe = self._target()
        if not exe or not confirm(
                self, tr("Убрать в карантин?", "Quarantine?"),
                tr(f"WhySlow закроет «{name}» и переместит файл\n{exe}\nв свою папку карантина. Файл не удаляется: "
                   "вернуть его можно на вкладке «Журнал». Убедитесь, что это не нужная вам программа.",
                   f"WhySlow will close “{name}” and move the file\n{exe}\nto its quarantine folder. The file is not "
                   "deleted: you can restore it in the Journal tab. Make sure it isn't a program you need."),
                danger=True):
            return

        def work(_p):
            kill(self._group(name))
            return quarantine(exe)

        def done(dst):
            self.main.status(tr(f"«{name}» в карантине: {dst}", f"“{name}” quarantined: {dst}"))
            self.main.journal_tab.reload()
            self.scan()

        run_async(work, done)

    def open_manager(self):
        kind, ext = self._current()
        if kind != "ext":
            return
        if ext.browser != "Safari":
            QApplication.clipboard().setText(ext.manage_url)
        opened = open_manager(ext)
        QMessageBox.information(self, tr("Удаление расширения", "Removing the extension"),
                                manager_hint(ext) + "\n\n" + (
                                    tr("Браузер открыт; адрес также скопирован — вставьте его в строку адреса, "
                                       "если страница не открылась сама.",
                                       "The browser is open; the address is also copied — paste it into the address "
                                       "bar if the page didn't open by itself.") if opened else
                                    tr("Адрес скопирован — вставьте его в строку адреса браузера.",
                                       "The address is copied — paste it into the browser's address bar.")
                                    if ext.browser != "Safari" else ""))

    def remove_policy(self):
        kind, ext = self._current()
        if kind != "ext" or not ext.policy:
            return
        if not confirm(self, tr("Снять принудительную установку?", "Remove the forced install?"),
                       tr(f"Удалить запись политики, которая ставит «{ext.name}» в {ext.browser} принудительно? "
                          "Если компьютер рабочий и это поставил администратор — не надо. "
                          "Откатить можно на вкладке «Журнал». После этого перезапустите браузер и удалите расширение.",
                          f"Delete the policy entry that force-installs “{ext.name}” in {ext.browser}? "
                          "If this is a work computer and your admin set it up, don't. "
                          "You can undo it in the Journal tab. Then restart the browser and remove the extension."),
                       danger=True):
            return
        try:
            n = remove_policy(ext)
        except ActionError as e:
            QMessageBox.warning(self, tr("Не получилось", "Failed"), str(e))
            return
        self.main.status(tr(f"Снято записей политики: {n}", f"Policy entries removed: {n}"))
        self.main.journal_tab.reload()
        self.scan()

    def reveal(self):
        _name, path = self._target()
        if path:
            bigfiles.reveal(path)

    def clear_log(self):
        if confirm(self, tr("Очистить ночной журнал?", "Clear the night log?"),
                   tr("Все записи о том, кто выходил в сеть ночью, будут удалены.",
                      "All records of who went online at night will be deleted.")):
            clear_log()
            self.scan()
