"""Автозагрузка: Windows (реестр, папка «Автозагрузка», Планировщик заданий,
службы), Linux (XDG autostart + systemd), macOS (объекты входа, LaunchAgents,
LaunchDaemons). Отключение сделано так же, как в системных инструментах, —
поэтому его всегда можно включить обратно."""

from __future__ import annotations

import json
import os
import shutil
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import journal
from .i18n import tr
from .knowledge import KnowledgeBase, Verdict
from .util import IS_LINUX, IS_MAC, IS_WINDOWS, backup_dir, is_admin, run


class ActionError(Exception):
    pass


@dataclass
class StartupItem:
    name: str
    command: str
    source: str
    enabled: bool
    kind: str
    meta: dict = field(default_factory=dict)
    can_delete: bool = False
    can_toggle: bool = True
    needs_admin: bool = False
    system: bool = False  # системные службы и задания прячутся за флажком
    verdict: Verdict | None = None


def list_items(kb: KnowledgeBase | None = None) -> list[StartupItem]:
    global last_error
    last_error = ""
    if IS_WINDOWS:
        items = _win_list() + _win_tasks_list() + _win_services_list()
    elif IS_LINUX:
        items = _xdg_list() + _systemd_list()
    elif IS_MAC:
        items = _mac_list()
    else:
        items = []
    if kb:
        for it in items:
            # Имя службы последним: записи базы для служб ищут его как `\\bимя$`
            it.verdict = kb.lookup(it.name, it.command, it.meta.get("service", ""))
    return items


def set_enabled(item: StartupItem, enabled: bool, log: bool = True) -> None:
    handler = {
        "win_reg": _win_set_enabled, "win_folder": _win_set_enabled,
        "win_task": _win_task_set_enabled, "win_service": _win_service_set_enabled,
        "xdg": _xdg_set_enabled, "systemd": _systemd_set_enabled, "launchd": _mac_set_enabled,
        "launchctl": _mac_launchctl_set_enabled, "mac_login": _mac_login_set_enabled,
    }.get(item.kind)
    if not handler:
        raise ActionError(tr("Этот тип автозагрузки пока не поддерживается", "This startup type is not supported yet"))
    if not item.can_toggle:
        raise ActionError(tr("Эту запись можно только удалить: Windows не умеет её отключать",
                              "This entry can only be deleted: Windows can't disable it"))
    handler(item, enabled)
    if log:
        if enabled:
            title = tr(f"Включено в автозагрузке: {item.name}", f"Enabled at startup: {item.name}")
        else:
            title = tr(f"Отключено в автозагрузке: {item.name}", f"Disabled at startup: {item.name}")
        journal.add("startup_toggle", title,
                    {"name": item.name, "kind": item.kind, "meta": item.meta, "enabled": enabled})


def delete(item: StartupItem) -> None:
    if not item.can_delete:
        raise ActionError(tr("Этот элемент нельзя удалить, только отключить", "This item can't be deleted, only disabled"))
    if item.kind == "win_reg":
        data = _win_delete_value(item)
    else:  # файл: переносим в резервную папку, а не удаляем насовсем
        src = Path(item.meta["file"])
        dst = backup_dir() / f"{int(time.time())}_{src.name}"
        try:
            shutil.move(str(src), dst)
        except OSError as e:
            raise ActionError(tr(f"Не удалось переместить файл: {e}", f"Could not move the file: {e}")) from e
        data = {"from": str(src), "to": str(dst)}
    journal.add("startup_delete", tr(f"Удалено из автозагрузки: {item.name}", f"Removed from startup: {item.name}"),
                {"name": item.name, "kind": item.kind, "meta": item.meta, "backup": data})


def undo(entry: dict) -> None:
    d = entry["data"]
    item = StartupItem(d["name"], "", "", True, d["kind"], d["meta"])
    if entry["kind"] == "startup_toggle":
        set_enabled(item, not d["enabled"], log=False)
    elif entry["kind"] == "startup_delete":
        b = d["backup"]
        if d["kind"] == "win_reg":
            _win_restore_value(b)
        else:
            os.makedirs(os.path.dirname(b["from"]), exist_ok=True)
            shutil.move(b["to"], b["from"])


