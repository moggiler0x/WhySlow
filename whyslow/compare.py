"""«Твой ПК против таких же»: анонимное сравнение с компьютерами на похожем железе.

С согласия пользователя WhySlow отправляет на сервер обезличенный снимок: модель
процессора и видеокарты, объём памяти, тип диска, версию ОС и несколько замеров
(загрузка системы, нагрузка в простое, сколько всего в автозагрузке). Программы
уходят только как id записей из открытой базы known.json — то, чего нет в базе,
лишь считается, но не называется. Имени пользователя, имени компьютера, путей,
IP в базе сервера и ключа подписки рядом со снимком нет.

Сервер отвечает только сводкой по группе похожих ПК (медианы и доли программ), и
только если таких ПК не меньше MIN_PEERS — по сводке нельзя вычислить отдельный компьютер.
"""

from __future__ import annotations

import json
import os
import platform
import re
import time
import uuid
from dataclasses import asdict, dataclass, field

import psutil

from . import settings
from .i18n import tr
from .util import IS_LINUX, IS_MAC, IS_WINDOWS, app_dir, ps_json, read_json, run, write_json

FEATURE = "compare"
SERVER = "https://whyslow.moggiler0x.workers.dev"  # тот же Worker, что проверяет подписку
SCHEMA = 1
MIN_PEERS = 5           # меньше — сравнивать не с чем, и сводка начинает выдавать отдельные ПК
RAM_SIZES = (2, 4, 6, 8, 12, 16, 24, 32, 48, 64, 96, 128, 192, 256)


class CompareError(Exception):
    pass


# ---------------------------------------------------------------- лицензия ---

def has_premium() -> bool:
    """Функция платная. Проверку ключа делает модуль подписки; пока его нет — закрыто."""
    try:
        from . import license as lic  # type: ignore[attr-defined]
    except ImportError:
        return False
    try:
        return bool(lic.has_premium(FEATURE))
    except Exception:  # noqa: BLE001 — сломанная проверка не должна ронять вкладку
        return False


def _auth_headers() -> dict:
    """Заголовки, по которым сервер убеждается, что подписка оплачена."""
    try:
        from . import license as lic  # type: ignore[attr-defined]
        return dict(lic.auth_headers() or {})
    except ImportError:
        return {}
    except Exception:  # noqa: BLE001
        return {}


# ------------------------------------------------------------- нормализация ---

