"""Игровой режим: игра запускается — фон уступает ей процессор, после игры всё как было.

WhySlow замечает запуск игры (по папкам Steam, Epic, GOG и других магазинов, по списку
известных игр и по играм, которые человек добавил сам) и предлагает план: понизить
приоритет тяжёлых фоновых программ, приостановить ненужные на время игры, поднять
приоритет самой игры и включить схему питания «Высокая производительность».

Ничего не делается без согласия: план показывается с галочками, и только после
«Включить» он выполняется. Для игры, где человек поставил «включать сам», план с теми
же пунктами применяется при следующих запусках без вопроса. Всё, что изменено, пишется
в файл состояния; когда игра закрывается (или WhySlow падает и запускается снова),
приоритеты, пауза и питание возвращаются как были. В конце — отчёт: сколько процессора
освободилось для игры и сколько кадров в секунду стало, если есть чем мерить FPS.
"""

from __future__ import annotations

import csv
import io
import os
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

from . import journal, settings
from .i18n import tr
from .util import IS_LINUX, IS_WINDOWS, app_dir, human_size, read_json, run, write_json

# Папки, куда магазины ставят игры
GAME_DIRS = re.compile(
    r"(steamapps[\\/]common|epic games|gog games|gog galaxy[\\/]games|riot games|ubisoft game launcher[\\/]games|"
    r"ea games|origin games|xboxgames|battle\.net[\\/]|blizzard|wargaming|lesta|my\.games|"
    r"roblox[\\/]versions|minecraft|\.minecraft|heroic[\\/]|lutris[\\/]games)", re.I)

# Известные игры, которые ставятся куда попало
KNOWN_GAMES = re.compile(
    r"^(cs2|csgo|dota2|valorant-win64-shipping|fortniteclient-win64-shipping|gta5|gtav|playgtav|r5apex|"
    r"eldenring|cyberpunk2077|overwatch|league of legends|rocketleague|tslgame|robloxplayerbeta|genshinimpact|"
    r"zenlesszonezero|starrail|witcher3|rdr2|eurotrucks2|amtrucks|worldoftanks|wotblitz|warthunder|aces|"
    r"minecraft\.windows|javaw|wow|wowclassic|diablo iv|hearthstone|destiny2|pathofexile|pathofexile_x64|"
    r"baldur's gate 3|bg3|bg3_dx11|starfield|hogwartslegacy|forzahorizon5|forzahorizon4|palworld-win64-shipping|"
    r"helldivers2|deadlock|marvel-win64-shipping|escapefromtarkov|rust|rustclient|arma3_x64|squadgame|"
    r"cod|modernwarfare|blackops6|bf2042|re4|monsterhunterwilds|stardew valley|terraria|factorio|"
    r"sims4|ts4_x64|civilizationvi|civ6|hoi4|eu4|stellaris|cities|cities2)(\.exe)?$", re.I)

# Магазины, античиты и служебные процессы рядом с играми — не игры
NOT_GAMES = re.compile(
    r"(launcher|crash|report|helper|setup|install|unins|redist|vcredist|directx|dxsetup|updater|update|"
    r"easyanticheat|battleye|beservice|vanguard|steam|epicgames|unrealcef|galaxyclient|origin|eadesktop|"
    r"ubisoftconnect|upc|battle\.net|agent|overlay|webhelper|cefprocess|bootstrapper|benchmark|config|"
    r"server|dedicated|editor|modmanager|anticheat|service|svc)", re.I)

# Чем пользуются прямо во время игры — не трогаем
GAME_FRIENDLY = re.compile(
    r"^(discord|teamspeak|ts3client|mumble|skype|zoom|telegram|obs|obs64|nvcontainer|nvidia|amdrsserv|"
    r"radeonsoftware|msiafterburner|rtss|steam|steamwebhelper|epicgameslauncher|gameoverlayui|"
    r"audiodg|spotify|whyslow|python|pythonw|explorer|dwm|csrss|winlogon|windowserver)", re.I)

