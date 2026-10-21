"""Двойник ПК: что дал бы апгрейд — на реальных данных этого компьютера.

Берётся то, что WhySlow уже знает: железо (как для «Прогноза»), сколько памяти занято
прямо сейчас и кем, и сколько раз за последний месяц система упиралась в процессор
или память (вкладка «История»). К этому «двойнику» подставляется деталь из
hardware.json, и видно, что изменится: кадры в играх, загрузка памяти, на сколько
лет дольше хватит железа.

Скорость деталей — относительно эталона (RTX 3060 и Ryzen 5 3600 = 1.0). Чего нет
в таблице, прикидывается по году выхода с поправкой на класс, как в forecast.py.
"""

from __future__ import annotations

import copy
import math
import re
import time
from dataclasses import dataclass, field

from . import forecast
from .i18n import lang, pick, tr
from .util import human_size

REF_EFF = {"gpu": 2020.5, "cpu": 2019.5}   # «год с поправкой» эталонных RTX 3060 и Ryzen 5 3600
DOUBLE = {"gpu": 3.5, "cpu": 6.0}           # за сколько лет скорость в играх удваивается
GAME_WORKLOADS = ("online_games", "aaa_games")
MONTH = 30 * 86400


# ------------------------------------------------------------ скорость ---

def perf(name: str, kind: str, db: dict, chip: forecast.Chip | None = None) -> float:
    """Скорость детали относительно эталона: из таблицы, иначе по году выхода."""
    for r in db.get("perf", {}).get(kind, []):
        try:
            if re.search(r["match"], name, re.IGNORECASE):
                return float(r["perf"])
        except (KeyError, TypeError, ValueError, re.error):
            continue
    if chip is None:
        return 1.0
    return 2 ** ((chip.eff - REF_EFF[kind]) / DOUBLE[kind])


@dataclass
class GameFps:
    id: str
    title: str
    now: float
    after: float
    limit_now: str     # что держит кадры: gpu / cpu / vram
    limit_after: str


def game_fps(game: dict, cpu: float, gpu: float, vram_gb: float | None, ram_gb: float) -> tuple[float, str]:
    """Кадров в секунду и что их ограничивает. Кадры — меньшее из того, что успевает
    видеокарта, и того, что успевает подготовить процессор."""
    g = game.get("fps_gpu", 60) * gpu
    limit = "gpu"
    if vram_gb is not None and vram_gb < game.get("vram_gb", 0):
        g *= 0.75  # текстуры не помещаются: рывки и просадки
        limit = "vram"
    c = game.get("fps_cpu", 120) * cpu
    if ram_gb < game.get("ram_gb", 0):
        c *= 0.8   # памяти мало, система подкачивает с диска
    return (g, limit) if g <= c else (c, "cpu")


# -------------------------------------------------------- живые данные ---

@dataclass
class Usage:
    """Что происходит с этим компьютером на самом деле."""
    ram_used: int = 0
    ram_total: int = 0
    swap_used: int = 0
    top_mem: list = field(default_factory=list)      # [(программа, байт)]
    ram_episodes: int = 0                            # эпизодов «кончилась память» за 30 дней
    cpu_episodes: int = 0
    cpu_culprits: list = field(default_factory=list)
    peak_cpu: float = 0.0


def usage() -> Usage:
    """Медленно (обход процессов) — звать в фоне."""
    import psutil

    from . import sysinfo, watch
    from .processes import ProcessMonitor
    used, total, _ = sysinfo.memory()
    try:
        swap = psutil.swap_memory().used
    except (OSError, RuntimeError):
        swap = 0
    groups = ProcessMonitor().sample()
    top = sorted(((g.name, g.mem) for g in groups if not g.system), key=lambda x: -x[1])[:3]
    u = Usage(used, total, swap, top)
    since = time.time() - MONTH
    who: dict[str, int] = {}
    for ep in watch.load():
        if ep.start < since:
            continue
        if "ram" in ep.kind:
            u.ram_episodes += 1
        if "cpu" in ep.kind:
            u.cpu_episodes += 1
            u.peak_cpu = max(u.peak_cpu, ep.peak_cpu)
            for c in ep.culprits[:1]:
                who[c[0]] = who.get(c[0], 0) + 1
    u.cpu_culprits = sorted(who, key=lambda k: -who[k])[:2]
    return u


