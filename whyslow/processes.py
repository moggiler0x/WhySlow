"""Процессы, сгруппированные по программе: «Chrome — 2.1 ГБ, 34 процесса»."""

from __future__ import annotations

import os
from dataclasses import dataclass, field

import psutil

from . import journal
from .knowledge import KnowledgeBase, Verdict

_SYSTEM_USERS = {"root", "system", "nt authority\\system", "local service", "network service",
                 "nt authority\\local service", "nt authority\\network service"}


@dataclass
class ProcGroup:
    name: str
    pids: list[int] = field(default_factory=list)
    cpu: float = 0.0     # % от всего процессора
    mem: int = 0
    exe: str = ""
    system: bool = False
    verdict: Verdict | None = None


class ProcessMonitor:
    """Держит объекты Process между замерами — иначе cpu_percent всегда 0."""

    def __init__(self):
        self._procs: dict[int, psutil.Process] = {}
        self._ncpu = psutil.cpu_count() or 1
        self._me = os.getpid()

    def sample(self, kb: KnowledgeBase | None = None) -> list[ProcGroup]:
        groups: dict[str, ProcGroup] = {}
        alive = set()
        for p in psutil.process_iter(["name", "exe", "username", "memory_info"]):
            if p.pid in (0, self._me):
                continue
            alive.add(p.pid)
            proc = self._procs.setdefault(p.pid, p)
            try:
                cpu = proc.cpu_percent(None) / self._ncpu
            except psutil.Error:
                continue
            info = p.info
            name = info.get("name") or "?"
            if name.lower() in ("system idle process", "idle"):
                continue
            g = groups.setdefault(name.lower(), ProcGroup(name))
            g.pids.append(p.pid)
            g.cpu += cpu
            g.mem += info["memory_info"].rss if info.get("memory_info") else 0
            g.exe = g.exe or info.get("exe") or ""
            user = (info.get("username") or "").lower()
            g.system = g.system or user in _SYSTEM_USERS
        for pid in list(self._procs):
            if pid not in alive:
                del self._procs[pid]
        result = list(groups.values())
        if kb:
            for g in result:
                g.verdict = kb.lookup(g.name, g.exe)
        return result


def kill(group: ProcGroup) -> tuple[int, int]:
    """Закрывает все процессы группы. Возвращает (закрыто, не удалось)."""
    procs = []
    for pid in group.pids:
        try:
            procs.append(psutil.Process(pid))
        except psutil.Error:
            pass
    for p in procs:
        try:
            p.terminate()
        except psutil.Error:
            pass
    gone, alive = psutil.wait_procs(procs, timeout=3)
    for p in alive:
        try:
            p.kill()
        except psutil.Error:
            pass
    gone2, alive2 = psutil.wait_procs(alive, timeout=2)
    closed = len(gone) + len(gone2)
    journal.add("process_kill", f"Закрыта программа {group.name} ({closed} проц.)",
                {"name": group.name}, undoable=False)
    return closed, len(alive2)
