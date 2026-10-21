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
import re
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import psutil

from .. import gamerestore, journal, settings
from ..gamerestore import same_process as _same, why as _why
from ..i18n import tr
from ..util import IS_LINUX, IS_WINDOWS, app_dir, human_size, is_admin, run, write_json

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


# ------------------------------------------------------------------- план ---

def can_lower() -> bool:
    """В macOS и Linux понизить приоритет может любой, а вернуть обратно — только root.
    Без прав такое изменение не откатить, поэтому его там и не предлагаем."""
    return IS_WINDOWS or is_admin()


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
        if not can_lower():
            continue
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
    return gamerestore.state_path()


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
    errors = gamerestore.undo(s.undo)
    s.undo = []
    _state_path().unlink(missing_ok=True)
    return errors


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


# ------------------------------------------------------------------ окно ---

from PySide6.QtCore import QObject, Qt, QTimer  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QCheckBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from .. import gui, license  # noqa: E402
from ..gui import C, Switch, _esc, accent, muted, pt, px, section  # noqa: E402

class GamesTab(QWidget):
    """Игровой режим: какая игра запущена, план с галочками, отчёт после игры."""

    def __init__(self, main, ctl: "_Games"):
        super().__init__()
        self.main, self.ctl = main, ctl
        self.boxes: list[QCheckBox] = []
        lay = QVBoxLayout(self)
        lay.setContentsMargins(px(20), px(18), px(20), px(18))
        lay.setSpacing(px(10))
        title = QLabel(accent(tr("Игровой режим", "Game mode")))
        title.setObjectName("h1")
        lay.addWidget(title)
        lay.addWidget(muted(tr("WhySlow замечает запуск игры и предлагает план: понизить приоритет тяжёлых фоновых "
                               "программ, приостановить ненужные на время игры, поднять приоритет игры и включить "
                               "питание на максимум. Ничего не меняется без «Включить», а когда игра закрывается, "
                               "всё возвращается как было и показывается, что это дало.",
                               "WhySlow notices when a game starts and offers a plan: lower heavy background apps, "
                               "pause unneeded ones while you play, raise the game's priority and set power to maximum. "
                               "Nothing changes until you press “Turn on”, and when the game closes everything is "
                               "restored and you see what it gave you.")))
        opts = QHBoxLayout()
        self.sw = Switch(settings.get("gamemode"))
        self.sw.clicked.connect(self._toggle)
        opts.addWidget(self.sw)
        opts.addWidget(QLabel(tr("Замечать запуск игр", "Watch for games")))
        opts.addStretch()
        b_add = QPushButton(tr("Добавить игру…", "Add a game…"))
        b_add.clicked.connect(self.add_game)
        opts.addWidget(b_add)
        lay.addLayout(opts)

        self.game_label = QLabel()
        self.game_label.setWordWrap(True)
        self.game_label.setStyleSheet(f"font-size: {pt(13)}; font-weight: 700;")
        lay.addWidget(self.game_label)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.list = QWidget()
        self.list_lay = QVBoxLayout(self.list)
        self.list_lay.setContentsMargins(0, 0, px(6), 0)
        self.list_lay.setSpacing(px(8))
        self.scroll.setWidget(self.list)
        lay.addWidget(self.scroll, 1)

        row = QHBoxLayout()
        self.cb_auto = QCheckBox(tr("Для этой игры включать без вопроса", "Turn on for this game without asking"))
        row.addWidget(self.cb_auto)
        row.addStretch()
        self.b_ignore = QPushButton(tr("Это не игра", "Not a game"))
        self.b_ignore.clicked.connect(self.not_a_game)
        row.addWidget(self.b_ignore)
        self.b_off = QPushButton(tr("Выключить", "Turn off"))
        self.b_off.clicked.connect(lambda: self.ctl.finish(manual=True))
        row.addWidget(self.b_off)
        self.b_on = QPushButton(tr("Включить", "Turn on"))
        self.b_on.setObjectName("primary")
        self.b_on.setCursor(Qt.PointingHandCursor)
        self.b_on.clicked.connect(self.turn_on)
        row.addWidget(self.b_on)
        lay.addLayout(row)
        self.fps = muted("")
        self.fps.setTextFormat(Qt.RichText)
        self.fps.setOpenExternalLinks(True)
        lay.addWidget(self.fps)
        self.refresh()

    def showEvent(self, e):
        super().showEvent(e)
        self.refresh()

    def _toggle(self):
        on = not settings.get("gamemode")
        settings.set("gamemode", on)
        self.sw.setChecked(on)

    def _clear(self):
        while self.list_lay.count():
            w = self.list_lay.takeAt(0).widget()
            if w:
                w.deleteLater()

    def refresh(self):
        """Перерисовать по состоянию MainWindow: нет игры / игра замечена / режим включён / отчёт."""
        s = self.ctl.session
        self._clear()
        self.boxes = []
        active = bool(s and s.applied_at)
        if s:
            name = s.game["name"]
            self.game_label.setText(tr(f"Игровой режим включён: {name}", f"Game mode is on: {name}") if active
                                    else tr(f"Запущена игра: {name}", f"Game running: {name}"))
            if active:
                for t in s.titles:
                    self.list_lay.addWidget(muted("✓ " + t))
            else:
                self.list_lay.addWidget(muted(s.game.get("why", "")))
                for a in self.ctl.actions:
                    self.list_lay.addWidget(self._row(a))
                if not self.ctl.actions:
                    self.list_lay.addWidget(muted(tr("Мешать игре нечему — фон и так тихий.",
                                                     "Nothing is in the game's way — the background is already quiet.")))
            self.cb_auto.setChecked(name.lower() in settings.get("games_auto"))
        else:
            self.game_label.setText(tr("Игра не запущена", "No game running"))
            if self.ctl.report:
                self.list_lay.addWidget(section(tr("Что дал игровой режим", "What game mode gave you")))
                for line in self.ctl.report:
                    self.list_lay.addWidget(muted(line))
            else:
                self.list_lay.addWidget(muted(tr("Запустите игру — WhySlow заметит её и покажет план здесь. Если игра "
                                                 "не определилась, нажмите «Добавить игру…».",
                                                 "Start a game — WhySlow will notice it and show the plan here. If it "
                                                 "isn't detected, press “Add a game…”.")))
        self.list_lay.addStretch()
        self.cb_auto.setEnabled(bool(s))
        self.b_on.setEnabled(bool(s) and not active)
        self.b_off.setEnabled(active)
        self.b_ignore.setEnabled(bool(s) and not active)
        pm = presentmon()
        if not IS_WINDOWS:
            self.fps.setText(tr("Замер FPS пока есть только в Windows; отчёт покажет, сколько процессора освободилось для игры.",
                                "FPS measuring is Windows-only for now; the report shows how much CPU was freed for the game."))
        elif pm:
            self.fps.setText(tr("FPS меряется через PresentMon: 15 секунд до и после включения.",
                                "FPS is measured with PresentMon: 15 seconds before and after turning it on."))
        else:
            folder = _esc(str(app_dir() / "presentmon"))
            self.fps.setText(tr(f"Чтобы отчёт показывал FPS, скачайте бесплатный "
                                f"<a style='color:{C['accent2']}' href='{PRESENTMON_URL}'>PresentMon</a> "
                                f"(консольную версию) и положите его в {folder}.",
                                f"For FPS in the report, download the free "
                                f"<a style='color:{C['accent2']}' href='{PRESENTMON_URL}'>PresentMon</a> "
                                f"(console version) and put it into {folder}."))

    def _row(self, a: Action) -> QFrame:
        card = QFrame()
        card.setObjectName("card")
        h = QHBoxLayout(card)
        h.setContentsMargins(px(14), px(10), px(14), px(10))
        h.setSpacing(px(12))
        cb = QCheckBox()
        cb.setChecked(a.checked)
        self.boxes.append(cb)
        h.addWidget(cb, 0, Qt.AlignTop)
        v = QVBoxLayout()
        v.setSpacing(px(2))
        t = QLabel(a.title)
        t.setWordWrap(True)
        t.setStyleSheet("font-weight: 700;")
        v.addWidget(t)
        if a.detail:
            v.addWidget(muted(a.detail))
        h.addLayout(v, 1)
        card.mousePressEvent = lambda _e, cb=cb: cb.toggle()
        return card

    def chosen(self) -> list[Action]:
        for a, b in zip(self.ctl.actions, self.boxes):
            a.checked = b.isChecked()
        return [a for a in self.ctl.actions if a.checked]

    def turn_on(self):
        s = self.ctl.session
        if not s:
            return
        chosen = self.chosen()
        auto = dict(settings.get("games_auto"))
        name = s.game["name"].lower()
        if self.cb_auto.isChecked():
            auto[name] = sorted({a.kind for a in chosen})
        else:
            auto.pop(name, None)
        settings.set("games_auto", auto)
        self.ctl.apply(chosen)

    def not_a_game(self):
        s = self.ctl.session
        if not s:
            return
        ignore = list(settings.get("games_ignore"))
        ignore.append(s.game["name"])
        settings.set("games_ignore", ignore)
        self.ctl.session = None
        self.ctl.actions = []
        self.refresh()

    def add_game(self):
        from PySide6.QtWidgets import QInputDialog
        names = sorted({g.name for g in self.ctl.groups if not g.system}, key=str.lower)
        if not names:
            return
        name, ok = QInputDialog.getItem(self, tr("Добавить игру", "Add a game"),
                                        tr("Запущенная программа, которую считать игрой:", "Running program to treat as a game:"),
                                        names, 0, False)
        if not ok or not name:
            return
        games = list(settings.get("games"))
        if name not in games:
            games.append(name)
            settings.set("games", games)
        settings.set("games_ignore", [g for g in settings.get("games_ignore") if g.lower() != name.lower()])
        self.ctl.look()