HEAVY_CPU = 2.0          # % всего процессора — уже заметно игре
HEAVY_MEM = 300 * 2**20
HIGH_PERF = "8c5e7fda-e8bf-4a96-9a85-a6e23a8c635c"   # «Высокая производительность»
ULTIMATE = "e9a42b02-d5df-448d-aa00-03f14749eb61"    # «Максимальная производительность»


@dataclass
class Game:
    name: str
    pid: int
    exe: str = ""
    why: str = ""


@dataclass
class Action:
    kind: str            # game_priority / lower / suspend / power
    title: str
    detail: str = ""
    checked: bool = True
    name: str = ""
    pids: list = field(default_factory=list)


def is_game(name: str, exe: str = "", extra: list[str] | None = None) -> str:
    """Почему это игра (пустая строка — не игра)."""
    base = name.lower()
    stem = base[:-4] if base.endswith(".exe") else base
    if any(stem == g.lower().removesuffix(".exe") for g in extra or []):
        return tr("вы добавили эту игру сами", "you added this game yourself")
    if base == "javaw.exe" or stem == "javaw":
        return tr("Minecraft (Java)", "Minecraft (Java)") if "minecraft" in exe.lower() else ""
    if KNOWN_GAMES.match(stem):
        return tr("известная игра", "a known game")
    if NOT_GAMES.search(stem):
        return ""
    m = GAME_DIRS.search(exe or "")
    if m:
        return tr(f"установлена в папку игр ({m.group(1)})", f"installed in a games folder ({m.group(1)})")
    return ""


def detect(procs, extra: list[str] | None = None, ignore: list[str] | None = None) -> Game | None:
    """procs: [(pid, имя, путь)] — первая найденная игра (у которой путь известен — выше)."""
    ignore = {i.lower() for i in ignore or []}
    found = []
    for pid, name, exe in procs:
        if not name or name.lower() in ignore or name.lower().removesuffix(".exe") in ignore:
            continue
        why = is_game(name, exe or "", extra)
        if why:
            found.append(Game(name, pid, exe or "", why))
    if not found:
        return None
    return sorted(found, key=lambda g: (not g.exe, g.pid))[0]


def running() -> list[tuple[int, str, str]]:
    out = []
    me = os.getpid()
    for p in psutil.process_iter(["name", "exe"]):
        if p.pid in (0, me):
            continue
        out.append((p.pid, p.info.get("name") or "", p.info.get("exe") or ""))
    return out


def detect_running() -> Game | None:
    return detect(running(), settings.get("games"), settings.get("games_ignore"))


# ------------------------------------------------------------------- план ---

def plan(game: Game, groups) -> list[Action]:
    """План для игры по текущим процессам (ProcGroup с оценками базы). Ничего не меняет."""
    acts = [Action("game_priority", tr(f"Поднять приоритет {game.name}", f"Raise {game.name}'s priority"),
                   tr("Игра получит процессор раньше фоновых программ.", "The game gets the CPU before background apps."),
                   True, game.name, [game.pid])]
    if not IS_WINDOWS:
        acts[0].checked = False
        acts[0].detail += tr(" Здесь для этого нужны права администратора.", " This needs administrator rights here.")
    for g in sorted(groups, key=lambda g: g.cpu, reverse=True):
        if game.pid in g.pids or g.name.lower() == game.name.lower() or g.system or GAME_FRIENDLY.match(g.name):
            continue
        v = g.verdict.verdict if g.verdict else "unknown"
        if v == "keep" or (g.cpu < HEAVY_CPU and g.mem < HEAVY_MEM):
            continue
        load = tr(f"{g.cpu:.0f}% ЦП, {human_size(g.mem)}", f"{g.cpu:.0f}% CPU, {human_size(g.mem)}")
        if v == "safe" and g.cpu >= HEAVY_CPU:
            acts.append(Action("suspend", tr(f"Приостановить {g.name} на время игры", f"Pause {g.name} while playing"),
                               tr(f"Сейчас {load}. После игры продолжит работу с того же места.",
                                  f"Now {load}. Resumes where it left off after the game."),
                               False, g.name, list(g.pids)))
        acts.append(Action("lower", tr(f"Понизить приоритет {g.name}", f"Lower {g.name}'s priority"),
                           tr(f"Сейчас {load}. Программа продолжит работать, но уступит процессор игре.",
                              f"Now {load}. Keeps running but yields the CPU to the game."),
                           v in ("safe", "optional", "bad"), g.name, list(g.pids)))
    pw = power_action()
    if pw:
        acts.append(pw)
    return acts


