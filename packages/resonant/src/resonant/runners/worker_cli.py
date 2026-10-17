"""The worker CLI's wire: the ``resonant`` shim on a session's PATH and the per-run socket it
talks to (docs/employee-plan.md D3, EM-3).

Layout, all daemon-owned::

    <work_dir>/.ctl/            0700, every run's control dir; the thin guard denies writes here
    <work_dir>/.ctl/R4/         0700
    <work_dir>/.ctl/R4/sock     the run's unix socket (listening only while a session runs)
    <work_dir>/.ctl/R4/token    0600, a fresh random token per session
    <work_dir>/R4/bin/resonant  the shim (stdlib Python), first on the session's PATH

The control dir sits beside the run dir, not in it: the run dir is the session's own (it can
rename or replace anything there), and a socket it could swap would let it read another run's
token or answer another run's questions. The guard (``runner.sb``, ``ctl_dir``/``ctl_root``)
lets a session read only its own control dir, write none of them, and connect to no unix
socket under ``work_dir`` but its own run's. On top of that the daemon checks every caller:
the token (``hmac.compare_digest``), and the peer's pid (``LOCAL_PEERPID``), which must be the
run's live session leader or a descendant of it. A token copied out of a run is useless to
any other process.

The protocol is one JSON line each way. Request: ``{"token", "cmd", ...}`` with ``cmd`` one
of ``ask``/``note``/``check``/``restart``/``update`` (GD-1: restart Resonant, or pull its main,
sync and restart; the run is stopped by the restart and resumed after it). Reply:
``{"text", "exit"}``; the shim prints ``text`` (plain text for Claude to read) and exits with
``exit``:

    0  ok: the answer, the note recorded, or the check is ok
    1  the check was denied: don't do it
    2  usage error
    3  no answer yet: finish what you can, then end your turn; the run resumes with the answer
    4  Resonant can't be reached (or the run is over)
    5  too many pending checks: the check wasn't asked; wait for answers, then run it again
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import math
import os
import secrets
import socket
import stat
import struct
import sys
import tempfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

import psutil

log = logging.getLogger(__name__)

CTL_DIR = ".ctl"  # in work_dir
SOCK = "sock"
TOKEN = "token"  # noqa: S105 - a file name
SHIM = "resonant"
EXIT_OK, EXIT_DENIED, EXIT_USAGE, EXIT_PENDING, EXIT_UNREACHABLE = 0, 1, 2, 3, 4
EXIT_BUSY = 5  # too many checks wait for the owner: the check wasn't asked
MAX_REQUEST = 64 * 1024
MAX_QUESTION = 2000
MAX_NOTE = 2000
MAX_OPTIONS = 10
MAX_OPTION = 100
MAX_WHAT = 1000
MAX_NAME = 64
READ_TIMEOUT_S = 10.0
SUN_PATH_MAX = 100  # macOS sun_path is 104 bytes with the NUL; keep a margin
START_TOLERANCE_S = 1.0
NO_ANSWER_YET = (
    "No answer yet. Finish what you can, then end your turn with a short summary; "
    "you'll be resumed with the owner's answer."
)


# --- requests and replies ---------------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    question: str
    options: tuple[str, ...] = ()
    urgent: bool = False


@dataclass(frozen=True)
class Note:
    text: str
    urgent: bool = False


@dataclass(frozen=True)
class Check:
    system: str
    action: str
    what: str
    irreversible: bool = False
    cost_usd: float = 0.0


@dataclass(frozen=True)
class SelfOp:
    """``resonant restart`` or ``resonant update``: Resonant itself (GD-1)."""

    op: Literal["restart", "update"]


Request = Ask | Note | Check | SelfOp


@dataclass(frozen=True)
class Reply:
    """``on_sent`` runs once the reply is written: what it delivered is marked delivered
    only then (a caller that hung up gets it again on its next turn)."""

    text: str
    exit: int = EXIT_OK
    on_sent: Callable[[], None] | None = field(default=None, compare=False, repr=False)


class BadRequest(Exception):
    """A request the CLI should report as a usage error."""


def _text(obj: dict[str, Any], key: str, limit: int, *, required: bool = True) -> str:
    value = obj.get(key, "")
    if not isinstance(value, str):
        raise BadRequest(f"{key} must be text")
    value = value.strip()
    if required and not value:
        raise BadRequest(f"{key} is empty")
    if len(value) > limit:
        raise BadRequest(f"{key} is too long ({len(value)} characters, at most {limit})")
    return value


def _flag(obj: dict[str, Any], key: str) -> bool:
    value = obj.get(key, False)
    if not isinstance(value, bool):
        raise BadRequest(f"{key} must be true or false")
    return value


def parse_request(obj: Any) -> Request:
    """The request in ``obj`` (the decoded JSON line, token already checked)."""
    if not isinstance(obj, dict):
        raise BadRequest("not a request")
    req: dict[str, Any] = obj  # pyright: ignore[reportUnknownVariableType]
    cmd = req.get("cmd")
    if cmd == "ask":
        raw = req.get("options", [])
        if not isinstance(raw, list):
            raise BadRequest("options must be a list")
        options: list[str] = []
        for item in raw:  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(item, str) or not item.strip():
                raise BadRequest("each option must be non-empty text")
            if len(item) > MAX_OPTION:
                raise BadRequest(f"an option is too long (at most {MAX_OPTION} characters)")
            options.append(item.strip())
        if len(options) > MAX_OPTIONS:
            raise BadRequest(f"too many options (at most {MAX_OPTIONS})")
        return Ask(_text(req, "question", MAX_QUESTION), tuple(options), _flag(req, "urgent"))
    if cmd == "note":
        return Note(_text(req, "text", MAX_NOTE), _flag(req, "urgent"))
    if cmd == "check":
        cost = req.get("cost_usd", 0.0)
        if isinstance(cost, bool) or not isinstance(cost, (int, float)):
            raise BadRequest("cost must be a number")
        if not math.isfinite(cost) or cost < 0:
            raise BadRequest("cost must be 0 or more")
        return Check(
            _text(req, "system", MAX_NAME),
            _text(req, "action", MAX_NAME),
            _text(req, "what", MAX_WHAT),
            _flag(req, "irreversible"),
            float(cost),
        )
    if cmd == "restart" or cmd == "update":
        return SelfOp(cmd)
    raise BadRequest("unknown command; use ask, note, check, restart or update")


# --- the control dir --------------------------------------------------------------------------


def ctl_root(work_dir: Path) -> Path:
    return work_dir / CTL_DIR


def _private_dir(path: Path) -> None:
    """``path`` as a 0700 dir owned by this user, never through a link."""
    with contextlib.suppress(FileExistsError):
        path.mkdir(mode=0o700)
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
        raise OSError(f"{path} isn't a private directory")
    os.chmod(path, 0o700, follow_symlinks=False)


def make_ctl_dir(work_dir: Path, code: str) -> Path:
    """``<work_dir>/.ctl/<code>``, 0700. Daemon-side; the guard keeps sessions out of it."""
    work_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    root = ctl_root(work_dir)
    _private_dir(root)
    ctl = root / code
    _private_dir(ctl)
    return ctl


def write_token(ctl: Path) -> str:
    """A fresh token in ``<ctl>/token`` (0600, replaced by rename, never through a link)."""
    token = secrets.token_urlsafe(32)
    fd, tmp = tempfile.mkstemp(dir=ctl, prefix=".token-")
    try:
        os.write(fd, token.encode())
        os.fchmod(fd, 0o600)
    finally:
        os.close(fd)
    os.replace(tmp, ctl / TOKEN)
    return token


def remove_ctl(work_dir: Path, code: str) -> None:
    """Remove a run's control dir (socket and token). Safe to repeat; never follows a link."""
    ctl = ctl_root(work_dir) / code
    try:
        st = os.lstat(ctl)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(ctl)
        return
    for name in os.listdir(ctl):
        with contextlib.suppress(FileNotFoundError):
            os.unlink(ctl / name)
    with contextlib.suppress(OSError):
        os.rmdir(ctl)