# ---------------------------------------------------------------- Windows ---

_RUN = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUNONCE = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
_APPROVED = r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved"


def _win_sources():
    import winreg
    return [
        (winreg.HKEY_CURRENT_USER, "HKCU", _RUN, "Run", tr("Реестр (пользователь)", "Registry (user)")),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", _RUN, "Run", tr("Реестр (все пользователи)", "Registry (all users)")),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
         "Run32", tr("Реестр (все, 32-бит)", "Registry (all, 32-bit)")),
        # RunOnce: запускается один раз при следующем входе и сам себя стирает
        (winreg.HKEY_CURRENT_USER, "HKCU", _RUNONCE, "RunOnce", tr("Реестр, один раз (пользователь)", "Registry, run once (user)")),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", _RUNONCE, "RunOnce", tr("Реестр, один раз (все)", "Registry, run once (all)")),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce",
         "RunOnce", tr("Реестр, один раз (все, 32-бит)", "Registry, run once (all, 32-bit)")),
    ]


def _need_admin() -> str:
    return tr("Нужны права администратора: запустите WhySlow от имени администратора",
              "Administrator rights required: run WhySlow as administrator")


def _hive(name: str):
    import winreg
    return winreg.HKEY_CURRENT_USER if name == "HKCU" else winreg.HKEY_LOCAL_MACHINE


def _win_approved_enabled(hive_name: str, sub: str, value: str) -> bool:
    import winreg
    try:
        with winreg.OpenKey(_hive(hive_name), f"{_APPROVED}\\{sub}", 0,
                            winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as k:
            data, _ = winreg.QueryValueEx(k, value)
    except OSError:
        return True  # записи нет — значит включено
    return not (isinstance(data, bytes) and data and data[0] & 1)


def _win_list() -> list[StartupItem]:
    import winreg
    items = []
    for hive, hname, path, sub, label in _win_sources():
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY)
        except OSError:
            continue
        with key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    break
                i += 1
                once = sub == "RunOnce"  # StartupApproved на RunOnce не действует
                items.append(StartupItem(
                    name=name, command=str(value), source=label,
                    enabled=once or _win_approved_enabled(hname, sub, name), kind="win_reg",
                    meta={"hive": hname, "path": path, "sub": sub, "value": name},
                    can_delete=True, can_toggle=not once, needs_admin=hname == "HKLM"))
    folders = [
        ("HKCU", Path(os.environ.get("APPDATA", "")) / r"Microsoft\Windows\Start Menu\Programs\Startup",
         tr("Папка «Автозагрузка»", "Startup folder")),
        ("HKLM", Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs\StartUp",
         tr("Папка «Автозагрузка» (все)", "Startup folder (all users)")),
    ]
    for hname, folder, label in folders:
        if not folder.is_dir():
            continue
        for f in folder.iterdir():
            if f.name.lower() == "desktop.ini" or not f.is_file():
                continue
            items.append(StartupItem(
                name=f.stem, command=str(f), source=label,
                enabled=_win_approved_enabled(hname, "StartupFolder", f.name), kind="win_folder",
                meta={"hive": hname, "sub": "StartupFolder", "value": f.name, "file": str(f)},
                can_delete=True, needs_admin=hname == "HKLM"))
    return items


