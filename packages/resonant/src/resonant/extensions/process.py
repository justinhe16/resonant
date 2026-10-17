"""The extension process runner: argv templating, a scratch environment, caps, tree kills.

An extension command is the manifest's ``run`` string, ``shlex.split`` once by
:meth:`ExtCommand.compile`. Placeholders such as ``{tz}`` are filled *inside* tokens by
:meth:`ExtCommand.render`, so a value is never parsed and never adds or splits an argv
element. Nothing runs through a shell.

:class:`ExtProcess` runs one :class:`ExtRequest`:

- The environment is exactly ``ExtRequest.env`` (build it with :func:`scratch_env`); nothing
  is inherited from ``os.environ``.
- stdin is ``/dev/null``; the process starts in a new session (its own process group).
- stdout is read in chunks up to ``stdout_cap``; past that the tree is killed. stderr keeps
  only its last ``stderr_cap`` bytes and is never logged or returned, except a redacted
  200-char tail on the exceptions for the failure audit row.
- Secrets never touch the child's environment or argv (a same-user process can read both with
  ``sysctl KERN_PROCARGS2``, which no sandbox can deny). The ``RESONANT_SECRET_*`` keys that
  :func:`scratch_env` builds are the daemon-side hand-off only: :meth:`ExtProcess.run_raw` strips
  them out of the child's env and delivers them as one JSON object ``{name: value}`` on fd 4, a
  pipe whose write end the daemon fills and closes before the child starts
  (``RESONANT_SECRETS_FD=4``; ``resonant_sdk.secret`` reads it once). No file, so nothing to
  clean up after a kill or timeout.
- fd 3 is a pipe for structured events (``RESONANT_EVENTS_FD=3``): one JSON object per line,
  capped like stdout.
- Every live run is tracked by a :class:`ProcessTracker`: its process group plus the
  descendants sampled with ``psutil``, which catches children that ``setsid`` out of the
  group. Timeout, cancel, :meth:`ProcessTracker.kill_all` (the kill switch) and plain exit
  all end with SIGTERM to the group and the tracked tree, then SIGKILL after a grace period.

Result contract: exit 0 with exactly one JSON object is success. A non-zero exit is
:class:`ExtFailed` (a ``ToolFailed``: the author promises nothing took effect). A timeout,
kill, cap overflow, signal death or unparsable output is :class:`ExtAmbiguous`: the executor
treats it as a definite failure for reads and as ambiguous for writes.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import os
import pwd
import re
import shlex
import signal
import string
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, cast

import psutil

from resonant.executor import ToolFailed
from resonant.extensions.checks import argv0_error

log = logging.getLogger(__name__)

SCRATCH_PATH = "/usr/bin:/bin"
EVENTS_FD = 3
SECRETS_FD = 4
SECRETS_CAP = 8 * 1024  # JSON bytes; fits an empty pipe buffer, so the write never blocks
MAX_ARG_CHARS = 1000
STDOUT_CAP = 256 * 1024
STDERR_CAP = 8 * 1024
EVENTS_CAP = 256 * 1024
MAX_EVENTS = 100
MAX_EVENT_TEXT = 1000
MAX_DEDUPE_KEY = 200
STDERR_TAIL = 200
SAMPLE_S = 2.0
GRACE_S = 2.0
DRAIN_S = 1.0  # after exit and cleanup, how long to wait for the pipes to reach EOF
_POLL_S = 0.05

_SECRET_ENV = re.compile(r"RESONANT_SECRET_[A-Z][A-Z0-9_]{0,31}")
_BIN_ENV = re.compile(r"RESONANT_BIN_[A-Z]{1,16}")
_EVENT_TYPE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_EVENT_KEYS = frozenset({"type", "text", "dedupe_key", "data"})

# Runs between fork and the extension's exec: moves the events pipe to fd 3 and the secrets
# pipe (if any, else "-1") to fd 4, then execs the real argv. Both are first copied above
# fd 10 so neither move can clobber the other. A fixed string; argv goes through execve. The
# env passed on is only the keys we set (names in argv[3], values from the env), since the
# interpreter itself may add some at startup (macOS: __CF_USER_TEXT_ENCODING).
_TRAMPOLINE = (
    "import os,sys,fcntl\n"
    "src=[int(sys.argv[1]),int(sys.argv[2])]\n"
    "tmp=[fcntl.fcntl(f,fcntl.F_DUPFD,10) if f>=0 else -1 for f in src]\n"
    "for f in src:\n"
    "    if f>=0:\n"
    "        os.close(f)\n"
    "for dst,t in zip((3,4),tmp):\n"
    "    if t>=0:\n"
    "        os.dup2(t,dst)\n"
    "        os.close(t)\n"
    "keys=sys.argv[3].split('\\x1f') if sys.argv[3] else []\n"
    "env={k:os.environ[k] for k in keys if k in os.environ}\n"
    "try:\n"
    "    os.execve(sys.argv[4],sys.argv[5:],env)\n"
    "except OSError:\n"
    "    os._exit(127)\n"
)
_KEY_SEP = "\x1f"

RedactFn = Callable[[str], str]


class TemplateError(ValueError):
    """A ``run`` string that can't be compiled (refused at load, never at call time)."""