# --- the shim ---------------------------------------------------------------------------------

_SHIM_SOURCE = r'''
"""resonant: reach the owner from a Resonant worker session.

  resonant ask "<question>" [--options a,b,c] [--urgent]
      Ask the owner something only they can decide. Waits for the answer (up to about
      8 minutes); if there is none yet, finish what you can and end your turn: the
      session is resumed with the answer.
  resonant note "<progress>" [--urgent]
      Report meaningful progress. --urgent only for what the owner must see now.
  resonant check --system S --action A [--irreversible] [--cost USD] "<what>"
      Before anything that could be net new: ok (go ahead) or denied (don't).
  resonant restart
      Restart Resonant itself. This session is stopped by the restart and resumed after it.
  resonant update
      Deploy Resonant: pull main in its checkout, sync, restart (as /update does).

Exit codes: 0 ok (answered, noted, check ok); 1 check denied; 2 usage error;
3 no answer yet (end your turn, you'll be resumed); 4 Resonant can't be reached.
"""
import argparse
import json
import os
import socket
import sys

CTL = @CTL@
WAIT_S = @WAIT_S@


def main():
    p = argparse.ArgumentParser(prog="resonant", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    a = sub.add_parser("ask", help="ask the owner and wait for the answer")
    a.add_argument("question", nargs="+")
    a.add_argument("--options", default="", help="comma-separated choices")
    a.add_argument("--urgent", action="store_true")
    n = sub.add_parser("note", help="report progress")
    n.add_argument("text", nargs="+")
    n.add_argument("--urgent", action="store_true")
    c = sub.add_parser("check", help="check an action against the trust rules")
    c.add_argument("--system", required=True)
    c.add_argument("--action", required=True)
    c.add_argument("--irreversible", action="store_true")
    c.add_argument("--cost", type=float, default=0.0)
    c.add_argument("what", nargs="+")
    sub.add_parser("restart", help="restart Resonant (this session is resumed after it)")
    sub.add_parser("update", help="pull Resonant's main, sync and restart")
    args = p.parse_args()
    if args.cmd is None:
        p.print_help()
        return 2
    if args.cmd == "ask":
        opts = [o.strip() for o in args.options.split(",") if o.strip()]
        req = {"cmd": "ask", "question": " ".join(args.question), "options": opts,
               "urgent": args.urgent}
    elif args.cmd == "note":
        req = {"cmd": "note", "text": " ".join(args.text), "urgent": args.urgent}
    elif args.cmd in ("restart", "update"):
        req = {"cmd": args.cmd}
    else:
        req = {"cmd": "check", "system": args.system, "action": args.action,
               "irreversible": args.irreversible, "cost_usd": args.cost,
               "what": " ".join(args.what)}
    try:
        with open(os.path.join(CTL, "token")) as f:
            req["token"] = f.read().strip()
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(WAIT_S + 60)
        path = os.path.join(CTL, "sock")
        if len(path.encode()) > 100:  # too long for sun_path: connect from inside the dir
            os.chdir(CTL)
            path = "sock"
        s.connect(path)
        s.sendall(json.dumps(req).encode() + b"\n")
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = s.recv(65536)
            if not chunk:
                break
            buf += chunk
            if len(buf) > 1 << 20:
                break
        s.close()
        reply = json.loads(buf.decode())
    except (OSError, ValueError) as e:
        print("Resonant can't be reached from this session (%s). Carry on without it and "
              "say so in your summary." % (getattr(e, "strerror", None) or e))
        return 4
    print(reply.get("text", ""))
    code = reply.get("exit", 4)
    return code if isinstance(code, int) else 4


sys.exit(main())
'''


