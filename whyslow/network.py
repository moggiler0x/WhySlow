"""Проверка «VPN сломал интернет»: VPN-клиенты при аварийном закрытии
оставляют включённый системный прокси на 127.0.0.1, который никто не слушает."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import psutil

from . import journal
from .util import IS_LINUX, IS_WINDOWS, run

_INET = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"


@dataclass
class ProxyState:
    enabled: bool
    server: str = ""      # как записано в настройках
    where: str = ""       # откуда настройка: Windows / GNOME / переменная окружения
    host: str = ""
    port: int = 0
    listener: str = ""    # процесс, который слушает порт ("" — никто)
    fixable: bool = False

    @property
    def is_local(self) -> bool:
        return self.host in ("127.0.0.1", "localhost", "::1") or self.host.startswith("127.")

    @property
    def dead(self) -> bool:
        return self.enabled and self.is_local and self.port > 0 and not self.listener


def _parse(server: str) -> tuple[str, int]:
    # "127.0.0.1:1080", "http=127.0.0.1:8080;https=...", "socks5://127.0.0.1:2080"
    first = server.split(";")[0]
    first = first.split("=", 1)[-1]
    first = re.sub(r"^\w+://", "", first)
    m = re.match(r"\[([^\]]+)\]:(\d+)|([^:/]+):(\d+)", first)
    if not m:
        return first, 0
    return (m.group(1), int(m.group(2))) if m.group(1) else (m.group(3), int(m.group(4)))


def _listener(port: int) -> str:
    try:
        for c in psutil.net_connections(kind="inet"):
            if c.status == psutil.CONN_LISTEN and c.laddr and c.laddr.port == port:
                try:
                    return psutil.Process(c.pid).name() if c.pid else "неизвестный процесс"
                except psutil.Error:
                    return "неизвестный процесс"
    except (psutil.AccessDenied, OSError):
        return "?"  # не можем проверить — не будем пугать
    return ""


def proxy_state() -> ProxyState:
    st = ProxyState(False)
    if IS_WINDOWS:
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INET) as k:
                enabled = winreg.QueryValueEx(k, "ProxyEnable")[0] == 1
                server = winreg.QueryValueEx(k, "ProxyServer")[0] if enabled else ""
            st = ProxyState(enabled, server, "Windows", fixable=True)
        except OSError:
            pass
    elif IS_LINUX:
        code, mode = run(["gsettings", "get", "org.gnome.system.proxy", "mode"])
        if code == 0 and mode.strip().strip("'") == "manual":
            for proto in ("http", "https", "socks"):
                _, host = run(["gsettings", "get", f"org.gnome.system.proxy.{proto}", "host"])
                _, port = run(["gsettings", "get", f"org.gnome.system.proxy.{proto}", "port"])
                host = host.strip().strip("'")
                if host:
                    st = ProxyState(True, f"{host}:{port.strip()}", "Настройки GNOME", fixable=True)
                    break
    if not st.enabled:
        for var in ("all_proxy", "https_proxy", "http_proxy", "ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY"):
            if os.environ.get(var):
                st = ProxyState(True, os.environ[var], f"переменная {var}")
                break
    if st.enabled:
        st.host, st.port = _parse(st.server)
        if st.is_local and st.port:
            st.listener = _listener(st.port)
    return st


def disable_proxy(st: ProxyState) -> None:
    if IS_WINDOWS:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INET, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 0)
        _win_refresh()
        data = {"os": "windows"}
    elif IS_LINUX:
        run(["gsettings", "set", "org.gnome.system.proxy", "mode", "none"])
        data = {"os": "gnome"}
    else:
        return
    journal.add("proxy_off", f"Выключен системный прокси {st.server}", data)


def undo(entry: dict) -> None:
    if entry["data"]["os"] == "windows":
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _INET, 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "ProxyEnable", 0, winreg.REG_DWORD, 1)
        _win_refresh()
    else:
        run(["gsettings", "set", "org.gnome.system.proxy", "mode", "manual"])


def _win_refresh() -> None:
    """Сообщаем системе, что настройки прокси поменялись (иначе браузер не заметит)."""
    import ctypes
    wininet = ctypes.windll.wininet
    wininet.InternetSetOptionW(0, 39, 0, 0)  # INTERNET_OPTION_SETTINGS_CHANGED
    wininet.InternetSetOptionW(0, 37, 0, 0)  # INTERNET_OPTION_REFRESH