# ------------------------------------------------------------ апгрейды ---

def price(entry: dict) -> tuple[int, int]:
    """Цена в валюте интерфейса: рубли для русского, доллары для английского."""
    p = entry.get("price", entry) if isinstance(entry, dict) else {}
    lo, hi = (p.get("usd") if lang() == "en" else p.get("rub")) or (0, 0)
    return int(lo), int(hi)


def money(lo: float, hi: float | None = None) -> str:
    def one(x: float) -> str:
        x = int(round(x, -2)) if x >= 1000 else int(round(x))
        return f"${x:,}" if lang() == "en" else f"{x:,}".replace(",", " ")
    hi = lo if hi is None else hi
    text = one(lo) if round(lo) == round(hi) else (f"{one(lo)}–{one(hi).lstrip('$')}")
    return text if lang() == "en" else text + " ₽"


def apply(hw: dict, up: dict) -> dict:
    """Двойник: то же железо, но с новой деталью."""
    new = copy.deepcopy(hw)
    rep = up.get("replace", {})
    if "cpu" in rep:
        new["cpu"] = rep["cpu"]
    if "gpu" in rep:
        g = {"name": rep["gpu"]}
        if rep.get("vram_gb"):  # vram_used_mb — знак, что объёму можно верить (см. forecast._vram_gb)
            g.update(vram_mb=rep["vram_gb"] * 1024, vram_used_mb=0)
        new["gpus"] = [g]
    if "ram_gb" in rep:
        new["ram"] = rep["ram_gb"] * 2**30
    if "disk" in rep:
        new["disk"] = rep["disk"]
    return new


@dataclass
class Side:
    """Одна сторона сравнения: как есть или с апгрейдом."""
    cpu: float
    gpu: float
    vram: float | None
    ram: float
    fc: forecast.Forecast


def side(hw: dict, db: dict, now: float | None = None) -> Side:
    fc = forecast.estimate(hw, db, now)
    vram = None
    for g in hw.get("gpus", []):
        if fc.gpu.known and g.get("name") == fc.gpu.name and not fc.gpu.integrated:
            vram = forecast._vram_gb(g)
    return Side(perf(fc.cpu.name, "cpu", db, fc.cpu), perf(fc.gpu.name, "gpu", db, fc.gpu), vram, fc.ram_gb, fc)


def options(hw: dict, base: Side, db: dict) -> list[dict]:
    """Апгрейды, которые имеют смысл для этого компьютера."""
    mac, laptop = hw.get("mac"), hw.get("laptop")
    out = []
    for up in db.get("upgrades", []):
        if not isinstance(up, dict) or "replace" not in up:
            continue
        kind, rep = up.get("kind"), up["replace"]
        if (up.get("platform") == "mac") != bool(mac):
            continue
        if kind == "ssd" and hw.get("disk") != "hdd":
            continue
        if kind == "ram" and rep.get("ram_gb", 0) <= base.ram:
            continue
        if kind in ("gpu", "cpu") and laptop:
            continue  # в ноутбуке процессор и видеокарту не поменять
        if kind == "gpu" and perf(rep.get("gpu", ""), "gpu", db) < base.gpu * 1.15:
            continue
        if kind == "cpu" and perf(rep.get("cpu", ""), "cpu", db) < base.cpu * 1.15:
            continue
        out.append(up)
    return out


@dataclass
class Simulation:
    up: dict
    title: str
    price: tuple[int, int]
    games: list[GameFps]
    years: list[tuple[str, str, float, float]]   # (id задачи, название, сейчас, после)
    effects: list[str]
    gain: float = 0.0                              # условная польза для выбранной задачи

    def get_years(self, wid: str) -> tuple[float, float]:
        return next(((b, a) for i, _t, b, a in self.years if i == wid), (0.0, 0.0))


def times(n: int) -> str:
    """«1 раз», «3 раза», «5 раз»."""
    if lang() == "en":
        return "once" if n == 1 else f"{n} times"
    few = n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14)
    return f"{n} раза" if few else f"{n} раз"


def _fps_line(g: GameFps) -> str:
    return f"«{g.title}» ~{g.now:.0f} → ~{g.after:.0f} FPS" if lang() != "en" else \
        f"{g.title}: ~{g.now:.0f} → ~{g.after:.0f} FPS"


