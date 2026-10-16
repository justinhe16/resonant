"""The extension process runner (P3-4): templating, scratch env, caps, tree kills, fd-3 events.

The run tests spawn small local Python helpers from ``tmp_path`` (stdlib only) to exercise
real process groups, caps and fd 3. Nothing touches the system beyond those helpers.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import psutil
import pytest

from resonant.executor import ToolFailed
from resonant.extensions.process import (
    SCRATCH_PATH,
    ExtAmbiguous,
    ExtCommand,
    ExtFailed,
    ExtOutput,
    ExtProcess,
    ExtRequest,
    FakeExtProcess,
    ProcessTracker,
    TemplateError,
    ensure_data_dir,
    parse_event,
    scratch_env,
)
from resonant.secrets import FakeKeychain, SecretStore

# Placeholder secret values (not real credentials).
FAKE_A = "fake" + "-a" * 4
FAKE_B = "fake" + "-b" * 4


def same(text: str) -> str:
    return text


# --- templating --------------------------------------------------------------------------

GH = "/opt/homebrew/bin/gh"  # a pinned path; templating never touches the file


def cc(run: str) -> ExtCommand:
    return ExtCommand.compile(run, root=Path(__file__).parent, argv0=GH)


@pytest.mark.parametrize(
    ("run", "args", "argv"),
    [
        ("gh time --tz {tz}", {"tz": "Asia/Tokyo"}, [GH, "time", "--tz", "Asia/Tokyo"]),
        ("gh note --text {t}", {"t": "a b  c"}, [GH, "note", "--text", "a b  c"]),
        ("gh {t}", {"t": "x; rm -rf ~"}, [GH, "x; rm -rf ~"]),
        ("gh {t}", {"t": "$(id) `id` $HOME"}, [GH, "$(id) `id` $HOME"]),
        ("gh {t}", {"t": "'quoted' \"too\""}, [GH, "'quoted' \"too\""]),
        ("gh {t}", {"t": "line1\nline2"}, [GH, "line1\nline2"]),
        ("gh {t}", {"t": "a|b>c&&d"}, [GH, "a|b>c&&d"]),
        ("gh {t}", {"t": ""}, [GH, ""]),
        ("gh {t}", {"t": "{other}"}, [GH, "{other}"]),
        ("gh --tz={tz}", {"tz": "-05:00"}, [GH, "--tz=-05:00"]),
        ("gh {a}{b}", {"a": "x y", "b": "-z"}, [GH, "x y-z"]),
        ("gh '{a} {b}'", {"a": "1", "b": "2"}, [GH, "1 2"]),
        ("gh {{literal}} {a}", {"a": "v"}, [GH, "{literal}", "v"]),
        ("gh {n} {f} {b}", {"n": 3, "f": 1.5, "b": True}, [GH, "3", "1.5", "true"]),
        ("gh {a}", {"a": "v", "extra": "ignored"}, [GH, "v"]),
    ],
)
def test_render_substitutes_inside_tokens(run: str, args: dict[str, Any], argv: list[str]) -> None:
    assert list(cc(run).render(args)) == argv


@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({"t": "-rf"}, "may not start with '-'"),
        ({"t": "--help"}, "may not start with '-'"),
        ({"t": -3}, "may not start with '-'"),
        ({"t": "a\x00b"}, "NUL"),
        ({"t": "x" * 1001}, "over 1000"),
        ({"t": None}, "string, number or boolean"),
        ({"t": ["a", "b"]}, "string, number or boolean"),
        ({"t": float("nan")}, "finite"),
        ({}, "missing arg t"),
        ({"t": 10**5000}, "over 1000"),
    ],
)
def test_render_refuses_bad_values(args: dict[str, Any], message: str) -> None:
    with pytest.raises(ToolFailed, match=message) as info:
        cc("gh note {t}").render(args)
    assert "rm" not in str(info.value)


@pytest.mark.parametrize(
    ("run", "args"),
    [
        ("gh {a}{b}", {"a": "", "b": "--evil"}),
        ("gh {a}{b}", {"a": "-", "b": "x"}),
        ("gh {a}x", {"a": "-o"}),
    ],
)
def test_split_placeholders_cant_smuggle_an_option(run: str, args: dict[str, Any]) -> None:
    with pytest.raises(ToolFailed, match="may not start with '-'"):
        cc(run).render(args)


def test_render_never_echoes_the_value() -> None:
    with pytest.raises(ToolFailed) as info:
        cc("gh {t}").render({"t": "-secret-value"})
    assert "secret-value" not in str(info.value)


def test_render_allows_max_length_and_dash_inside_token() -> None:
    cmd = cc("gh --text={t} {u}")
    assert cmd.render({"t": "-x", "u": "y" * 1000})[1:] == ("--text=-x", "y" * 1000)
    assert cmd.placeholders == {"t", "u"}


@pytest.mark.parametrize(
    "run",
    [
        "",
        "   ",
        "{cmd} arg",
        "gh{x} arg",
        "gh {a:>3}",
        "gh {a!r}",
        "gh {a.b}",
        "gh {0}",
        "gh 'unterminated",
        "python3 {script}",
        "python3 -c print(1)",
        "python3",
        "npx some-pkg",
        "uv run x",
        "sh -c {x}",
        "/usr/bin/env npx x",
        "curl {url}",
        "ls -la",
    ],
)
def test_compile_refuses(run: str) -> None:
    with pytest.raises(TemplateError):
        cc(run)


def ext_root(tmp_path: Path) -> Path:
    (tmp_path / "cli.py").write_text("print('{}')\n")
    run_sh = tmp_path / "run.sh"
    run_sh.write_text("#!/usr/bin/env npx\nsome-package\n")
    run_sh.chmod(0o755)
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    return tmp_path


def test_in_dir_python_runs_isolated_and_ignores_the_shebang(tmp_path: Path) -> None:
    root = ext_root(tmp_path)
    cmd = ExtCommand.compile("./cli.py ping", root=root, python="/py", argv0="/other")
    assert cmd.render({}) == ("/py", "-I", "-S", "./cli.py", "ping")
    cmd = ExtCommand.compile("python3 cli.py {a}", root=root, python="/py")
    assert cmd.render({"a": "v"}) == ("/py", "-I", "-S", "cli.py", "v")


@pytest.mark.parametrize("run", ["./run.sh", "./tool --x", "./missing.py", "../cli.py"])
def test_other_in_dir_executables_are_refused(tmp_path: Path, run: str) -> None:
    root = ext_root(tmp_path)
    with pytest.raises(TemplateError):
        ExtCommand.compile(run, root=root, argv0="/usr/bin/true")


def test_gh_and_git_run_only_by_their_pinned_path(tmp_path: Path) -> None:
    cmd = ExtCommand.compile("gh pr list --repo {r}", root=tmp_path, argv0=GH)
    assert cmd.render({"r": "a/b"}) == (GH, "pr", "list", "--repo", "a/b")
    git = ExtCommand.compile("git status", root=tmp_path, argv0="/usr/bin/git")
    assert git.render({})[0] == "/usr/bin/git"
    for argv0 in (None, "gh", "bin/gh", "/usr/bin/git"):
        with pytest.raises(TemplateError, match="pinned absolute path"):
            ExtCommand.compile("gh pr list", root=tmp_path, argv0=argv0)


# --- environment -------------------------------------------------------------------------


def test_scratch_env_is_exactly_the_allowlist(tmp_path: Path) -> None:
    env = scratch_env(
        ext="example",
        data_dir=tmp_path,
        secrets={"RESONANT_SECRET_TOKEN": FAKE_A, "RESONANT_SECRET_API_KEY": FAKE_B},
        job_due_at="2026-10-14T08:00:00+00:00",
        user="justin",
        bins={"RESONANT_BIN_GH": "/opt/homebrew/Cellar/gh/2.0/bin/gh"},
    )
    assert env == {
        "PATH": SCRATCH_PATH,
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path / "tmp"),
        "LANG": "en_US.UTF-8",
        "USER": "justin",
        "LOGNAME": "justin",
        "RESONANT_EXT": "example",
        "RESONANT_EXT_DATA": str(tmp_path),
        "RESONANT_EVENTS_FD": "3",
        "GIT_TERMINAL_PROMPT": "0",
        "RESONANT_JOB_DUE_AT": "2026-10-14T08:00:00+00:00",
        "RESONANT_BIN_GH": "/opt/homebrew/Cellar/gh/2.0/bin/gh",
        "RESONANT_SECRET_TOKEN": FAKE_A,
        "RESONANT_SECRET_API_KEY": FAKE_B,
    }
    for bad in ({"PATH": "/x"}, {"RESONANT_BIN_GH": "gh"}, {"RESONANT_BIN_": "/x"}):
        with pytest.raises(ValueError, match="pinned bin"):
            scratch_env(ext="e", data_dir=tmp_path, bins=bad)
    plain = scratch_env(ext="example", data_dir=tmp_path)
    assert "RESONANT_JOB_DUE_AT" not in plain
    assert not [k for k in plain if k.startswith("RESONANT_SECRET_")]
    assert plain["USER"]


@pytest.mark.parametrize(
    "key",
    [
        "token",
        "RESONANT_SECRET_token",
        "RESONANT_SECRET_",
        "RESONANT_SECRET_1X",
        "RESONANT_SECRET_A-B",
        "RESONANT_SECRET_" + "X" * 33,
        "RESONANT_SECRET_TOKEN\n",
        "PATH",
        "HOME",
    ],
)
def test_secret_keys_are_secret_env_names(key: str, tmp_path: Path) -> None:
    # Only RESONANT_SECRET_<NAME> keys (SecretStore.env_for), so a secret can't replace PATH.
    with pytest.raises(ValueError):
        scratch_env(ext="e", data_dir=tmp_path, secrets={key: FAKE_A})


async def test_env_for_output_plugs_straight_in(tmp_path: Path) -> None:
    store = SecretStore(FakeKeychain())
    await store.set("example/token", FAKE_A)
    env = scratch_env(
        ext="example", data_dir=tmp_path, secrets=await store.env_for("example", ["token"])
    )
    assert env["RESONANT_SECRET_TOKEN"] == FAKE_A


@pytest.mark.parametrize("value", ["", "a\x00b"])
def test_secret_values_are_checked(value: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        scratch_env(ext="e", data_dir=tmp_path, secrets={"RESONANT_SECRET_TOKEN": value})


def test_ensure_data_dir_creates_private_tmp(tmp_path: Path) -> None:
    ensure_data_dir(tmp_path / "data")
    assert (tmp_path / "data" / "tmp").is_dir()
    assert (tmp_path / "data" / "tmp").stat().st_mode & 0o077 == 0


# --- running -----------------------------------------------------------------------------


def helper(tmp_path: Path, name: str, body: str) -> Path:
    path = tmp_path / name
    path.write_text("import json, os, sys, time, subprocess, signal\n" + body)
    return path


def request(
    tmp_path: Path, script: Path, *args: str, timeout_s: float = 10.0, **env: str
) -> ExtRequest:
    data = tmp_path / "data"
    ensure_data_dir(data)
    base = scratch_env(ext="example", data_dir=data)
    return ExtRequest(
        argv=(sys.executable, "-I", "-S", str(script), *args),
        cwd=tmp_path,
        env={**base, **env},
        timeout_s=timeout_s,
        label="example/test",
    )


def runner(**kw: Any) -> ExtProcess:
    return ExtProcess(ProcessTracker(sample_s=0.05, grace_s=0.5), **kw)


def gone(pid: int) -> bool:
    try:
        return psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return True


def real(path: str | Path) -> str:
    return os.path.realpath(path)


def read_if_any(path: Path) -> str:
    return path.read_text().strip() if path.exists() else ""


async def wait_for_file(path: Path, within: float = 5.0) -> str:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if text := read_if_any(path):
            return text
        await asyncio.sleep(0.02)
    raise AssertionError(f"{path} never appeared")


async def wait_gone(pid: int, within: float = 3.0) -> bool:
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if gone(pid):
            return True
        await asyncio.sleep(0.02)
    return False


async def test_success_returns_the_json_object(tmp_path: Path) -> None:
    script = helper(tmp_path, "ok.py", "print(json.dumps({'a': 1, 'argv': sys.argv[1:]}))")
    out = await runner().run(request(tmp_path, script, "x; y", "$(id)"), redact=same)
    assert out.data == {"a": 1, "argv": ["x; y", "$(id)"]}
    assert out.events == ()
    assert out.duration_ms >= 0


async def test_env_has_nothing_from_the_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RESONANT_LEAK_SENTINEL", "leak-me-0xdeadbeef")
    monkeypatch.setenv("PYTHONPATH", "/nonexistent")
    # /usr/bin/env, not Python: a Python child adds keys of its own at startup on macOS.
    base = request(tmp_path, tmp_path / "unused.py")
    req = ExtRequest(("/usr/bin/env",), tmp_path, base.env, 10)
    raw = await runner().run_raw(req, redact=same)
    assert raw.returncode == 0
    seen = dict(line.split("=", 1) for line in raw.stdout.splitlines())
    assert seen == dict(req.env)
    assert "leak-me" not in raw.stdout


async def test_secrets_reach_only_the_call_that_declares_them(tmp_path: Path) -> None:
    script = helper(tmp_path, "env.py", "print(json.dumps(dict(os.environ)))")
    data = tmp_path / "data"
    ensure_data_dir(data)
    with_secret = ExtRequest(
        argv=(sys.executable, "-I", "-S", str(script)),
        cwd=tmp_path,
        env=scratch_env(ext="e", data_dir=data, secrets={"RESONANT_SECRET_TOKEN": FAKE_A}),
        timeout_s=10,
    )
    without = ExtRequest(with_secret.argv, tmp_path, scratch_env(ext="e", data_dir=data), 10)
    ext = runner()
    got = (await ext.run(with_secret, redact=same)).data
    assert got["RESONANT_SECRETS_FD"] == "4" and FAKE_A not in json.dumps(got)
    assert not [k for k in (await ext.run(without, redact=same)).data if "SECRET" in k]


async def test_cwd_and_stdin(tmp_path: Path) -> None:
    script = helper(
        tmp_path, "cwd.py", "print(json.dumps({'cwd': os.getcwd(), 'stdin': sys.stdin.read()}))"
    )
    out = await runner().run(request(tmp_path, script), redact=same)
    assert real(out.data["cwd"]) == real(tmp_path)
    assert out.data["stdin"] == ""


@pytest.mark.parametrize("argv0", ["true", "./tool", "tool", "/no/such/binary"])
async def test_runtime_argv0_must_be_an_absolute_executable(tmp_path: Path, argv0: str) -> None:
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    base = request(tmp_path, tool)
    with pytest.raises(ExtFailed, match="absolute path"):
        await runner().run_raw(ExtRequest((argv0,), tmp_path, base.env, 10), redact=same)


async def test_nonzero_exit_is_tool_failed_and_stderr_is_never_returned(tmp_path: Path) -> None:
    script = helper(
        tmp_path,
        "fail.py",
        "sys.stderr.write('x' * 20000 + ' token=SENSITIVE-123 tail')\nsys.exit(3)",
    )
    with pytest.raises(ExtFailed) as info:
        await runner().run(
            request(tmp_path, script), redact=lambda s: s.replace("SENSITIVE-123", "[redacted]")
        )
    assert isinstance(info.value, ToolFailed)
    assert str(info.value) == "exit 3"
    assert len(info.value.stderr_tail) <= 200
    assert info.value.stderr_tail.endswith("token=[redacted] tail")
    assert "SENSITIVE" not in info.value.stderr_tail


async def test_success_carries_no_stderr(tmp_path: Path) -> None:
    script = helper(
        tmp_path, "noisy.py", "sys.stderr.write('e' * 50000)\nprint(json.dumps({'ok': 1}))"
    )
    out = await runner().run(request(tmp_path, script), redact=same)
    assert out.data == {"ok": 1}
    assert "stderr" not in ExtOutput.__dataclass_fields__


@pytest.mark.parametrize(
    "body",
    ["print('not json')", "print('[1, 2]')", 'print(\'{"a": 1}{"b": 2}\')', "pass"],
)
async def test_exit_zero_without_one_object_is_ambiguous(tmp_path: Path, body: str) -> None:
    script = helper(tmp_path, "bad.py", body)
    with pytest.raises(ExtAmbiguous, match="invalid output") as info:
        await runner().run(request(tmp_path, script), redact=same)
    assert not isinstance(info.value, ToolFailed)


async def test_death_by_signal_is_ambiguous(tmp_path: Path) -> None:
    script = helper(tmp_path, "sig.py", "os.kill(os.getpid(), signal.SIGKILL)")
    with pytest.raises(ExtAmbiguous, match="signal 9"):
        await runner().run(request(tmp_path, script), redact=same)


async def test_redaction_runs_before_parsing(tmp_path: Path) -> None:
    script = helper(
        tmp_path,
        "leak.py",
        "tok = json.load(os.fdopen(4, 'rb'))['token']\n"
        "print(json.dumps({'echo': tok}))\n"
        "os.write(3, (json.dumps({'type': 'x', 'text': tok, 'dedupe_key': 'k'}) + '\\n').encode())",
    )
    req = request(tmp_path, script, RESONANT_SECRET_TOKEN=FAKE_B)
    out = await runner().run(req, redact=lambda s: s.replace(FAKE_B, "[redacted]"))
    assert out.data == {"echo": "[redacted]"}
    assert out.events[0].text == "[redacted]"


async def test_stdout_cap_kills_the_tree(tmp_path: Path) -> None:
    pidfile = tmp_path / "child.pid"
    script = helper(
        tmp_path,
        "big.py",
        f"c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "sys.stdout.write('x' * 300_000)\nsys.stdout.flush()\ntime.sleep(60)",
    )
    started = time.monotonic()
    with pytest.raises(ExtAmbiguous, match="output too large") as info:
        await runner().run(request(tmp_path, script, timeout_s=30), redact=same)
    assert not isinstance(info.value, ToolFailed)
    assert time.monotonic() - started < 10
    assert await wait_gone(int(pidfile.read_text()))


async def test_small_caps_are_configurable(tmp_path: Path) -> None:
    script = helper(tmp_path, "mid.py", "print(json.dumps({'x': 'y' * 2000}))")
    with pytest.raises(ExtAmbiguous, match="output too large"):
        await runner(stdout_cap=1024).run(request(tmp_path, script), redact=same)
    assert (await runner().run(request(tmp_path, script), redact=same)).data["x"]


GRANDCHILDREN = """
pids = sys.argv[1]
same_group = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
detached = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True
)
open(pids, 'w').write(f'{same_group.pid} {detached.pid}')
time.sleep(60)
"""


async def test_timeout_kills_the_group_and_setsid_grandchildren(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = helper(tmp_path, "tree.py", GRANDCHILDREN)
    task = asyncio.create_task(
        runner().run(request(tmp_path, script, str(pids), timeout_s=2), redact=same)
    )
    same_group, detached = map(int, (await wait_for_file(pids)).split())
    assert os.getpgid(detached) != os.getpgid(same_group)  # it really left the group
    with pytest.raises(ExtAmbiguous, match="timeout"):
        await task
    assert await wait_gone(same_group)
    assert await wait_gone(detached)


async def test_kill_all_ends_running_processes(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = helper(tmp_path, "tree.py", GRANDCHILDREN)
    ext = runner()
    task = asyncio.create_task(
        ext.run(request(tmp_path, script, str(pids), timeout_s=30), redact=same)
    )
    same_group, detached = map(int, (await wait_for_file(pids)).split())
    await asyncio.sleep(0.15)  # at least one sample after the grandchildren started
    assert ext.tracker.live == 1
    assert await ext.tracker.kill_all() == 1
    with pytest.raises(ExtAmbiguous, match="killed"):
        await task
    assert ext.tracker.live == 0
    assert await wait_gone(same_group)
    assert await wait_gone(detached)


async def test_cancel_kills_the_tree_and_propagates(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = helper(tmp_path, "tree.py", GRANDCHILDREN)
    ext = runner()
    task = asyncio.create_task(
        ext.run(request(tmp_path, script, str(pids), timeout_s=30), redact=same)
    )
    same_group, detached = map(int, (await wait_for_file(pids)).split())
    await asyncio.sleep(0.15)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert ext.tracker.live == 0
    assert await wait_gone(same_group)
    assert await wait_gone(detached)


async def test_term_is_tried_before_kill(tmp_path: Path) -> None:
    marker = tmp_path / "termed"
    script = helper(
        tmp_path,
        "graceful.py",
        f"signal.signal(signal.SIGTERM, lambda *_: (open({str(marker)!r}, 'w').write('1'),"
        " sys.exit(0)))\nprint('ready', file=sys.stderr, flush=True)\ntime.sleep(60)",
    )
    with pytest.raises(ExtAmbiguous, match="timeout"):
        await runner().run(request(tmp_path, script, timeout_s=1), redact=same)
    assert marker.read_text() == "1"


async def test_leftovers_are_killed_after_a_normal_exit(tmp_path: Path) -> None:
    pidfile = tmp_path / "bg.pid"
    script = helper(
        tmp_path,
        "bg.py",
        "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'],"
        " stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"open({str(pidfile)!r}, 'w').write(str(c.pid))\n"
        "print(json.dumps({'ok': True}))",
    )
    out = await runner().run(request(tmp_path, script), redact=same)
    assert out.data == {"ok": True}
    assert await wait_gone(int(pidfile.read_text()))


ESCAPEE = """
pid = sys.argv[1]
c = subprocess.Popen(
    [sys.executable, '-c', 'import time; time.sleep(60)'], start_new_session=True{redirect}
)
open(pid, 'w').write(str(c.pid))
print(json.dumps({{'ok': True}}))
"""


def kill_pid(pid: int) -> None:
    with contextlib.suppress(psutil.Error):
        psutil.Process(pid).kill()


@pytest.mark.parametrize("timeout_s", [10.0, 0.5])
async def test_untracked_escapee_holding_stdout_never_hangs_the_run(
    tmp_path: Path, timeout_s: float
) -> None:
    pidfile = tmp_path / "esc.pid"
    script = helper(tmp_path, "esc.py", ESCAPEE.format(redirect=""))
    if timeout_s < 1:  # the leader also outlives the timeout
        script.write_text(script.read_text() + "time.sleep(60)\n")
    # A slow sampler: the escapee is never tracked, the worst case.
    ext = ExtProcess(ProcessTracker(sample_s=30, grace_s=0.3))
    started = time.monotonic()
    try:
        with pytest.raises(ExtAmbiguous, match=r"output still open|timeout"):
            await ext.run(request(tmp_path, script, str(pidfile), timeout_s=timeout_s), redact=same)
        assert time.monotonic() - started < 5
        assert ext.tracker.live == 0
    finally:
        kill_pid(int(read_if_any(pidfile) or 0) or -1)


async def test_escapee_with_closed_pipes_does_not_block_success(tmp_path: Path) -> None:
    pidfile = tmp_path / "esc.pid"
    redirect = ", stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=()"
    script = helper(tmp_path, "esc.py", ESCAPEE.format(redirect=redirect))
    ext = ExtProcess(ProcessTracker(sample_s=30, grace_s=0.3))
    try:
        started = time.monotonic()
        out = await ext.run(request(tmp_path, script, str(pidfile)), redact=same)
        assert out.data == {"ok": True}
        assert time.monotonic() - started < 5
    finally:
        kill_pid(int(read_if_any(pidfile) or 0) or -1)


async def test_double_cancel_still_kills_a_term_ignoring_tree(tmp_path: Path) -> None:
    pidfile = tmp_path / "pid"
    script = helper(
        tmp_path,
        "stubborn.py",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\ntime.sleep(60)",
    )
    ext = ExtProcess(ProcessTracker(sample_s=0.05, grace_s=1.0))
    task = asyncio.create_task(ext.run(request(tmp_path, script, timeout_s=30), redact=same))
    pid = int(await wait_for_file(pidfile))
    task.cancel()
    await asyncio.sleep(0.1)  # inside terminate()'s grace
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await wait_gone(pid)


async def test_paused_tracker_refuses_new_runs(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = helper(tmp_path, "tree.py", GRANDCHILDREN)
    ext = runner()
    task = asyncio.create_task(
        ext.run(request(tmp_path, script, str(pids), timeout_s=30), redact=same)
    )
    await wait_for_file(pids)
    assert await ext.tracker.kill_all(hold=True) == 1
    with pytest.raises(ExtAmbiguous, match="killed"):
        await task
    ok = helper(tmp_path, "ok.py", "print('{}')")
    with pytest.raises(ExtFailed, match="paused"):
        await ext.run(request(tmp_path, ok), redact=same)
    ext.tracker.paused = False
    assert (await ext.run(request(tmp_path, ok), redact=same)).data == {}


async def test_deeply_nested_output_is_invalid_not_a_crash(tmp_path: Path) -> None:
    script = helper(tmp_path, "deep.py", "sys.stdout.write('[' * 200000)")
    with pytest.raises(ExtAmbiguous, match="invalid output"):
        await runner().run(request(tmp_path, script), redact=same)


# --- fd-3 events -------------------------------------------------------------------------


async def test_fd3_events_are_parsed_validated_and_merged(tmp_path: Path) -> None:
    script = helper(
        tmp_path,
        "events.py",
        "fd = int(os.environ['RESONANT_EVENTS_FD'])\n"
        "assert fd == 3\n"
        "def emit(obj): os.write(3, (json.dumps(obj) + '\\n').encode())\n"
        "emit({'type': 'ci_failed', 'text': 'red', 'dedupe_key': 'r#1:run:9:failed'})\n"
        "emit({'type': 'ping', 'text': 'hi', 'dedupe_key': 'p', 'data': {'n': 1}})\n"
        "emit({'type': 'Bad Type', 'text': 'x', 'dedupe_key': 'k'})\n"
        "emit({'type': 'ok', 'text': 'x', 'dedupe_key': 'k' * 201})\n"
        "emit({'type': 'ok', 'text': 'x'})\n"
        "os.write(3, b'not json\\n\\n')\n"
        "print(json.dumps({'summary': 's', 'events': [{'type': 'digest', 'text': 'd',"
        " 'dedupe_key': 'd1'}, 'junk']}))",
    )
    out = await runner().run(request(tmp_path, script), redact=same)
    assert out.data == {"summary": "s"}
    assert [(e.type, e.dedupe_key) for e in out.events] == [
        ("digest", "d1"),
        ("ci_failed", "r#1:run:9:failed"),
        ("ping", "p"),
    ]
    assert out.events[2].data == {"n": 1}
    assert out.dropped_events == 5


async def test_fd3_event_count_is_capped(tmp_path: Path) -> None:
    script = helper(
        tmp_path,
        "many.py",
        "for i in range(150):\n"
        "    os.write(3, (json.dumps({'type': 't', 'text': 'x', 'dedupe_key': str(i)}) + '\\n')"
        ".encode())\n"
        "print('{}')",
    )
    out = await runner().run(request(tmp_path, script), redact=same)
    assert len(out.events) == 100
    assert out.dropped_events == 50


async def test_fd3_bytes_are_capped(tmp_path: Path) -> None:
    script = helper(
        tmp_path,
        "flood.py",
        "line = (json.dumps({'type': 't', 'text': 'x' * 900, 'dedupe_key': 'k'}) + '\\n')"
        ".encode()\n"
        "for _ in range(400):\n    os.write(3, line)\n"
        "print('{}')\ntime.sleep(60)",
    )
    with pytest.raises(ExtAmbiguous, match="events too large"):
        await runner().run(request(tmp_path, script, timeout_s=30), redact=same)


async def test_events_from_a_failed_run_are_not_returned(tmp_path: Path) -> None:
    script = helper(
        tmp_path,
        "failev.py",
        "os.write(3, (json.dumps({'type': 't', 'text': 'x', 'dedupe_key': 'k'}) + '\\n')"
        ".encode())\nsys.exit(2)",
    )
    with pytest.raises(ExtFailed, match="exit 2"):
        await runner().run(request(tmp_path, script), redact=same)


def test_parse_event_shapes() -> None:
    ok = {"type": "ci_fixed", "text": "t" * 5000, "dedupe_key": "k"}
    event = parse_event(ok)
    assert event is not None and len(event.text) == 1000 and event.data is None
    assert parse_event({**ok, "extra": 1}) is None
    assert parse_event({**ok, "data": [1]}) is None
    assert parse_event({**ok, "dedupe_key": ""}) is None
    assert parse_event({**ok, "text": 3}) is None
    assert parse_event("string") is None
    assert parse_event({**ok, "type": "ci\n"}) is None


# --- fake --------------------------------------------------------------------------------


async def test_fake_records_and_scripts(tmp_path: Path) -> None:
    req = ExtRequest(("./cli", "ping"), tmp_path, {"PATH": SCRATCH_PATH}, 5, "example/ping")
    fake = FakeExtProcess(results=[ExtOutput({"a": 1}, (), 0, 3), ExtFailed("exit 1")])
    assert (await fake.run(req, redact=same)).data == {"a": 1}
    with pytest.raises(ExtFailed):
        await fake.run(req, redact=same)
    raw = await fake.run_raw(req, redact=same)
    assert raw.returncode == 1
    assert fake.calls == [req, req, req]
    responder = FakeExtProcess(respond=lambda r: ExtOutput({"argv": list(r.argv)}, (), 0, 0))
    assert (await responder.run(req, redact=same)).data == {"argv": ["./cli", "ping"]}


# --- secrets are delivered on fd 4, never in env or argv (P5A-7) ---------------------------


def secret_request(tmp_path: Path, script: Path, *, timeout_s: float = 10.0) -> ExtRequest:
    data = tmp_path / "data"
    ensure_data_dir(data)
    env = scratch_env(
        ext="e",
        data_dir=data,
        secrets={"RESONANT_SECRET_TOKEN": FAKE_A, "RESONANT_SECRET_API_KEY": FAKE_B},
    )
    return ExtRequest((sys.executable, "-I", "-S", str(script)), tmp_path, env, timeout_s, "e/t")


def open_fds() -> int:
    return psutil.Process().num_fds()


async def test_secrets_are_read_from_fd_4_and_not_in_env_or_argv(tmp_path: Path) -> None:
    script = helper(
        tmp_path,
        "s.py",
        "fd = int(os.environ['RESONANT_SECRETS_FD'])\n"
        "got = json.load(os.fdopen(fd, 'rb'))\n"
        "print(json.dumps({'got': got, 'fd': fd, 'env': dict(os.environ), 'argv': sys.argv}))",
    )
    out = await runner().run(secret_request(tmp_path, script), redact=same)
    assert out.data["got"] == {"token": FAKE_A, "api_key": FAKE_B}
    assert out.data["fd"] == 4
    blob = json.dumps({"env": out.data["env"], "argv": out.data["argv"]})
    assert FAKE_A not in blob and FAKE_B not in blob
    assert not [k for k in out.data["env"] if k.startswith("RESONANT_SECRET_")]


async def test_a_live_process_shows_no_secret_in_its_env_or_argv(tmp_path: Path) -> None:
    """What KERN_PROCARGS2 would show a same-user observer, via psutil, while it runs."""
    pidfile = tmp_path / "pid"
    script = helper(
        tmp_path,
        "live.py",
        f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n"
        "tok = json.load(os.fdopen(4, 'rb'))['token']\n"
        "time.sleep(60)",
    )
    ext = runner()
    task = asyncio.create_task(ext.run(secret_request(tmp_path, script), redact=same))
    pid = int(await wait_for_file(pidfile))
    seen = psutil.Process(pid)
    blob = json.dumps(seen.environ()) + json.dumps(seen.cmdline())
    assert FAKE_A not in blob and FAKE_B not in blob
    await ext.tracker.kill_all("test")
    with pytest.raises(ExtAmbiguous):
        await task


async def test_no_secrets_means_no_fd_4(tmp_path: Path) -> None:
    script = helper(
        tmp_path,
        "n.py",
        "try:\n    os.fstat(4); has = True\nexcept OSError:\n    has = False\n"
        "print(json.dumps({'has': has, 'env': 'RESONANT_SECRETS_FD' in os.environ}))",
    )
    out = await runner().run(request(tmp_path, script), redact=same)
    assert out.data == {"has": False, "env": False}


async def test_secrets_pipe_is_closed_in_the_daemon_after_every_ending(tmp_path: Path) -> None:
    ok = helper(tmp_path, "ok.py", "print('{}')")
    hang = helper(tmp_path, "hang.py", "time.sleep(60)")
    ext = runner()
    before = open_fds()
    await ext.run(secret_request(tmp_path, ok), redact=same)  # never reads fd 4
    with pytest.raises(ExtAmbiguous, match="timeout"):
        await ext.run(secret_request(tmp_path, hang, timeout_s=0.5), redact=same)
    task = asyncio.create_task(ext.run(secret_request(tmp_path, hang), redact=same))
    await asyncio.sleep(0.5)
    await ext.tracker.kill_all("test")
    with pytest.raises(ExtAmbiguous):
        await task
    task = asyncio.create_task(ext.run(secret_request(tmp_path, hang), redact=same))
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert open_fds() == before


async def test_a_failed_start_leaves_no_secrets_fd(tmp_path: Path) -> None:
    req = secret_request(tmp_path, tmp_path / "x.py")
    bad = ExtRequest(("relative",), req.cwd, req.env, 5, "e/t")
    before = open_fds()
    with pytest.raises(ExtFailed):
        await runner().run(bad, redact=same)
    broken = ExtRequest((sys.executable,), tmp_path / "missing-cwd", req.env, 5, "e/t")
    with pytest.raises(ExtFailed):
        await runner().run(broken, redact=same)
    assert open_fds() == before


async def test_oversized_secrets_are_refused_without_starting(tmp_path: Path) -> None:
    script = helper(tmp_path, "ok.py", "print('{}')")
    req = secret_request(tmp_path, script)
    big = ExtRequest(req.argv, req.cwd, {**req.env, "RESONANT_SECRET_BIG": "x" * 9000}, 5, "e/t")
    before = open_fds()
    with pytest.raises(ExtFailed, match="too large"):
        await runner().run(big, redact=same)
    assert open_fds() == before


async def test_a_grandchild_does_not_inherit_the_secrets_fd(tmp_path: Path) -> None:
    script = helper(
        tmp_path,
        "g.py",
        "r = subprocess.run([sys.executable, '-c', 'import os\\n"
        "try:\\n os.fstat(4); print(1)\\nexcept OSError:\\n print(0)'],\n"
        "                   capture_output=True, text=True)\n"
        "print(json.dumps({'child_has_fd4': r.stdout.strip()}))",
    )
    out = await runner().run(secret_request(tmp_path, script), redact=same)
    assert out.data == {"child_has_fd4": "0"}
