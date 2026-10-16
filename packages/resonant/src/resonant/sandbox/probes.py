"""Probes that prove the thin guard holds, and the cheap per-run self-check.

Every probe runs a small Python snippet (or a real tool) *inside* the sandbox, built the way
a session's is, and compares what it saw with what the profile promises. Where a denial
could also be a missing file or a dead service, a control run outside the sandbox first
proves the thing is reachable, so a pass means "denied", not "absent". The guard is
allow-default, so the probes check both sides: each deny holds, and the owner-like allows
(reading a planted ``.env`` under ``$HOME``, writing beside it, sockets, HTTPS) still work.

Two scopes, by where they can run:

- ``ci``: file, exec, network and mach behaviour that is the same on any macOS host. These
  run in CI on the GitHub macOS runners.
- ``local``: probes that need the Mini (the real ``$HOME``'s paths, the Keychain, launchd,
  the HID drivers, the display, the LAN address, a Docker socket, ``claude`` itself).

The full deny battery (appends, renames, hardlinks, symlinks, ancestor renames) runs against
a fake home under ``/private/var/tmp``. On the real paths, probes only open existing files for
writing without writing, create uniquely named entries (removed again if one appears: that
is a fail) and read, so a working guard leaves the owner's files untouched.
"""

from __future__ import annotations

import contextlib
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Generator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import psutil

from resonant.sandbox.profile import (
    API_PORT,
    SandboxError,
    SandboxParams,
    default_read_paths,
    link_tools,
    macos_build,
    prepare_dirs,
    profile_hash,
    resolve_tool,
    sandbox_exec_available,
    scratch_env,
    wrap,
)

Status = Literal["pass", "fail", "skip", "info"]
Scope = Literal["ci", "local"]

DENIED_ERRNOS = frozenset({1, 13})  # EPERM, EACCES
BOOTSTRAP_NOT_PRIVILEGED = 1100  # kr from bootstrap_look_up when the sandbox says no
SELF_CHECK_TTL_S = 3600.0
SNIPPET_TIMEOUT_S = 30.0
NET_TIMEOUT_S = 60.0
CLAUDE_FAIL_FAST_S = 5.0

DENIED_EXEC = ("/bin/launchctl", "/usr/bin/crontab", "/usr/bin/at")
# Looked up by name: the sandbox answers 1100 whether or not the service exists.
DENIED_MACH = (
    f"com.apple.xpc.launchd.domain.user.{os.getuid()}",
    "com.apple.SecurityServer",
    "com.apple.securityd",
    "com.apple.securityd.xpc",
    "com.apple.security.agent",
    "com.apple.imagent",
    "com.apple.imagent.desktop.auth",
    "com.apple.imdpersistence.IMDPersistenceAgent",
    "com.apple.iChat",
    "com.apple.MobileSMS",
    "com.apple.coreservices.appleevents",
    "com.apple.coreservices.appleevent-send",
    "com.apple.coreservices.launchservicesd",
    "com.apple.lsd.open",
    "com.apple.windowserver.active",
    "com.apple.hidsystem.server",
    "com.apple.accessibility.AXVisualSupport",
    "com.apple.universalaccessd",
    "com.apple.tccd",
)
# Kept on purpose: TLS for Go and Apple git, and DNS.
ALLOWED_MACH = ("com.apple.trustd", "com.apple.dnssd.service")
DENIED_OPS = ("appleevent-send", "lsopen", "mach-priv-task-port", "mach-task-name", "signal")
# Scratch dirs (fake homes, planted files) go here: outside $HOME, writable by everyone.
OUTSIDE_PARENT = "/private/var/tmp"
# Where a Docker daemon's socket usually sits (Docker Desktop, colima, OrbStack).
DOCKER_SOCKETS = (
    "/var/run/docker.sock",
    "~/.docker/run/docker.sock",
    "~/.colima/default/docker.sock",
    "~/.orbstack/run/docker.sock",
)
HTTPS_URL = "https://github.com"
GIT_REMOTE = "https://github.com/git/git"
MAX_SOCKET_PATH = 104


@dataclass(frozen=True)
class ProbeResult:
    name: str
    scope: Scope
    status: Status
    detail: str = ""
    ms: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope,
            "status": self.status,
            "detail": self.detail,
            "ms": self.ms,
        }


@dataclass
class ProbeContext:
    """A live sandbox setup: params, the interpreter, and things outside it to probe."""

    params: SandboxParams
    python: str
    env: dict[str, str]
    canary: Path  # a dir under the real $HOME, outside the run: a session may read and write it
    outside_pid: int  # a process outside the sandbox
    lan_ip: str | None
    claude: Path | None
    daemon_pid: int | None = None
    # a scratch dir outside $HOME holding a subdir that params.deny_read covers
    outside: Path | None = None
    cleanup: list[Path] = field(default_factory=lambda: [])

    def sandboxed(
        self,
        argv: Sequence[str],
        *,
        timeout: float = SNIPPET_TIMEOUT_S,
        env: dict[str, str] | None = None,
        params: SandboxParams | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run argv inside the sandbox (``params``, default the run's) from the run's repo
        dir."""
        return subprocess.run(  # noqa: S603 - argv is built here
            wrap(argv, params or self.params),
            cwd=self.params.run_dir / "repo",
            env=self.env if env is None else env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    def snippet(
        self,
        code: str,
        arg: object = None,
        *,
        bare: bool = False,
        params: SandboxParams | None = None,
    ) -> dict[str, Any]:
        """Run ``code`` (which prints one JSON object) in the sandbox, or outside it."""
        argv = [self.python, "-I", "-c", _PRELUDE + code, json.dumps(arg)]
        if bare:
            proc = subprocess.run(  # noqa: S603 - argv is built here
                argv, capture_output=True, text=True, timeout=SNIPPET_TIMEOUT_S, check=False
            )
        else:
            proc = self.sandboxed(argv, params=params)
        lines = proc.stdout.strip().splitlines()
        if proc.returncode != 0 or not lines:
            tail = (proc.stderr.strip().splitlines() or [""])[-1][:200]
            raise ProbeError(f"snippet failed (rc={proc.returncode}): {tail}")
        data: dict[str, Any] = json.loads(lines[-1])
        return data


class ProbeError(Exception):
    """The probe itself could not run (distinct from the sandbox failing a check)."""


# Runs in both worlds. ``tryit`` turns an OSError into "errno:N".
_PRELUDE = """
import ctypes, ctypes.util, json, os, socket, sys
A = json.loads(sys.argv[1])
def tryit(f):
    try:
        f()
        return "ok"
    except OSError as e:
        return "errno:%d" % e.errno
def out(**kw):
    print(json.dumps(kw), flush=True)
"""

# File operations, each [kind, path, other]. "ok" means it worked; anything that changed a
# file is undone right away where the snippet still can (the host removes the rest).
_FS = """
def create(p, other):
    os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    os.unlink(p)
def openw(p, other):
    # opened for writing, nothing written: the open is what the sandbox judges
    os.close(os.open(p, os.O_WRONLY | os.O_APPEND))
def mkdir(p, other):
    os.mkdir(p)
    os.rmdir(p)
def rename(p, other):
    os.rename(p, other)
    os.rename(other, p)
def rename_over(p, other):
    # put the file `other` (made by the worker) in place of `p`
    os.rename(other, p)
def link(p, other):
    os.link(p, other)
    try:
        open(other, "rb").read(1)
        os.close(os.open(other, os.O_WRONLY | os.O_APPEND))
    finally:
        os.unlink(other)
def symlink_write(p, other):
    os.symlink(p, other)
    try:
        os.close(os.open(other, os.O_WRONLY | os.O_APPEND))
    finally:
        os.unlink(other)
OPS = {
    "read": lambda p, o: open(p, "rb").read(1),
    "list": lambda p, o: os.listdir(p),
    "stat": lambda p, o: os.stat(p),
    "create": create, "openw": openw, "mkdir": mkdir, "rename": rename,
    "rename_over": rename_over, "link": link, "symlink_write": symlink_write,
}
res = {}
for kind, p, other in A:
    res["%s %s" % (kind, p)] = tryit(lambda kind=kind, p=p, other=other: OPS[kind](p, other))
out(res=res)
"""

_NET = """
def conn(fam, addr):
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(3)
    try:
        s.connect(addr)
    finally:
        s.close()
def bind(addr):
    s = socket.socket()
    try:
        s.bind(addr)
    finally:
        s.close()
res = {}
for host, port in A.get("connect", []):
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    res["connect %s:%d" % (host, port)] = tryit(lambda h=host, p=port, f=fam: conn(f, (h, p)))
for host, port in A.get("bind", []):
    res["bind %s:%d" % (host, port)] = tryit(lambda h=host, p=port: bind((h, p)))
for path in A.get("unix", []):
    res["unix %s" % path] = tryit(lambda p=path: conn(socket.AF_UNIX, p))
out(res=res)
"""

_MACH = """
libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
libc.bootstrap_look_up.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)]
bs = ctypes.c_uint.in_dll(libc, "bootstrap_port")
res = {}
for name in A:
    port = ctypes.c_uint(0)
    res[name] = libc.bootstrap_look_up(bs, name.encode(), ctypes.byref(port))