def power_action() -> Action | None:
    if IS_WINDOWS:
        cur = _win_scheme()
        if not cur or cur in (HIGH_PERF, ULTIMATE):
            return None
        return Action("power", tr("Включить питание «Высокая производительность»", "Switch to the High performance power plan"),
                      tr("Процессор не будет сбрасывать частоту посреди боя. После игры вернётся текущая схема.",
                         "The CPU won't drop its clock mid-fight. The current plan comes back after the game."))
    if IS_LINUX and shutil.which("powerprofilesctl"):
        code, cur = run(["powerprofilesctl", "get"], timeout=3)
        if code == 0 and cur.strip() and cur.strip() != "performance":
            return Action("power", tr("Включить профиль питания «performance»", "Switch to the performance power profile"),
                          tr("После игры вернётся текущий профиль.", "The current profile comes back after the game."))
    return None  # macOS: режим энергосбережения меняется только с паролем администратора


def _win_scheme() -> str:
    code, out = run(["powercfg", "/getactivescheme"], timeout=5)
    m = re.search(r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})", out, re.I)
    return m.group(1).lower() if code == 0 and m else ""


# ------------------------------------------------------- применение и откат ---

def _state_path() -> Path:
    return app_dir() / "gamemode.json"


@dataclass
class Session:
    game: dict
    started: float
    applied_at: float = 0.0
    undo: list = field(default_factory=list)   # [{kind, pid, created, old}] — что вернуть
    titles: list = field(default_factory=list)
    # средние за «до» и «после»: сумма и число замеров
    before: dict = field(default_factory=lambda: {"n": 0, "bg_cpu": 0.0, "game_cpu": 0.0, "free": 0.0})
    after: dict = field(default_factory=lambda: {"n": 0, "bg_cpu": 0.0, "game_cpu": 0.0, "free": 0.0})
    fps_before: dict = field(default_factory=dict)
    fps_after: dict = field(default_factory=dict)

    def save(self) -> None:
        write_json(_state_path(), asdict(self))

    def feed(self, total_cpu: float, groups, free_mem: int) -> None:
        """Замер раз в пару секунд: сколько процессора у игры и у всего остального."""
        pid = self.game["pid"]
        game_cpu = sum(g.cpu for g in groups if pid in g.pids)
        bucket = self.after if self.applied_at else self.before
        bucket["n"] += 1
        bucket["game_cpu"] += game_cpu
        bucket["bg_cpu"] += max(total_cpu - game_cpu, 0)
        bucket["free"] += free_mem


def _created(pid: int) -> float:
    try:
        return psutil.Process(pid).create_time()
    except psutil.Error:
        return 0.0


def _same(pid: int, created: float) -> psutil.Process | None:
    """Процесс, если это всё ещё тот же (номер не достался новому процессу)."""
    try:
        p = psutil.Process(pid)
        return p if abs(p.create_time() - created) < 1 else None
    except psutil.Error:
        return None


def _nice(kind: str):
    if IS_WINDOWS:
        return psutil.ABOVE_NORMAL_PRIORITY_CLASS if kind == "up" else psutil.BELOW_NORMAL_PRIORITY_CLASS
    return -5 if kind == "up" else 10


def apply(game: Game, actions: list[Action], session: Session | None = None, progress=lambda _t: None) -> tuple[Session, list[str]]:
    """Выполняет отмеченные пункты. Возвращает сессию (для отката) и ошибки."""
    s = session or start(game)
    errors = []
    for a in actions:
        if not a.checked:
            continue
        progress(a.title)
        try:
            if a.kind == "power":
                _apply_power(s)
            else:
                for pid in a.pids:
                    _apply_proc(s, a.kind, pid)
            s.titles.append(a.title)
        except (psutil.Error, OSError) as e:
            errors.append(f"{a.title}: {_why(e)}")
        s.save()  # после каждого шага: если WhySlow упадёт, откат всё равно найдёт, что вернуть
    s.applied_at = time.time()
    s.save()
    journal.add("gamemode", tr(f"Игровой режим для {game.name}: {len(s.titles)} изм.", f"Game mode for {game.name}: {len(s.titles)} changes"),
                {"game": game.name, "titles": s.titles}, undoable=False)
    return s, errors