def norm_cpu(name: str) -> str:
    """«Intel(R) Core(TM) i5-8250U CPU @ 1.60GHz» → «intel core i5-8250u»."""
    s = name.lower()
    s = re.sub(r"\((r|tm|c)\)|®|™", " ", s)
    s = re.sub(r"@.*$", " ", s)
    s = re.sub(r"\b(\d+|eight|six|quad|dual|twelve|sixteen)[- ]core\b", " ", s)
    s = re.sub(r"\b(cpu|processor|with radeon( \w+)? graphics|w/ radeon.*$|apu)\b", " ", s)
    s = re.sub(r"\b\d+(th|nd|rd|st) gen\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:60]


def norm_gpu(name: str) -> str:
    """«NVIDIA GeForce RTX 3060 Laptop GPU» → «nvidia geforce rtx 3060 laptop»."""
    s = name.lower()
    s = re.sub(r"\((r|tm)\)|®|™|\bgpu\b|\bgraphics\b|\bcorporation\b", " ", s)
    s = re.sub(r"\[.*?\]|\(.*?\)", " ", s)
    return re.sub(r"\s+", " ", s).strip()[:60]


def ram_bucket(total_bytes: int) -> int:
    """Объём памяти, округлённый до «магазинного»: 15.8 ГБ → 16."""
    gb = total_bytes / 2**30
    return min(RAM_SIZES, key=lambda s: abs(s - gb))


def os_family() -> str:
    if IS_WINDOWS:
        build = platform.version().split(".")[-1]
        return "win11" if build.isdigit() and int(build) >= 22000 else f"win{platform.release()}"
    if IS_MAC:
        return "mac" + (platform.mac_ver()[0].split(".")[0] or "")
    return "linux"


# --------------------------------------------------------------- замеры ---

def boot_seconds() -> float | None:
    """Сколько длилась последняя загрузка системы до рабочего стола."""
    if IS_WINDOWS:
        # Журнал Diagnostics-Performance, событие 100: BootTime в мс. Читается не на всех
        # редакциях и иногда только от администратора — тогда замера нет.
        rows = ps_json("$e = Get-WinEvent -FilterHashtable @{LogName='Microsoft-Windows-Diagnostics-Performance/Operational';"
                       "Id=100} -MaxEvents 1 -ErrorAction SilentlyContinue; if ($e) { $x = [xml]$e.ToXml(); "
                       "[pscustomobject]@{ ms = [int](($x.Event.EventData.Data | ? Name -eq 'BootTime').'#text') } "
                       "| ConvertTo-Json }", timeout=20)
        ms = rows[0].get("ms") if rows else None
        return round(ms / 1000, 1) if isinstance(ms, (int, float)) and ms > 0 else None
    if IS_LINUX:
        code, out = run(["systemd-analyze"], timeout=10)
        m = re.search(r"=\s*(?:(\d+)min\s*)?([\d.]+)s", out) if code == 0 else None
        if m:
            return round(int(m.group(1) or 0) * 60 + float(m.group(2)), 1)
    return None  # macOS не хранит длительность загрузки в доступном виде


@dataclass
class Snapshot:
    """Что уходит на сервер. Всё здесь пользователь видит до отправки."""
    id: str
    hw: dict
    metrics: dict
    programs: list[str] = field(default_factory=list)   # id записей базы в автозагрузке и в фоне
    unknown_programs: int = 0                           # прочие — только количеством
    schema: int = SCHEMA

    def to_json(self) -> dict:
        return asdict(self)


def install_id() -> str:
    """Случайный id этой установки: чтобы повторная отправка заменяла снимок, а не
    добавляла второй. С ключом подписки, железом или именем никак не связан."""
    sid = settings.get("compare_id")
    if not sid:
        sid = uuid.uuid4().hex
        settings.set("compare_id", sid)
    return sid


def collect(result, cpu_name: str, gpus: list[dict], progress=lambda t: None) -> Snapshot:
    """Собирает снимок из последней проверки (checks.ScanResult) и пары быстрых замеров."""
    progress(tr("Замеряю нагрузку в простое…", "Measuring idle load…"))
    idle_cpu = psutil.cpu_percent(interval=3)
    vm = psutil.virtual_memory()
    sys_disk = next((d for d in result.physical if d.system), None) if result else None
    hw = {
        "os": os_family(),
        "cpu": norm_cpu(cpu_name),
        "cores": psutil.cpu_count(logical=False) or 0,
        "threads": psutil.cpu_count() or 0,
        "ram_gb": ram_bucket(vm.total),
        "disk": sys_disk.kind if sys_disk and sys_disk.kind else "",
        "gpu": norm_gpu(gpus[0]["name"]) if gpus else "",
    }
    startup_on = [it for it in (result.startup if result else []) if it.enabled and not it.system]
    progress(tr("Узнаю, сколько грузилась система…", "Finding out how long the system took to boot…"))
    metrics = {
        "boot_s": boot_seconds(),
        "idle_cpu": round(idle_cpu, 1),
        "ram_used_pct": round(vm.percent, 1),
        "procs": len(psutil.pids()),
        "startup": len(startup_on),
        "junk_gb": round(sum(c.size for c in result.junk) / 2**30, 1) if result else None,
    }
    ids: set[str] = set()
    unknown = 0
    for it in startup_on:
        v = it.verdict
        if v and v.entry_id and v.verdict != "unknown":
            ids.add(v.entry_id)
        else:
            unknown += 1
    for g in (result.procs if result else []):
        v = g.verdict
        if not g.system and v and v.entry_id and v.verdict not in ("unknown", "keep"):
            ids.add(v.entry_id)
    return Snapshot(install_id(), hw, metrics, sorted(ids), unknown)


# ------------------------------------------------------------------ сервер ---

def server_url() -> str:
    """Адрес сервера: из окружения (для проверки), иначе тот же, что у подписки."""
    url = os.environ.get("WHYSLOW_COMPARE_URL", "")
    if not url:
        try:
            from . import license as lic  # type: ignore[attr-defined]
            url = getattr(lic, "SERVER", "") or getattr(lic, "SERVER_URL", "")
        except ImportError:
            pass
    return (url or SERVER).rstrip("/")


def _post(path: str, body: dict, timeout: float = 15) -> dict:
    import urllib.error
    import urllib.request

    from .net import explain, urlopen
    req = urllib.request.Request(server_url() + path, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **_auth_headers()})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            msg = json.loads(e.read().decode("utf-8")).get("error", "")
        except (ValueError, OSError, AttributeError):
            msg = ""
        if e.code in (401, 402, 403):
            raise CompareError(tr("Сервер не принял подписку. Проверьте ключ в разделе «Подписка».",
                                  "The server didn't accept the subscription. Check the key in Subscription.")) from e
        if e.code == 429:
            raise CompareError(tr("Слишком часто. Попробуйте через час.", "Too often. Try again in an hour.")) from e
        raise CompareError(msg or f"HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise CompareError(explain(e, tr("сервер сравнения", "comparison server"))) from e


def compare(snap: Snapshot) -> dict:
    """Отправляет снимок и получает сводку по похожим ПК."""
    data = _post("/compare", snap.to_json())
    if not isinstance(data, dict):
        raise CompareError(tr("Сервер ответил непонятно.", "The server sent an unexpected reply."))
    _save_last(snap, data)
    return data


def forget() -> None:
    """Удалить свой снимок с сервера и забыть id: следующая отправка будет как с нового ПК."""
    sid = settings.get("compare_id")
    if sid:
        _post("/compare/forget", {"id": sid})
    settings.set("compare_id", "")
    settings.set("compare_consent", False)
    (app_dir() / "compare.json").unlink(missing_ok=True)


def _save_last(snap: Snapshot, data: dict) -> None:
    try:
        write_json(app_dir() / "compare.json", {"time": time.time(), "snapshot": snap.to_json(), "result": data})
    except OSError:
        pass


def last() -> tuple[Snapshot | None, dict | None, float]:
    d = read_json(app_dir() / "compare.json", {})
    try:
        return Snapshot(**d["snapshot"]), d["result"], float(d["time"])
    except (KeyError, TypeError, ValueError):
        return None, None, 0.0


# ------------------------------------------------------------- объяснение ---

# (ключ, подпись, единица, меньше — лучше)
METRICS = [
    ("boot_s", ("Загрузка системы", "System boot"), ("с", "s")),
    ("idle_cpu", ("Нагрузка процессора в простое", "Idle CPU load"), ("%", "%")),
    ("ram_used_pct", ("Занято памяти", "Memory in use"), ("%", "%")),
    ("startup", ("Программ в автозагрузке", "Startup programs"), ("", "")),
    ("procs", ("Процессов", "Processes"), ("", "")),
    ("junk_gb", ("Мусора", "Junk"), ("ГБ", "GB")),
]


@dataclass
class Row:
    key: str
    title: str
    mine: float | None
    median: float | None
    best: float | None           # 25-й перцентиль: «как у быстрых»
    unit: str
    diff_pct: float | None       # насколько хуже медианы, %; минус — лучше

    def text(self) -> str:
        if self.mine is None or self.median is None:
            return tr("нет данных", "no data")
        if self.diff_pct is None or abs(self.diff_pct) < 10:
            return tr("как у похожих", "same as similar PCs")
        if self.diff_pct > 0:
            return tr(f"на {self.diff_pct:.0f}% хуже", f"{self.diff_pct:.0f}% worse")
        return tr(f"на {-self.diff_pct:.0f}% лучше", f"{-self.diff_pct:.0f}% better")


@dataclass
class Suspect:
    id: str
    title: str
    note: str
    share_fast: float    # у какой доли быстрых похожих ПК она тоже есть
    share_all: float


@dataclass
class Report:
    peers: int
    level: str              # насколько похожи: model / class / broad
    level_text: str
    rows: list[Row]
    headline: str
    suspects: list[Suspect]


def _fmt(v: float, unit: str) -> str:
    s = f"{v:.0f}" if abs(v) >= 10 or float(v).is_integer() else f"{v:.1f}"
    return f"{s} {unit}".strip()


def explain_result(snap: Snapshot, data: dict, kb=None) -> Report | None:
    """Сводку сервера — в понятный отчёт: где хуже похожих и какие программы это объясняют."""
    peers = int(data.get("peers") or 0)
    if peers < MIN_PEERS:
        return None
    level = str(data.get("level", ""))
    level_text = {
        "model": tr(f"{peers} ПК с тем же процессором, памятью и диском",
                    f"{peers} PCs with the same CPU, memory and disk"),
        "class": tr(f"{peers} ПК с таким же числом ядер, памятью и диском",
                    f"{peers} PCs with the same core count, memory and disk"),
    }.get(level, tr(f"{peers} ПК с похожей памятью и диском", f"{peers} PCs with similar memory and disk"))
    stats = data.get("metrics") or {}
    rows = []
    for key, title, unit in METRICS:
        st = stats.get(key) or {}
        mine, med, best = snap.metrics.get(key), st.get("median"), st.get("p25")
        diff = None
        if isinstance(mine, (int, float)) and isinstance(med, (int, float)) and med > 0:
            diff = (mine - med) / med * 100
        rows.append(Row(key, tr(*title), mine, med, best, tr(*unit), diff))

    # Заголовок — по самому важному из того, что хуже: загрузка системы, потом простой
    headline = tr("Ваш ПК работает не хуже похожих.", "Your PC performs as well as similar ones.")
    for r in rows:
        if r.key in ("boot_s", "idle_cpu", "ram_used_pct", "startup") and r.diff_pct and r.diff_pct >= 20:
            if r.key == "boot_s":
                headline = tr(f"У похожих ПК система грузится на {r.diff_pct:.0f}% быстрее "
                              f"({_fmt(r.median, r.unit)} против ваших {_fmt(r.mine, r.unit)}).",
                              f"Similar PCs boot {r.diff_pct:.0f}% faster "
                              f"({_fmt(r.median, r.unit)} vs your {_fmt(r.mine, r.unit)}).")
            else:
                headline = tr(f"{r.title}: у вас {_fmt(r.mine, r.unit)}, у похожих ПК обычно {_fmt(r.median, r.unit)}.",
                              f"{r.title}: yours is {_fmt(r.mine, r.unit)}, similar PCs usually have {_fmt(r.median, r.unit)}.")
            break

    # Программы, которые есть у вас, но редко встречаются у быстрых похожих ПК
    progs = data.get("programs") or {}
    suspects = []
    for pid in snap.programs:
        p = progs.get(pid) or {}
        fast, share = float(p.get("fast", 0.0)), float(p.get("all", 0.0))
        if fast >= 0.4:
            continue  # у быстрых она тоже обычно есть — дело не в ней
        title, note, verdict = pid, "", ""
        if kb:
            e = next((e for e in kb.entries if e.id == pid), None)
            if e:
                title, note, verdict = e.title, e.note, e.verdict
        if verdict == "keep":
            continue
        suspects.append(Suspect(pid, title, note, fast, share))
    suspects.sort(key=lambda s: (s.share_fast, s.share_all))
    return Report(peers, level, level_text, rows, headline, suspects[:5])
