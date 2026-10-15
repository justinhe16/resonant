"""Probes that prove the runner sandbox holds, and the cheap per-run self-check.

Every probe runs a small Python snippet *inside* the sandbox (the real profile, built the
way a session's is) and compares what it saw with what the profile promises. Where a denial
could also be a missing file or a dead service, a control run outside the sandbox first
proves the thing is reachable, so a pass means "denied", not "absent".

Two scopes, by where they can run:

- ``ci``: file, exec, network and mach denials that behave the same on any macOS host. These
  run in CI on the GitHub macOS runners.
- ``local``: probes that need the Mini (Keychain, launchd, the HID drivers, the display, the
  LAN address, sockets that may or may not exist, ``claude`` itself).

Probes write only uniquely named, nonexistent files, and delete any that unexpectedly
appear (and fail).
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from resonant.sandbox.profile import (
    API_PORT,
    OLLAMA_PORT,
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

Status = Literal["pass", "fail", "skip"]
Scope = Literal["ci", "local"]

DENIED_ERRNOS = frozenset({1, 13})  # EPERM, EACCES
BOOTSTRAP_NOT_PRIVILEGED = 1100  # kr from bootstrap_look_up when the sandbox says no
SELF_CHECK_TTL_S = 3600.0
SNIPPET_TIMEOUT_S = 30.0
CLAUDE_FAIL_FAST_S = 5.0

DENIED_EXEC = (
    "/usr/bin/security",
    "/usr/bin/osascript",
    "/usr/bin/open",
    "/bin/launchctl",
    "/usr/bin/shortcuts",
    "/usr/bin/crontab",
    "/usr/bin/at",
    "/usr/bin/sudo",
    "/usr/sbin/screencapture",
)
# Looked up by name: the sandbox answers 1100 whether or not the service exists.
DENIED_MACH = (
    "com.apple.xpc.launchd.domain.user.501",
    "com.apple.SecurityServer",
    "com.apple.securityd.xpc",
    "com.apple.windowserver.active",
    "com.apple.hidsystem.server",
    "com.apple.accessibility.AXVisualSupport",
    "com.apple.universalaccessd",
    "com.apple.coreservices.appleevents",
    "com.apple.coreservices.appleevent-send",
    "com.apple.coreservices.launchservicesd",
    "com.apple.lsd.open",
    "com.apple.pasteboard.1",
    "com.apple.tccd",
)
# Kept on purpose: TLS for Go and Apple git, and DNS.
ALLOWED_MACH = ("com.apple.trustd", "com.apple.dnssd.service")
DENIED_OPS = (
    "appleevent-send",
    "lsopen",
    "mach-priv-task-port",
    "mach-task-name",
    "process-info-pidinfo",
    "signal",
    "file-write-create",
)
MDNS_SOCKET = "/private/var/run/mDNSResponder"
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
    canary: Path  # a dir under $HOME, written from outside; a session must not see into it
    outside_pid: int  # a process outside the sandbox
    lan_ip: str | None
    claude: Path | None
    daemon_pid: int | None = None
    cleanup: list[Path] = field(default_factory=lambda: [])

    def sandboxed(
        self,
        argv: Sequence[str],
        *,
        timeout: float = SNIPPET_TIMEOUT_S,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Run argv inside the sandbox from the run's repo dir."""
        return subprocess.run(  # noqa: S603 - argv is built here
            wrap(argv, self.params),
            cwd=self.params.run_dir / "repo",
            env=self.env if env is None else env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            stdin=subprocess.DEVNULL,
        )

    def snippet(self, code: str, arg: object = None, *, bare: bool = False) -> dict[str, Any]:
        """Run ``code`` (which prints one JSON object) in the sandbox, or outside it."""
        argv = [self.python, "-I", "-c", _PRELUDE + code, json.dumps(arg)]
        if bare:
            proc = subprocess.run(  # noqa: S603 - argv is built here
                argv, capture_output=True, text=True, timeout=SNIPPET_TIMEOUT_S, check=False
            )
        else:
            proc = self.sandboxed(argv)
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

