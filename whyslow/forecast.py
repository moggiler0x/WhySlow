"""Прогноз: сколько ещё лет железо компьютера будет тянуть программы и что обновлять первым.

Модели процессоров и видеокарт, требования задач (браузер, игры, монтаж) и сроки поддержки
систем лежат в whyslow/data/hardware.json. Как и база программ, файл обновляется с GitHub:
когда выходят новые игры и меняются требования, достаточно поправить его в репозитории.

Оценка нарочно простая и объяснимая. У каждого процессора и видеокарты есть «год с поправкой»:
год выхода плюс поправка за класс (топовая модель держится как средняя на пару лет новее).
Задача говорит, сколько лет после этого года железо её ещё тянет. Память считается отдельно:
сколько её нужно задаче сейчас и за сколько лет требование удваивается.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import net
from .i18n import lang, pick, tr
from .util import IS_LINUX, IS_MAC, IS_WINDOWS, app_dir, write_json

DB_URL = os.environ.get(
    "WHYSLOW_HW_URL",
    "https://raw.githubusercontent.com/moggiler0x/WhySlow/main/whyslow/data/hardware.json",
)
BUNDLED = Path(__file__).resolve().parent / "data" / "hardware.json"
MAX_YEARS = 10  # дальше любой прогноз — гадание
KINDS = ("disk", "ram", "vram", "gpu", "cpu")  # от дешёвого апгрейда к дорогому


def kind_label(kind: str) -> str:
    return {"cpu": tr("процессор", "CPU"), "gpu": tr("видеокарта", "graphics"),
            "ram": tr("оперативная память", "RAM"), "vram": tr("видеопамять", "video memory"),
            "disk": tr("жёсткий диск вместо SSD", "hard drive instead of an SSD"),
            "os": tr("поддержка системы", "OS support")}.get(kind, kind)


# ---------------------------------------------------------------- база ---

def _valid(data) -> bool:
    return isinstance(data, dict) and isinstance(data.get("workloads"), list) and isinstance(data.get("cpu"), list)


def _version(data: dict) -> int:
    try:
        return int(data.get("version", 0))
    except (TypeError, ValueError):
        return 0


def _read(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if _valid(data) else None
    except (OSError, ValueError):
        return None


def _cache_path() -> Path:
    return app_dir() / "hardware.json"


def load() -> dict:
    """Самая свежая из встроенной и скачанной копий."""
    bundled = _read(BUNDLED) or {"cpu": [], "gpu": [], "workloads": []}
    cached = _read(_cache_path())
    return cached if cached and _version(cached) > _version(bundled) else bundled


def update(timeout: float = 6) -> tuple[dict, str]:
    """Скачивает свежие требования с GitHub. Возвращает (база, сообщение)."""
    current = load()
    try:
        with net.urlopen(DB_URL, timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # сеть, DNS, кривой JSON
        why = net.explain(e, tr("файл требований", "requirements file"))
        return current, tr(f"Не удалось обновить требования: {why}. Считаю по имеющимся.",
                           f"Couldn't update requirements: {why}. Using the existing ones.")
    if not _valid(data):
        return current, tr("Файл требований на GitHub повреждён, считаю по имеющимся.",
                           "The requirements file on GitHub is corrupted, using the existing ones.")
    if _version(data) <= _version(current):
        return current, tr(f"Требования актуальны (от {current.get('updated', '?')})",
                           f"Requirements are up to date (from {current.get('updated', '?')})")
    try:
        write_json(_cache_path(), data)
    except OSError:
        pass
    return data, tr(f"Требования обновлены (от {data.get('updated', '?')})",
                    f"Requirements updated (from {data.get('updated', '?')})")


# ------------------------------------------------------ распознавание ---

@dataclass
class Chip:
    """Опознанный процессор или видеокарта."""
    name: str
    year: int            # год выхода (примерно)
    tier: int = 0        # поправка за класс, в годах
    known: bool = True   # нашлась в базе, а не угадана
    integrated: bool = False
    win11: bool | None = None

    @property
    def eff(self) -> float:
        """Год с поправкой: середина года выхода плюс класс."""
        return self.year + 0.5 + self.tier


def _value(spec, m: re.Match, default=None, cpu_year=None):
    """Поле правила: число, "cpu" (год процессора) или {"group", "map", "else"}."""
    if spec == "cpu":
        return cpu_year
    if isinstance(spec, dict):
        key = (m.groupdict().get(spec.get("group", "")) or "")
        return spec.get("map", {}).get(key, spec.get("else", default))
    return spec if spec is not None else default


def _win11(spec, m: re.Match) -> bool | None:
    if isinstance(spec, bool) or spec is None:
        return spec
    try:
        return int(m.group(spec["group"])) >= spec["min"]
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def identify(name: str, rules: list, cpu_year: int | None = None) -> Chip | None | bool:
    """Chip по первому подходящему правилу; False — устройство нужно пропустить
    (виртуальный адаптер); None — в базе нет."""
    for r in rules:
        try:
            m = re.search(r["match"], name, re.IGNORECASE)
        except (KeyError, TypeError, re.error):
            continue
        if not m:
            continue
        if r.get("skip"):
            return False
        year = _value(r.get("year"), m, cpu_year=cpu_year)
        tier = _value(r.get("tier"), m, 0)
        if not isinstance(year, int) or not isinstance(tier, int):
            continue
        for adj in r.get("adjust", []):
            text = (m.groupdict().get(adj["group"]) or "") if adj.get("group") else name
            if re.search(adj.get("match", "$^"), text, re.IGNORECASE):
                tier += adj.get("delta", 0)
        return Chip(name, year, tier, integrated=bool(r.get("integrated")), win11=_win11(r.get("win11"), m))
    return None


def _guess_cpu(name: str, threads: int) -> Chip:
    """Процессора нет в базе — очень грубо по числу потоков."""
    year = 2010 if threads <= 2 else 2014 if threads <= 4 else 2018 if threads <= 8 else 2021
    return Chip(name, year, 0, known=False)


# ------------------------------------------------------------- железо ---

def hardware() -> dict:
    """Что стоит в компьютере. Медленно (PowerShell, system_profiler) — звать в фоне."""
    import psutil

    from . import diskhealth, sysinfo
    disks = diskhealth.physical()
    system = next((d for d in disks if d.system), disks[0] if len(disks) == 1 else None)
    try:
        laptop = psutil.sensors_battery() is not None
    except (AttributeError, OSError, RuntimeError):
        laptop = False
    return {
        "cpu": sysinfo.cpu_name(),
        "threads": psutil.cpu_count() or 0,
        "ram": psutil.virtual_memory().total,
        "gpus": sysinfo.gpus(),
        "disk": system.kind if system else "",
        "os": sysinfo.os_name(),
        "laptop": laptop,
        "mac": IS_MAC,
        "windows": IS_WINDOWS,
    }


# ------------------------------------------------------------- прогноз ---

@dataclass
class WorkloadResult:
    id: str
    title: str
    years: float                 # сколько ещё тянет (0 — уже не тянет), не больше MAX_YEARS
    by: str                      # что упрётся первым
    parts: dict[str, float] = field(default_factory=dict)


@dataclass
class Advice:
    kind: str
    when: float                  # год, к которому стоит сделать
    text: str


@dataclass
class Forecast:
    now: float
    cpu: Chip
    gpu: Chip
    ram_gb: float
    hw: dict
    workloads: list[WorkloadResult]
    os_text: str = ""
    os_until: float | None = None
    os_action: str = ""
    notes: list[str] = field(default_factory=list)
    market: list[str] = field(default_factory=list)
    updated: str = ""

    def get(self, wid: str) -> WorkloadResult:
        return next((w for w in self.workloads if w.id == wid), self.workloads[0])


def now_year(t: float | None = None) -> float:
    lt = time.localtime(t if t is not None else time.time())
    return lt.tm_year + (lt.tm_yday - 1) / 365


def _clip(x: float) -> float:
    return max(0.0, min(float(MAX_YEARS), x))


def _mem_left(have_gb: float, need_gb: float, year: float, double: float, now: float) -> float:
    """Через сколько лет требование к памяти дорастёт до того, что есть."""
    if have_gb <= 0 or need_gb <= 0 or double <= 0:
        return MAX_YEARS
    need_now = need_gb * 2 ** ((now - year) / double)
    return double * math.log2(have_gb / need_now)


def _ram_gb(total: float) -> float:
    """Память в «магазинных» гигабайтах: система видит 15.7 из 16."""
    gb = total / 2**30
    for size in (2, 3, 4, 6, 8, 12, 16, 18, 24, 32, 36, 48, 64, 96, 128):
        if gb <= size * 1.0 and gb >= size * 0.85:
            return float(size)
    return round(gb)


def _vram_gb(g: dict) -> float | None:
    """Видеопамять, только если ей можно верить: WMI в Windows упирается в 4 ГБ."""
    mb = g.get("vram_mb")
    if not mb or ("vram_used_mb" not in g and mb <= 4096):
        return None
    return mb / 1024


def estimate(hw: dict, db: dict, now: float | None = None) -> Forecast:
    now = now_year() if now is None else now
    notes: list[str] = []
    cpu = identify(hw.get("cpu", ""), db.get("cpu", []))
    if not cpu:
        cpu = _guess_cpu(hw.get("cpu", ""), hw.get("threads", 0))
        notes.append(tr(f"Процессора «{cpu.name}» нет в базе — его возраст прикинут по числу ядер, очень приблизительно.",
                        f"The CPU “{cpu.name}” isn't in the database — its age is guessed from the core count, very roughly."))

    gpu, vram = None, None
    for g in hw.get("gpus", []):
        chip = identify(g.get("name", ""), db.get("gpu", []), cpu.year)
        if chip is False:
            continue
        if chip is None:
            notes.append(tr(f"Видеокарты «{g.get('name')}» нет в базе, она не учтена.",
                            f"The graphics “{g.get('name')}” isn't in the database and wasn't counted."))
            continue
        if gpu is None or chip.eff > gpu.eff:
            gpu, vram = chip, (None if chip.integrated else _vram_gb(g))
    if gpu is None:
        gpu = Chip(tr("встроенная графика", "integrated graphics"), cpu.year, -5, known=False, integrated=True)
        if not hw.get("gpus"):
            notes.append(tr("Видеокарту определить не удалось — считаю, что графика встроенная.",
                            "Couldn't detect the graphics card — assuming integrated graphics."))

    ram = _ram_gb(hw.get("ram", 0))
    results = []
    for w in db.get("workloads", []):
        parts = {
            "cpu": cpu.eff + w.get("cpu_years", MAX_YEARS) - now,
            "gpu": gpu.eff + w.get("gpu_years", MAX_YEARS) - now,
            "ram": _mem_left(ram, w.get("ram_gb", 0), w.get("ram_year", now), w.get("ram_double", 6), now),
        }
        if vram and w.get("vram_gb"):
            parts["vram"] = _mem_left(vram, w["vram_gb"], w.get("ram_year", now), w.get("vram_double", 5), now)
        if hw.get("disk") == "hdd" and w.get("ssd"):
            parts["disk"] = 0.0
        parts = {k: _clip(v) for k, v in parts.items()}
        by = min(parts, key=lambda k: (parts[k], KINDS.index(k)))
        results.append(WorkloadResult(w.get("id", "?"), pick(w, "title"), parts[by], by, parts))

    fc = Forecast(now, cpu, gpu, ram, hw, results, notes=notes,
                  market=[pick(m, "text") for m in db.get("market", []) if isinstance(m, dict)],
                  updated=str(db.get("updated", "")))
    _os(fc, db.get("os", {}))
    return fc


def _date(s: str) -> float:
    try:
        y, m, d = (int(x) for x in s.split("-"))
        return y + (m - 1) / 12 + (d - 1) / 365
    except (ValueError, AttributeError):
        return 0.0


def _os(fc: Forecast, rules: dict) -> None:
    """Сколько ещё система будет получать обновления на этом железе."""
    name, cpu = fc.hw.get("os", ""), fc.cpu
    if fc.hw.get("windows") and "Windows 10" in name:
        w10 = rules.get("win10", {})
        end, esu = _date(w10.get("end", "")), _date(w10.get("esu_end", ""))
        fc.os_until = max(end, esu)
        over = fc.now >= fc.os_until
        fc.os_text = (tr("Windows 10 больше не получает обновлений безопасности.",
                         "Windows 10 no longer receives security updates.") if over else
                      tr(f"Windows 10 получает обновления безопасности только до {w10.get('esu_end')} "
                         "(и то по программе расширенной поддержки).",
                         f"Windows 10 only gets security updates until {w10.get('esu_end')} "
                         "(and only through extended support)."))
        if cpu.win11:
            fc.os_action = tr("Обновитесь до Windows 11: процессор её поддерживает, обновление бесплатное "
                              "(Параметры → Центр обновления Windows).",
                              "Upgrade to Windows 11: your CPU supports it and the upgrade is free "
                              "(Settings → Windows Update).")
        elif cpu.win11 is False:
            fc.os_action = tr("Процессор официально не поддерживает Windows 11. Это главный повод обновить "
                              "компьютер: без обновлений безопасности в интернете становится рискованно.",
                              "The CPU doesn't officially support Windows 11. That's the main reason to upgrade "
                              "the computer: without security updates, going online gets risky.")
        else:
            fc.os_action = tr("Проверьте, подходит ли компьютер для Windows 11: приложение «Проверка работоспособности ПК».",
                              "Check whether the PC can run Windows 11 with the PC Health Check app.")
    elif fc.hw.get("mac"):
        if "Apple" in cpu.name:
            fc.os_until = cpu.year + rules.get("mac_arm_years", 8)
            fc.os_text = tr(f"Новые версии macOS для этого Mac ожидаются примерно до {fc.os_until:.0f} года, "
                            "обновления безопасности — ещё пару лет после.",
                            f"New macOS versions are expected for this Mac until about {fc.os_until:.0f}, "
                            "security updates for a couple of years after.")
        else:
            fc.os_until = rules.get("mac_intel_security_end", 2028)
            last = rules.get("mac_intel_last", "macOS 26")
            fc.os_text = tr(f"{last} — последняя версия для Mac на Intel. Обновления безопасности — примерно до "
                            f"{fc.os_until:.0f} года.",
                            f"{last} is the last version for Intel Macs. Security updates until about {fc.os_until:.0f}.")
    elif fc.hw.get("windows") and "Windows 11" in name:
        fc.os_text = tr("Windows 11 поддерживается и обновляется — система вас не ограничит.",
                        "Windows 11 is supported and updated — the OS won't hold you back.")
    elif IS_LINUX and not fc.hw.get("windows"):
        fc.os_text = tr("Linux поддерживает старое железо долго — система вас не ограничит.",
                        "Linux supports old hardware for a long time — the OS won't hold you back.")


# ------------------------------------------------------------- советы ---

def span(y: float) -> str:
    """«3 года», «год», «10+ лет»."""
    if y >= MAX_YEARS:
        return tr(f"{MAX_YEARS}+ лет", f"{MAX_YEARS}+ years")
    n = max(1, round(y))
    if lang() == "en":
        return "1 year" if n == 1 else f"{n} years"
    if n == 1:
        return "год"
    return f"{n} " + ("года" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "лет")


def years_text(y: float) -> str:
    if y < 0.5:
        return tr("уже не хватает", "already not enough")
    return span(y) if y >= MAX_YEARS else "≈ " + span(y)


def until_text(now: float, y: float) -> str:
    if y < 0.5:
        return tr("сейчас", "now")
    if y >= MAX_YEARS:
        return tr(f"после {int(now + y)}", f"after {int(now + y)}")
    return tr(f"до ~{int(now + y)}", f"until ~{int(now + y)}")


def headline(fc: Forecast, wid: str) -> str:
    w = fc.get(wid)
    if w.years < 0.5:
        return tr(f"Для задачи «{w.title}» железа уже не хватает: мешает {kind_label(w.by)}.",
                  f"For “{w.title}” the hardware is already not enough: the {kind_label(w.by)} holds it back.")
    if w.years >= MAX_YEARS:
        return tr(f"Для задачи «{w.title}» железа хватит на {MAX_YEARS} лет и больше.",
                  f"For “{w.title}” the hardware will last {MAX_YEARS} years or more.")
    return tr(f"Для задачи «{w.title}» железа хватит примерно на {span(w.years)} "
              f"({until_text(fc.now, w.years)}). Первым упрётся {kind_label(w.by)}.",
              f"For “{w.title}” the hardware will last about {span(w.years)} "
              f"({until_text(fc.now, w.years)}). The {kind_label(w.by)} will run out first.")


def _when(fc: Forecast, y: float) -> str:
    return tr("сейчас", "now") if y < 0.5 else tr(f"к ~{int(fc.now + y)}", f"by ~{int(fc.now + y)}")


def advice(fc: Forecast, wid: str, db: dict) -> list[Advice]:
    """Что обновлять и когда — для выбранной задачи. Сначала то, что нужно раньше,
    при равных сроках — что дешевле."""
    w = fc.get(wid)
    wdb = next((x for x in db.get("workloads", []) if x.get("id") == wid), {})
    mac, laptop = fc.hw.get("mac"), fc.hw.get("laptop")
    soon = 4  # о том, что понадобится позже, говорить рано
    out: list[Advice] = []
    for kind, y in w.parts.items():
        if y >= soon:
            continue
        when = _when(fc, y)
        if kind == "disk":
            out.append(Advice(kind, fc.now, tr(
                "Поставьте SSD вместо жёсткого диска — сейчас. Самый дешёвый апгрейд с самым заметным эффектом: "
                "система и программы запускаются в разы быстрее.",
                "Replace the hard drive with an SSD — now. The cheapest upgrade with the biggest effect: "
                "the system and apps start several times faster.")))
        elif kind == "ram":
            target = next((s for s in (8, 16, 32, 64, 128) if s > fc.ram_gb and _mem_left(
                s, wdb.get("ram_gb", 0), wdb.get("ram_year", fc.now), wdb.get("ram_double", 6), fc.now) >= 5), None)
            size = tr(f" до {target} ГБ", f" to {target} GB") if target else ""
            if mac:
                text = tr(f"Памяти ({fc.ram_gb:.0f} ГБ) станет мало {when}. В Mac она распаяна: при покупке "
                          f"следующего берите{size or ' больше'}.",
                          f"The memory ({fc.ram_gb:.0f} GB) will run short {when}. It's soldered in a Mac: get"
                          f"{size or ' more'} with the next one.")
            else:
                tail = tr(" В ноутбуке сначала проверьте, что память не распаяна.",
                          " In a laptop, first check the memory isn't soldered.") if laptop else ""
                text = tr(f"Добавьте оперативной памяти{size} — {when}.{tail}",
                          f"Add RAM{size} — {when}.{tail}")
            out.append(Advice(kind, fc.now + y, text))
        elif kind in ("gpu", "vram"):
            sug = db.get("suggest", {}).get("gpu", {})
            if not wdb.get("vram_gb"):  # браузеру и офису видеокарта почти не нужна
                text = tr(f"Графика — {when}: хватит любой современной, даже встроенной в новый процессор.",
                          f"Graphics — {when}: any modern one will do, even the one built into a new CPU.")
            elif mac or laptop:
                text = tr(f"Графики перестанет хватать {when}. В {'Mac' if mac else 'ноутбуке'} её не заменить — "
                          "только вместе с компьютером.",
                          f"Graphics will fall short {when}. It can't be replaced in a {'Mac' if mac else 'laptop'} — "
                          "only with the whole computer.")
            else:
                last = _sug_years(sug, wdb.get("gpu_years", MAX_YEARS), fc.now)
                text = tr(f"Видеокарта — {when}. Например, {sug.get('text', 'средний класс')} прослужит ещё "
                          f"{span(last)}.",
                          f"Graphics card — {when}. For example, {sug.get('text', 'a mid-range card')} will last "
                          f"another {span(last)}.") if sug else \
                    tr(f"Видеокарта — {when}.", f"Graphics card — {when}.")
            out.append(Advice("gpu", fc.now + y, text))
        elif kind == "cpu":
            if mac or laptop:
                what = "Mac" if mac else tr("ноутбук", "laptop")
                text = tr(f"Новый {what} — {when}: процессор в нём не меняется.",
                          f"A new {what} — {when}: its CPU can't be replaced.")
            else:
                sug = db.get("suggest", {}).get("cpu", {})
                ex = tr(f" Уровень {sug['text']} или новее.", f" Something like {sug['text']} or newer.") if sug.get("text") else ""
                text = tr(f"Процессор — {when}. Обычно вместе с платой и памятью, то есть по сути новый компьютер.{ex}",
                          f"CPU — {when}. Usually together with the motherboard and memory, so effectively a new computer.{ex}")
            out.append(Advice(kind, fc.now + y, text))
    seen, uniq = set(), []
    for a in sorted(out, key=lambda a: (a.when, KINDS.index(a.kind))):
        if a.kind not in seen:
            seen.add(a.kind)
            uniq.append(a)
    if fc.os_action:
        uniq.insert(0, Advice("os", fc.now, fc.os_action))
    return uniq


def _sug_years(sug: dict, need: float, now: float) -> float:
    return _clip(sug.get("year", int(now)) + 0.5 + sug.get("tier", 0) + need - now)


def disclaimer() -> str:
    return tr("Это примерная оценка по типичным требованиям программ, а не гарантия: многое зависит от того, "
              "какие именно программы и игры вы запускаете и на каких настройках.",
              "This is a rough estimate based on typical software requirements, not a guarantee: a lot depends on "
              "which apps and games you actually run and at what settings.")


def summary(fc: Forecast) -> str:
    """Что WhySlow понял про железо — одной строкой."""
    gpu = fc.gpu.name if fc.gpu.known else tr("встроенная графика", "integrated graphics")
    disk = {"ssd": "SSD", "hdd": "HDD"}.get(fc.hw.get("disk", ""), "")
    parts = [fc.cpu.name, gpu, tr(f"{fc.ram_gb:.0f} ГБ памяти", f"{fc.ram_gb:.0f} GB RAM")] + ([disk] if disk else [])
    return " · ".join(p for p in parts if p)