def _why(e: Exception) -> str:
    if isinstance(e, psutil.AccessDenied):
        return tr("нет прав", "access denied")
    if isinstance(e, psutil.NoSuchProcess):
        return tr("программа уже закрылась", "the program has already exited")
    return str(e)


def _apply_proc(s: Session, kind: str, pid: int) -> None:
    try:
        p = psutil.Process(pid)
        created = p.create_time()
        if kind == "suspend":
            p.suspend()
            s.undo.append({"kind": "suspend", "pid": pid, "created": created})
            return
        old = p.nice()
        p.nice(_nice("up" if kind == "game_priority" else "down"))
        s.undo.append({"kind": "nice", "pid": pid, "created": created, "old": int(old)})
    except psutil.NoSuchProcess:
        return  # процесс из группы успел закрыться — нечего менять


def _apply_power(s: Session) -> None:
    if IS_WINDOWS:
        old = _win_scheme()
        code, out = run(["powercfg", "/setactive", HIGH_PERF], timeout=10)
        if code != 0:  # на некоторых ноутбуках схемы нет — пробуем её добавить
            run(["powercfg", "-duplicatescheme", HIGH_PERF], timeout=10)
            code, out = run(["powercfg", "/setactive", HIGH_PERF], timeout=10)
        if code != 0:
            raise OSError(out.strip() or "powercfg")
        s.undo.append({"kind": "power", "old": old})
    elif IS_LINUX:
        code, old = run(["powerprofilesctl", "get"], timeout=3)
        code2, out = run(["powerprofilesctl", "set", "performance"], timeout=5)
        if code or code2:
            raise OSError(out.strip() or "powerprofilesctl")
        s.undo.append({"kind": "power", "old": old.strip()})


def restore(s: Session) -> list[str]:
    """Возвращает всё как было. Закрывшиеся процессы пропускает. Удаляет файл состояния."""
    errors = []
    for u in reversed(s.undo):
        try:
            if u["kind"] == "power":
                if IS_WINDOWS:
                    run(["powercfg", "/setactive", u["old"]], timeout=10)
                elif IS_LINUX:
                    run(["powerprofilesctl", "set", u["old"]], timeout=5)
                continue
            p = _same(u["pid"], u["created"])
            if not p:
                continue
            if u["kind"] == "suspend":
                p.resume()
            elif u["kind"] == "nice":
                p.nice(u["old"])
        except (psutil.Error, OSError, KeyError) as e:
            errors.append(_why(e))
    s.undo = []
    try:
        _state_path().unlink()
    except OSError:
        pass
    return errors


def recover() -> Session | None:
    """После падения или перезагрузки WhySlow: незакрытая сессия откатывается."""
    data = read_json(_state_path(), None)
    if not isinstance(data, dict):
        return None
    try:
        s = Session(**data)
    except TypeError:
        _state_path().unlink(missing_ok=True)
        return None
    restore(s)
    return s


def start(game: Game) -> Session:
    """Сессия с момента, когда игра замечена: до «Включить» копятся замеры «до»."""
    d = asdict(game)
    d["created"] = _created(game.pid)
    return Session(d, time.time())


def game_alive(s: Session) -> bool:
    return _same(s.game["pid"], s.game.get("created", 0)) is not None


# ------------------------------------------------------------------- отчёт ---

def _avg(b: dict, key: str) -> float:
    return b[key] / b["n"] if b.get("n") else 0.0