_FS = """
def create(p):
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    try:
        os.unlink(p)
    except OSError:
        pass
    return "WROTE"
res = {}
for kind, p in A:
    if kind == "read":
        res[p] = tryit(lambda p=p: open(p, "rb").read(1))
    elif kind == "list":
        res[p] = tryit(lambda p=p: os.listdir(p))
    elif kind == "stat":
        res[p] = tryit(lambda p=p: os.stat(p))
    elif kind == "create":
        try:
            res[p] = create(p)
        except OSError as e:
            res[p] = "errno:%d" % e.errno
    elif kind == "mkdir":
        res[p] = tryit(lambda p=p: os.mkdir(p))
    elif kind == "symlink":
        res[p] = tryit(lambda p=p: os.symlink("/etc/hosts", p))
    elif kind == "rename":
        dst = p + ".moved"
        res[p] = tryit(lambda p=p, dst=dst: os.rename(p, dst))
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
code = "import os,json;\\ntry:\\n open(%r,'rb').read(1); r='READ'\\nexcept OSError as e: r='errno:%%d'%%e.errno\\nprint(r)" % A["path"]
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

_SELF = """
home = A["home"]
res = {}
target = os.path.join(home, ".resonant")
if not os.path.isdir(target):
    target = home
res["read"] = tryit(lambda: os.listdir(target))
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


def _fmt(results: dict[str, Any], names: Sequence[str] | None = None) -> str:
    keys = names or list(results)
    return ", ".join(f"{os.path.basename(str(k)) or k}={results[k]}" for k in keys)


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
    try:
        import psutil
    except ImportError:  # pragma: no cover
        return None
    for proc in psutil.process_iter(["pid", "cmdline"]):
        cmd = proc.info["cmdline"] or []
        if "resonant" in " ".join(cmd) and cmd[-2:] == ["daemon", "run"]:
            return int(proc.info["pid"])
    return None


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
    base_dir: Path | None = None,
    home: Path | None = None,
    deny_exec: Sequence[Path] = (),
    read_paths: Sequence[Path] | None = None,
) -> Iterator[ProbeContext]:
    """Build a throwaway run dir and cache (under ``base_dir``, default ``~/resonant-runs``),
    a canary dir under ``$HOME`` and a process outside the sandbox. Cleans up on exit."""
    if not sandbox_exec_available():
        raise SandboxError("sandbox-exec is not available on this host")
    home = home or Path.home()
    base = base_dir or home / "resonant-runs"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".check-", dir=base))
    canary = Path(tempfile.mkdtemp(prefix=".resonant-probe-", dir=home))
    sleeper = subprocess.Popen(["/bin/sleep", "300"], start_new_session=True)
    interpreter = Path(os.path.realpath(getattr(sys, "_base_executable", sys.executable)))
    reads = [
        *(default_read_paths() if read_paths is None else read_paths),
        Path(sys.base_prefix),
        interpreter.parent,
    ]
    params = SandboxParams(
        run_dir=work / "run",
        cache_dir=work / "cache",
        home=home,
        read_paths=tuple(reads),
        deny_exec=tuple(deny_exec),
    ).resolved()
    try:
        (canary / "canary.txt").write_text("canary")
        prepare_dirs(params)
        (work / "other").mkdir()
        (work / "other" / "keep.txt").write_text("sibling run")
        claude = resolve_tool("claude")
        if claude is not None:
            with contextlib.suppress(SandboxError):
                link_tools(params, {"claude": claude})
        yield ProbeContext(
            params=params,
            python=str(interpreter),
            env=scratch_env(params),
            canary=canary,
            outside_pid=sleeper.pid,
            lan_ip=_lan_ip(),
            claude=claude,
            daemon_pid=_daemon_pid(),
        )
    finally:
        sleeper.kill()
        sleeper.wait()
        for d in (work, canary):
            shutil.rmtree(d, ignore_errors=True)


# --- the probes ------------------------------------------------------------------------------


def _fs(ctx: ProbeContext, ops: Sequence[tuple[str, str]]) -> dict[str, Any]:
    res: dict[str, Any] = ctx.snippet(_FS, ops)["res"]
    for kind, path in ops:  # a create that stuck is a hole: remove it from outside
        if kind == "rename" and os.path.lexists(path + ".moved"):
            os.rename(path + ".moved", path)  # undo, then report
            res[path] = "WROTE"
        if kind in {"create", "mkdir", "symlink"} and os.path.lexists(path):
            if os.path.isdir(path) and not os.path.islink(path):
                os.rmdir(path)
            else:
                os.unlink(path)
            res[path] = "WROTE"
    return res