def _win_set_enabled(item: StartupItem, enabled: bool) -> None:
    """Пишет флаг в StartupApproved — ровно так делает Диспетчер задач."""
    import winreg
    m = item.meta
    if enabled:
        data = bytes([2]) + bytes(11)
    else:
        ft = int((time.time() + 11644473600) * 10_000_000)  # FILETIME
        data = bytes([3, 0, 0, 0]) + struct.pack("<Q", ft)
    try:
        with winreg.CreateKeyEx(_hive(m["hive"]), f"{_APPROVED}\\{m['sub']}", 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            winreg.SetValueEx(k, m["value"], 0, winreg.REG_BINARY, data)
    except PermissionError as e:
        raise ActionError(_need_admin()) from e


def _win_delete_value(item: StartupItem) -> dict:
    import winreg
    m = item.meta
    try:
        with winreg.OpenKey(_hive(m["hive"]), m["path"], 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            value, vtype = winreg.QueryValueEx(k, m["value"])
            winreg.DeleteValue(k, m["value"])
    except PermissionError as e:
        raise ActionError(_need_admin()) from e
    except OSError as e:
        raise ActionError(tr(f"Не удалось удалить: {e}", f"Could not delete: {e}")) from e
    return {"hive": m["hive"], "path": m["path"], "value": m["value"], "data": value, "type": vtype}


def _win_restore_value(b: dict) -> None:
    import winreg
    with winreg.CreateKeyEx(_hive(b["hive"]), b["path"], 0, winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
        winreg.SetValueEx(k, b["value"], 0, b["type"], b["data"])


def _ps_json(script: str, timeout: float = 60) -> list[dict]:
    """Выполняет PowerShell и разбирает ConvertTo-Json. Один объект тоже даёт список."""
    code, out = run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                     "[Console]::OutputEncoding = [Text.Encoding]::UTF8; " + script], timeout=timeout)
    if code != 0 or not out.strip():
        return []
    try:
        data = json.loads(out)
    except ValueError:
        return []
    data = data if isinstance(data, list) else [data]
    return [d for d in data if isinstance(d, dict)]


def _ours(kind: str) -> dict[str, dict]:
    """Что выключили мы и ещё не откатили: key -> meta на момент отключения."""
    out = {}
    for e in journal.load():
        d = e["data"]
        if e["kind"] == "startup_toggle" and d["kind"] == kind and not e["undone"]:
            out[d["meta"]["key"]] = d["meta"]
    return out


def _win_fail(code: int, out: str, what: str) -> None:
    if code == 0:
        return
    if not is_admin():
        raise ActionError(_need_admin())
    raise ActionError(tr(f"{what} не сработал: {out.strip()[:300]}", f"{what} failed: {out.strip()[:300]}"))


# Задания, которые стартуют при входе в систему или при загрузке
_TASKS_PS = r"""
Get-ScheduledTask | ForEach-Object {
  $kinds = @($_.Triggers | ForEach-Object { $_.CimClass.CimClassName })
  if ($kinds -match 'Logon|Boot') {
    [pscustomobject]@{
      Path = $_.TaskPath; Name = $_.TaskName; State = [string]$_.State
      User = [string]$_.Principal.UserId; Boot = [bool]($kinds -match 'Boot')
      Exec = (@($_.Actions | ForEach-Object { "$($_.Execute) $($_.Arguments)".Trim() }) -join '; ')
    }
  }
} | ConvertTo-Json -Compress
"""


def _win_tasks_list() -> list[StartupItem]:
    me = os.environ.get("USERNAME", "").lower()
    items = []
    for t in _ps_json(_TASKS_PS):
        path, name = t.get("Path") or "\\", t.get("Name") or ""
        if not name:
            continue
        tn = path + name
        user = (t.get("User") or "").lower()
        items.append(StartupItem(
            name=name, command=t.get("Exec") or tn,
            source=tr("Планировщик (при загрузке)", "Task Scheduler (at boot)") if t.get("Boot")
            else tr("Планировщик (при входе)", "Task Scheduler (at logon)"),
            enabled=t.get("State") != "Disabled", kind="win_task",
            meta={"key": tn, "tn": tn},
            needs_admin=not user or user.rsplit("\\", 1)[-1] != me,
            system=path.lower().startswith("\\microsoft\\")))
    return items


def _win_task_set_enabled(item: StartupItem, enabled: bool) -> None:
    code, out = run(["schtasks", "/Change", "/TN", item.meta["tn"], "/ENABLE" if enabled else "/DISABLE"],
                    timeout=60)
    _win_fail(code, out, "schtasks")


_SERVICES_PS = ("Get-CimInstance Win32_Service | "
                "Select-Object Name, DisplayName, PathName, StartMode, DelayedAutoStart | ConvertTo-Json -Compress")


def _win_services_list() -> list[StartupItem]:
    # Плюс службы, которые перевели в «Вручную» мы: чтобы их можно было вернуть
    ours = _ours("win_service")
    items = []
    for s in _ps_json(_SERVICES_PS):
        name = s.get("Name") or ""
        auto = s.get("StartMode") == "Auto"
        if not name or not (auto or (name in ours and s.get("StartMode") == "Manual")):
            continue
        path = s.get("PathName") or ""
        if auto:
            start = "delayed-auto" if s.get("DelayedAutoStart") else "auto"
        else:
            start = ours[name].get("start", "auto")
        low = path.lower().replace("/", "\\")
        items.append(StartupItem(
            name=s.get("DisplayName") or name, command=path,
            source=tr("Служба (отложенный запуск)", "Service (delayed start)") if start == "delayed-auto"
            else tr("Служба", "Service"),
            enabled=auto, kind="win_service",
            meta={"key": name, "service": name, "start": start},
            needs_admin=True,
            system="\\windows\\" in low or "svchost.exe" in low))
    return items


def _win_service_set_enabled(item: StartupItem, enabled: bool) -> None:
    """Отключение = режим «Вручную»: служба не стартует сама, но запустится, если её позовут."""
    start = item.meta.get("start", "auto") if enabled else "demand"
    code, out = run(["sc.exe", "config", item.meta["service"], "start=", start], timeout=60)
    _win_fail(code, out, "sc config")


# ------------------------------------------------------- Linux: autostart ---

_USER_AUTOSTART = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "autostart"
_OVERRIDE_KEY = "X-WhySlow-Override"


def _system_autostart_dirs() -> list[Path]:
    dirs = os.environ.get("XDG_CONFIG_DIRS", "/etc/xdg").split(":")
    return [Path(d) / "autostart" for d in dirs if d]


def _read_desktop(path: Path) -> dict[str, str]:
    """Ключи секции [Desktop Entry]. Свой парсер — чтобы не портить файл при записи."""
    out, in_entry = {}, False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return out
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            in_entry = s == "[Desktop Entry]"
        elif in_entry and "=" in s and not s.startswith("#"):
            k, v = s.split("=", 1)
            out.setdefault(k.strip(), v.strip())
    return out


def _write_desktop_key(path: Path, key: str, value: str | None) -> None:
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    out, in_entry, done = [], False, False
    for line in lines:
        s = line.strip()
        if s.startswith("["):
            if in_entry and not done and value is not None:
                out.append(f"{key}={value}")
                done = True
            in_entry = s == "[Desktop Entry]"
        elif in_entry and s.split("=", 1)[0].strip() == key:
            if value is not None and not done:
                out.append(f"{key}={value}")
            done = True
            continue
        out.append(line)
    if in_entry and not done and value is not None:
        out.append(f"{key}={value}")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _desktop_enabled(keys: dict) -> bool:
    return keys.get("Hidden", "false").lower() != "true" and \
        keys.get("X-GNOME-Autostart-enabled", "true").lower() != "false"


def _xdg_list() -> list[StartupItem]:
    files: dict[str, Path] = {}
    for d in reversed(_system_autostart_dirs()):
        if d.is_dir():
            for f in d.glob("*.desktop"):
                files[f.name] = f
    system_names = set(files)
    if _USER_AUTOSTART.is_dir():
        for f in _USER_AUTOSTART.glob("*.desktop"):
            files[f.name] = f  # файл пользователя перекрывает системный
    items = []
    for fname, path in sorted(files.items()):
        keys = _read_desktop(path)
        is_user = path.parent == _USER_AUTOSTART
        is_override = is_user and (keys.get(_OVERRIDE_KEY) == "true" or fname in system_names)
        items.append(StartupItem(
            name=keys.get("Name", fname.removesuffix(".desktop")),
            command=keys.get("Exec", ""),
            source=tr("Автозапуск (пользователь)", "Autostart (user)") if is_user and not is_override
            else tr("Автозапуск (система)", "Autostart (system)"),
            enabled=_desktop_enabled(keys), kind="xdg",
            meta={"file": str(path), "fname": fname},
            can_delete=is_user and not is_override))
    return items


def _xdg_set_enabled(item: StartupItem, enabled: bool) -> None:
    fname = item.meta["fname"]
    user_file = _USER_AUTOSTART / fname
    system_file = next((d / fname for d in _system_autostart_dirs() if (d / fname).exists()), None)
    if user_file.exists() and _read_desktop(user_file).get(_OVERRIDE_KEY) == "true":
        user_file.unlink()  # наша прошлая копия — начинаем с чистого листа
    if system_file and not user_file.exists():
        if _desktop_enabled(_read_desktop(system_file)) == enabled:
            return
        # Системный файл не трогаем — кладём рядом свою копию, она его перекрывает
        _USER_AUTOSTART.mkdir(parents=True, exist_ok=True)
        shutil.copy(system_file, user_file)
        _write_desktop_key(user_file, _OVERRIDE_KEY, "true")
    if not user_file.exists():
        raise ActionError(tr("Файл автозапуска не найден", "Autostart file not found"))
    keys = _read_desktop(user_file)
    _write_desktop_key(user_file, "Hidden", None if enabled else "true")
    if "X-GNOME-Autostart-enabled" in keys or enabled:
        _write_desktop_key(user_file, "X-GNOME-Autostart-enabled", "true" if enabled else "false")


# --------------------------------------------------------- Linux: systemd ---

def _systemd_units(scope: str, state: str) -> list[str]:
    cmd = ["systemctl"] + (["--user"] if scope == "user" else []) + [
        "list-unit-files", "--type=service", f"--state={state}", "--no-legend", "--no-pager"]
    code, out = run(cmd)
    if code != 0:
        return []
    return [line.split()[0] for line in out.splitlines() if line.strip() and not line.split()[0].endswith("@.service")]


def _systemd_list() -> list[StartupItem]:
    # Плюс службы, которые выключили мы: чтобы их можно было включить обратно
    ours = {(e["data"]["meta"]["scope"], e["data"]["meta"]["unit"]) for e in journal.load()
            if e["kind"] == "startup_toggle" and e["data"]["kind"] == "systemd" and not e["undone"]}
    items = []
    for scope in ("user", "system"):
        enabled = _systemd_units(scope, "enabled")
        disabled = [u for u in _systemd_units(scope, "disabled") if (scope, u) in ours]
        for unit, on in [(u, True) for u in enabled] + [(u, False) for u in disabled]:
            items.append(StartupItem(
                name=unit.removesuffix(".service"), command=unit,
                source=tr("Служба (пользователь)", "Service (user)") if scope == "user"
                else tr("Служба (система)", "Service (system)"),
                enabled=on, kind="systemd", meta={"unit": unit, "scope": scope},
                needs_admin=scope == "system", system=True))
    return items


def _systemd_set_enabled(item: StartupItem, enabled: bool) -> None:
    verb = "enable" if enabled else "disable"
    if item.meta["scope"] == "user":
        cmd = ["systemctl", "--user", verb, item.meta["unit"]]
    elif os.geteuid() == 0:
        cmd = ["systemctl", verb, item.meta["unit"]]
    else:
        cmd = ["pkexec", "systemctl", verb, item.meta["unit"]]  # спросит пароль
    code, out = run(cmd, timeout=120)
    if code != 0:
        raise ActionError(tr(f"systemctl {verb} не сработал: {out.strip()[:300]}", f"systemctl {verb} failed: {out.strip()[:300]}"))


# ------------------------------------------------------------------ macOS ---
# Три источника, как в «Настройки → Основные → Объекты входа»:
#  * объекты входа (приложения, которые открываются при входе) — через System Events;
#  * агенты пользователя ~/Library/LaunchAgents — переносим plist в соседнюю папку;
#  * агенты и демоны из /Library — `launchctl disable`, файлы программ не трогаем.

_MAC_DISABLED = Path.home() / "Library" / "LaunchAgents" / "WhySlow-disabled"


def _mac_plist(path: Path) -> dict:
    import plistlib
    try:
        with open(path, "rb") as f:
            data = plistlib.load(f)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _mac_disabled_labels(domain: str) -> set[str]:
    """Что выключено через launchctl. Формат строк: "com.foo.bar" => disabled (или => true)."""
    code, out = run(["launchctl", "print-disabled", domain])
    if code != 0:
        return set()
    labels = set()
    for line in out.splitlines():
        if "=>" not in line:
            continue
        label, state = line.split("=>", 1)
        if state.strip().lower() in ("disabled", "true"):
            labels.add(label.strip().strip('"'))
    return labels


def _mac_list() -> list[StartupItem]:
    items = _mac_login_list()
    gui = f"gui/{os.getuid()}"
    off = {gui: _mac_disabled_labels(gui), "system": _mac_disabled_labels("system")}
    user_agent = tr("Агент (пользователь)", "Agent (user)")
    sources = [(Path.home() / "Library/LaunchAgents", user_agent, None),
               (_MAC_DISABLED, user_agent, None),
               (Path("/Library/LaunchAgents"), tr("Агент (все пользователи)", "Agent (all users)"), gui),
               (Path("/Library/LaunchDaemons"), tr("Фоновая служба (система)", "Background daemon (system)"), "system")]
    for folder, label, domain in sources:
        if not folder.is_dir():
            continue
        for f in sorted(folder.glob("*.plist")):
            plist = _mac_plist(f)
            name = str(plist.get("Label") or f.stem)
            args = plist.get("ProgramArguments") or []
            prog = plist.get("Program") or (args[0] if args and isinstance(args, list) else "")
            if domain is None:  # свой агент: выключение = перенос файла
                items.append(StartupItem(
                    name=name, command=str(prog or f), source=label, enabled=folder != _MAC_DISABLED,
                    kind="launchd", meta={"file": str(f), "user": True},
                    can_delete=True))
            else:
                items.append(StartupItem(
                    name=name, command=str(prog or f), source=label, enabled=name not in off[domain],
                    kind="launchctl", meta={"label": name, "domain": "system" if domain == "system" else "gui",
                                            "file": str(f)},
                    needs_admin=domain == "system"))
    return items


def _mac_set_enabled(item: StartupItem, enabled: bool) -> None:
    src = Path(item.meta["file"])
    dst_dir = src.parent.parent if enabled else _MAC_DISABLED
    if src.parent == dst_dir:
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), dst)
    item.meta["file"] = str(dst)