class ExtFailed(ToolFailed):
    """The command exited non-zero, or couldn't start: definitely nothing took effect.

    ``stderr_tail`` is at most 200 redacted chars, for the failure audit row only.
    """

    def __init__(self, reason: str, *, stderr_tail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.stderr_tail = stderr_tail


class ExtAmbiguous(Exception):
    """The run was cut short or its outcome can't be read; it may have taken effect.

    ``reason`` is one of ``timeout``, ``killed``, ``output too large``,
    ``events too large``, ``invalid output`` or ``signal <n>``.
    """

    def __init__(self, reason: str, *, stderr_tail: str = "") -> None:
        super().__init__(reason)
        self.reason = reason
        self.stderr_tail = stderr_tail


# --- argv templating ---------------------------------------------------------------------


@dataclass(frozen=True)
class _Slot:
    name: str


_Part = str | _Slot


@dataclass(frozen=True)
class ExtCommand:
    """A compiled ``run`` string: tokens of literal text and ``{arg}`` slots."""

    tokens: tuple[tuple[_Part, ...], ...]

    @classmethod
    def compile(
        cls, run: str, *, root: Path, argv0: str | None = None, python: str = sys.executable
    ) -> ExtCommand:
        """``shlex.split`` ``run`` once and parse each token's placeholders.

        argv[0] must pass ``checks.argv0_error(root, run)`` and then:

        - a file in the extension dir (``./cli.py``) or ``python3 <script>``: only ``.py``
          files run, as ``[python, -I, -S, <script>, …]``; the shebang is never used, so
          ``./run.sh`` with ``#!/usr/bin/env npx`` is refused;
        - ``gh`` / ``git``: run by ``argv0``, the absolute path pinned at install
          (``ExtensionRecord.argv0_paths[spec]``), never a PATH lookup at run time.

        argv0 and the script are literal: a placeholder there is a ``TemplateError``.
        """
        try:
            words = shlex.split(run)
        except ValueError as e:
            raise TemplateError(f"run: {e}") from e
        if not words:
            raise TemplateError("run is empty")
        tokens = [_parse_token(w) for w in words]
        head = words[0]
        if not _literal(tokens[0]) or not head:
            raise TemplateError("the command name must be literal")
        if err := argv0_error(root.resolve(), run):
            raise TemplateError(err)
        if head == "python3":
            if len(tokens) < 2 or not _literal(tokens[1]):
                raise TemplateError("python3 must be followed by a literal script path")
            return cls(((python,), ("-I",), ("-S",), *tokens[1:]))
        if "/" in head:
            if not head.endswith(".py"):
                raise TemplateError(
                    f"{head!r}: only .py files in the extension dir can run (no shebangs)"
                )
            return cls(((python,), ("-I",), ("-S",), *tokens))
        if argv0 is None or not os.path.isabs(argv0) or Path(argv0).name != head:
            raise TemplateError(f"{head!r} must run by its pinned absolute path")
        return cls(((argv0,), *tokens[1:]))

    @property
    def placeholders(self) -> frozenset[str]:
        return frozenset(p.name for tok in self.tokens for p in tok if isinstance(p, _Slot))

    def render(self, args: Mapping[str, object]) -> tuple[str, ...]:
        """The argv for ``args``: one element per token, whatever the values contain.

        Raises ``ToolFailed`` (definite: nothing ran) for a missing arg, an unsupported
        type, a string with NUL or over 1000 chars, or a token that begins with a
        placeholder and renders starting with ``-`` (write ``--tz={tz}`` instead). Values
        are never echoed.
        """
        argv: list[str] = []
        for tok in self.tokens:
            out: list[str] = []
            for part in tok:
                if isinstance(part, str):
                    out.append(part)
                    continue
                if part.name not in args:
                    raise ToolFailed(f"missing arg {part.name}")
                out.append(_arg_text(part.name, args[part.name]))
            rendered = "".join(out)
            # A token that starts with a slot may not start with '-' once filled, however
            # the values split it ({a}{b} with a="" and b="--evil" included).
            if isinstance(tok[0], _Slot) and rendered.startswith("-"):
                raise ToolFailed(f"arg {tok[0].name} may not start with '-'")
            argv.append(rendered)
        return tuple(argv)


def _parse_token(word: str) -> tuple[_Part, ...]:
    parts: list[_Part] = []
    try:
        parsed = list(string.Formatter().parse(word))
    except ValueError as e:
        raise TemplateError(f"bad placeholder in {word!r}: {e}") from e
    for literal, name, spec, conversion in parsed:
        if literal:
            parts.append(literal)
        if name is None:
            continue
        if not name.isidentifier() or spec or conversion:
            raise TemplateError(f"placeholder {{{name}}} must be a plain arg name like {{date}}")
        parts.append(_Slot(name))
    return tuple(parts) or ("",)


def _literal(token: tuple[_Part, ...]) -> bool:
    return all(isinstance(p, str) for p in token)


def _arg_text(name: str, value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        try:
            text = str(value)
        except ValueError:  # over Python's int-to-str digit limit
            text = "x" * (MAX_ARG_CHARS + 1)
        if len(text) > MAX_ARG_CHARS:
            raise ToolFailed(f"arg {name} is over {MAX_ARG_CHARS} chars")
        return text
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ToolFailed(f"arg {name} must be a finite number")
        return repr(value)
    if isinstance(value, str):
        if "\x00" in value:
            raise ToolFailed(f"arg {name} may not contain NUL")
        if len(value) > MAX_ARG_CHARS:
            raise ToolFailed(f"arg {name} is over {MAX_ARG_CHARS} chars")
        return value
    raise ToolFailed(f"arg {name} must be a string, number or boolean")


# --- environment -------------------------------------------------------------------------


def scratch_env(
    *,
    ext: str,
    data_dir: Path,
    secrets: Mapping[str, str] | None = None,
    job_due_at: str | None = None,
    user: str | None = None,
    path: str = SCRATCH_PATH,
    bins: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """The whole environment for one extension call, built from scratch (no ``os.environ``).

    ``secrets`` is ``SecretStore.env_for(ext, declared)``: ``{RESONANT_SECRET_<NAME>:
    value}`` for only this tool's or job's declared secrets. ``path`` is the whole
    ``PATH`` (``settings.extensions.process.path_env()``). ``bins`` is the manifest's pinned
    ``bins``: ``{RESONANT_BIN_<NAME>: absolute path}``.
    """
    name = user or pwd.getpwuid(os.getuid()).pw_name
    env = {
        "PATH": path,
        "HOME": str(data_dir),
        "TMPDIR": str(data_dir / "tmp"),
        "LANG": "en_US.UTF-8",
        "USER": name,
        "LOGNAME": name,
        "RESONANT_EXT": ext,
        "RESONANT_EXT_DATA": str(data_dir),
        "RESONANT_EVENTS_FD": str(EVENTS_FD),
        "GIT_TERMINAL_PROMPT": "0",
    }
    if job_due_at is not None:
        env["RESONANT_JOB_DUE_AT"] = job_due_at
    for key, value in (bins or {}).items():
        if not _BIN_ENV.fullmatch(key) or not os.path.isabs(value) or "\x00" in value:
            raise ValueError(f"bad pinned bin {key!r}")
        env[key] = value
    for key, value in (secrets or {}).items():
        if not _SECRET_ENV.fullmatch(key):
            raise ValueError(f"bad secret env name {key!r}")
        if not value or "\x00" in value:
            raise ValueError(f"secret {key} is empty or has NUL")
        env[key] = value
    return env


def ensure_data_dir(data_dir: Path) -> None:
    """Create the data dir and its ``tmp`` (owner-only) if missing."""
    (data_dir / "tmp").mkdir(mode=0o700, parents=True, exist_ok=True)


# --- process tracking --------------------------------------------------------------------


@dataclass(eq=False)
class _Tracked:
    pid: int  # also the pgid: every run starts a new session
    label: str
    tree: dict[int, float] = field(default_factory=dict[int, float])  # pid -> create_time
    reason: str | None = None  # set when we chose to end it: timeout, killed, …


class ProcessTracker:
    """Every live extension process, with its group and sampled descendants.

    One per daemon. ``kill_all`` is the kill switch's hook: each affected run raises
    ``ExtAmbiguous("killed")``.
    """

    def __init__(self, *, sample_s: float = SAMPLE_S, grace_s: float = GRACE_S) -> None:
        self.sample_s = sample_s
        self.grace_s = grace_s
        # While True, new runs are refused (ExtFailed "paused"): the kill switch sets it
        # with kill_all(hold=True) and clears it on resume.
        self.paused = False
        self._live: set[_Tracked] = set()

    @property
    def live(self) -> int:
        return len(self._live)

    async def kill_all(self, reason: str = "killed", *, hold: bool = False) -> int:
        """End every tracked run (SIGTERM, then SIGKILL after the grace). Returns how many.

        ``hold=True`` also sets ``paused`` first, so no new run starts until it's cleared.
        """
        if hold:
            self.paused = True
        entries = list(self._live)
        for e in entries:
            e.reason = e.reason or reason
        await asyncio.gather(*(self.terminate(e) for e in entries))
        if entries:
            log.warning("ext.process.kill_all", extra={"count": len(entries), "reason": reason})
        return len(entries)

    def resume(self) -> None:
        """Allow new runs again (the kill switch's release)."""
        self.paused = False

    def track(self, pid: int, label: str) -> _Tracked:
        entry = _Tracked(pid=pid, label=label)
        self._live.add(entry)
        self.sample(entry)
        return entry

    def untrack(self, entry: _Tracked) -> None:
        self._live.discard(entry)

    def sample(self, entry: _Tracked) -> None:
        """Record the run's current descendants (they stay recorded after they reparent)."""
        try:
            children = psutil.Process(entry.pid).children(recursive=True)
        except psutil.Error:
            return
        for child in children:
            with contextlib.suppress(psutil.Error):
                entry.tree.setdefault(child.pid, child.create_time())

    async def sample_loop(self, entry: _Tracked) -> None:
        while True:
            await asyncio.sleep(self.sample_s)
            self.sample(entry)

    async def terminate(self, entry: _Tracked) -> None:
        """SIGTERM the group and tracked tree; SIGKILL whatever is left after the grace."""
        self.sample(entry)
        self._signal(entry, signal.SIGTERM)
        for _ in range(max(1, math.ceil(self.grace_s / _POLL_S))):
            if not self._alive(entry):
                return
            await asyncio.sleep(_POLL_S)
        if self._alive(entry):
            self._signal(entry, signal.SIGKILL)

    def kill_now(self, entry: _Tracked) -> None:
        """SIGKILL the group and tracked tree at once if anything is left (no await)."""
        if self._alive(entry):
            self._signal(entry, signal.SIGKILL)

    def _signal(self, entry: _Tracked, sig: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(entry.pid, sig)
        for proc in self._tree(entry):
            with contextlib.suppress(psutil.Error):
                proc.send_signal(sig)

    def _alive(self, entry: _Tracked) -> bool:
        try:
            os.killpg(entry.pid, 0)
            group = True
        except (ProcessLookupError, PermissionError):
            group = False
        return group or bool(self._tree(entry))

    def _tree(self, entry: _Tracked) -> list[psutil.Process]:
        """Tracked descendants still running as the same process (not a reused pid)."""
        out: list[psutil.Process] = []
        for pid, created in list(entry.tree.items()):
            try:
                proc = psutil.Process(pid)
                if proc.create_time() != created or proc.status() == psutil.STATUS_ZOMBIE:
                    del entry.tree[pid]
                    continue
            except psutil.Error:
                del entry.tree[pid]
                continue
            out.append(proc)
        return out


# --- running -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ExtRequest:
    """One call: a rendered argv, its cwd (the extension copy), its whole env, a timeout."""

    argv: tuple[str, ...]
    cwd: Path
    env: Mapping[str, str]
    timeout_s: float
    label: str = ""  # "<ext>/<tool>", for logs; never args


@dataclass(frozen=True)
class ExtEvent:
    type: str
    text: str
    dedupe_key: str
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class RawResult:
    """A run that exited on its own. stdout is redacted; stderr is only ``stderr_tail``."""

    returncode: int
    stdout: str
    stderr_tail: str
    event_lines: tuple[str, ...]
    duration_ms: int


@dataclass(frozen=True)
class ExtOutput:
    """A successful run: its JSON object (minus ``events``) and its validated events."""

    data: dict[str, Any]
    events: tuple[ExtEvent, ...]
    dropped_events: int
    duration_ms: int


class ExtRunner(Protocol):
    async def run(self, req: ExtRequest, *, redact: RedactFn) -> ExtOutput: ...

    async def run_raw(self, req: ExtRequest, *, redact: RedactFn) -> RawResult: ...


class _Overflow(Exception):
    pass


class ExtProcess:
    """The real runner. ``redact`` rewrites stdout, events and the stderr tail first."""

    def __init__(
        self,
        tracker: ProcessTracker | None = None,
        *,
        python: str = sys.executable,
        stdout_cap: int = STDOUT_CAP,
        stderr_cap: int = STDERR_CAP,
        events_cap: int = EVENTS_CAP,
        max_events: int = MAX_EVENTS,
    ) -> None:
        self.tracker = tracker or ProcessTracker()
        self.python = python
        self.stdout_cap = stdout_cap
        self.stderr_cap = stderr_cap
        self.events_cap = events_cap
        self.max_events = max_events

    async def run(self, req: ExtRequest, *, redact: RedactFn) -> ExtOutput:
        """Run and apply the result contract (see the module docstring)."""
        raw = await self.run_raw(req, redact=redact)
        if raw.returncode > 0:
            raise ExtFailed(f"exit {raw.returncode}", stderr_tail=raw.stderr_tail)
        if raw.returncode < 0:
            raise ExtAmbiguous(f"signal {-raw.returncode}", stderr_tail=raw.stderr_tail)
        try:
            data: object = json.loads(raw.stdout)
        except (ValueError, RecursionError):
            data = None
        if not isinstance(data, dict):
            raise ExtAmbiguous("invalid output", stderr_tail=raw.stderr_tail)
        result = cast(dict[str, Any], data)
        candidates: list[object] = []
        dropped = 0
        if "events" in result:
            listed = result.pop("events")
            if isinstance(listed, list):
                candidates.extend(cast(list[object], listed))
            else:
                dropped += 1
        for line in raw.event_lines:
            try:
                candidates.append(json.loads(line))
            except (ValueError, RecursionError):
                dropped += 1
        events: list[ExtEvent] = []
        for item in candidates:
            event = parse_event(item)
            if event is None or len(events) >= self.max_events:
                dropped += 1
            else:
                events.append(event)
        return ExtOutput(result, tuple(events), dropped, raw.duration_ms)

    async def run_raw(self, req: ExtRequest, *, redact: RedactFn) -> RawResult:
        """Run to exit, under the caps and timeout. Returns any exit code (health checks
        use it directly). Raises ``ExtFailed`` if it can't start (or the tracker is paused)
        and ``ExtAmbiguous`` if it was cut short; on cancel the tree is killed and
        ``CancelledError`` propagates."""
        if not req.argv:
            raise ExtFailed("empty argv")
        if self.tracker.paused:
            raise ExtFailed("paused")
        env = dict(req.env)
        secrets = _take_secrets(env)
        if any(not k or "=" in k or _KEY_SEP in k for k in env):
            raise ExtFailed("bad env name")
        exe = _resolve(req.argv[0])
        if exe is None:
            raise ExtFailed("argv[0] is not an absolute path to an executable")
        started = time.monotonic()
        loop = asyncio.get_running_loop()
        # Our own pipes (not asyncio's subprocess pipes), so exit is seen even while an
        # escaped grandchild still holds stdout or stderr open.
        pipes = [os.pipe() for _ in range(3)]  # stdout, stderr, events
        secrets_fd = -1
        try:
            if secrets:
                secrets_fd = _secrets_pipe(secrets)
                env["RESONANT_SECRETS_FD"] = str(SECRETS_FD)
        except BaseException as e:
            for r, w in pipes:
                os.close(r)
                os.close(w)
            if isinstance(e, ExtFailed):
                raise
            raise ExtFailed("could not pass secrets") from e
        try:
            proc = await asyncio.create_subprocess_exec(
                self.python,
                "-I",
                "-S",
                "-c",
                _TRAMPOLINE,
                str(pipes[2][1]),
                str(secrets_fd),
                _KEY_SEP.join(env),
                exe,
                *req.argv,
                cwd=req.cwd,
                env=env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=pipes[0][1],
                stderr=pipes[1][1],
                pass_fds=(pipes[2][1], *([secrets_fd] if secrets_fd >= 0 else [])),
                start_new_session=True,
            )
        except BaseException as e:
            for r, _ in pipes:
                os.close(r)
            if isinstance(e, OSError | ValueError):
                raise ExtFailed("could not start") from e
            raise
        finally:
            for _, w in pipes:
                os.close(w)
            if secrets_fd >= 0:
                os.close(secrets_fd)
        entry = self.tracker.track(proc.pid, req.label)
        bufs = [bytearray(), bytearray(), bytearray()]
        transports: list[asyncio.BaseTransport] = []
        tasks: list[asyncio.Task[Any]] = []
        files = [os.fdopen(r, "rb", 0) for r, _ in pipes]  # each fd is closed via its file
        try:
            readers: list[asyncio.StreamReader] = []
            for f in files:
                reader = asyncio.StreamReader()
                transport, _proto = await loop.connect_read_pipe(
                    lambda reader=reader: asyncio.StreamReaderProtocol(reader), f
                )
                transports.append(transport)
                readers.append(reader)
            t_out = asyncio.create_task(_read_capped(readers[0], bufs[0], self.stdout_cap))
            t_err = asyncio.create_task(_read_tail(readers[1], bufs[1], self.stderr_cap))
            t_ev = asyncio.create_task(_read_capped(readers[2], bufs[2], self.events_cap))
            t_wait = asyncio.create_task(proc.wait())
            tasks = [
                t_out,
                t_err,
                t_ev,
                t_wait,
                asyncio.create_task(self.tracker.sample_loop(entry)),
            ]
            if self.tracker.paused:  # paused between the check above and track()
                entry.reason = "killed"
                await self.tracker.terminate(entry)
            else:
                await self._watch(entry, t_wait, t_out, t_ev, started + req.timeout_s)
            # The leader has exited or been killed: bound the wait anyway.
            await asyncio.wait({t_wait}, timeout=self.tracker.grace_s + 1)
            await self.tracker.terminate(entry)  # leftovers that outlived the leader
            await asyncio.wait({t_out, t_ev, t_err}, timeout=DRAIN_S)
            if entry.reason is None:
                entry.reason = _late_reason(t_out, t_ev)
        except asyncio.CancelledError:
            entry.reason = entry.reason or "cancelled"
            await self.tracker.terminate(entry)
            raise
        finally:
            # Never leave a live tree behind, even if a second cancel cut terminate() short.
            self.tracker.kill_now(entry)
            self.tracker.untrack(entry)
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for transport in transports:
                transport.close()
            for f in files[len(transports) :]:
                f.close()
        out, err, evs = bufs
        tail = redact(err.decode(errors="replace"))[-STDERR_TAIL:]
        if entry.reason is not None:
            log.info("ext.process.ended", extra={"label": req.label, "reason": entry.reason})
            raise ExtAmbiguous(entry.reason, stderr_tail=tail)
        returncode = proc.returncode if proc.returncode is not None else -1
        lines = redact(evs.decode(errors="replace")).splitlines()
        return RawResult(
            returncode=returncode,
            stdout=redact(out.decode(errors="replace")),
            stderr_tail=tail,
            event_lines=tuple(line for line in lines if line.strip()),
            duration_ms=int((time.monotonic() - started) * 1000),
        )

    async def _watch(
        self,
        entry: _Tracked,
        t_wait: asyncio.Task[int],
        t_out: asyncio.Task[None],
        t_ev: asyncio.Task[None],
        deadline: float,
    ) -> None:
        """Wait for exit; on timeout or a cap overflow, end the tree first."""
        pending: set[asyncio.Task[Any]] = {t_wait, t_out, t_ev}
        while not t_wait.done():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                entry.reason = entry.reason or "timeout"
                break
            _, pending = await asyncio.wait(
                pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
            )
            overflow = _overflow_reason(t_out, t_ev)
            if overflow is not None:
                entry.reason = entry.reason or overflow
                break
            if not pending:
                break
        if entry.reason is not None and not t_wait.done():
            await self.tracker.terminate(entry)


async def spawn_stdio(
    req: ExtRequest, *, python: str = sys.executable, limit: int = STDOUT_CAP
) -> asyncio.subprocess.Process:
    """Start a long-lived process (an MCP server) with pipes on stdin, stdout and stderr.

    The same launch rules as :meth:`ExtProcess.run_raw`: the env is exactly ``req.env``, its
    ``RESONANT_SECRET_*`` keys go to fd 4 instead (``RESONANT_SECRETS_FD=4``), argv[0] must be
    an absolute executable, the process gets a new session (its own group), and no fd 3. The
    caller tracks, reads, and ends it; ``limit`` caps one stdout line. Raises ``ExtFailed``.
    """
    if not req.argv:
        raise ExtFailed("empty argv")
    env = dict(req.env)
    env.pop("RESONANT_EVENTS_FD", None)
    secrets = _take_secrets(env)
    if any(not k or "=" in k or _KEY_SEP in k for k in env):
        raise ExtFailed("bad env name")
    exe = _resolve(req.argv[0])
    if exe is None:
        raise ExtFailed("argv[0] is not an absolute path to an executable")
    secrets_fd = -1
    try:
        if secrets:
            secrets_fd = _secrets_pipe(secrets)
            env["RESONANT_SECRETS_FD"] = str(SECRETS_FD)
        return await asyncio.create_subprocess_exec(
            python,
            "-I",
            "-S",
            "-c",
            _TRAMPOLINE,
            "-1",
            str(secrets_fd),
            _KEY_SEP.join(env),
            exe,
            *req.argv,
            cwd=req.cwd,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=limit,
            pass_fds=(secrets_fd,) if secrets_fd >= 0 else (),
            start_new_session=True,
        )
    except ExtFailed:
        raise
    except (OSError, ValueError) as e:
        raise ExtFailed("could not start") from e
    finally:
        if secrets_fd >= 0:
            os.close(secrets_fd)


def _take_secrets(env: dict[str, str]) -> dict[str, str]:
    """Remove the ``RESONANT_SECRET_<NAME>`` keys from ``env``; return ``{name: value}``."""
    found = {k: env.pop(k) for k in [k for k in env if _SECRET_ENV.fullmatch(k)]}
    return {k.removeprefix("RESONANT_SECRET_").lower(): v for k, v in found.items()}


def _secrets_pipe(secrets: Mapping[str, str]) -> int:
    """A pipe holding ``secrets`` as JSON with its write end already closed; returns the read
    end. The payload is capped so the write can't block on a full pipe."""
    data = json.dumps(secrets, separators=(",", ":")).encode()
    if len(data) > SECRETS_CAP:
        raise ExtFailed("secrets too large")
    r, w = os.pipe()
    try:
        os.write(w, data)
    except BaseException:
        os.close(r)
        raise
    finally:
        os.close(w)
    return r


def _overflow_reason(t_out: asyncio.Task[None], t_ev: asyncio.Task[None]) -> str | None:
    for task, reason in ((t_out, "output too large"), (t_ev, "events too large")):
        if task.done() and not task.cancelled() and isinstance(task.exception(), _Overflow):
            return reason
    return None


def _late_reason(t_out: asyncio.Task[None], t_ev: asyncio.Task[None]) -> str | None:
    """After exit and the drain: an overflow seen late, or output a leftover kept open."""
    overflow = _overflow_reason(t_out, t_ev)
    if overflow is not None:
        return overflow
    if not (t_out.done() and t_ev.done()):
        return "output still open"
    return None


async def _read_capped(reader: asyncio.StreamReader, buf: bytearray, cap: int) -> None:
    while chunk := await reader.read(64 * 1024):
        if len(buf) + len(chunk) > cap:
            raise _Overflow
        buf += chunk


async def _read_tail(reader: asyncio.StreamReader, buf: bytearray, cap: int) -> None:
    while chunk := await reader.read(64 * 1024):
        buf += chunk
        if len(buf) > cap:
            del buf[: len(buf) - cap]


def _resolve(argv0: str) -> str | None:
    """argv[0] must be an absolute path to an executable file (no PATH lookup)."""
    if not os.path.isabs(argv0):
        return None
    return argv0 if os.path.isfile(argv0) and os.access(argv0, os.X_OK) else None


def parse_event(item: object) -> ExtEvent | None:
    """One ``{type, text, dedupe_key, data?}`` object, or None if it's malformed.

    ``type`` is a lowercase slug; ``dedupe_key`` is 1..200 chars; ``text`` is clipped to
    1000 chars; ``data`` is an object if present. Unknown keys make it malformed.
    """
    if not isinstance(item, dict):
        return None
    obj = cast(dict[str, object], item)
    if set(obj) - _EVENT_KEYS:
        return None
    kind, text, key, data = obj.get("type"), obj.get("text"), obj.get("dedupe_key"), obj.get("data")
    if not isinstance(kind, str) or not _EVENT_TYPE.fullmatch(kind):
        return None
    if not isinstance(text, str):
        return None
    if not isinstance(key, str) or not 0 < len(key) <= MAX_DEDUPE_KEY or "\x00" in key:
        return None
    if data is not None and not isinstance(data, dict):
        return None
    return ExtEvent(kind, text[:MAX_EVENT_TEXT], key, cast(dict[str, Any] | None, data))


# --- test double -------------------------------------------------------------------------


@dataclass
class FakeExtProcess:
    """A scripted :class:`ExtRunner`: records each request; answers from ``results`` in
    order (the last one repeating), or from ``respond`` when set. A result may be an
    exception to raise. Nothing runs."""

    results: list[ExtOutput | BaseException] = field(
        default_factory=list[ExtOutput | BaseException]
    )
    respond: Callable[[ExtRequest], ExtOutput | BaseException] | None = None
    calls: list[ExtRequest] = field(default_factory=list[ExtRequest])

    async def run(self, req: ExtRequest, *, redact: RedactFn) -> ExtOutput:
        self.calls.append(req)
        if self.respond is not None:
            result = self.respond(req)
        elif self.results:
            result = self.results.pop(0) if len(self.results) > 1 else self.results[0]
        else:
            result = ExtOutput({}, (), 0, 0)
        if isinstance(result, BaseException):
            raise result
        return result

    async def run_raw(self, req: ExtRequest, *, redact: RedactFn) -> RawResult:
        try:
            out = await self.run(req, redact=redact)
        except ExtFailed as e:
            return RawResult(1, "", e.stderr_tail, (), 0)
        return RawResult(0, redact(json.dumps(out.data)), "", (), out.duration_ms)


__all__ = [
    "EVENTS_FD",
    "SCRATCH_PATH",
    "SECRETS_FD",
    "ExtAmbiguous",
    "ExtCommand",
    "ExtEvent",
    "ExtFailed",
    "ExtOutput",
    "ExtProcess",
    "ExtRequest",
    "ExtRunner",
    "FakeExtProcess",
    "ProcessTracker",
    "RawResult",
    "RedactFn",
    "TemplateError",
    "ensure_data_dir",
    "parse_event",
    "scratch_env",
    "spawn_stdio",
]