def _unique(parent: Path, tag: str) -> str:
    return str(parent / f".zz-sandbox-probe-{tag}-{uuid.uuid4().hex[:10]}")


def p_read_denied(ctx: ProbeContext) -> tuple[Status, str]:
    """Nothing under $HOME, /Volumes, /Users/Shared or the shared temp dirs can be read."""
    home = ctx.params.home
    ops: list[tuple[str, str]] = [
        ("read", str(ctx.canary / "canary.txt")),
        ("list", str(ctx.canary)),
        ("list", str(home)),
        ("list", "/Users/Shared"),
        ("list", "/private/tmp"),
        ("list", "/private/var/folders"),
    ]
    if Path("/Volumes").is_dir():
        ops.append(("list", "/Volumes"))
    for sub in (".resonant", ".ssh", ".aws", ".config/gh", ".claude", "Library", ".local/bin"):
        if (home / sub).exists():
            ops.append(("list", str(home / sub)))
    # the control: from outside, the canary is readable
    if (ctx.canary / "canary.txt").read_text() != "canary":
        raise ProbeError("canary unreadable from outside")
    res = _fs(ctx, ops)
    bad = {k: v for k, v in res.items() if not _denied(v)}
    return (
        ("fail", f"readable or inconclusive: {_fmt(bad)}")
        if bad
        else ("pass", f"{len(res)} denied")
    )


def p_write_denied(ctx: ProbeContext) -> tuple[Status, str]:
    """Persistence paths can't be written: a unique, nonexistent name in each."""
    home = ctx.params.home
    ops: list[tuple[str, str]] = [
        ("create", _unique(home, "home")),
        ("create", _unique(ctx.canary, "canary")),
        ("create", str(home / f".zshrc-zz-probe-{uuid.uuid4().hex[:10]}")),
        ("create", str(home / f".gitconfig-zz-probe-{uuid.uuid4().hex[:10]}")),
        ("create", _unique(Path("/Users/Shared"), "shared")),
        ("create", _unique(Path("/private/tmp"), "tmp")),
        ("create", _unique(Path("/Library"), "lib")),
        ("mkdir", _unique(home, "dir")),
    ]
    for sub in ("Library/LaunchAgents", ".claude", ".ssh", ".resonant", ".config"):
        if (home / sub).is_dir():
            ops.append(("create", _unique(home / sub, sub.replace("/", "_"))))
    if Path("/Volumes").is_dir():
        ops.append(("create", _unique(Path("/Volumes"), "vol")))
    res = _fs(ctx, ops)
    bad = {k: v for k, v in res.items() if not _denied(v)}
    return (
        ("fail", f"writable or inconclusive: {_fmt(bad)}")
        if bad
        else ("pass", f"{len(res)} denied")
    )


def p_work_dir(ctx: ProbeContext) -> tuple[Status, str]:
    """Only the run's own dir is writable: not the work dir above it (create, symlink, mkdir,
    rename the run dir) and not a sibling run dir. The daemon's O_NOFOLLOW opens of a run
    dir rest on this."""
    work = ctx.params.run_dir.parent
    ops = [
        ("create", _unique(work, "entry")),
        ("symlink", _unique(work, "link")),
        ("mkdir", _unique(work, "dir")),
        ("rename", str(ctx.params.run_dir)),
        ("create", _unique(work / "other", "sibling")),
        ("symlink", _unique(work / "other", "sibling-link")),
    ]
    res = _fs(ctx, ops)
    bad = {k: v for k, v in res.items() if not _denied(v)}
    if not ctx.params.run_dir.is_dir():
        return "fail", "run dir was renamed away"
    return (
        ("fail", f"writable or inconclusive: {_fmt(bad)}")
        if bad
        else ("pass", f"{len(res)} denied")
    )


def p_other_env(ctx: ProbeContext) -> tuple[Status, str]:
    """Another process's environment and arguments (what `ps eww` shows) can't be read, so
    the daemon's `git push` with a token in its env stays private. `ps` itself can't be
    exec'd by a sandboxed process (setuid), so the sysctl behind it is probed."""
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
    return (
        ("fail", "read another process's environment")
        if res == "found"
        else (
            "pass",
            f"KERN_PROCARGS2 refused ({res})",
        )
    )