def _mac_launchctl_set_enabled(item: StartupItem, enabled: bool) -> None:
    """Как `launchctl disable`: система запоминает запрет и не запускает агент при входе.
    Для своего домена (gui/<uid>) права не нужны, для system macOS спросит пароль."""
    verb = "enable" if enabled else "disable"
    label = item.meta["label"]
    if item.meta["domain"] == "gui":
        code, out = run(["launchctl", verb, f"gui/{os.getuid()}/{label}"])
    elif os.geteuid() == 0:
        code, out = run(["launchctl", verb, f"system/{label}"])
    else:
        cmd = f"/bin/launchctl {verb} system/{_sh_quote(label)}"
        code, out = run(["osascript", "-e",
                         f"do shell script {_as_quote(cmd)} with administrator privileges"], timeout=180)
        if code != 0 and "-128" in out:
            raise ActionError(tr("Отменено: macOS не получила пароль администратора",
                                  "Cancelled: macOS did not receive the administrator password"))
    if code != 0:
        raise ActionError(tr(f"launchctl {verb} не сработал: {out.strip()[:300]}", f"launchctl {verb} failed: {out.strip()[:300]}"))


def _sh_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def _as_quote(s: str) -> str:
    """Строка для AppleScript."""
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


# Объекты входа: читаем и меняем через System Events (JavaScript for Automation).
# При первом обращении macOS спросит, можно ли WhySlow управлять System Events.
_JXA_LIST = """
var se = Application("System Events");
JSON.stringify(se.loginItems().map(function (i) {
  return {name: i.name(), path: i.path(), hidden: i.hidden()};
}));
"""