def simulate(hw: dict, up: dict, db: dict, use: Usage | None, wid: str,
             base: Side | None = None, measured: dict | None = None, now: float | None = None) -> Simulation:
    """Что изменится с апгрейдом up. measured — FPS, которые пользователь видит сам
    ({id игры: кадры}): по ним расчёт подгоняется под его настройки."""
    base = base or side(hw, db, now)
    twin = side(apply(hw, up), db, now)
    measured = measured or {}
    kind = up.get("kind")
    effects: list[str] = []

    games = []
    for g in db.get("games", []):
        a, la = game_fps(g, base.cpu, base.gpu, base.vram, base.ram)
        b, lb = game_fps(g, twin.cpu, twin.gpu, twin.vram, twin.ram)
        k = 1.0
        if measured.get(g.get("id")) and a > 0:
            k = float(measured[g["id"]]) / a
        games.append(GameFps(g.get("id", "?"), pick(g, "title"), a * k, b * k, la, lb))

    years = [(w.id, w.title, w.years, twin.fc.get(w.id).years) for w in base.fc.workloads]
    before, after = next(((b, a) for i, _t, b, a in years if i == wid), (0.0, 0.0))

    if kind in ("gpu", "cpu", "system"):
        moved = [g for g in games if g.after > g.now * 1.08]
        if wid not in ("online_games",):  # для новых игр важнее те, где кадров меньше всего
            moved.sort(key=lambda g: g.now)
        for g in moved[:3]:
            effects.append(_fps_line(g))
        held = [g for g in games if g.limit_after == "cpu" and g.limit_now != "cpu" and g.after < g.now * 1.3]
        if kind == "gpu" and held:
            effects.append(tr(f"В {', '.join('«' + g.title + '»' for g in held[:2])} упрётся уже процессор: "
                              "видеокарта даст меньше, чем могла бы.",
                              f"In {', '.join(g.title for g in held[:2])} the CPU becomes the limit: "
                              "the card gives less than it could."))
        if kind == "gpu" and not moved:
            effects.append(tr("В играх почти ничего не изменится: кадры держит процессор.",
                              "Games will barely change: the CPU is holding the frame rate."))
    if kind == "cpu" and use and use.cpu_episodes:
        k = base.cpu / max(twin.cpu, 0.01)
        who = tr(f" (чаще всего — {', '.join(use.cpu_culprits)})", f" (mostly {', '.join(use.cpu_culprits)})") \
            if use.cpu_culprits else ""
        effects.append(tr(f"За месяц процессор был перегружен {times(use.cpu_episodes)}{who}. С новым те же задачи "
                          f"заняли бы его примерно на {min(use.peak_cpu * k, 100):.0f}% вместо {use.peak_cpu:.0f}%.",
                          f"Over the month the CPU was overloaded {times(use.cpu_episodes)}{who}. With the new one "
                          f"the same work would load it to about {min(use.peak_cpu * k, 100):.0f}% instead of "
                          f"{use.peak_cpu:.0f}%."))
    if "ram_gb" in up.get("replace", {}) and use and use.ram_total:
        effects.append(_ram_effect(use, twin.ram))
    if kind in ("ssd", "system") and hw.get("disk") == "hdd":
        effects.append(tr("Система и программы будут запускаться в 3–5 раз быстрее, игры — быстрее грузиться "
                          "и меньше подтормаживать на подгрузке.",
                          "The system and apps will start 3–5 times faster, games will load quicker "
                          "and stutter less while streaming data."))
    if after > before + 0.4:
        wt = base.fc.get(wid).title
        if before < 0.5:
            effects.append(tr(f"Для задачи «{wt}» железа уже не хватает, а с апгрейдом хватит примерно на "
                              f"{forecast.span(after)}.",
                              f"For “{wt}” the hardware is already not enough; with the upgrade it will last "
                              f"about {forecast.span(after)}."))
        else:
            effects.append(tr(f"Для задачи «{wt}» железа хватит примерно на {forecast.span(after)} вместо "
                              f"{forecast.span(before)}.",
                              f"For “{wt}” the hardware will last about {forecast.span(after)} instead of "
                              f"{forecast.span(before)}."))
    else:
        by = twin.fc.get(wid).by
        if after < forecast.MAX_YEARS and by != kind and not (kind == "gpu" and by == "vram"):
            wt = base.fc.get(wid).title
            effects.append(tr(f"Срок для задачи «{wt}» это не продлит: мешает {forecast.kind_label(by)}.",
                              f"It won't extend how long “{wt}” lasts: the {forecast.kind_label(by)} is the limit."))

    gain = after - before
    if wid in GAME_WORKLOADS:
        ratios = [g.after / g.now for g in games if g.now > 0]
        if ratios:
            gain += 3 * math.log2(sum(ratios) / len(ratios))
    if use and use.ram_total and "ram_gb" in up.get("replace", {}):
        pressure = use.ram_used / use.ram_total
        gain += 2 * max(0.0, pressure - 0.75) * 4 + min(use.ram_episodes, 10) * 0.2
    if kind in ("ssd", "system") and hw.get("disk") == "hdd":
        gain += 2
    return Simulation(up, pick(up, "title"), price(up), games, years, effects, gain)