def shim_interpreter() -> str:
    """A Python for the shim that a session may run: the daemon's base interpreter (its
    install is write-protected by the guard but not exec-denied), never the venv (which is).
    """
    denied = Path(os.path.realpath(sys.prefix))  # sandbox.default_deny_exec
    base: str = getattr(sys, "_base_executable", None) or sys.executable
    if os.path.isabs(base) and os.access(base, os.X_OK):
        real = Path(os.path.realpath(base))
        if not real.is_relative_to(denied):
            return str(real)
    return "/usr/bin/python3"


def shim_text(ctl: Path, wait_s: float, interpreter: str | None = None) -> str:
    python = interpreter or shim_interpreter()
    if any(c.isspace() for c in python):
        raise OSError(f"the shim's interpreter path has a space: {python!r}")
    body = _SHIM_SOURCE.replace("@CTL@", repr(str(ctl))).replace("@WAIT_S@", repr(float(wait_s)))
    return f"#!{python} -IS\n{body.lstrip()}"


def write_shim(bin_dir: Path, ctl: Path, wait_s: float, interpreter: str | None = None) -> Path:
    """``<bin_dir>/resonant`` (0700), replaced by rename so a planted link is never followed."""
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=bin_dir, prefix=".resonant-")
    try:
        os.write(fd, shim_text(ctl, wait_s, interpreter).encode())
        os.fchmod(fd, 0o700)
    finally:
        os.close(fd)
    path = bin_dir / SHIM
    os.replace(tmp, path)
    return path


# --- who is calling ---------------------------------------------------------------------------


def peer_pid(sock: Any) -> int | None:
    """The connecting process's pid (``LOCAL_PEERPID`` on macOS, ``SO_PEERCRED`` on Linux)."""
    try:
        if sys.platform == "darwin":
            return int(sock.getsockopt(0, 0x002))  # SOL_LOCAL, LOCAL_PEERPID
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        return int(struct.unpack("3i", creds)[0])
    except (OSError, AttributeError, ValueError):
        return None


def descends_from(pid: int, leader_pid: int, leader_started: float | None) -> bool:
    """Whether ``pid`` is the leader (the same process: its start time matches) or one of
    its descendants."""
    try:
        proc = psutil.Process(pid)
        chain = [proc, *proc.parents()]
    except psutil.Error:
        return False
    for p in chain:
        if p.pid != leader_pid:
            continue
        if leader_started is None:
            return False
        try:
            return abs(p.create_time() - leader_started) <= START_TOLERANCE_S
        except psutil.Error:
            return False
    return False