def report(s: Session) -> list[str]:
    """Что дал игровой режим — строками для окна и уведомления."""
    lines = []
    b, a = s.before, s.after
    if b.get("n") and a.get("n"):
        bg0, bg1 = _avg(b, "bg_cpu"), _avg(a, "bg_cpu")
        lines.append(tr(f"Фоновые программы: {bg0:.0f}% процессора до → {bg1:.0f}% в игровом режиме.",
                        f"Background apps: {bg0:.0f}% CPU before → {bg1:.0f}% in game mode."))
        g0, g1 = _avg(b, "game_cpu"), _avg(a, "game_cpu")
        lines.append(tr(f"Игре досталось {g0:.0f}% → {g1:.0f}% процессора.", f"The game got {g0:.0f}% → {g1:.0f}% of the CPU."))
        f0, f1 = _avg(b, "free"), _avg(a, "free")
        if abs(f1 - f0) > 50 * 2**20:
            lines.append(tr(f"Свободной памяти: {human_size(f0)} → {human_size(f1)}.",
                            f"Free memory: {human_size(f0)} → {human_size(f1)}."))
    elif a.get("n"):
        lines.append(tr(f"В игровом режиме фон занимал в среднем {_avg(a, 'bg_cpu'):.0f}% процессора.",
                        f"In game mode, background apps used {_avg(a, 'bg_cpu'):.0f}% CPU on average."))
    if s.fps_before.get("avg") and s.fps_after.get("avg"):
        f0, f1 = s.fps_before, s.fps_after
        gain = (f1["avg"] / f0["avg"] - 1) * 100
        lines.append(tr(f"FPS: {f0['avg']:.0f} → {f1['avg']:.0f} ({gain:+.0f}%), "
                        f"1% худших кадров: {f0.get('low', 0):.0f} → {f1.get('low', 0):.0f}.",
                        f"FPS: {f0['avg']:.0f} → {f1['avg']:.0f} ({gain:+.0f}%), "
                        f"1% lows: {f0.get('low', 0):.0f} → {f1.get('low', 0):.0f}."))
    elif s.fps_after.get("avg"):
        lines.append(tr(f"FPS в игровом режиме: {s.fps_after['avg']:.0f}.", f"FPS in game mode: {s.fps_after['avg']:.0f}."))
    if s.applied_at:
        took = (time.time() - s.applied_at) / 60
        if took >= 1:
            lines.append(tr(f"Игровой режим работал {took:.0f} мин.", f"Game mode was on for {took:.0f} min."))
    return lines or [tr("Слишком мало замеров для отчёта.", "Too few samples for a report.")]


# --------------------------------------------------------------------- FPS ---

PRESENTMON_URL = "https://github.com/GameTechDev/PresentMon/releases"


def presentmon() -> str:
    """Путь к PresentMon (бесплатная утилита Intel для замера кадров), если он есть."""
    if not IS_WINDOWS:
        return ""
    folder = app_dir() / "presentmon"
    for p in sorted(folder.glob("PresentMon*.exe"), reverse=True) if folder.is_dir() else []:
        return str(p)
    return shutil.which("PresentMon") or shutil.which("presentmon") or ""


def parse_frames(text: str) -> dict:
    """CSV PresentMon → средний FPS и «1% худших кадров». Понимает столбцы v1 и v2."""
    times = []
    for row in csv.DictReader(io.StringIO(text)):
        v = row.get("MsBetweenPresents") or row.get("FrameTime") or row.get("msBetweenPresents")
        try:
            ms = float(v)
        except (TypeError, ValueError):
            continue
        if 0 < ms < 1000:
            times.append(ms)
    if len(times) < 10:
        return {}
    avg = 1000 / (sum(times) / len(times))
    worst = sorted(times, reverse=True)[:max(len(times) // 100, 1)]
    low = 1000 / (sum(worst) / len(worst))
    return {"avg": round(avg, 1), "low": round(low, 1), "frames": len(times)}


def measure_fps(pid: int, seconds: int = 15) -> dict:
    """Замер кадров игры на seconds секунд. Пусто — нечем мерить или игра не рисует."""
    exe = presentmon()
    if not exe:
        return {}
    cmd = [exe, "--process_id", str(pid), "--output_stdout", "--timed", str(seconds),
           "--terminate_after_timed", "--stop_existing_session", "--session_name", "WhySlowFPS"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=seconds + 15,
                           creationflags=0x08000000 if IS_WINDOWS else 0)
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_frames(p.stdout or "")


def free_memory() -> int:
    return psutil.virtual_memory().available

