"""The Claude brain (BR-11): one long-lived ``claude`` session that answers the owner's hard
texts, one user message per text (docs/brain-plan.md, BR-11 as built).

Before BR-11 every hand-off was its own ``claude -p`` (``runners/ask.py``); now an owner's
hand-off with Resonant's tools is a *turn* of this session. The ask task stays what it was
(the queue entry, the bridge, the approvals, the reply and the history note); only where the
answer comes from changes. :class:`ClaudeSession` owns the process and its stdin; the ask
runner owns everything a turn decides.

**The process** is RC-1's live machinery (``runners/live.py``): ``claude -p
--input-format stream-json --output-format stream-json`` with stdin kept open, Claude Code's
file, shell and web built-ins with every permission prompt sent to Resonant's permission tool
(PER-258; the reads on ``gate.shell.READ_RULES`` never prompt), the ask's deny and read rules
(``runners.ask.session_argv``), and Resonant's MCP server. ``shell`` tracks where its Bash
is (``ask_tools.ShellCwd``). Each turn is one ``user``
line; its end is the ``result`` line. Nothing is written mid-turn.

**The MCP server lives as long as the session** (the relay Claude Code started holds its
socket and token), and each turn binds its own ``ClaudeTools`` to it for that turn only
(``ask_tools.McpServer``), so a call is always that turn's task, principal, trace and origin.

**Serial.** :meth:`turn` holds ``lock``: one turn at a time, in one process. The ask runner
keeps the queue (``AskRunner`` orders the waiting turns).

**Lifecycle.**

- Started lazily by the first turn. The session id is kv ``SESSION_KV``; once a turn has
  been written (a ``result`` came back) the next start, after a daemon restart, is
  ``--resume <id>``. A resume that doesn't take (the process ends before answering) and any
  fresh start begin a new id with the **primer**: a short summary from Resonant's own
  history (``brain.history.primer``: the fold's rules, so never Claude's text or anything a
  tainted turn wrote), prepended to that turn's message.
- **Health**: before every turn the process must be alive and its reader running, else it
  restarts. A turn past ``turn_timeout_s`` is a hang: the session is killed and the next turn
  starts fresh. A crash mid-turn (the process ends with no ``result``) restarts it fresh; the
  turn is tried once more there only if no change reached the gate (``may_retry``), so a
  change never runs twice.
- **Rotate**: after ``rotate_turns`` turns or ``rotate_hours`` the next turn starts a fresh
  session (primed), a backstop to Claude Code's auto-compaction.
- **Ends** the RC-1 way (:meth:`stop`): Remote Control off (best effort), stdin closed, a
  short wait, then the process group reaped. ``/kill`` (a pause hook) stops it; so does
  daemon shutdown (:meth:`aclose`, the id kept for a resume). The process's pid, start time
  and marker are in kv, so one a crashed daemon left is reaped before the next start.

**Upkeep** (PER-262):

- **A pause from outside.** ``/kill`` in this process stops the session through a pause
  hook; a pause another process writes (``resonant pause`` from the CLI) has no hook here,
  so a live session checks ``paused`` every ``pause_check_s`` and, when idle, stops the same
  way (a turn in flight sees the pause on its own poll).
- **Startup** (:meth:`startup`, before the loop resumes anything): a session process a killed
  daemon left (its pid, start time and marker are in kv) is reaped at once, not on the next
  turn, and old transcripts are swept.
- **Old transcripts.** Claude Code keeps each session's transcript in
  ``<home>/.claude/projects/<the real cwd, every non-alphanumeric as "-">/<id>.jsonl``.
  Rotations and restarts leave the earlier ones; those older than ``transcript_days`` are
  removed (:func:`sweep_transcripts`) at startup and whenever a new session starts. Only in
  the brain's own dir, only UUID-named entries, never the current session, never a symlink.
- **Rate limits.** A ``rate_limit_event`` in the stream is logged (its status, window and
  reset, never text), and a warning one (``allowed_warning``: the limit is near) once per
  window and reset, before the limit hits.

**Remote Control** (``claude_session.remote_control``, default off): the RC-1 control
request goes first; the claude.ai URL (validated, ``live.session_url``) is kept in kv for
/status and the dashboard, and cleared when the session ends.

**Taint across turns** (``inherited_hops`` / ``record``): the session remembers the last
turn's taint hops (kv). The next turn starts tainted only while it is within
``brain.taint_hops`` turns (BR-10b's rule, PER-245), so taint lifts after the hop window
however long the session lives; a fresh session (primed from clean records) starts clean.

Message bodies are PII: logs and spans carry ids, counts and states, never text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

import psutil

from resonant import killswitch
from resonant.brain.history import DEFAULT_TAINT_HOPS
from resonant.config import ClaudeSessionConfig
from resonant.runners import reaper
from resonant.runners.ask_tools import McpServer, ShellCwd
from resonant.runners.claude import read_lines
from resonant.runners.live import RC_TIMEOUT_S, ControlReply, LiveInput, session_url
from resonant.runners.reaper import MARKER_ENV, Target
from resonant.store.audit import audit
from resonant.store.claude_brain import SESSION_KV, session_state, status_of
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow

log = logging.getLogger(__name__)

END_WAIT_S = 3.0  # after stdin closes, before the group is reaped
POLL_S = 0.25
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

Status = Literal["answered", "crashed", "timeout", "killed", "refused"]
# The usage limit's reset (iso): asks refuse until then (``runners.ask``, which re-exports
# these). A rejected ``rate_limit_event`` sets it before the next turn hits the wall (PER-256).
LIMIT_KV = "brain.ask_claude.limited_until"
LIMIT_KNOWN_KV = "brain.ask_claude.limit_reset_known"  # the reset time came from Claude


@dataclass(frozen=True)
class TurnOutcome:
    """How a turn ended: ``answered`` (``result`` is the final stream-json ``result``, which
    may still be an error or a usage limit), ``crashed`` (the process ended first),
    ``timeout``, ``killed`` (the kill switch) or ``refused`` (the init check said no:
    ``refusal``). ``fresh``: the turn ran in a new session (primed)."""

    status: Status
    result: dict[str, Any] | None = None
    refusal: str | None = None
    fresh: bool = False
    retried: bool = False


Argv = Callable[[str, bool], list[str]]  # (session id, resume) -> argv
Observe = Callable[[dict[str, Any]], None]
InitCheck = Callable[[Sequence[str]], str | None]


class ClaudeSession:
    """The Claude brain's process. Public: :meth:`turn`, :meth:`stop`, :meth:`aclose`,
    :meth:`inherited_hops`, :meth:`record`, ``server`` and ``busy``."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        cfg: ClaudeSessionConfig,
        cwd: Path,
        server: McpServer,
        clock: Callable[[], datetime] = utcnow,
        poll_s: float = POLL_S,
        kill_grace_s: float = 2.0,
        rc_timeout_s: float = RC_TIMEOUT_S,
        end_wait_s: float = END_WAIT_S,
        taint_hops: int = DEFAULT_TAINT_HOPS,
        paused: Callable[[], bool] | None = None,
        home: Path | None = None,
    ) -> None:
        """``paused``: the kill switch as stored (unknown counts as paused), checked every
        ``cfg.pause_check_s`` while the session is up (PER-262). ``home``: the ``HOME`` the
        CLI runs with, whose ``.claude/projects`` holds this session's transcripts; None:
        transcripts are never swept."""
        self.conn = conn
        self.paused = paused
        self.home = home
        self.cfg = cfg
        self.taint_hops = taint_hops  # brain.taint_hops
        self.cwd = cwd
        self.server = server
        self.clock = clock
        self.poll_s = poll_s
        self.kill_grace_s = kill_grace_s
        self.rc_timeout_s = rc_timeout_s
        self.end_wait_s = end_wait_s
        self.lock = asyncio.Lock()  # one turn at a time
        self.shell = ShellCwd(str(cwd))  # PER-258: where its Bash is (reset at each start)
        self.proc: asyncio.subprocess.Process | None = None
        self.live: LiveInput | None = None
        self.target: Target | None = None
        self._tasks: set[asyncio.Task[Any]] = set()
        self._exited: asyncio.Task[int] | None = None
        self._result: asyncio.Future[dict[str, Any]] | None = None
        self._observe: Observe | None = None
        self._check: InitCheck | None = None
        self._refusal: str | None = None
        self._output = False  # the process wrote anything (a resume that took)
        self._stopping: asyncio.Task[None] | None = None
        self._stops: set[asyncio.Task[None]] = set()  # pause-hook stops (never in _tasks)
        self._killed = False  # /kill landed during this turn: never a crash, never retried
        self._fingerprint = ""
        self._unhook: Callable[[], None] | None = None
        self._watch: asyncio.Task[None] | None = None  # the pause check (PER-262)
        self._limit_warned: set[str] = set()  # rate-limit warnings logged (window, reset)

    # -- state --

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    def alive(self) -> bool:
        if self.proc is None or self.proc.returncode is not None:
            return False
        return self._exited is not None and not self._exited.done()

    def _save(self, **update: Any) -> dict[str, Any]:
        st = session_state(self.conn)
        st.update(update)
        st = {k: v for k, v in st.items() if v is not None}
        with atomic(self.conn):
            kv_set(self.conn, SESSION_KV, st)
        return st

    def _forget(self) -> None:
        """No resume: the next start is a new session (primed). The taint chain goes with
        it: the primer holds only clean records."""
        with atomic(self.conn):
            kv_set(self.conn, SESSION_KV, {})

    def inherited_hops(self) -> int | None:
        """The hops a turn starts tainted with from the session's earlier turns, or None
        (clean): the last turn was tainted ``h`` turns from outside content, and ``h + 1``
        is within ``taint_hops``: this one is ``h + 1``. Lifts after the hop window."""
        hops = session_state(self.conn).get("hops")
        if not isinstance(hops, int) or isinstance(hops, bool) or hops < 0:
            return None
        return hops + 1 if hops < self.taint_hops else None

    def record(self, hops: int | None) -> None:
        """A turn's taint (None: clean), for the next one."""
        if not session_state(self.conn).get("session_id"):
            return
        if hops is not None:  # tainted: the context may hold outside text until a fresh start
            self._save(hops=hops, outside=True)
        else:
            self._drop("hops")

    def _stale(self, st: dict[str, Any]) -> bool:
        """The context holds outside content whose taint has lifted (review of BR-11): the
        next turn starts a fresh, primed session, so what the session holds lifts with the
        taint instead of outliving it."""
        if st.get("outside") is not True:
            return False
        hops = st.get("hops")
        return not (isinstance(hops, int) and not isinstance(hops, bool) and hops < self.taint_hops)

    def _drop(self, key: str) -> None:
        st = session_state(self.conn)
        if key in st:
            st.pop(key)
            with atomic(self.conn):
                kv_set(self.conn, SESSION_KV, st)

    def _rotate_due(self, st: dict[str, Any]) -> bool:
        turns = st.get("turns")
        if isinstance(turns, int) and turns >= self.cfg.rotate_turns:
            return True
        started = st.get("started_at")
        if isinstance(started, str):
            try:
                at = datetime.fromisoformat(started)
            except ValueError:
                return True
            return self.clock() - at >= timedelta(hours=self.cfg.rotate_hours)
        return False

    # -- a turn --

    async def turn(
        self,
        *,
        argv: Argv,
        env: dict[str, str],
        message: str,
        primer: Callable[[], str],
        observe: Observe,
        check_init: InitCheck,
        paused: Callable[[], bool],
        may_retry: Callable[[], bool],
        started: Callable[[], None] | None = None,
        fingerprint: str = "",
        hops_of: Callable[[TurnOutcome], int | None] | None = None,
    ) -> TurnOutcome:
        """One owner message into the session (started, resumed or rotated first as
        needed): wait for its ``result``. ``primer`` is prepended in a new session.
        ``may_retry``: whether a crash may be retried in a fresh session (no change ran).
        ``started`` runs once the message is written. ``fingerprint``: the tool names the
        argv allows; a change restarts the session (its allowlist is fixed at spawn).
        ``hops_of``: the turn's taint hops, recorded before the lock is released."""
        async with self.lock:
            self._killed = False
            self._check = check_init  # before the spawn: an early init is checked too
            fresh = await self._ready(argv, env, fingerprint)
            out = await self._exchange(message, primer, fresh, observe, check_init, paused,
                                       started)  # fmt: skip
            if out.status == "crashed" and not paused() and not self._killed and may_retry():
                log.warning("claude brain: the session ended mid-turn; retrying fresh")
                await self._kill()
                self._forget()
                self._check = check_init
                fresh = await self._ready(argv, env, fingerprint)
                out = await self._exchange(message, primer, fresh, observe, check_init, paused,
                                           None)  # fmt: skip
                out = TurnOutcome(out.status, out.result, out.refusal, out.fresh, retried=True)
            if out.status in ("crashed", "timeout", "refused"):
                await self._kill()
                self._forget()  # the next turn starts fresh, with the primer
            elif hops_of is not None:
                self.record(hops_of(out))
            self._check = None
            return out

    async def _ready(self, argv: Argv, env: dict[str, str], fingerprint: str = "") -> bool:
        """A live process for the next turn; True when it is a new session (prime it). A
        session is rotated (stopped, the next one new and primed) after ``rotate_turns`` or
        ``rotate_hours``, when its tools changed, or once outside content it holds has
        passed the hop window (``_stale``)."""
        st = session_state(self.conn)
        if self.alive():
            changed = fingerprint != self._fingerprint
            if not (self._rotate_due(st) or self._stale(st) or changed):
                return False
            log.info("claude brain: rotating the session",
                     extra={"turns": st.get("turns"), "tools_changed": changed})  # fmt: skip
            await self.stop(keep=False)
            st = {}
        await self._reap_left(st)
        resume = (
            isinstance(st.get("session_id"), str)
            and st.get("written") is True
            and not self._rotate_due(st)
            and not self._stale(st)
        )
        if resume:
            sid = str(st["session_id"])
            ok = await self._spawn(argv(sid, True), env, sid, fingerprint)
            if ok and await self._resumed():
                return False
            log.warning("claude brain: the resume didn't take; starting a new session")
            await self._kill()
        sid = str(uuid.uuid4())
        with atomic(self.conn):
            kv_set(self.conn, SESSION_KV, {"session_id": sid, "started_at": iso(self.clock()),
                                           "turns": 0})  # fmt: skip
        await self.sweep(keep=sid)  # the session just left behind may be old enough now
        await self._spawn(argv(sid, False), env, sid, fingerprint)
        return True

    async def _resumed(self) -> bool:
        """A resumed process that is still up a moment after starting took the session (a
        bad id makes the CLI exit at once)."""
        exited = self._exited
        if exited is None:
            return False
        done, _ = await asyncio.wait({exited}, timeout=min(1.0, self.end_wait_s))
        return not done

    async def _spawn(
        self, argv: list[str], env: dict[str, str], sid: str, fingerprint: str = ""
    ) -> bool:
        self.cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._fingerprint = fingerprint
        self.shell.path = str(self.cwd)  # a new process's Bash starts in its cwd
        marker = f"brain-{sid}"
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=self.cwd,
                env={**env, MARKER_ENV: marker},
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError as e:
            log.warning("claude brain: couldn't start (%s)", e.strerror)
            return False
        try:
            created: float | None = psutil.Process(proc.pid).create_time()
        except psutil.Error:
            created = None
        assert proc.stdin and proc.stdout and proc.stderr
        self.proc = proc
        self.target = Target(proc.pid, proc.pid, created, marker=marker)
        self.live = LiveInput(proc.stdin)
        self._output = False
        self._refusal = None
        self._exited = asyncio.ensure_future(proc.wait())
        self._track(self._pump_out(proc, self.live))
        self._track(self._pump_err(proc))
        self._save(pid=proc.pid, created=created, marker=marker, remote_url=None)
        if self._unhook is None:
            self._unhook = killswitch.add_pause_hook(self._on_pause)
        if self.paused is not None:
            if self._watch is not None:  # an earlier process's (it ended without _kill)
                self._watch.cancel()
            self._watch = asyncio.ensure_future(self._watch_pause(proc))
        with atomic(self.conn):
            audit(self.conn, "brain.session_start", session=sid[:8], pid=proc.pid)
        if self.cfg.remote_control:
            async with self.live.lock:  # first, as the CLI expects it (RC-1)
                self.live.remote = True
                ask = await self.live.control({"subtype": "remote_control", "enabled": True})
            if ask is not None:
                self._track(self._remote(self.live, ask))
        return True

    async def _exchange(
        self,
        message: str,
        primer: Callable[[], str],
        fresh: bool,
        observe: Observe,
        check_init: InitCheck,
        paused: Callable[[], bool],
        started: Callable[[], None] | None,
    ) -> TurnOutcome:
        live, exited = self.live, self._exited
        if not self.alive() or live is None or exited is None:
            return TurnOutcome("crashed", fresh=fresh)
        if fresh and (context := primer()):
            message = f"{context}\n\n{message}"
        loop = asyncio.get_running_loop()
        result: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._result, self._observe, self._check = result, observe, check_init
        begun = time.monotonic()
        try:
            async with live.lock:
                wrote = await live.write_user(message)
            if not wrote:
                return TurnOutcome("crashed", fresh=fresh)
            if started is not None:
                started()
            while True:
                done, _ = await asyncio.wait({result, exited}, timeout=self.poll_s)
                if result in done or result.done():
                    break
                if self._killed or paused():  # first: a /kill's stop is not a crash
                    await self.stop(keep=True)
                    return TurnOutcome("killed", fresh=fresh)
                if self._refusal is not None:
                    return TurnOutcome("refused", refusal=self._refusal, fresh=fresh)
                if done:  # the process ended with no result
                    return TurnOutcome("crashed", fresh=fresh)
                if self.target is not None:
                    reaper.sample(self.target)
                if time.monotonic() - begun >= self.cfg.turn_timeout_s:
                    log.warning("claude brain: turn timed out; killing the session")
                    return TurnOutcome("timeout", fresh=fresh)
        except asyncio.CancelledError:
            self._kill_now()  # the step was cancelled mid-turn: never leave it answering
            raise
        finally:
            self._result, self._observe, self._check = None, None, None
        res = result.result()
        turns: Any = session_state(self.conn).get("turns")
        self._save(turns=(turns if isinstance(turns, int) else 0) + 1, written=True)
        return TurnOutcome("answered", result=res, fresh=fresh)

    # -- the process's output --

    def _track(self, coro: Any) -> None:
        job = asyncio.ensure_future(coro)
        self._tasks.add(job)
        job.add_done_callback(self._tasks.discard)

    async def _pump_out(self, proc: asyncio.subprocess.Process, live: LiveInput) -> None:
        assert proc.stdout
        async for raw, truncated in read_lines(proc.stdout):
            if truncated or not raw.startswith(b"{"):
                continue
            try:
                msg: Any = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            m = cast(dict[str, Any], msg)
            self._output = True
            if live.feed(m):
                continue
            kind = m.get("type")
            if kind == "rate_limit_event":
                self.note_rate_limit(m)
                continue
            if kind == "system" and m.get("subtype") == "init":
                tools = m.get("tools")
                names = [t for t in cast(list[Any], tools) if isinstance(t, str)] if (
                    isinstance(tools, list)) else []  # fmt: skip
                check = self._check
                if check is not None and self._refusal is None:
                    self._refusal = check(names)
                continue
            if self._observe is not None:
                self._observe(m)
            if kind == "result" and self._result is not None and not self._result.done():
                self._result.set_result(m)

    async def _pump_err(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stderr
        async for _raw, _truncated in read_lines(proc.stderr, 8192):
            pass  # drained, never logged: it can quote what Claude read

    def note_rate_limit(self, m: dict[str, Any]) -> None:
        """A ``rate_limit_event`` (PER-262): logged with its status, window and reset; a
        warning one (the limit is near) once per window and reset. The limit itself, once
        hit, comes back in the turn's ``result`` (``runners.claude.usage_limit``)."""
        raw: Any = m.get("rate_limit_info")
        info = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        status, window, resets = (info.get(k) for k in ("status", "rateLimitType", "resetsAt"))
        status = status if isinstance(status, str) else "unknown"
        seen = {
            "status": status,
            "window": window if isinstance(window, str) else None,
            "resets_at": resets if isinstance(resets, (int, float, str)) else None,
            "utilization": u if isinstance(u := info.get("utilization"), (int, float)) else None,
        }
        if status == "allowed":
            log.debug("claude brain: rate limit", extra=seen)
            return
        if status == "rejected":
            self._limited(resets)
        key = f"{seen['window']}:{seen['resets_at']}:{status}"
        if key in self._limit_warned:
            return
        self._limit_warned.add(key)
        log.warning("claude brain: near a Claude rate limit (%s)", status, extra=seen)

    def _limited(self, resets: object) -> None:
        """PER-256: a rejected rate limit with a reset time in the future stops asks until
        then, so the next text hears so once (``ask.LIMIT_OWNER``) instead of failing a turn.
        An earlier or equal stored reset stays."""
        if not isinstance(resets, (int, float)) or isinstance(resets, bool):
            return
        try:
            until = datetime.fromtimestamp(float(resets), UTC)
        except (OverflowError, OSError, ValueError):
            return
        if until <= self.clock():
            return
        try:
            with atomic(self.conn):
                raw = kv_get(self.conn, LIMIT_KV)
                held = datetime.fromisoformat(raw) if isinstance(raw, str) else None
                if held is not None and held.tzinfo is not None and held >= until:
                    return
                kv_set(self.conn, LIMIT_KV, iso(until))
                kv_set(self.conn, LIMIT_KNOWN_KV, True)
        except (sqlite3.Error, ValueError):
            log.warning("claude brain: usage limit not recorded", exc_info=True)

    async def _remote(self, live: LiveInput, ask: asyncio.Future[ControlReply]) -> None:
        """The Remote Control answer (RC-1): a valid URL is kept for /status and the
        dashboard; an error or no answer leaves the session going without it."""
        reply = await live.reply(ask, self.rc_timeout_s)
        url = session_url(reply) if reply is not None and reply.ok else None
        if url is None:
            live.remote = False  # nothing to turn off at the end
        if live is not self.live:
            return  # that process is gone
        self._save(remote_url=url)
        with atomic(self.conn):
            audit(self.conn, "brain.session_remote", ok=url is not None)

    # -- ending --

    def _on_pause(self, conn: sqlite3.Connection, paused: bool) -> None:
        """``/kill``: stop the session (a turn in flight sees the pause itself)."""
        if not paused:
            return
        self._killed = True
        if self.alive() and self._stopping is None:
            with contextlib.suppress(RuntimeError):  # no running loop: nothing to stop
                job = asyncio.ensure_future(self.stop(keep=True))
                self._stops.add(job)
                job.add_done_callback(self._stops.discard)

    async def _watch_pause(self, proc: asyncio.subprocess.Process) -> None:
        """PER-262: a pause written by another process (no hook fires here) stops an idle
        session within ``pause_check_s``. A turn in flight checks the pause on its own."""
        paused = self.paused
        assert paused is not None
        while True:
            await asyncio.sleep(self.cfg.pause_check_s)
            if self.proc is not proc or not self.alive():
                return  # that process is gone
            if self.busy or self._stopping is not None:
                continue
            if paused():
                log.info("claude brain: paused from outside; stopping the session")
                self._on_pause(self.conn, True)
                return

    async def stop(self, *, keep: bool = True) -> None:
        """End the session the RC-1 way: Remote Control off, stdin closed, then the group
        reaped after ``end_wait_s``. ``keep``: the id stays for a later ``--resume``."""
        if self._stopping is not None:
            await asyncio.shield(self._stopping)
            return
        self._stopping = asyncio.ensure_future(self._stop())
        try:
            await asyncio.shield(self._stopping)
        finally:
            self._stopping = None
        if not keep:
            self._forget()

    async def _stop(self) -> None:
        live, exited = self.live, self._exited
        if live is not None:
            await live.end(rc_timeout=min(self.rc_timeout_s, 3.0))
        if exited is not None and not exited.done():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(exited), self.end_wait_s)
        await self._kill()

    async def _kill(self) -> None:
        """The process group gone now (reaped), the readers finished, kv cleared of it."""
        proc, target, live, exited = self.proc, self.target, self.live, self._exited
        self.proc = self.target = self.live = None
        self._exited = None
        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
        if live is not None:
            live.close()
        if target is not None:
            await reaper.reap(target, grace_s=self.kill_grace_s)
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
        if exited is not None:
            exited.cancel()
        jobs = [j for j in self._tasks if j is not asyncio.current_task()]
        if jobs:
            _, pending = await asyncio.wait(jobs, timeout=5)
            for j in pending:
                j.cancel()
        if session_state(self.conn) and self.proc is None:  # not a newer process's
            self._save(pid=None, created=None, marker=None, remote_url=None)

    def _kill_now(self) -> None:
        """Synchronous kill (a cancelled step): the group is signalled; reaping and the kv
        follow on the next start (``_reap_left``)."""
        proc, live = self.proc, self.live
        if live is not None:
            live.close()
        if proc is not None and proc.returncode is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)

    async def _reap_left(self, st: dict[str, Any]) -> None:
        """A session process a crashed daemon (or a cancelled step) left: end it first."""
        pid, created, marker = st.get("pid"), st.get("created"), st.get("marker")
        if not isinstance(pid, int):
            return
        if self.proc is not None and self.proc.pid == pid:
            await self._kill()
            return
        target = Target(pid, pid, float(created) if isinstance(created, (int, float)) else None,
                        marker=marker if isinstance(marker, str) else None)  # fmt: skip
        await reaper.reap(target, grace_s=self.kill_grace_s)
        self._save(pid=None, created=None, marker=None, remote_url=None)
        log.warning("claude brain: reaped a session left behind")

    # -- upkeep (PER-262) --

    def transcripts(self) -> Path | None:
        """This session's transcript dir: ``<home>/.claude/projects/<encoded real cwd>``."""
        if self.home is None:
            return None
        return self.home / ".claude" / "projects" / project_dir_name(self.cwd)

    async def sweep(self, *, keep: str | None = None) -> int:
        """Remove the brain's earlier transcripts older than ``transcript_days`` (never
        ``keep`` nor the kv session). The count removed; a failure is logged, never raised."""
        where, days = self.transcripts(), self.cfg.transcript_days
        if where is None or days <= 0:
            return 0
        current = session_state(self.conn).get("session_id")
        spare = {k for k in (keep, current) if isinstance(k, str)}
        cutoff = time.time() - days * 86400
        try:
            n = await asyncio.to_thread(sweep_transcripts, where, cutoff, spare)
        except OSError as e:
            log.warning("claude brain: transcript sweep failed (%s)", type(e).__name__)
            return 0
        if n:
            log.info("claude brain: removed %d old transcript(s)", n)
        return n

    async def startup(self) -> None:
        """Daemon start (PER-262), before the loop resumes anything: reap a session a killed
        daemon left (kv), then sweep old transcripts. Never raises."""
        try:
            st = session_state(self.conn)
            if isinstance(st.get("pid"), int) and self.proc is None:
                await self._reap_left(st)
        except Exception:
            log.exception("claude brain: startup reap failed")
        await self.sweep()

    async def aclose(self) -> None:
        """Daemon shutdown: end the session (the id is kept for a resume), the server too."""
        if self._unhook is not None:
            self._unhook()
            self._unhook = None
        if self.alive():
            await self.stop(keep=True)
        else:
            await self._kill()
        await self.server.aclose()