# --- the server -------------------------------------------------------------------------------

Handler = Callable[[Request, "asyncio.Future[None]"], Awaitable[Reply | None]]


class WorkerServer:
    """One run's socket, for the length of one session.

    ``handle(request, gone)`` answers a request; ``gone`` completes when the caller hangs up
    (a Bash tool timeout), so a long wait can stop early. A handler that returns None sends
    nothing. ``peer_ok(pid)`` decides whether the connecting process may speak for the run.
    """

    def __init__(
        self,
        ctl: Path,
        token: str,
        handle: Handler,
        peer_ok: Callable[[int | None], bool],
        *,
        read_timeout_s: float = READ_TIMEOUT_S,
    ) -> None:
        self.ctl = ctl
        self.path = ctl / SOCK
        self._token = token.encode()
        self._handle = handle
        self._peer_ok = peer_ok
        self.read_timeout_s = read_timeout_s
        self._server: asyncio.AbstractServer | None = None
        self._clients: set[asyncio.Task[Any]] = set()

    async def start(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)
        if len(str(self.path).encode()) <= SUN_PATH_MAX:
            self._server = await asyncio.start_unix_server(
                self._client, path=str(self.path), limit=MAX_REQUEST
            )
        else:  # bound at a short private path and renamed in (same filesystem)
            short_dir = tempfile.mkdtemp(prefix="rs-", dir="/tmp")
            try:
                short = os.path.join(short_dir, "s")
                self._server = await asyncio.start_unix_server(
                    self._client, path=short, limit=MAX_REQUEST
                )
                os.rename(short, self.path)
            finally:
                with contextlib.suppress(OSError):
                    os.rmdir(short_dir)
        os.chmod(self.path, 0o600)

    async def close(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
        for t in list(self._clients):
            t.cancel()
        if self._clients:
            await asyncio.wait(list(self._clients), timeout=5)
        if server is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), 5)
        with contextlib.suppress(FileNotFoundError):
            os.unlink(self.path)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._clients.add(task)
        gone: asyncio.Future[None] | None = None
        try:
            pid = peer_pid(writer.get_extra_info("socket"))
            reply = await self._serve(reader, pid)
            if isinstance(reply, tuple):
                req, gone = reply
                result = await self._handle(req, gone)
                if result is None:
                    return
                reply = result
            writer.write(json.dumps({"text": reply.text, "exit": reply.exit}).encode() + b"\n")
            await writer.drain()
            if reply.on_sent is not None:
                try:
                    reply.on_sent()
                except Exception:  # the reply is out: it may be delivered again, never lost
                    log.exception("worker CLI: marking a delivery failed")
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception:
            log.exception("worker CLI request failed")
            with contextlib.suppress(Exception):
                failed = {"text": "Resonant hit an error.", "exit": EXIT_UNREACHABLE}
                writer.write(json.dumps(failed).encode() + b"\n")
                await writer.drain()
        finally:
            if gone is not None and not gone.done():
                gone.cancel()
            with contextlib.suppress(Exception):
                writer.close()
            if task is not None:
                self._clients.discard(task)

    async def _serve(
        self, reader: asyncio.StreamReader, pid: int | None
    ) -> Reply | tuple[Request, asyncio.Future[None]]:
        try:
            line = await asyncio.wait_for(reader.readline(), self.read_timeout_s)
        except (TimeoutError, ValueError, asyncio.LimitOverrunError):
            return Reply("The request was too slow or too long.", EXIT_USAGE)
        try:
            obj: Any = json.loads(line)
        except ValueError:
            return Reply("The request wasn't understood.", EXIT_USAGE)
        token: object = None
        if isinstance(obj, dict):
            token = cast(dict[str, Any], obj).get("token")
        if not isinstance(token, str) or not hmac.compare_digest(token.encode(), self._token):
            log.warning("worker CLI: bad token", extra={"ctl": str(self.ctl), "pid": pid})
            return Reply("Not allowed.", EXIT_UNREACHABLE)
        if not self._peer_ok(pid):
            log.warning("worker CLI: caller isn't the run's session", extra={"pid": pid})
            return Reply("Not allowed.", EXIT_UNREACHABLE)
        try:
            req = parse_request(obj)
        except BadRequest as e:
            return Reply(f"Usage error: {e}.", EXIT_USAGE)
        gone: asyncio.Future[None] = asyncio.ensure_future(_hangup(reader))
        return req, gone


async def _hangup(reader: asyncio.StreamReader) -> None:
    """Completes when the caller closes its end (it sends nothing after its request)."""
    with contextlib.suppress(Exception):
        while await reader.read(4096):
            pass
