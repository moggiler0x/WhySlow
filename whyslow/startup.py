"""Автозагрузка: Windows (реестр, папка «Автозагрузка», Планировщик заданий,
службы), Linux (XDG autostart + systemd), macOS (LaunchAgents). Отключение
сделано так же, как в системных инструментах, — поэтому его всегда можно
включить обратно."""

from __future__ import annotations

import json
import os
import shutil
import struct
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import journal
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
    }.get(item.kind)
    if not handler:
        raise ActionError("Этот тип автозагрузки пока не поддерживается")
    if not item.can_toggle:
        raise ActionError("Эту запись можно только удалить: Windows не умеет её отключать")
    handler(item, enabled)
    if log:
        verb = "Включено" if enabled else "Отключено"
        journal.add("startup_toggle", f"{verb} в автозагрузке: {item.name}",
                    {"name": item.name, "kind": item.kind, "meta": item.meta, "enabled": enabled})


def delete(item: StartupItem) -> None:
    if not item.can_delete:
        raise ActionError("Этот элемент нельзя удалить, только отключить")
    if item.kind == "win_reg":
        data = _win_delete_value(item)
    else:  # файл: переносим в резервную папку, а не удаляем насовсем
        src = Path(item.meta["file"])
        dst = backup_dir() / f"{int(time.time())}_{src.name}"
        try:
            shutil.move(str(src), dst)
        except OSError as e:
            raise ActionError(f"Не удалось переместить файл: {e}") from e
        data = {"from": str(src), "to": str(dst)}
    journal.add("startup_delete", f"Удалено из автозагрузки: {item.name}",
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
        (winreg.HKEY_CURRENT_USER, "HKCU", _RUN, "Run", "Реестр (пользователь)"),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", _RUN, "Run", "Реестр (все пользователи)"),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run",
         "Run32", "Реестр (все, 32-бит)"),
        # RunOnce: запускается один раз при следующем входе и сам себя стирает
        (winreg.HKEY_CURRENT_USER, "HKCU", _RUNONCE, "RunOnce", "Реестр, один раз (пользователь)"),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", _RUNONCE, "RunOnce", "Реестр, один раз (все)"),
        (winreg.HKEY_LOCAL_MACHINE, "HKLM", r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\RunOnce",
         "RunOnce", "Реестр, один раз (все, 32-бит)"),
    ]


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
         "Папка «Автозагрузка»"),
        ("HKLM", Path(os.environ.get("PROGRAMDATA", r"C:\ProgramData")) / r"Microsoft\Windows\Start Menu\Programs\StartUp",
         "Папка «Автозагрузка» (все)"),
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
        raise ActionError("Нужны права администратора: запустите WhySlow от имени администратора") from e


def _win_delete_value(item: StartupItem) -> dict:
    import winreg
    m = item.meta
    try:
        with winreg.OpenKey(_hive(m["hive"]), m["path"], 0,
                            winreg.KEY_READ | winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY) as k:
            value, vtype = winreg.QueryValueEx(k, m["value"])
            winreg.DeleteValue(k, m["value"])
    except PermissionError as e:
        raise ActionError("Нужны права администратора: запустите WhySlow от имени администратора") from e
    except OSError as e:
        raise ActionError(f"Не удалось удалить: {e}") from e
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
        raise ActionError("Нужны права администратора: запустите WhySlow от имени администратора")
    raise ActionError(f"{what} не сработал: {out.strip()[:300]}")


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
            source="Планировщик (при загрузке)" if t.get("Boot") else "Планировщик (при входе)",
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
            source="Служба (отложенный запуск)" if start == "delayed-auto" else "Служба",
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
            source="Автозапуск (пользователь)" if is_user and not is_override else "Автозапуск (система)",
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
        raise ActionError("Файл автозапуска не найден")
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
                source="Служба (пользователь)" if scope == "user" else "Служба (система)",
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
        raise ActionError(f"systemctl {verb} не сработал: {out.strip()[:300]}")


# ------------------------------------------------------------------ macOS ---

_MAC_DISABLED = Path.home() / "Library" / "LaunchAgents" / "WhySlow-disabled"


def _mac_list() -> list[StartupItem]:
    items = []
    sources = [(Path.home() / "Library/LaunchAgents", "LaunchAgents (пользователь)", True),
               (_MAC_DISABLED, "LaunchAgents (пользователь)", True),
               (Path("/Library/LaunchAgents"), "LaunchAgents (система)", False),
               (Path("/Library/LaunchDaemons"), "LaunchDaemons (система)", False)]
    for folder, label, user in sources:
        if not folder.is_dir():
            continue
        for f in folder.glob("*.plist"):
            items.append(StartupItem(
                name=f.stem, command=str(f), source=label, enabled=folder != _MAC_DISABLED,
                kind="launchd", meta={"file": str(f), "user": user},
                can_delete=user, needs_admin=not user))
    return items


def _mac_set_enabled(item: StartupItem, enabled: bool) -> None:
    if not item.meta["user"]:
        raise ActionError("Системные агенты macOS пока можно только посмотреть")
    src = Path(item.meta["file"])
    dst_dir = src.parent.parent if enabled else _MAC_DISABLED
    if src.parent == dst_dir:
        return
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    shutil.move(str(src), dst)
    item.meta["file"] = str(dst)