out(res=res)
"""

_SBCHECK = """
lib = ctypes.CDLL("/usr/lib/system/libsystem_sandbox.dylib")
lib.sandbox_check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
res = {op: lib.sandbox_check(os.getpid(), op.encode(), 0) for op in A}
out(res=res)
"""

_TASK = """
libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
libc.mach_task_self.restype = ctypes.c_uint
me = libc.mach_task_self()
t = ctypes.c_uint(0)
libc.task_name_for_pid.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
libc.task_for_pid.argtypes = [ctypes.c_uint, ctypes.c_int, ctypes.POINTER(ctypes.c_uint)]
res = {}
for pid in A:
    res["name %d" % pid] = libc.task_name_for_pid(me, pid, ctypes.byref(t))
    res["port %d" % pid] = libc.task_for_pid(me, pid, ctypes.byref(t))
res["self"] = libc.task_name_for_pid(me, os.getpid(), ctypes.byref(t))
out(res=res)
"""

_PROCARGS = """
libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
mib = (ctypes.c_int * 3)(1, 49, A["pid"])  # CTL_KERN, KERN_PROCARGS2, pid
size = ctypes.c_size_t(65536)
buf = ctypes.create_string_buffer(65536)
r = libc.sysctl(mib, 3, buf, ctypes.byref(size), None, 0)
if r != 0:
    out(res="errno:%d" % ctypes.get_errno())
else:
    out(res="found" if A["marker"].encode() in buf.raw[: size.value] else "empty")
"""

_SIGNAL = """
import subprocess, signal
res = {"outside": tryit(lambda: os.kill(A["pid"], 0))}
child = subprocess.Popen(["/bin/sleep", "30"], start_new_session=True)
res["child_signal"] = tryit(lambda: os.kill(child.pid, 0))
res["child_kill"] = tryit(child.kill)
child.wait()
out(res=res)
"""

_EXEC = """
import subprocess
res = {}
for p in A:
    try:
        subprocess.run([p, "--help"], capture_output=True, timeout=5)
        res[p] = "ran"
    except OSError as e:
        res[p] = "errno:%d" % e.errno
    except subprocess.TimeoutExpired:
        res[p] = "ran"
out(res=res)
"""

_ENV = """
out(env=dict(os.environ), cwd=os.getcwd(), real=os.path.realpath("."))
"""

# A child that starts a new session still carries the sandbox.
_SETSID = """
import subprocess
code = ("import os\\ntry:\\n open(%r,'rb').read(1); r='READ'\\n"
        "except OSError as e: r='errno:%%d'%%e.errno\\nprint(r)") % A["path"]
p = subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True,
                   start_new_session=True)
out(res=p.stdout.strip())
"""

_KEYCHAIN = """
sec = ctypes.CDLL(ctypes.util.find_library("Security"))
sec.SecKeychainCopySearchList.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
r = ctypes.c_void_p()
out(res=sec.SecKeychainCopySearchList(ctypes.byref(r)))
"""

# Submits a harmless job (/usr/bin/true) through liblaunch, the way a Python launchd client
# would. errno 1 = refused, 0 = launchd accepted it (a hole).
_LAUNCHD = """
lib = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
P = ctypes.c_void_p
try:
    for n, a, r in [("launch_data_alloc", [ctypes.c_int], P),
                    ("launch_data_new_string", [ctypes.c_char_p], P),
                    ("launch_data_dict_insert", [P, P, ctypes.c_char_p], None),
                    ("launch_data_array_set_index", [P, P, ctypes.c_size_t], None),
                    ("launch_msg", [P], P),
                    ("launch_data_get_type", [P], ctypes.c_int),
                    ("launch_data_get_errno", [P], ctypes.c_int)]:
        f = getattr(lib, n)
        f.argtypes, f.restype = a, r
except AttributeError:
    out(res="unavailable")
    sys.exit(0)
job = lib.launch_data_alloc(1)
lib.launch_data_dict_insert(job, lib.launch_data_new_string(A["label"].encode()), b"Label")
args = lib.launch_data_alloc(2)
lib.launch_data_array_set_index(args, lib.launch_data_new_string(b"/usr/bin/true"), 0)
lib.launch_data_dict_insert(job, args, b"ProgramArguments")
msg = lib.launch_data_alloc(1)
lib.launch_data_dict_insert(msg, job, b"SubmitJob")
resp = lib.launch_msg(msg)
if not resp:
    out(res="null")
elif lib.launch_data_get_type(resp) == 9:
    out(res="errno:%d" % lib.launch_data_get_errno(resp))
else:
    out(res="type:%d" % lib.launch_data_get_type(resp))
"""

# The classic synthetic-input route: open the IOHIDSystem user client.
_HID = """
io = ctypes.CDLL(ctypes.util.find_library("IOKit"))
libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
io.IOServiceMatching.restype = ctypes.c_void_p
io.IOServiceMatching.argtypes = [ctypes.c_char_p]
io.IOServiceGetMatchingService.restype = ctypes.c_uint
io.IOServiceGetMatchingService.argtypes = [ctypes.c_uint, ctypes.c_void_p]
io.IOServiceOpen.restype = ctypes.c_int
io.IOServiceOpen.argtypes = [ctypes.c_uint, ctypes.c_uint, ctypes.c_uint32,
                             ctypes.POINTER(ctypes.c_uint)]
io.IOServiceClose.argtypes = [ctypes.c_uint]
libc.mach_task_self.restype = ctypes.c_uint
svc = io.IOServiceGetMatchingService(0, io.IOServiceMatching(b"IOHIDSystem"))
res = {}
for ctype in (1, 2, 3):
    conn = ctypes.c_uint(0)
    kr = io.IOServiceOpen(svc, libc.mach_task_self(), ctype, ctypes.byref(conn))
    res[str(ctype)] = kr & 0xFFFFFFFF
    if kr == 0:
        io.IOServiceClose(conn)