class _Games(QObject):
    """Фоновая часть: раз в 10 секунд ищет игру, ведёт сессию и откатывает её."""

    def __init__(self, main):
        super().__init__(main)
        self.main = main
        self.tab: GamesTab | None = None
        self.session: Session | None = None
        self.actions: list[Action] = []
        self.report: list[str] = []
        self.groups: list = []
        self.cpu = 0.0
        self.skip: set[int] = set()  # игры, для которых режим выключили вручную
        self._tick = 0
        QTimer.singleShot(0, self._attach)  # Sampler создаётся в MainWindow позже вкладок

    def _attach(self):
        self.main.sampler.stats.connect(self.on_stats)
        self.main.sampler.procs.connect(self.on_procs)
        QApplication.instance().aboutToQuit.connect(self.quit)

    def on_stats(self, s: dict):
        self.cpu = s["cpu"]

    def on_procs(self, groups):
        self.groups = groups
        s = self.session
        if s:
            if not game_alive(s):
                self.finish()
                return
            s.feed(self.cpu, groups, free_memory())
            return
        self._tick += 1
        if self._tick % 5 == 0 and settings.get("gamemode"):  # раз в 10 секунд
            self.look()

    def refresh(self):
        if self.tab:
            self.tab.refresh()

    def look(self):
        if self.session or not license.has_premium("gamemode"):
            return
        groups = list(self.groups)
        procs = [(g.pids[0], g.name, g.exe) for g in groups if g.pids]
        game = detect(procs, settings.get("games"), settings.get("games_ignore"))
        if not game or game.pid in self.skip:
            return
        s = self.session = start(game)
        self.report = []
        gui.run_async(lambda _p: plan(game, groups), lambda acts: self._planned(s, game, acts),
                      lambda _e: self._planned(s, game, []))
        if presentmon():
            gui.run_async(lambda _p: measure_fps(game.pid), lambda r: s.fps_before.update(r), lambda _e: None)

    def _planned(self, s: Session, game: Game, actions: list[Action]):
        if self.session is not s:
            return  # игру уже закрыли или пометили «не игра»
        auto = settings.get("games_auto").get(game.name.lower())
        self.actions = actions
        self.refresh()
        if auto is not None:  # человек уже согласился на эти пункты для этой игры
            for a in actions:
                a.checked = a.kind in auto and (a.checked or a.kind == "suspend")
            self.apply([a for a in actions if a.checked])
            return
        self.main.notify(tr(f"Запущена игра {game.name}", f"{game.name} started"),
                         tr("Нажмите, чтобы посмотреть план игрового режима.", "Click to see the game mode plan."), "games")

    def apply(self, chosen: list[Action]):
        s = self.session
        if not s or s.applied_at:
            return
        game = Game(s.game["name"], s.game["pid"], s.game.get("exe", ""), s.game.get("why", ""))

        def done(res):
            _s, errors = res
            self.main.journal_tab.reload()
            self.refresh()
            self.main.status(tr(f"Игровой режим для {game.name} включён", f"Game mode for {game.name} is on") +
                             (tr(f" (не получилось: {len(errors)})", f" ({len(errors)} failed)") if errors else ""))
            if errors and self.main.isVisible():
                QMessageBox.warning(self.main, tr("Не всё получилось", "Not everything worked"), "\n".join(errors))
            if presentmon():  # замер «после» — когда игра успокоится
                QTimer.singleShot(10000, lambda: self.session is s and gui.run_async(
                    lambda _p: measure_fps(game.pid), lambda r: s.fps_after.update(r), lambda _e: None))

        gui.run_async(lambda progress: apply(game, chosen, s, progress), done)

    def finish(self, manual: bool = False):
        s, self.session = self.session, None
        if not s:
            return
        if manual:
            self.skip.add(s.game["pid"])
        self.report = report(s) if s.applied_at else []
        self.actions = []
        self.refresh()
        if not s.applied_at:
            return
        name = s.game["name"]
        gui.run_async(lambda _p: restore(s), lambda _e: None)
        self.main.status(tr(f"Игровой режим для {name} выключен, всё вернул как было",
                            f"Game mode for {name} is off, everything restored"))
        if not manual:
            self.main.notify(tr(f"{name} закрыта — всё вернул как было", f"{name} closed — everything restored"),
                             self.report[0], "games")

    def quit(self):
        s, self.session = self.session, None
        if s and s.applied_at:  # игра ещё идёт, а WhySlow закрывают — вернуть всё сразу
            restore(s)


_ctl: _Games | None = None


def build(main):
    """Вкладка «Игры» (зовёт license_ui.PremiumGate). Фоновая часть создаётся один раз."""
    global _ctl
    if _ctl is None:
        _ctl = _Games(main)
    _ctl.tab = GamesTab(main, _ctl)
    return _ctl.tab