def p_run_dir(ctx: ProbeContext) -> tuple[Status, str]:
    """The allows hold: the run dir and cache are writable and readable, the working
    directory resolves, /dev/null works."""
    run, cache = ctx.params.run_dir, ctx.params.cache_dir
    probe = str(run / "repo" / "allow.txt")
    code = (
        "import os\n"
        f"p = {probe!r}\n"
        "open(p, 'w').write('x')\n"
        "r = open(p).read()\n"
        f"open({str(cache / 'uv' / 'c.txt')!r}, 'w').write('y')\n"
        "open(os.devnull, 'w').write('z')\n"
        "out(r=r, cwd=os.getcwd(), real=os.path.realpath('.'))\n"
    )
    data = ctx.snippet(code)
    want = str(run / "repo")
    ok = data["r"] == "x" and data["cwd"] == want and data["real"] == want
    return ("pass", "run dir and cache usable") if ok else ("fail", f"unexpected: {data}")


def p_exec_denied(ctx: ProbeContext) -> tuple[Status, str]:
    """The named binaries, and anything in the denied-exec paths (the `resonant` CLI),
    can't be started."""
    targets = [p for p in DENIED_EXEC if os.path.exists(p)]
    targets += [str(p) for p in exec_candidates(ctx.params.deny_exec)]
    res: dict[str, Any] = ctx.snippet(_EXEC, targets)["res"]
    bad = {k: v for k, v in res.items() if not _denied(v)}
    return (
        ("fail", f"started or inconclusive: {_fmt(bad)}") if bad else ("pass", f"{len(res)} denied")
    )


def p_ports(ctx: ProbeContext) -> tuple[Status, str]:
    """The daemon API and Ollama ports are unreachable on loopback (v4 and v6), for connect
    and for bind, while another loopback port stays open."""
    with _listener() as (host, port):
        arg = {
            "connect": [[h, p] for h in ("127.0.0.1", "::1") for p in (API_PORT, OLLAMA_PORT)]
            + [[host, port]],
            "bind": [["127.0.0.1", API_PORT], ["0.0.0.0", OLLAMA_PORT]],  # noqa: S104
        }
        res: dict[str, Any] = ctx.snippet(_NET, arg)["res"]
    bad = [k for k, v in res.items() if f":{port}" not in k and not _denied(v)]
    open_ok = res.get(f"connect {host}:{port}") == "ok"
    if bad:
        return "fail", f"reachable or inconclusive: {_fmt(res, bad)}"
    if not open_ok:
        return "fail", f"a free loopback port was blocked too: {res.get(f'connect {host}:{port}')}"
    return "pass", f"{len(res) - 1} denied, loopback dev port open"


def p_bind_dev_server(ctx: ProbeContext) -> tuple[Status, str]:
    """A localhost dev server on another port works (http.server on a free port + a client)."""
    code = (
        "import http.server, threading, urllib.request\n"
        "srv = http.server.HTTPServer(('127.0.0.1', 0), http.server.SimpleHTTPRequestHandler)\n"
        "threading.Thread(target=srv.serve_forever, daemon=True).start()\n"
        "code = urllib.request.urlopen('http://127.0.0.1:%d/' % srv.server_port, timeout=5).status\n"
        "out(status=code)\n"
    )
    data = ctx.snippet(code)
    return ("pass", "served on loopback") if data["status"] == 200 else ("fail", f"{data}")


def p_unix_socket(ctx: ProbeContext) -> tuple[Status, str]:
    """A unix-socket connect is denied (stand-in for the Docker and ssh-agent sockets). The
    socket sits in the run dir, so file access can't be what says no."""
    path = ctx.params.run_dir / "tmp" / "s.sock"
    if len(str(path)) >= MAX_SOCKET_PATH:
        return "skip", "run dir path too long for a unix socket"
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(1)
    try:
        control = socket.socket(socket.AF_UNIX)
        control.connect(str(path))
        control.close()
        res: dict[str, Any] = ctx.snippet(_NET, {"unix": [str(path)]})["res"]
    finally:
        srv.close()
        path.unlink(missing_ok=True)
    got = res[f"unix {path}"]
    return ("pass", "denied") if _denied(got) else ("fail", f"connect gave {got}")