def _ram_effect(use: Usage, new_gb: float) -> str:
    pct_now = use.ram_used / use.ram_total * 100
    pct_after = min(use.ram_used / (new_gb * 2**30) * 100, 100)
    have = forecast._ram_gb(use.ram_total)
    top = ""
    if use.top_mem and use.top_mem[0][1] >= 300 * 2**20:  # называем, только если программа правда заметная
        name, mem = use.top_mem[0]
        top = tr(f", больше всех — {name} ({human_size(mem)})", f", the biggest is {name} ({human_size(mem)})")
    text = tr(f"Сейчас занято {human_size(use.ram_used)} из {have:.0f} ГБ ({pct_now:.0f}%){top}. "
              f"С {new_gb:.0f} ГБ было бы занято {pct_after:.0f}%",
              f"Right now {human_size(use.ram_used)} of {have:.0f} GB is in use ({pct_now:.0f}%){top}. "
              f"With {new_gb:.0f} GB it would be {pct_after:.0f}%")
    if pct_now >= 80:
        who = use.top_mem[0][0] if top else tr("программы", "apps")
        text += tr(f": {who} перестанет упираться в память, и система не будет выгружать её на диск.",
                   f": {who} would stop running into the memory limit and the system would stop swapping to disk.")
    elif pct_now < 60:
        text += tr(". Прямо сейчас памяти хватает, так что это запас на будущее, а не ускорение.",
                   ". There's enough memory right now, so this is headroom for the future, not a speed-up.")
    else:
        text += "."
    if use.ram_episodes:
        text += tr(f" За месяц память кончалась {times(use.ram_episodes)}.",
                   f" Over the month memory ran out {times(use.ram_episodes)}.")
    return text


def simulate_all(hw: dict, db: dict, use: Usage | None, wid: str, measured: dict | None = None,
                 now: float | None = None) -> tuple[Side, list[Simulation]]:
    base = side(hw, db, now)
    sims = [simulate(hw, up, db, use, wid, base, measured, now) for up in options(hw, base, db)]
    return base, sims


def best(sims: list[Simulation]) -> Simulation | None:
    """Лучшее вложение: больше всего пользы на рубль."""
    useful = [s for s in sims if s.gain > 0.3]
    if not useful:
        return None
    return max(useful, key=lambda s: s.gain / max(sum(s.price) / 2, 1))


def no_options_text(hw: dict) -> str:
    if hw.get("mac"):
        return tr("В Mac детали не меняются — можно сравнить только с новой моделью.",
                  "Parts can't be changed in a Mac — only a new model can be compared.")
    if hw.get("laptop"):
        return tr("В этом ноутбуке менять почти нечего: память уже достаточная, диск — SSD.",
                  "There's little to upgrade in this laptop: memory is already enough and the disk is an SSD.")
    return tr("Заметно быстрее деталей из списка для этого компьютера нет.",
              "No parts in the list would make this computer noticeably faster.")


def disclaimer() -> str:
    return tr("Кадры и сроки — оценка по типичным результатам тестов на 1080p и высоких настройках, а не замер. "
              "Если укажете, сколько кадров видите сами, расчёт подстроится под ваши настройки. "
              "Цены примерные, проверяйте в магазине.",
              "Frame rates and years are estimates from typical 1080p high-settings benchmarks, not a measurement. "
              "If you enter the FPS you actually see, the estimate adjusts to your settings. "
              "Prices are approximate; check the store.")
