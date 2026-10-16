"""Ending every process of a Claude session (docs/phase5a-plan.md D4).

A session is a process group (the runner starts it in a new session), but ``setsid``,
``nohup`` and a dev server left running escape the group. So a run's processes are found four
ways, and all of them are signalled:

1. the leader and every descendant of it (``psutil``, recursively);
2. the descendants sampled earlier (``Target.known``): a child that ``setsid``-ed and whose
   parent then exited is reparented to launchd, and only this finds it again;
3. every member of the process group, while the group is provably the session's;
4. any process of this user that carries the run's marker in its environment
   (``RESONANT_RUN=<code>``) or whose working directory is inside the run dir. This is what
   catches an escapee after a daemon crash, when nothing else records it.

A pid is never signalled on a pid match alone: the leader is trusted only when its start time
equals the one saved at spawn, so a reused pid is left alone. The group is signalled only when
the leader is that process, or is gone (a group id can't be reused while the group has members).

``reap`` is SIGTERM, a grace period, then SIGKILL, rescanning each round (the session may
start more processes while it dies). Harvest and cleanup start only once it returns, so
nothing writes into the run dir while it is read.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

import psutil

START_TOLERANCE_S = 1.0
GRACE_S = 10.0
POLL_S = 0.5  # between full rescans of the process table
KILL_WAIT_S = 5.0
MARKER_ENV = "RESONANT_RUN"


@dataclass
class Target:
    """What identifies one run's processes."""

    pgid: int | None
    leader_pid: int | None
    leader_started_at: float | None
    run_dir: Path | None = None
    marker: str | None = None
    # the run's worktree or scratch dir (EM-2): the session's cwd, also searched like run_dir
    work_dir: Path | None = None
    # pid -> create_time of descendants seen alive (see ``sample``).
    known: dict[int, float] = field(default_factory=dict[int, float])


def _same(proc: psutil.Process, created: float | None) -> bool:
    """Whether ``proc`` is the process that started at ``created`` (same pid is not enough)."""
    if created is None:
        return False
    try:
        return abs(proc.create_time() - created) <= START_TOLERANCE_S
    except psutil.Error:
        return False


def _dead(proc: psutil.Process) -> bool:
    try:
        return proc.status() == psutil.STATUS_ZOMBIE or not proc.is_running()
    except psutil.Error:
        return True


def _leader(t: Target) -> tuple[psutil.Process | None, bool]:
    """The leader if it is still the same process, and whether its group is safe to signal."""
    if t.leader_pid is None:
        return None, False
    try:
        proc = psutil.Process(t.leader_pid)
    except psutil.NoSuchProcess:
        return None, t.pgid is not None  # gone: the group id can't have been reused
    except psutil.Error:
        return None, False
    if _same(proc, t.leader_started_at):
        return proc, t.pgid is not None
    return None, False  # the pid belongs to someone else now


def _members(pgid: int) -> list[psutil.Process]:
    out: list[psutil.Process] = []
    for proc in psutil.process_iter():
        with contextlib.suppress(psutil.Error, OSError):
            if os.getpgid(proc.pid) == pgid:
                out.append(proc)
    return out


def _marked(t: Target) -> list[psutil.Process]:
    """Own-user processes marked with the run's env marker or working inside its run dir (or
    its worktree)."""
    dirs = [os.path.realpath(d) for d in (t.run_dir, t.work_dir) if d is not None]
    if t.marker is None and not dirs:
        return []
    me, uid = os.getpid(), os.getuid()
    out: list[psutil.Process] = []
    for proc in psutil.process_iter():
        if proc.pid == me:
            continue
        try:
            if proc.uids().real != uid or _dead(proc):
                continue
            if t.marker is not None and proc.environ().get(MARKER_ENV) == t.marker:
                out.append(proc)
            elif dirs and not proc.terminal():  # not the owner's terminal
                cwd = os.path.realpath(proc.cwd())
                if any(cwd == d or cwd.startswith(d + os.sep) for d in dirs):
                    out.append(proc)
        except (psutil.Error, OSError):
            continue
    return out


def find(t: Target) -> tuple[list[psutil.Process], bool]:
    """The live processes of the run, and whether its process group may be signalled."""
    found: dict[int, psutil.Process] = {}
    leader, group_ok = _leader(t)
    if leader is not None:
        found[leader.pid] = leader
        with contextlib.suppress(psutil.Error):
            for child in leader.children(recursive=True):
                found[child.pid] = child
    for pid, created in list(t.known.items()):
        try:
            proc = psutil.Process(pid)
        except psutil.Error:
            del t.known[pid]
            continue
        if _same(proc, created):
            found[pid] = proc
        else:
            del t.known[pid]
    if group_ok and t.pgid is not None:
        for proc in _members(t.pgid):
            found.setdefault(proc.pid, proc)
    for proc in _marked(t):
        found.setdefault(proc.pid, proc)
    me = os.getpid()
    return [p for p in found.values() if p.pid != me and not _dead(p)], group_ok


def sample(t: Target) -> None:
    """Remember the leader's current descendants, so they can still be found after they are
    reparented (a ``setsid`` child whose parent exits)."""
    leader, _ = _leader(t)
    if leader is None:
        return
    with contextlib.suppress(psutil.Error):
        for child in leader.children(recursive=True):
            with contextlib.suppress(psutil.Error):
                t.known.setdefault(child.pid, child.create_time())
    with contextlib.suppress(psutil.Error):
        t.known.setdefault(leader.pid, leader.create_time())


def _signal(procs: list[psutil.Process], pgid: int | None, sig: signal.Signals) -> None:
    if pgid is not None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, sig)
    for proc in procs:
        with contextlib.suppress(psutil.Error):
            proc.send_signal(sig)


async def reap(t: Target, *, grace_s: float = GRACE_S, poll_s: float = POLL_S) -> int:
    """End every process of the run: SIGTERM, wait up to ``grace_s``, then SIGKILL until none
    is left (at most ``KILL_WAIT_S`` more). Returns how many distinct processes it found.
    Nothing alive afterwards is possible only for a process stuck in the kernel."""
    seen: set[tuple[int, float]] = set()

    def scan() -> tuple[list[psutil.Process], bool]:
        procs, group_ok = find(t)
        for p in procs:
            with contextlib.suppress(psutil.Error):
                seen.add((p.pid, p.create_time()))
        return procs, group_ok

    procs, group_ok = await asyncio.to_thread(scan)
    if not procs:
        return 0
    _signal(procs, t.pgid if group_ok else None, signal.SIGTERM)
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        await asyncio.sleep(poll_s)
        procs, group_ok = await asyncio.to_thread(scan)
        if not procs:
            return len(seen)
    deadline = time.monotonic() + KILL_WAIT_S
    while True:
        _signal(procs, t.pgid if group_ok else None, signal.SIGKILL)
        await asyncio.sleep(poll_s)
        procs, group_ok = await asyncio.to_thread(scan)
        if not procs or time.monotonic() >= deadline:
            return len(seen)


def alive(t: Target) -> bool:
    return bool(find(t)[0])