last_error = ""  # почему не удалось прочитать часть автозагрузки — показывается в интерфейсе


def _jxa(script: str, timeout: float = 30) -> tuple[int, str]:
    return run(["osascript", "-l", "JavaScript", "-e", script], timeout=timeout)


def _mac_login_list() -> list[StartupItem]:
    global last_error
    import json
    code, out = _jxa(_JXA_LIST)
    data = []
    if code == 0:
        try:
            data = json.loads(out.strip() or "[]")
        except ValueError:
            data = []
    elif "-1743" in out or "not allowed" in out.lower():
        last_error = tr("macOS не разрешила прочитать объекты входа. Разрешите WhySlow (или Терминалу) управлять "
                        "System Events: Настройки → Конфиденциальность и безопасность → Автоматизация.",
                        "macOS did not allow reading login items. Allow WhySlow (or Terminal) to control "
                        "System Events: System Settings → Privacy & Security → Automation.")
    items, seen = [], set()
    for d in data if isinstance(data, list) else []:
        path = d.get("path") or ""
        seen.add(path)
        items.append(StartupItem(
            name=d.get("name") or Path(path).stem, command=path, source=tr("Объект входа", "Login item"), enabled=True,
            kind="mac_login", meta={"key": path, "path": path, "hidden": bool(d.get("hidden"))}))
    # Плюс объекты, которые убрали мы: чтобы их можно было включить обратно
    for e in journal.load():
        dd = e["data"]
        if e["kind"] == "startup_toggle" and dd["kind"] == "mac_login" and not e["undone"] \
                and not dd["enabled"] and dd["meta"]["path"] not in seen:
            seen.add(dd["meta"]["path"])
            items.append(StartupItem(
                name=dd["name"], command=dd["meta"]["path"], source=tr("Объект входа", "Login item"), enabled=False,
                kind="mac_login", meta=dd["meta"]))
    return items