def p_mdns(ctx: ProbeContext) -> tuple[Status, str]:
    """mDNSResponder stays reachable (DNS), and HTTPS-relevant name lookup works."""
    if not os.path.exists(MDNS_SOCKET):
        return "skip", "no mDNSResponder socket"
    res: dict[str, Any] = ctx.snippet(_NET, {"unix": [MDNS_SOCKET]})["res"]
    got = res[f"unix {MDNS_SOCKET}"]
    return ("pass", "reachable") if got == "ok" else ("fail", f"connect gave {got}")


def p_mach_denied(ctx: ProbeContext) -> tuple[Status, str]:
    """Services that act on the user's behalf outside the sandbox can't be looked up, and
    the ones kept for TLS and DNS still can."""
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
    task ports, process info, signals."""
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
    """Signals to processes outside the sandbox are refused; the session's own children are
    still killable."""
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
    """A child in a new session is still sandboxed."""
    res = ctx.snippet(_SETSID, {"path": str(ctx.canary / "canary.txt")})["res"]
    return ("pass", "new-session child denied") if _denied(res) else ("fail", f"child gave {res}")


# local-only


def p_keychain(ctx: ProbeContext) -> tuple[Status, str]:
    """The Keychain is unreachable (securityd is denied), and a denied call fails fast."""
    control = ctx.snippet(_KEYCHAIN, bare=True)["res"]
    if control != 0:
        return "skip", f"control failed: keychain search list gives {control} outside"
    start = time.monotonic()
    res = ctx.snippet(_KEYCHAIN)["res"]
    ms = (time.monotonic() - start) * 1000
    if res == 0:
        return "fail", "SecKeychainCopySearchList succeeded"
    return "pass", f"denied (status {res}) in a {ms:.0f} ms run"


def p_launchd(ctx: ProbeContext) -> tuple[Status, str]:
    """A launchd job submission from Python is refused."""
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
    """The HID drivers (synthetic keyboard and mouse input) can't be opened."""
    control: dict[str, int] = ctx.snippet(_HID, bare=True)["res"]
    opened = [k for k, v in control.items() if v == 0]
    if not opened:
        return "skip", "control failed: IOHIDSystem can't be opened outside the sandbox either"
    res: dict[str, int] = ctx.snippet(_HID)["res"]
    bad = [k for k in opened if res[k] == 0]
    return (
        ("fail", f"IOHIDSystem client type {', '.join(bad)} opened")
        if bad
        else (
            "pass",
            f"client types {', '.join(opened)} refused",
        )
    )


def p_display(ctx: ProbeContext) -> tuple[Status, str]:
    """The window server is unreachable: no screenshot of the display."""
    control = ctx.snippet(_DISPLAY, bare=True)["res"]
    if control != "image":
        return "skip", "control failed: no display image outside the sandbox either"
    res = ctx.snippet(_DISPLAY)["res"]
    return ("fail", "captured the display") if res == "image" else ("pass", "no image")


def p_lan_ports(ctx: ProbeContext) -> tuple[Status, str]:
    """The API and Ollama ports are unreachable on the machine's LAN address too."""
    if ctx.lan_ip is None:
        return "skip", "no LAN address"
    arg = {
        "connect": [[ctx.lan_ip, API_PORT], [ctx.lan_ip, OLLAMA_PORT]],
        "bind": [[ctx.lan_ip, API_PORT]],
    }
    res: dict[str, Any] = ctx.snippet(_NET, arg)["res"]
    bad = {k: v for k, v in res.items() if not _denied(v)}
    return (
        ("fail", f"reachable or inconclusive: {_fmt(bad)}")
        if bad
        else ("pass", f"{ctx.lan_ip}: {len(res)} denied")
    )


def p_sockets(ctx: ProbeContext) -> tuple[Status, str]:
    """The Docker and ssh-agent sockets, where present, can't be connected to."""
    home = ctx.params.home
    candidates = [
        "/var/run/docker.sock",
        str(home / ".docker/run/docker.sock"),
        os.environ.get("SSH_AUTH_SOCK", ""),
    ]
    reachable: list[str] = []
    for path in candidates:
        if not path or not os.path.exists(path):
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
        return "skip", "no connectable Docker or ssh-agent socket"
    res: dict[str, Any] = ctx.snippet(_NET, {"unix": reachable})["res"]
    bad = {k: v for k, v in res.items() if not _denied(v)}
    return ("fail", f"connected: {_fmt(bad)}") if bad else ("pass", f"{len(reachable)} denied")