def project_dir_name(cwd: Path) -> str:
    """Claude Code's name for a cwd's dir under ``~/.claude/projects``: the real path with
    every character but ASCII letters and digits as ``-`` (``/Users/me/.resonant/ask/session``
    -> ``-Users-me--resonant-ask-session``; checked against 2.1.305's own dirs)."""
    return re.sub(r"[^A-Za-z0-9]", "-", os.path.realpath(cwd))


def sweep_transcripts(where: Path, cutoff: float, keep: set[str]) -> int:
    """Remove the session transcripts in ``where`` last changed before ``cutoff`` (epoch
    seconds): ``<uuid>.jsonl`` files and ``<uuid>/`` dirs (a session's tool results), never
    an id in ``keep``, a symlink, another name (``memory/``) or anything outside ``where``.
    ``where`` itself must be a real dir (not a link). The count removed."""
    try:
        if not where.is_dir() or where.is_symlink():
            return 0
        entries = list(os.scandir(where))
    except FileNotFoundError:
        return 0
    removed = 0
    for entry in entries:
        name = entry.name
        sid = name[: -len(".jsonl")] if name.endswith(".jsonl") else name
        if not _UUID.match(sid) or sid in keep or entry.is_symlink():
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except FileNotFoundError:
            continue
        if st.st_mtime >= cutoff:
            continue
        if name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(entry.path)
            removed += 1
        elif name == sid and entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path, ignore_errors=True)
            removed += 1
    return removed


__all__ = [
    "SESSION_KV",
    "ClaudeSession",
    "TurnOutcome",
    "project_dir_name",
    "session_state",
    "status_of",
    "sweep_transcripts",
]