def _mac_login_set_enabled(item: StartupItem, enabled: bool) -> None:
    """Выключение = убрать из объектов входа; включение = добавить обратно с теми же настройками."""
    import json
    path = item.meta["path"]
    if enabled:
        props = json.dumps({"path": path, "hidden": bool(item.meta.get("hidden"))})
        script = (f'var se = Application("System Events");'
                  f'se.loginItems.push(se.LoginItem({props}));')
    else:
        script = (f'var se = Application("System Events"); var p = {json.dumps(path)};'
                  f'var all = se.loginItems();'
                  f'for (var k = all.length - 1; k >= 0; k--) {{ if (all[k].path() === p) all[k].delete(); }}')
    code, out = _jxa(script)
    if code != 0:
        if "-1743" in out or "not allowed" in out.lower():
            raise ActionError(tr("macOS не разрешила менять объекты входа. Разрешите WhySlow управлять System Events: "
                                 "Настройки → Конфиденциальность и безопасность → Автоматизация.",
                                 "macOS did not allow changing login items. Allow WhySlow to control System Events: "
                                 "System Settings → Privacy & Security → Automation."))
        raise ActionError(tr(f"Не удалось изменить объекты входа: {out.strip()[:300]}",
                             f"Could not change login items: {out.strip()[:300]}"))