def p_volumes(ctx: ProbeContext) -> tuple[Status, str]:
    """Mounted volumes (Time Machine and external disks hold copies of ~/.resonant)."""
    entries = sorted(p for p in Path("/Volumes").iterdir()) if Path("/Volumes").is_dir() else []
    if not entries:
        return "skip", "no volumes"
    ops = [("list", "/Volumes"), *(("list", str(p)) for p in entries[:3])]
    res = _fs(ctx, ops)
    bad = {k: v for k, v in res.items() if not _denied(v)}
    return ("fail", f"readable: {_fmt(bad)}") if bad else ("pass", f"{len(res)} denied")


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
    Probe("file read denies", "ci", p_read_denied),
    Probe("file write denies", "ci", p_write_denied),
    Probe("run dir and cache allows", "ci", p_run_dir),
    Probe("work dir and sibling run dirs", "ci", p_work_dir),
    Probe("other processes' environment", "ci", p_other_env),
    Probe("exec denies", "ci", p_exec_denied),
    Probe("ports 7777 and 11434 on loopback", "ci", p_ports),
    Probe("loopback dev server", "ci", p_bind_dev_server),
    Probe("unix socket connect", "ci", p_unix_socket),
    Probe("mDNSResponder socket", "ci", p_mdns),
    Probe("mach lookups", "ci", p_mach_denied),
    Probe("operations (AppleEvents, LaunchServices, task ports)", "ci", p_operations),
    Probe("task_for_pid on another process", "ci", p_task_ports),
    Probe("signals to other processes", "ci", p_signal),
    Probe("environment", "ci", p_env),
    Probe("new-session child stays sandboxed", "ci", p_setsid_child),
    Probe("keychain", "local", p_keychain),
    Probe("launchd job submit", "local", p_launchd),
    Probe("HID synthetic input", "local", p_hid),
    Probe("window server (screenshot)", "local", p_display),
    Probe("ports on the LAN address", "local", p_lan_ports),
    Probe("docker and ssh-agent sockets", "local", p_sockets),
    Probe("/Volumes", "local", p_volumes),
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
        except (OSError, KeyError, ValueError, json.JSONDecodeError) as e:
            status, detail = "fail", f"probe error: {type(e).__name__}: {e}"
        results.append(
            ProbeResult(
                probe.name, probe.scope, status, detail, round((time.monotonic() - start) * 1000)
            )
        )
    return results


@contextlib.contextmanager
def _listener() -> Iterator[tuple[str, int]]:
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
    lines.append(f"{len(results) - fails - skips} passed, {fails} failed, {skips} skipped")
    return "\n".join(lines)


# --- per-run self-check ----------------------------------------------------------------------


def self_check(params: SandboxParams, *, python: str | None = None) -> tuple[bool, str]:
    """The cheap fail-closed check before a session: a denied read of ``~/.resonant`` (or
    ``$HOME``), a denied mach lookup of securityd and a denied connect to the API port.
    One sandboxed interpreter start, about 50 to 100 ms."""
    if not sandbox_exec_available():
        return False, "sandbox-exec is not available"
    p = params.resolved()
    interpreter = python or os.path.realpath(getattr(sys, "_base_executable", sys.executable))
    code = _PRELUDE + _SELF
    check_params = SandboxParams(
        run_dir=p.run_dir,
        cache_dir=p.cache_dir,
        home=p.home,
        read_paths=(*p.read_paths, Path(sys.base_prefix), Path(interpreter).parent),
        deny_exec=p.deny_exec,
    )
    argv = [interpreter, "-I", "-c", code, json.dumps({"home": str(p.home), "port": API_PORT})]
    try:
        proc = subprocess.run(  # noqa: S603 - argv is built here
            wrap(argv, check_params),
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
    problems = []
    if not _denied(res["read"]):
        problems.append(f"read of {p.home} gave {res['read']}")
    if res["mach"] != BOOTSTRAP_NOT_PRIVILEGED:
        problems.append(f"securityd lookup gave {res['mach']}")
    if not _denied(res["net"]):
        problems.append(f"connect to :{API_PORT} gave {res['net']}")
    if problems:
        return False, "; ".join(problems)
    return True, "read, keychain lookup and API port denied"


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