out(res=res)
"""

_DISPLAY = """
cg = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
cg.CGMainDisplayID.restype = ctypes.c_uint32
cg.CGDisplayCreateImage.restype = ctypes.c_void_p
cg.CGDisplayCreateImage.argtypes = [ctypes.c_uint32]
out(res="image" if cg.CGDisplayCreateImage(cg.CGMainDisplayID()) else "none")
"""

# The per-run self-check: a write into ~/.resonant (a unique new name; ~/.resonant itself
# when it doesn't exist) and into the daemon's install, a Keychain lookup and the API port.
# A write that worked is undone.
_SELF = """
import uuid
res = {}
state = os.path.join(A["home"], ".resonant")
if os.path.isdir(state):
    p = os.path.join(state, ".sandbox-self-check-" + uuid.uuid4().hex[:10])
    def w():
        os.close(os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        os.unlink(p)
else:
    def w():
        os.mkdir(state)
        os.rmdir(state)
res["write"] = tryit(w)
if A.get("protect"):
    q = os.path.join(A["protect"], ".sandbox-self-check-" + uuid.uuid4().hex[:10])
    def wp():
        os.close(os.open(q, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        os.unlink(q)
    res["protect"] = tryit(wp)
libc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
libc.bootstrap_look_up.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)]
bs = ctypes.c_uint.in_dll(libc, "bootstrap_port")
port = ctypes.c_uint(0)
res["mach"] = libc.bootstrap_look_up(bs, b"com.apple.securityd.xpc", ctypes.byref(port))
s = socket.socket()
s.settimeout(3)
res["net"] = tryit(lambda: s.connect(("127.0.0.1", A["port"])))
out(res=res)
"""


# --- helpers ---------------------------------------------------------------------------------


def _denied(result: object) -> bool:
    return (
        isinstance(result, str)
        and result.startswith("errno:")
        and (int(result.split(":")[1]) in DENIED_ERRNOS)
    )


def _fmt(results: Mapping[str, Any], names: Sequence[str] | None = None) -> str:
    keys = names or list(results)
    return ", ".join(f"{k}={results[k]}" for k in keys)


def _lan_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 9))  # TEST-NET: UDP connect sends nothing
        ip: str = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip  # noqa: S104


def _daemon_pid() -> int | None:
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmd: list[str] = proc.info["cmdline"] or []
        if "resonant" in " ".join(cmd) and cmd[-2:] == ["daemon", "run"]:
            return int(proc.info["pid"])
    return None


def _interpreter() -> Path:
    return Path(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))


def exec_candidates(deny_exec: Sequence[Path]) -> list[Path]:
    """Executables under the denied-exec paths worth trying: the paths themselves if files,
    or a ``resonant`` entry point inside them."""
    found: list[Path] = []
    for p in deny_exec:
        for cand in (p, p / "resonant", p / "bin" / "resonant"):
            if cand.is_file() and os.access(cand, os.X_OK):
                found.append(cand)
                break
    return found


@contextlib.contextmanager
def probe_context(
    *,
    secrets_dir: Path,
    work_dir: Path | None = None,
    cache_dir: Path | None = None,
    home: Path | None = None,
    deny_exec: Sequence[Path] = (),
    read_paths: Sequence[Path] | None = None,
    deny_read: Sequence[Path] = (),
    protect: Sequence[Path] | None = None,
) -> Generator[ProbeContext]:
    """Build a throwaway run dir (a direct child of ``work_dir``, like a real run's) using the
    configured cache and secrets dirs, plus a canary dir under ``$HOME``, a scratch dir
    outside it and a process outside the sandbox. Removes what it made, including a
    ``work_dir`` or cache it had to create (if empty)."""
    if not sandbox_exec_available():
        raise SandboxError("sandbox-exec is not available on this host")
    home = home or Path.home()
    work_dir = work_dir or home / "resonant-runs"
    cache_dir = cache_dir or work_dir / ".cache"
    made: list[Path] = []
    for d in reversed([cache_dir, *cache_dir.parents, work_dir, *work_dir.parents]):
        if not d.exists() and d not in made:
            d.mkdir(mode=0o700)
            made.append(d)
    tag = uuid.uuid4().hex[:8]
    run = work_dir / f".check-{tag}"
    scratch: list[Path] = [run]  # removed on the way out, however far setup got
    sleeper: subprocess.Popen[bytes] | None = None
    reads = default_read_paths() if read_paths is None else tuple(read_paths)
    try:
        canary = Path(tempfile.mkdtemp(prefix=".resonant-probe-", dir=home))
        scratch.append(canary)
        outside = Path(
            os.path.realpath(tempfile.mkdtemp(prefix="resonant-probe-", dir=OUTSIDE_PARENT))
        )
        scratch.append(outside)
        sleeper = subprocess.Popen(["/bin/sleep", "300"], start_new_session=True)
        params = SandboxParams(
            run_dir=run,
            work_dir=work_dir,
            cache_dir=cache_dir,
            secrets_dir=secrets_dir,
            home=home,
            read_paths=tuple(reads),
            deny_exec=tuple(deny_exec),
            deny_read=(*deny_read, outside / "denied"),
            protect=None if protect is None else tuple(protect),
        ).resolved()
        (outside / "denied").mkdir()
        (outside / "denied" / "x.txt").write_text("secret")
        prepare_dirs(params)
        claude = resolve_tool("claude")
        if claude is not None:
            with contextlib.suppress(SandboxError):
                link_tools(params, {"claude": claude})
        yield ProbeContext(
            params=params,
            python=str(_interpreter()),
            env=scratch_env(params),
            canary=canary,
            outside_pid=sleeper.pid,
            lan_ip=_lan_ip(),
            claude=claude,
            daemon_pid=_daemon_pid(),
            outside=outside,
        )
    finally:
        if sleeper is not None:
            sleeper.kill()
            sleeper.wait()
        for d in scratch:
            shutil.rmtree(d, ignore_errors=True)
        for sub in ("uv", "npm", "pip"):  # the cache subdirs prepare_dirs made, if empty
            with contextlib.suppress(OSError):
                (cache_dir / sub).rmdir()
        for d in reversed(made):
            with contextlib.suppress(OSError):
                d.rmdir()


# --- the probes ------------------------------------------------------------------------------

Op = tuple[str, str, str | None]


def _fs(
    ctx: ProbeContext, ops: Sequence[Op], params: SandboxParams | None = None
) -> dict[str, Any]:
    res: dict[str, Any] = ctx.snippet(_FS, [list(op) for op in ops], params=params)["res"]
    for kind, path, _ in ops:  # a create that stuck is a hole: remove it from outside
        if kind in {"create", "mkdir"} and os.path.lexists(path):
            if os.path.isdir(path) and not os.path.islink(path):
                os.rmdir(path)
            else:
                os.unlink(path)
            res[f"{kind} {path}"] = "WROTE"
    return res


def _unique(parent: Path, tag: str) -> str:
    return str(parent / f".zz-sandbox-probe-{tag}-{uuid.uuid4().hex[:10]}")


def _short(results: Mapping[str, Any], home: Path) -> dict[str, Any]:
    return {k.replace(str(home), "~"): v for k, v in results.items()}


def _verdict(
    denied: Mapping[str, Any], allowed: Mapping[str, Any], home: Path
) -> tuple[Status, str]:
    holes = {k: v for k, v in denied.items() if not _denied(v)}
    blocked = {k: v for k, v in allowed.items() if v != "ok"}
    if holes:
        return "fail", f"not denied: {_fmt(_short(holes, home))}"
    if blocked:
        return "fail", f"owner-like access blocked: {_fmt(_short(blocked, home))}"
    return "pass", f"{len(denied)} denied, {len(allowed)} allowed"


@contextlib.contextmanager
def _fake_home(ctx: ProbeContext) -> Generator[tuple[SandboxParams, Path]]:
    """A fake home under /private/var/tmp with Resonant's state (``~/.resonant`` a symlink to
    ``root/state``), a fake daemon install, the persistence files (``~/.zshrc`` a symlink into
    ``root/dotfiles``), Messages and Keychain files and an owner's code with a ``.env``, plus
    a sandbox built for it the way a session's is. Yields (params, root)."""
    root = Path(
        os.path.realpath(tempfile.mkdtemp(prefix="resonant-probe-home-", dir=OUTSIDE_PARENT))
    )
    try:
        home = root / "home"
        files = {
            ".resonant/resonant.db": "db",
            ".resonant/config.yaml": "cfg",
            ".resonant/logs/daemon.log": "log",
            ".resonant/run-secrets/token": "ghs_fake",
            "daemon/venv/bin/python": "py",
            "daemon/checkout/pyproject.toml": "toml",
            "Library/LaunchAgents/com.resonant.daemon.plist": "plist",
            "Library/Messages/chat.db": "chat",
            "Library/Keychains/login.keychain-db": "kc",
            ".zshrc": "rc",
            ".gitconfig": "git",
            ".config/git/config": "git",
            ".claude/settings.json": "{}",
            ".ssh/config": "ssh",
            ".env": "OWNER=1",
            "code/proj/.env": "KEY=1",
            "code/proj/notes.txt": "notes",
            ".local/bin/owner-tool": "tool",
            "brew/Cellar/gh/1.0/bin/gh": "gh",
        }
        # ~/.resonant and ~/.zshrc are symlinks (a moved state dir, a dotfiles repo): the
        # guard must hold on the real files too
        state = root / "state"
        state.mkdir()
        home.mkdir()
        (home / ".resonant").symlink_to(state)
        for rel, text in files.items():
            (home / rel).parent.mkdir(parents=True, exist_ok=True)
            if rel == ".zshrc":
                (root / "dotfiles").mkdir()
                (root / "dotfiles" / "zshrc").write_text(text)
                (home / rel).symlink_to(root / "dotfiles" / "zshrc")
                continue
            (home / rel).write_text(text)
        (home / ".resonant" / "mnt").mkdir()
        # a Homebrew-like PATH dir: open to workers, except the daemon's own tool in it
        (home / "brew" / "bin").mkdir()
        (home / "brew" / "bin" / "gh").symlink_to(
            home / "brew" / "Cellar" / "gh" / "1.0" / "bin" / "gh"
        )
        (root / "denied").mkdir()
        (root / "denied" / "x.txt").write_text("secret")
        (root / "open").mkdir()
        params = SandboxParams(
            run_dir=root / "run",
            cache_dir=root / "cache",
            secrets_dir=home / ".resonant" / "run-secrets",
            home=home,
            deny_read=(root / "denied",),
            protect=(
                home / "daemon" / "venv",
                home / "daemon" / "checkout",
                home / "brew" / "bin" / "gh",
                home / "brew" / "Cellar" / "gh" / "1.0",
            ),
            deny_exec=ctx.params.deny_exec,
        ).resolved()
        prepare_dirs(params)
        yield params, root
    finally:
        shutil.rmtree(root, ignore_errors=True)


def p_state_writes(ctx: ProbeContext) -> tuple[Status, str]:
    """~/.resonant and the daemon's install (a fake home): no create, append, mkdir,
    rename, rename-out, hardlink, write through a symlink, or rename of the dir or an
    ancestor; nor of a daemon tool's link and keg in a Homebrew-like dir. Reads of
    ~/.resonant (outside run-secrets) stay allowed, and so do new files in the PATH dirs."""
    with _fake_home(ctx) as (params, root):
        home, open_dir = params.home, root / "open"
        st, venv = home / ".resonant", home / "daemon" / "venv"
        o = str(open_dir)
        denied: list[Op] = [
            ("create", _unique(st, "state"), None),
            ("openw", str(st / "resonant.db"), None),
            ("openw", str(st / "logs" / "daemon.log"), None),
            ("mkdir", _unique(st / "logs", "dir"), None),
            ("rename", str(st / "config.yaml"), str(st / "config.moved")),
            ("rename", str(st / "config.yaml"), o + "/config.yaml"),
            ("link", str(st / "resonant.db"), o + "/hardlink.db"),
            ("symlink_write", str(st / "config.yaml"), o + "/symlink.yaml"),
            ("rename", str(st), o + "/state"),
            ("openw", str(root / "state" / "resonant.db"), None),
            ("create", _unique(root / "state", "real"), None),
            ("rename", str(root / "state"), o + "/state2"),
            ("rename", str(home), str(root / "home2")),
            ("openw", str(venv / "bin" / "python"), None),
            ("create", _unique(venv / "bin", "venv"), None),
            ("create", _unique(home / "daemon" / "checkout", "code"), None),
            ("rename", str(home / "daemon"), o + "/daemon"),
            ("link", str(venv / "bin" / "python"), o + "/python"),
            ("rename", str(home / "brew" / "bin" / "gh"), o + "/gh"),
            ("openw", str(home / "brew" / "Cellar" / "gh" / "1.0" / "bin" / "gh"), None),
            ("create", _unique(home / "brew" / "Cellar" / "gh" / "1.0", "keg"), None),
        ]
        allowed: list[Op] = [
            # PATH dirs stay open (brew install, uv tool install): the daemon never runs a
            # tool from them by name
            ("create", _unique(home / "brew" / "bin", "brew"), None),
            ("create", _unique(home / "brew" / "Cellar", "formula"), None),
            ("create", _unique(home / ".local" / "bin", "uvtool"), None),
            ("openw", str(home / ".local" / "bin" / "owner-tool"), None),
            ("read", str(st / "config.yaml"), None),
            ("list", str(st), None),
            ("create", _unique(home / "daemon", "beside"), None),
            ("create", _unique(open_dir, "open"), None),
        ]
        res = _fs(ctx, [*denied, *allowed], params=params)
    keys = [f"{k} {p}" for k, p, _ in denied]
    return _verdict(
        {k: res[k] for k in keys},
        {k: v for k, v in res.items() if k not in keys},
        params.home,
    )


def p_persistence(ctx: ProbeContext) -> tuple[Status, str]:
    """Auto-start entries (a fake home): LaunchAgents, shell and git startup files (create,
    open for write and rename-over), ~/.config, ~/.claude, ~/.ssh and ~/Library."""
    with _fake_home(ctx) as (params, root):
        home = params.home
        evil = root / "open" / "evil.rc"
        evil.write_text("evil")
        denied: list[Op] = [
            ("create", _unique(home / "Library" / "LaunchAgents", "agent"), None),
            ("openw", str(home / "Library" / "LaunchAgents" / "com.resonant.daemon.plist"), None),
            ("rename", str(home / "Library" / "LaunchAgents"), str(root / "open" / "la")),
            ("create", _unique(home / "Library", "lib"), None),
            ("openw", str(home / ".zshrc"), None),
            ("rename_over", str(home / ".zshrc"), str(evil)),
            ("openw", str(root / "dotfiles" / "zshrc"), None),
            ("create", str(home / ".zprofile"), None),
            ("create", str(home / ".bash_profile"), None),
            ("openw", str(home / ".gitconfig"), None),
            ("openw", str(home / ".config" / "git" / "config"), None),
            ("create", _unique(home / ".config", "cfg"), None),
            ("create", _unique(home / ".claude", "claude"), None),
            ("create", _unique(home / ".ssh", "ssh"), None),
        ]
        allowed: list[Op] = [
            ("create", _unique(home, "home"), None),
            ("read", str(home / ".zshrc"), None),
            ("read", str(home / ".ssh" / "config"), None),
        ]
        res = _fs(ctx, [*denied, *allowed], params=params)
    keys = [f"{k} {p}" for k, p, _ in denied]
    return _verdict(
        {k: res[k] for k in keys}, {k: v for k, v in res.items() if k not in keys}, home
    )


def p_unreadable(ctx: ProbeContext) -> tuple[Status, str]:
    """run-secrets, Messages, the Keychain files and runner.deny_read (a fake home): no
    list, read or write, and no hardlink to read one through. The owner's ``.env`` files
    stay readable."""
    with _fake_home(ctx) as (params, root):
        home, o = params.home, str(root / "open")
        secrets = home / ".resonant" / "run-secrets"
        denied: list[Op] = [
            ("list", str(secrets), None),
            ("read", str(secrets / "token"), None),
            ("stat", str(secrets / "token"), None),
            ("link", str(secrets / "token"), o + "/token"),
            ("list", str(home / "Library" / "Messages"), None),
            ("read", str(home / "Library" / "Messages" / "chat.db"), None),
            ("link", str(home / "Library" / "Messages" / "chat.db"), o + "/chat.db"),
            ("read", str(home / "Library" / "Keychains" / "login.keychain-db"), None),
            ("list", str(root / "denied"), None),
            ("read", str(root / "denied" / "x.txt"), None),
            ("create", _unique(root / "denied", "deny"), None),
            ("rename", str(root / "denied"), o + "/denied"),
            ("link", str(root / "denied" / "x.txt"), o + "/x.txt"),
        ]
        allowed: list[Op] = [
            ("read", str(home / ".env"), None),
            ("read", str(home / "code" / "proj" / ".env"), None),
            ("openw", str(home / "code" / "proj" / "notes.txt"), None),
            ("mkdir", _unique(home / "code", "dir"), None),
        ]
        res = _fs(ctx, [*denied, *allowed], params=params)
    keys = [f"{k} {p}" for k, p, _ in denied]
    return _verdict(
        {k: res[k] for k in keys}, {k: v for k, v in res.items() if k not in keys}, home
    )


def p_owner_files(ctx: ProbeContext) -> tuple[Status, str]:
    """Owner-like access on the real host: a planted ``.env``, key and ``.pem`` under the real
    $HOME (outside the run) are readable; a scratch dir under $HOME, the shared temp dirs and
    the PATH dirs (``~/.local/bin``, ``/opt/homebrew/bin``) are writable; runner.deny_read
    stays denied."""
    canary = ctx.canary
    for name in (".env", ".env.local", "id_rsa", "server.pem"):
        (canary / name).write_text("planted")
    allowed: list[Op] = [
        *(("read", str(canary / n), None) for n in (".env", ".env.local", "id_rsa", "server.pem")),
        ("list", str(ctx.params.home), None),
        ("create", _unique(canary, "home"), None),
        ("mkdir", _unique(canary, "dir"), None),
        ("create", _unique(Path("/private/tmp"), "tmp"), None),
        ("create", _unique(Path(OUTSIDE_PARENT), "vartmp"), None),
    ]
    # the PATH dirs a worker installs into (brew install, uv tool install), where this user
    # can write them
    for d in (ctx.params.home / ".local" / "bin", Path("/opt/homebrew/bin")):
        if d.is_dir() and os.access(d, os.W_OK):
            allowed.append(("create", _unique(d, "path"), None))
    denied: list[Op] = []
    for d in ctx.params.deny_read:
        if d.exists():
            denied.append(("list", str(d), None))
    if ctx.outside is not None:
        denied.append(("read", str(ctx.outside / "denied" / "x.txt"), None))
    res: dict[str, Any] = ctx.snippet(_FS, [list(op) for op in [*allowed, *denied]])["res"]
    keys = [f"{k} {p}" for k, p, _ in denied]
    return _verdict(
        {k: res[k] for k in keys},
        {k: v for k, v in res.items() if k not in keys},
        ctx.params.home,
    )


def p_real_paths(ctx: ProbeContext) -> tuple[Status, str]:
    """The same denies on this host's real paths, side-effect free: existing files opened for
    writing (nothing written), uniquely named creates (removed again if one appears) and
    reads. Covers ~/.resonant, the installed daemon, LaunchAgents and the rc files,
    run-secrets, Messages and the Keychain files, wherever they exist."""
    home = ctx.params.home
    ops: list[Op] = []
    state = home / ".resonant"
    if state.is_dir():
        ops.append(("create", _unique(state, "state"), None))
        for name in ("resonant.db", "config.yaml"):
            if (state / name).is_file():
                ops.append(("openw", str(state / name), None))
    for p in ctx.params.protect or ():
        if p.is_dir():
            ops.append(("create", _unique(p, "daemon"), None))
        elif p.is_file() and not p.is_symlink():
            ops.append(("openw", str(p), None))
    agents = home / "Library" / "LaunchAgents"
    if agents.is_dir():
        ops.append(("create", _unique(agents, "agent"), None))
    for rc in (".zshrc", ".zprofile", ".bash_profile", ".gitconfig"):
        if (home / rc).is_file():  # absent ones are covered by the fake home
            ops.append(("openw", str(home / rc), None))
    # the control: only what this user could write outside the sandbox counts, so a denial
    # is the guard's and not the file's permissions (a root-owned keg, a read-only file)
    ops = [
        op
        for op in ops
        if os.access(op[1] if op[0] == "openw" else os.path.dirname(op[1]), os.W_OK)
    ]
    # reads: only where the control (outside the sandbox) can read, so a deny means the guard
    reads: list[Op] = [
        ("list", str(p), None)
        for p in (
            ctx.params.secrets_dir,
            home / "Library" / "Messages",
            home / "Library" / "Keychains",
            Path("/Library/Keychains"),
        )
        if p.is_dir()
    ]
    control: dict[str, Any] = ctx.snippet(_FS, [list(op) for op in reads], bare=True)["res"]
    readable = [op for op in reads if control[f"{op[0]} {op[1]}"] == "ok"]
    res = _fs(ctx, [*ops, *readable])
    skipped = len(reads) - len(readable)
    status, detail = _verdict(res, {}, home)
    if status == "pass" and skipped:
        detail += f" ({skipped} read(s) not checked: unreadable outside the sandbox too)"
    return status, detail


def _hdiutil(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed binary
        ["/usr/bin/hdiutil", *args], capture_output=True, text=True, timeout=60, check=False
    )


def p_mount(ctx: ProbeContext) -> tuple[Status, str]:
    """No disk image mounted over a protected path (a fake ``~/.resonant``): a worker could
    otherwise lay its own files over Resonant's state or the daemon's code. Outside the
    sandbox the same attach works (the control), and is detached again."""
    if not os.path.exists("/usr/bin/hdiutil"):
        return "skip", "no hdiutil"
    with _fake_home(ctx) as (params, root):
        image = root / "open" / "probe.dmg"
        made = _hdiutil("create", "-size", "1m", "-fs", "HFS+", "-volname", "rprobe", str(image))
        if made.returncode != 0:
            return "skip", f"control failed: can't make a disk image ({made.stderr.strip()[:80]})"
        mnt = root / "open" / "mnt"
        mnt.mkdir()
        attach = ["attach", "-nobrowse", "-noverify", "-noautoopen", "-mountpoint"]
        control = _hdiutil(*attach, str(mnt), str(image))
        if control.returncode != 0:
            return "skip", f"control failed: attach outside ({control.stderr.strip()[:80]})"
        _hdiutil("detach", "-force", str(mnt))
        target = params.home / ".resonant" / "mnt"
        argv = ["/usr/bin/hdiutil", *attach, str(target), str(image)]
        mounted = False
        try:
            proc = ctx.sandboxed(argv, params=params, timeout=60)
        finally:
            if os.path.ismount(target):  # a hole: take it down again
                mounted = True
                _hdiutil("detach", "-force", str(target))
        if mounted or proc.returncode == 0:
            return "fail", "a disk image was mounted over ~/.resonant"
    return "pass", f"attach refused ({(proc.stderr or proc.stdout).strip()[:60]})"


def p_other_env(ctx: ProbeContext) -> tuple[Status, str]:
    """KNOWN LIMITATION, informational. Another same-user process's environment and
    arguments (what `ps eww` shows) are readable through sysctl KERN_PROCARGS2, and no
    Seatbelt operation gates it. Secrets are therefore never put in a process's env or argv
    (see `daemon env clean`)."""
    marker = uuid.uuid4().hex
    victim = subprocess.Popen(  # noqa: S603 - fixed argv
        [ctx.python, "-I", "-c", "import time; time.sleep(300)"],  # not a SIP binary
        env={"GH_TOKEN": marker},
        start_new_session=True,
    )
    try:
        time.sleep(0.3)  # let it exec
        arg = {"pid": victim.pid, "marker": marker}
        control = ctx.snippet(_PROCARGS, arg, bare=True)["res"]
        if control != "found":
            return "skip", f"control failed: KERN_PROCARGS2 gave {control} outside the sandbox"
        res = ctx.snippet(_PROCARGS, arg)["res"]
    finally:
        victim.kill()
        victim.wait()
    if res == "found":
        return "info", "readable from the sandbox; mitigated by keeping secrets out of env/argv"
    return "pass", f"KERN_PROCARGS2 refused ({res})"


TOKEN_SHAPES = ("ghp_", "gho_", "ghs_", "github_pat_", "sk-ant-", "sk-proj-", "xoxb-", "xoxp-")
SECRET_NAMES = ("GH_TOKEN", "GITHUB_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")
SECRET_NAME_PREFIXES = ("RESONANT_SECRET_",)


def secret_findings(env: Mapping[str, str], *, allow: Sequence[str] = ()) -> list[str]:
    """Names (never values) of variables that look like they carry a secret."""
    found: list[str] = []
    for name, value in env.items():
        if name in allow:
            continue
        if (
            name in SECRET_NAMES
            or name.startswith(SECRET_NAME_PREFIXES)
            or any(shape in value for shape in TOKEN_SHAPES)
        ):
            found.append(name)
    return sorted(found)


def daemon_environments() -> dict[str, Mapping[str, str]]:
    """The environments a secret could sit in: this process, the LaunchAgent plist and the
    running daemon's own process."""
    envs: dict[str, Mapping[str, str]] = {"this process": dict(os.environ)}
    plist = Path("~/Library/LaunchAgents/com.resonant.daemon.plist").expanduser()
    try:
        with plist.open("rb") as f:
            envs["LaunchAgent plist"] = plistlib.load(f).get("EnvironmentVariables") or {}
    except (OSError, plistlib.InvalidFileException):
        pass
    pid = _daemon_pid()
    if pid is not None:
        with contextlib.suppress(psutil.Error, OSError):
            envs["running daemon"] = psutil.Process(pid).environ()
    return envs


def p_daemon_env(ctx: ProbeContext) -> tuple[Status, str]:
    """The mitigation for the KERN_PROCARGS2 limitation: no secret sits in the daemon's
    environment (this process, the LaunchAgent plist, the running daemon). Only names are
    ever reported. CLAUDE_CODE_OAUTH_TOKEN in this process is the owner's input to the
    authenticated claude probe and is not counted."""
    bad: list[str] = []
    for where, env in daemon_environments().items():
        allow = ("CLAUDE_CODE_OAUTH_TOKEN",) if where == "this process" else ()
        bad += [f"{name} in {where}" for name in secret_findings(env, allow=allow)]
    return ("fail", "; ".join(bad)) if bad else ("pass", "no secret-looking variables")


def p_run_dir(ctx: ProbeContext) -> tuple[Status, str]:
    """The run dir and cache are writable and readable, the working directory resolves,
    /dev/null works."""
    run, cache = ctx.params.run_dir, ctx.params.cache_dir
    probe = str(run / "repo" / "allow.txt")
    code = (
        "import os\n"
        f"p = {probe!r}\n"
        "open(p, 'w').write('x')\n"
        "r = open(p).read()\n"
        f"open({str(cache / 'uv' / ('zz-probe-' + uuid.uuid4().hex[:8]))!r}, 'w').write('y')\n"
        "open(os.devnull, 'w').write('z')\n"
        "out(r=r, cwd=os.getcwd(), real=os.path.realpath('.'))\n"
    )
    data = ctx.snippet(code)
    for leftover in (cache / "uv").glob("zz-probe-*"):
        leftover.unlink(missing_ok=True)
    want = str(run / "repo")
    ok = data["r"] == "x" and data["cwd"] == want and data["real"] == want
    return ("pass", "run dir and cache usable") if ok else ("fail", f"unexpected: {data}")


def p_exec_denied(ctx: ProbeContext) -> tuple[Status, str]:
    """launchctl, crontab and at, and anything in the denied-exec paths (the daemon's venv
    and the `resonant` CLI), can't be started."""
    targets = [p for p in DENIED_EXEC if os.path.exists(p)]
    targets += [str(p) for p in exec_candidates(ctx.params.deny_exec)]
    res: dict[str, Any] = ctx.snippet(_EXEC, targets)["res"]
    bad = {k: v for k, v in res.items() if not _denied(v)}
    return (
        ("fail", f"started or inconclusive: {_fmt(bad)}") if bad else ("pass", f"{len(res)} denied")
    )


def p_ports(ctx: ProbeContext) -> tuple[Status, str]:
    """The daemon's API port is unreachable on loopback (v4 and v6), for connect and for
    bind, while another loopback port stays open."""
    with _listener() as (host, port):
        arg = {
            "connect": [[h, API_PORT] for h in ("127.0.0.1", "::1")] + [[host, port]],
            "bind": [["127.0.0.1", API_PORT], ["0.0.0.0", API_PORT]],  # noqa: S104
        }
        res: dict[str, Any] = ctx.snippet(_NET, arg)["res"]
    bad = [k for k, v in res.items() if f":{port}" not in k and not _denied(v)]
    open_ok = res.get(f"connect {host}:{port}") == "ok"
    if bad:
        return "fail", f"reachable or inconclusive: {_fmt(res, bad)}"
    if not open_ok:
        return "fail", f"a free loopback port was blocked too: {res.get(f'connect {host}:{port}')}"
    return "pass", f"{len(res) - 1} denied, another loopback port open"


def p_bind_dev_server(ctx: ProbeContext) -> tuple[Status, str]:
    """A localhost dev server on another port works (a listener on a free port + a client)."""
    code = (  # raw sockets: http.server's getfqdn can stall on hosts without DNS
        "import threading\n"
        "srv = socket.socket()\n"
        "srv.bind(('127.0.0.1', 0))\n"
        "srv.listen(1)\n"
        "def serve():\n"
        "    c, _ = srv.accept()\n"
        "    c.sendall(c.recv(16))\n"
        "    c.close()\n"
        "threading.Thread(target=serve, daemon=True).start()\n"
        "cl = socket.create_connection(srv.getsockname(), timeout=5)\n"
        "cl.sendall(b'ping')\n"
        "out(status=200 if cl.recv(16) == b'ping' else 0)\n"
    )
    data = ctx.snippet(code)
    return ("pass", "echo server on loopback") if data["status"] == 200 else ("fail", f"{data}")


def p_unix_socket(ctx: ProbeContext) -> tuple[Status, str]:
    """A unix-socket connect works (the Docker socket's route), here to a socket outside the
    run dir."""
    sock_dir = Path(tempfile.mkdtemp(prefix="rsp-", dir=OUTSIDE_PARENT))
    path = sock_dir / "s.sock"
    if len(str(path)) >= MAX_SOCKET_PATH:
        shutil.rmtree(sock_dir, ignore_errors=True)
        return "skip", "scratch path too long for a unix socket"
    srv = socket.socket(socket.AF_UNIX)
    try:
        srv.bind(str(path))
        srv.listen(1)
        res: dict[str, Any] = ctx.snippet(_NET, {"unix": [str(path)]})["res"]
    finally:
        srv.close()
        shutil.rmtree(sock_dir, ignore_errors=True)
    got = res[f"unix {path}"]
    return ("pass", "connected") if got == "ok" else ("fail", f"connect gave {got}")


def p_https(ctx: ProbeContext) -> tuple[Status, str]:
    """Outbound HTTPS with certificate checks, with the Keychain denied: curl, Apple git
    (``ls-remote``), Python's ssl and gh (a dummy token: a 401 means TLS worked). Each tool
    is first tried outside the sandbox; one that fails there too is skipped."""
    git = resolve_tool("git")
    gh = resolve_tool("gh")
    py = (
        "import urllib.request, urllib.error\n"
        "try:\n"
        f"    print(urllib.request.urlopen({HTTPS_URL!r}, timeout=20).status)\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.code)\n"
    )
    gh_dir = ctx.params.run_dir / "tmp" / "gh-probe"
    gh_dir.mkdir(parents=True, exist_ok=True)
    gh_env = {**ctx.env, "GH_TOKEN": "resonant-probe-not-a-token", "GH_CONFIG_DIR": str(gh_dir)}
    tools: dict[str, tuple[list[str], dict[str, str], Callable[[str], bool]]] = {
        "curl": (
            ["/usr/bin/curl", "-sS", "-o", "/dev/null", "-w", "%{http_code}", HTTPS_URL],
            ctx.env,
            lambda out: out.strip().startswith(("2", "3")),
        ),
        "python": ([ctx.python, "-I", "-c", py], ctx.env, lambda out: out.strip() == "200"),
    }
    if git is not None:
        tools["git"] = (
            [str(git), "ls-remote", GIT_REMOTE, "HEAD"],
            {**ctx.env, "GIT_TERMINAL_PROMPT": "0"},
            lambda out: "HEAD" in out,
        )
    if gh is not None:
        tools["gh"] = (
            [str(gh), "api", "/zen"],
            gh_env,
            lambda out: "Bad credentials" in out or "401" in out,
        )
    passed: list[str] = []
    skipped: list[str] = []
    for name, (argv, env, ok) in tools.items():
        try:
            control = subprocess.run(  # noqa: S603 - argv is built here
                argv,
                env=env,
                capture_output=True,
                text=True,
                timeout=NET_TIMEOUT_S,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            skipped.append(name)
            continue
        if not ok(control.stdout + control.stderr):
            skipped.append(name)
            continue
        proc = ctx.sandboxed(argv, env=env, timeout=NET_TIMEOUT_S)
        text = proc.stdout + proc.stderr
        if not ok(text):
            return "fail", f"{name} failed in the sandbox: {text.strip()[:150]}"
        passed.append(name)
    if not passed:
        return "skip", f"control failed: no network outside the sandbox ({', '.join(skipped)})"
    tail = f"; skipped {', '.join(skipped)}" if skipped else ""
    return "pass", f"{', '.join(passed)} reached github.com{tail}"


def p_mach(ctx: ProbeContext) -> tuple[Status, str]:
    """The Keychain (securityd), launchd, iMessage and the services that drive apps as the
    owner can't be looked up, and the ones kept for TLS and DNS still can."""
    res: dict[str, Any] = ctx.snippet(_MACH, [*DENIED_MACH, *ALLOWED_MACH])["res"]
    bad = [n for n in DENIED_MACH if res[n] != BOOTSTRAP_NOT_PRIVILEGED]
    if bad:
        return "fail", f"lookup not denied: {_fmt(res, bad)}"
    control: dict[str, Any] = ctx.snippet(_MACH, list(ALLOWED_MACH), bare=True)["res"]
    broken = [n for n in ALLOWED_MACH if control[n] == 0 and res[n] != 0]
    if broken:
        return "fail", f"needed service blocked: {_fmt(res, broken)}"
    return "pass", f"{len(DENIED_MACH)} lookups denied, {', '.join(ALLOWED_MACH)} kept"


def p_operations(ctx: ProbeContext) -> tuple[Status, str]:
    """Operations that are not file or mach filters: Apple Events, LaunchServices opens,
    task ports, signals."""
    res: dict[str, Any] = ctx.snippet(_SBCHECK, DENIED_OPS)["res"]
    bad = [op for op, rc in res.items() if rc == 0]
    return ("fail", f"allowed: {', '.join(bad)}") if bad else ("pass", f"{len(res)} denied")


def p_task_ports(ctx: ProbeContext) -> tuple[Status, str]:
    """task_name_for_pid and task_for_pid on a process outside the sandbox fail; on the
    session's own process they work."""
    pids = [ctx.outside_pid] + ([ctx.daemon_pid] if ctx.daemon_pid else [])
    control: dict[str, Any] = ctx.snippet(_TASK, pids, bare=True)["res"]
    if control[f"name {ctx.outside_pid}"] != 0:
        return "skip", "control failed: task_name_for_pid is refused outside the sandbox too"
    res: dict[str, Any] = ctx.snippet(_TASK, pids)["res"]
    bad = [k for k, v in res.items() if k != "self" and v == 0]
    if bad:
        return "fail", f"task port obtained: {_fmt(res, bad)}"
    if res["self"] != 0:
        return "fail", "own task name refused"
    return "pass", f"{len(pids)} outside process(es) refused"


def p_signal(ctx: ProbeContext) -> tuple[Status, str]:
    """Signals to processes outside the sandbox (the daemon, the kill switch) are refused;
    the session's own children are still killable."""
    res: dict[str, Any] = ctx.snippet(_SIGNAL, {"pid": ctx.outside_pid})["res"]
    if not _denied(res["outside"]):
        return "fail", f"outside signal gave {res['outside']}"
    if res["child_signal"] != "ok" or res["child_kill"] != "ok":
        return "fail", f"own child not signalable: {res}"
    with contextlib.suppress(ProcessLookupError):
        os.kill(ctx.outside_pid, 0)  # still alive after the attempt
    return "pass", "outside refused, own children killable"


def p_env(ctx: ProbeContext) -> tuple[Status, str]:
    """The session sees only the built environment."""
    data = ctx.snippet(_ENV)
    env: dict[str, str] = data["env"]
    allowed = set(ctx.env) | {"LC_CTYPE", "__CF_USER_TEXT_ENCODING"}
    extra = sorted(set(env) - allowed)
    if extra or "SSH_AUTH_SOCK" in env:
        return "fail", f"unexpected variables: {', '.join(extra)}"
    if env["HOME"] != str(ctx.params.run_dir / "home"):
        return "fail", f"HOME is {env['HOME']}"
    return "pass", f"{len(env)} variables, HOME in the run dir"


def p_setsid_child(ctx: ProbeContext) -> tuple[Status, str]:
    """A child in a new session is still sandboxed (it can't read a runner.deny_read file)."""
    if ctx.outside is None:
        return "skip", "no scratch dir"
    res = ctx.snippet(_SETSID, {"path": str(ctx.outside / "denied" / "x.txt")})["res"]
    return ("pass", "new-session child denied") if _denied(res) else ("fail", f"child gave {res}")


# local-only


def p_keychain(ctx: ProbeContext) -> tuple[Status, str]:
    """The Keychain can't be read. A throwaway keychain file (never the login keychain, and
    not added to the search list) holds one item; `security find-generic-password` reads it
    outside the sandbox and must not inside, and SecKeychainCopySearchList must fail fast."""
    security = "/usr/bin/security"
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="resonant-probe-kc-", dir=OUTSIDE_PARENT)))
    kc = str(root / "probe.keychain-db")
    password, secret = uuid.uuid4().hex, uuid.uuid4().hex
    service = "resonant-sandbox-probe"

    def sec(*args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(  # noqa: S603 - fixed binary
            [security, *args], capture_output=True, text=True, timeout=20, check=False
        )

    try:
        made = sec("create-keychain", "-p", password, kc)
        if made.returncode != 0:
            return "skip", f"control failed: can't create a scratch keychain ({made.stderr[:80]})"
        sec("unlock-keychain", "-p", password, kc)
        sec("add-generic-password", "-s", service, "-a", "probe", "-w", secret, "-T", security, kc)
        control = sec("find-generic-password", "-s", service, "-w", kc)
        if control.stdout.strip() != secret:
            return "skip", "control failed: the scratch item is unreadable outside the sandbox"
        proc = ctx.sandboxed([security, "find-generic-password", "-s", service, "-w", kc])
        if secret in proc.stdout or proc.returncode == 0:
            return "fail", "security read a keychain item from inside the sandbox"
        start = time.monotonic()
        res = ctx.snippet(_KEYCHAIN)["res"]
        ms = (time.monotonic() - start) * 1000
        if res == 0:
            return "fail", "SecKeychainCopySearchList succeeded"
        return "pass", f"item unreadable (rc {proc.returncode}); search list denied in {ms:.0f} ms"
    finally:
        sec("delete-keychain", kc)
        shutil.rmtree(root, ignore_errors=True)


def p_launchd(ctx: ProbeContext) -> tuple[Status, str]:
    """A launchd job submission from Python is refused. (launchd refuses submits from any
    sandboxed caller, even under a bare allow-default profile, so this guards against a
    macOS change rather than against a rule in our profile.)"""
    label = f"com.resonant.sandbox-probe.{uuid.uuid4().hex[:10]}"
    try:
        res = ctx.snippet(_LAUNCHD, {"label": label})["res"]
    finally:
        subprocess.run(  # noqa: S603 - fixed argv; removes the job if one slipped through
            ["/bin/launchctl", "remove", label], capture_output=True, timeout=10, check=False
        )
    if res == "unavailable":
        return "skip", "liblaunch submit API is gone on this macOS"
    if res == "errno:0":
        return "fail", "launchd accepted a job from inside the sandbox"
    return ("pass", f"refused ({res})") if _denied(res) else ("fail", f"unexpected: {res}")


def p_hid(ctx: ProbeContext) -> tuple[Status, str]:
    """The HID drivers (synthetic keyboard and mouse input, e.g. typing into Messages) can't
    be opened."""
    control: dict[str, int] = ctx.snippet(_HID, bare=True)["res"]
    opened = [k for k, v in control.items() if v == 0]
    if not opened:
        return "skip", "control failed: IOHIDSystem can't be opened outside the sandbox either"
    res: dict[str, int] = ctx.snippet(_HID)["res"]
    bad = [k for k in opened if res[k] == 0]
    if bad:
        return "fail", f"IOHIDSystem client type {', '.join(bad)} opened"
    return "pass", f"client types {', '.join(opened)} refused"


def p_display(ctx: ProbeContext) -> tuple[Status, str]:
    """The window server is unreachable: no screenshot of the display."""
    control = ctx.snippet(_DISPLAY, bare=True)["res"]
    if control != "image":
        return "skip", "control failed: no display image outside the sandbox either"
    res = ctx.snippet(_DISPLAY)["res"]
    return ("fail", "captured the display") if res == "image" else ("pass", "no image")


def p_lan_ports(ctx: ProbeContext) -> tuple[Status, str]:
    """The API port is unreachable on the machine's LAN address too."""
    if ctx.lan_ip is None:
        return "skip", "no LAN address"
    arg = {"connect": [[ctx.lan_ip, API_PORT]], "bind": [[ctx.lan_ip, API_PORT]]}
    res: dict[str, Any] = ctx.snippet(_NET, arg)["res"]
    bad = {k: v for k, v in res.items() if not _denied(v)}
    return (
        ("fail", f"reachable or inconclusive: {_fmt(bad)}")
        if bad
        else ("pass", f"{ctx.lan_ip}: {len(res)} denied")
    )


def p_docker(ctx: ProbeContext) -> tuple[Status, str]:
    """The Docker socket, where a daemon is running, can be connected to."""
    reachable: list[str] = []
    for raw in DOCKER_SOCKETS:
        path = os.path.expanduser(raw)
        if not os.path.exists(path):
            continue
        s = socket.socket(socket.AF_UNIX)
        s.settimeout(2)
        try:
            s.connect(path)
            reachable.append(path)
        except OSError:
            pass
        finally:
            s.close()
    if not reachable:
        return "skip", "no running Docker daemon socket"
    res: dict[str, Any] = ctx.snippet(_NET, {"unix": reachable})["res"]
    bad = {k: v for k, v in res.items() if v != "ok"}
    return ("fail", f"blocked: {_fmt(bad)}") if bad else ("pass", f"{len(reachable)} connected")


def p_claude_runs(ctx: ProbeContext) -> tuple[Status, str]:
    """`claude --version` runs inside the sandbox."""
    if ctx.claude is None:
        return "skip", "claude is not installed"
    proc = ctx.sandboxed([str(ctx.params.run_dir / "bin" / "claude"), "--version"], timeout=30)
    if proc.returncode != 0 or "Claude Code" not in proc.stdout:
        return "fail", f"rc={proc.returncode}: {(proc.stderr or proc.stdout)[:150]}"
    return "pass", proc.stdout.strip()


def p_claude_no_token(ctx: ProbeContext) -> tuple[Status, str]:
    """With a fresh config dir and no token, `claude -p` fails fast: the Keychain is
    denied, so nothing else logs it in, and the denial doesn't hang."""
    if ctx.claude is None:
        return "skip", "claude is not installed"
    claude = str(ctx.params.run_dir / "bin" / "claude")
    start = time.monotonic()
    try:
        proc = ctx.sandboxed([claude, "-p", "say hi"], timeout=60)
    except subprocess.TimeoutExpired:
        return "fail", "hung for 60 s: a denied Keychain lookup must fail fast"
    secs = time.monotonic() - start
    text = (proc.stdout + proc.stderr).strip().replace("\n", " ")[:120]
    if proc.returncode == 0:
        return "fail", f"authenticated with no token: the Keychain is reachable ({text})"
    if secs > CLAUDE_FAIL_FAST_S:
        return "fail", f"failed after {secs:.1f} s (limit {CLAUDE_FAIL_FAST_S:.0f} s): {text}"
    return "pass", f"failed in {secs * 1000:.0f} ms: {text}"


def p_claude_auth(ctx: ProbeContext) -> tuple[Status, str]:
    """An authenticated `claude -p` from the env token alone. The owner's step: it only runs
    when CLAUDE_CODE_OAUTH_TOKEN is in the environment of `resonant sandbox check`."""
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if ctx.claude is None or not token:
        return "skip", "owner step: set CLAUDE_CODE_OAUTH_TOKEN and re-run"
    env = {**ctx.env, "CLAUDE_CODE_OAUTH_TOKEN": token}
    claude = str(ctx.params.run_dir / "bin" / "claude")
    start = time.monotonic()
    try:
        proc = ctx.sandboxed([claude, "-p", "reply with the single word ok"], env=env, timeout=180)
    except subprocess.TimeoutExpired:
        return "fail", "no answer in 180 s"
    secs = time.monotonic() - start
    if proc.returncode != 0:
        return "fail", f"rc={proc.returncode}: {(proc.stderr or proc.stdout)[:150]}"
    return "pass", f"answered in {secs:.1f} s"


@dataclass(frozen=True)
class Probe:
    name: str
    scope: Scope
    fn: Callable[[ProbeContext], tuple[Status, str]]


PROBES: tuple[Probe, ...] = (
    Probe("~/.resonant and the daemon install: no writes", "ci", p_state_writes),
    Probe("auto-start and rc files: no writes", "ci", p_persistence),
    Probe("run-secrets, Messages, Keychain files, deny_read", "ci", p_unreadable),
    Probe("owner's files: .env readable, $HOME writable", "ci", p_owner_files),
    Probe("run dir and cache", "ci", p_run_dir),
    Probe("exec denies (launchctl, cron, the daemon)", "ci", p_exec_denied),
    Probe("port 7777 on loopback", "ci", p_ports),
    Probe("loopback dev server", "ci", p_bind_dev_server),
    Probe("unix socket connect", "ci", p_unix_socket),
    Probe("outbound HTTPS (curl, git, python, gh)", "ci", p_https),
    Probe("mach lookups (Keychain, launchd, iMessage)", "ci", p_mach),
    Probe("operations (AppleEvents, LaunchServices, signals)", "ci", p_operations),
    Probe("task_for_pid on another process", "ci", p_task_ports),
    Probe("signals to other processes", "ci", p_signal),
    Probe("environment", "ci", p_env),
    Probe("new-session child stays sandboxed", "ci", p_setsid_child),
    Probe("other processes' environment (known limitation)", "ci", p_other_env),
    Probe("real paths on this host", "local", p_real_paths),
    Probe("disk image mount over ~/.resonant", "local", p_mount),
    Probe("daemon env holds no secrets", "local", p_daemon_env),
    Probe("keychain item read", "local", p_keychain),
    Probe("launchd job submit", "local", p_launchd),
    Probe("HID synthetic input", "local", p_hid),
    Probe("window server (screenshot)", "local", p_display),
    Probe("port 7777 on the LAN address", "local", p_lan_ports),
    Probe("docker socket", "local", p_docker),
    Probe("claude runs", "local", p_claude_runs),
    Probe("claude -p without a token fails fast", "local", p_claude_no_token),
    Probe("claude -p with CLAUDE_CODE_OAUTH_TOKEN", "local", p_claude_auth),
)


def run_probes(ctx: ProbeContext, scopes: Sequence[Scope] = ("ci", "local")) -> list[ProbeResult]:
    results: list[ProbeResult] = []
    for probe in PROBES:
        if probe.scope not in scopes:
            continue
        start = time.monotonic()
        try:
            status, detail = probe.fn(ctx)
        except ProbeError as e:
            status, detail = "fail", f"probe could not run: {e}"
        except subprocess.TimeoutExpired:
            status, detail = "fail", "timed out"
        except (OSError, KeyError, ValueError, json.JSONDecodeError, SandboxError) as e:
            status, detail = "fail", f"probe error: {type(e).__name__}: {e}"
        results.append(
            ProbeResult(
                probe.name, probe.scope, status, detail, round((time.monotonic() - start) * 1000)
            )
        )
    return results


@contextlib.contextmanager
def _listener() -> Generator[tuple[str, int]]:
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    try:
        yield "127.0.0.1", int(srv.getsockname()[1])
    finally:
        srv.close()


def render_table(results: Sequence[ProbeResult]) -> str:
    width = max((len(r.name) for r in results), default=0)
    lines = [
        f"{r.status.upper():4}  {r.scope:5}  {r.name:<{width}}  {r.ms:>5} ms  {r.detail}"
        for r in results
    ]
    fails = sum(r.status == "fail" for r in results)
    skips = sum(r.status == "skip" for r in results)
    infos = sum(r.status == "info" for r in results)
    passed = len(results) - fails - skips - infos
    lines.append(f"{passed} passed, {fails} failed, {skips} skipped, {infos} known limitation(s)")
    return "\n".join(lines)


# --- per-run self-check ----------------------------------------------------------------------


def self_check(params: SandboxParams, *, python: str | None = None) -> tuple[bool, str]:
    """The cheap fail-closed check before a session: denied writes into ``~/.resonant`` and
    the daemon's install, a denied mach lookup of securityd and a denied connect to the API
    port. One sandboxed
    interpreter start, about 50 to 100 ms. Writes nothing while the guard holds."""
    if not sandbox_exec_available():
        return False, "sandbox-exec is not available"
    p = params.resolved()
    interpreter = python or str(_interpreter())
    code = _PRELUDE + _SELF
    # the first protected dir this user could write without the guard (the daemon's venv)
    protect = next((str(d) for d in p.protect or () if d.is_dir() and os.access(d, os.W_OK)), None)
    arg = {"home": str(p.home), "port": API_PORT, "protect": protect}
    argv = [interpreter, "-I", "-c", code, json.dumps(arg)]
    try:
        proc = subprocess.run(  # noqa: S603 - argv is built here
            wrap(argv, p),
            cwd="/",
            env={"PATH": "/usr/bin:/bin"},
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
        res: dict[str, Any] = json.loads(proc.stdout.strip().splitlines()[-1])["res"]
    except (OSError, SandboxError, subprocess.TimeoutExpired, IndexError, ValueError) as e:
        return False, f"self-check could not run: {type(e).__name__}: {e}"
    problems: list[str] = []
    if not _denied(res["write"]):
        problems.append(f"write into {p.home / '.resonant'} gave {res['write']}")
    if "protect" in res and not _denied(res["protect"]):
        problems.append(f"write into {protect} gave {res['protect']}")
    if res["mach"] != BOOTSTRAP_NOT_PRIVILEGED:
        problems.append(f"securityd lookup gave {res['mach']}")
    if not _denied(res["net"]):
        problems.append(f"connect to :{API_PORT} gave {res['net']}")
    if problems:
        return False, "; ".join(problems)
    return True, "~/.resonant and daemon writes, keychain lookup and API port denied"


def cached_self_check(
    params: SandboxParams,
    cache_file: Path,
    *,
    ttl_s: float = SELF_CHECK_TTL_S,
    now: Callable[[], float] = time.time,
    python: str | None = None,
) -> tuple[bool, str]:
    """``self_check`` with a passing result cached for ``ttl_s``, keyed by the macOS build
    and the profile hash. A failure is never cached, so the next run re-checks."""
    key = f"{macos_build()}:{profile_hash(params)}"
    try:
        cached = json.loads(cache_file.read_text())
        if cached.get("key") == key and now() - float(cached["at"]) < ttl_s:
            return True, "cached"
    except (OSError, ValueError, KeyError, TypeError):
        pass
    ok, detail = self_check(params, python=python)
    if ok:
        cache_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = cache_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"key": key, "at": now()}))
        os.replace(tmp, cache_file)
    return ok, detail
