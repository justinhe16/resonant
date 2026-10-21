"""The thin guard: profile building (any host) and the CI-safe probes (macOS)."""

from __future__ import annotations

import os
import sys
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from resonant.cli import app
from resonant.sandbox import (
    SandboxError,
    SandboxParams,
    checked_path,
    default_protected,
    link_tools,
    prepare_dirs,
    profile_hash,
    render,
    scratch_env,
    wrap,
)
from resonant.sandbox import profile as profile_mod
from resonant.sandbox.probes import (
    PROBES,
    ProbeContext,
    Status,
    cached_self_check,
    p_mach,
    p_mount,
    p_persistence,
    p_ports,
    p_signal,
    p_state_writes,
    p_unreadable,
    probe_context,
    run_probes,
    secret_findings,
    self_check,
)

on_mac = pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")


def params_in(tmp_path: Path, **kw: object) -> SandboxParams:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    kw.setdefault("protect", (tmp_path / "daemon" / "venv",))
    return SandboxParams(
        run_dir=tmp_path / "work" / "R1",
        cache_dir=tmp_path / "work" / ".cache",
        secrets_dir=home / ".resonant" / "run-secrets",
        home=home,
        **kw,  # type: ignore[arg-type]
    )


def _real(p: Path) -> str:
    return os.path.realpath(p)


# --- profile building ------------------------------------------------------------------------


def test_paths_are_realpathed_and_checked(tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    assert checked_path(link / "x") == _real(tmp_path / "x")
    # follow=False keeps a symlink itself (its parent is still resolved)
    assert checked_path(link, follow=False) == os.path.join(_real(tmp_path), "link")
    with pytest.raises(SandboxError):
        checked_path("relative/path")
    for bad in ('/tmp/a"b', "/tmp/a\\b", "/tmp/a)b", "/tmp/a\nb", "/tmp/a\x00b"):
        with pytest.raises(SandboxError):
            checked_path(bad)


def test_render_puts_every_path_in_a_define_never_in_the_text(tmp_path: Path) -> None:
    deny = tmp_path / "private" / "keys"
    text, defs = render(params_in(tmp_path, deny_read=(deny,), deny_exec=(tmp_path / "x",)))
    assert _real(tmp_path) not in text
    assert "@" not in text  # every placeholder filled
    assert defs["HOME"] == _real(tmp_path / "home")
    assert defs["SECRETS"] == _real(tmp_path / "home" / ".resonant" / "run-secrets")
    assert defs["DENY_READ_0"] == _real(deny)
    assert defs["DENY_EXEC_0"] == _real(tmp_path / "x")
    protected = {v for k, v in defs.items() if k.startswith("PROTECT_")}
    assert _real(tmp_path / "daemon" / "venv") in protected
    # gone with the Phase 5a wall: no run-dir allow, no read allow-list, no work-dir deny
    for key in ("RUN", "CACHE", "WORK", "META_0", "READ_0"):
        assert key not in defs


def test_ancestors_of_every_protected_path_are_rename_guarded(tmp_path: Path) -> None:
    deny = tmp_path / "private" / "keys"
    _, defs = render(params_in(tmp_path, deny_read=(deny,)))
    ancestors = {v for k, v in defs.items() if k.startswith("ANCESTOR_")}
    for path in (
        tmp_path / "daemon",  # the protected venv's parent
        tmp_path / "private",  # runner.deny_read's parent
        tmp_path / "home",  # $HOME, the parent of ~/.resonant and ~/Library
        tmp_path / "home" / ".resonant",  # run-secrets' parent
        Path("/Library"),  # /Library/Keychains, /Library/LaunchAgents
        Path("/private/var"),  # the cron and at spools
    ):
        assert _real(path) in ancestors, path
    assert "/" in ancestors
    # the protected paths themselves are subpaths, not ancestors
    assert _real(tmp_path / "daemon" / "venv") not in ancestors


def test_the_profile_is_allow_default_with_only_denies(tmp_path: Path) -> None:
    text, _ = render(params_in(tmp_path))
    body = [ln.strip() for ln in text.splitlines() if not ln.lstrip().startswith(";")]
    allows = [ln for ln in body if ln.startswith("(allow")]
    assert allows == ["(allow default)"]  # no allow below a deny can undo it
    joined = "\n".join(body)
    for rule in (
        '(subpath (string-append (param "HOME") "/.resonant"))',
        '(subpath (param "SECRETS"))',
        '(subpath (string-append (param "HOME") "/Library/Messages"))',
        '(subpath (string-append (param "HOME") "/Library/Keychains"))',
        '(subpath "/Library/Keychains")',
        '(literal (string-append (param "HOME") "/.zshrc"))',
        '(subpath "/Library/LaunchAgents")',
        '(literal "/bin/launchctl")',
        '(global-name "com.apple.SecurityServer")',
        '(global-name-prefix "com.apple.securityd")',
        "(deny appleevent-send)",
        "(deny lsopen)",
        "(deny signal (require-not (target same-sandbox)))",
        '(remote ip "*:7777")',
    ):
        assert rule in joined, rule
    # what the thin guard dropped (docs/employee-plan.md D2): the Ollama port, the unix-socket
    # deny, the secret-file name rules
    for gone in ("11434", "unix-socket", "regex"):
        assert gone not in joined, gone
    assert "com.apple.trustd" not in joined  # TLS needs it


def test_wrap_prefixes_sandbox_exec_and_keeps_argv(tmp_path: Path) -> None:
    argv = wrap(["/bin/echo", "hi"], params_in(tmp_path))
    assert argv[0] == "/usr/bin/sandbox-exec"
    assert argv[-2:] == ["/bin/echo", "hi"]
    assert argv[1] == "-p"
    with pytest.raises(SandboxError):
        wrap([], params_in(tmp_path))


def test_profile_hash_is_stable_and_template_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    params = params_in(tmp_path)
    before = profile_hash(params)
    assert profile_hash(params) == before
    assert profile_hash(params_in(tmp_path, deny_read=(tmp_path / "k",))) != before
    monkeypatch.setattr(profile_mod, "template_text", lambda: "(version 1)\n(allow default)\n")
    with pytest.raises(SandboxError):  # a template that lost its placeholders is refused
        profile_hash(params)


def test_default_protected_is_the_installed_daemon() -> None:
    found = {_real(p) for p in default_protected()}
    assert _real(Path(sys.prefix)) in found  # the venv
    assert _real(Path(sys.base_prefix)) in found  # the interpreter it runs on
    checkout = profile_mod._package_checkout()  # pyright: ignore[reportPrivateUsage]
    assert checkout is not None  # tests run from the checkout
    assert _real(checkout) in found
    # and it is what a SandboxParams without `protect` gets
    p = SandboxParams(
        run_dir=Path("/private/var/tmp/r/R1"),
        cache_dir=Path("/private/var/tmp/r/.cache"),
        secrets_dir=Path("/private/var/tmp/s"),
        home=Path("/private/var/tmp/h"),
    ).resolved()
    assert p.protect is not None
    assert _real(Path(sys.prefix)) in {str(x) for x in p.protect}


def test_default_protected_covers_state_and_tool_kegs_but_not_path_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    state = tmp_path / "state"
    cellar = tmp_path / "brew" / "Cellar" / "gh" / "2.0" / "bin"
    cellar.mkdir(parents=True)
    (cellar / "gh").write_text("#!/bin/sh\n")
    (cellar / "gh").chmod(0o755)
    brew_bin = tmp_path / "brew" / "bin"
    brew_bin.mkdir()
    (brew_bin / "gh").symlink_to(cellar / "gh")
    monkeypatch.setattr(profile_mod, "HOST_TOOL_DIRS", (str(brew_bin), "/usr/bin", "/bin"))
    monkeypatch.setenv("RESONANT_HOME", str(state))
    # the process PATH plays no part: the daemon's is system dirs only
    monkeypatch.setenv("PATH", f"{tmp_path / 'elsewhere'}:/usr/bin:/bin")
    found = set(default_protected())
    assert state in found
    assert brew_bin / "gh" in found  # the link the daemon runs
    assert tmp_path / "brew" / "Cellar" / "gh" / "2.0" in found  # and its whole keg
    # the PATH dirs themselves stay writable (brew install, uv tool install), and so does
    # ~/.cache/uv (/update's uv sync uses its own cache under the Resonant home)
    assert not {brew_bin, Path.home() / ".local" / "bin", Path.home() / ".cache" / "uv"} & found
    assert not {Path("/usr/bin"), Path("/bin"), Path.home()} & found


def test_resolve_tool_searches_the_install_dirs_not_the_process_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "mytool").write_text("#!/bin/sh\n")
    (tools / "mytool").chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.setattr(profile_mod, "HOST_TOOL_DIRS", (str(tools), "/usr/bin", "/bin"))
    assert profile_mod.resolve_tool("mytool") == Path(_real(tools / "mytool"))
    monkeypatch.setattr(profile_mod, "HOST_TOOL_DIRS", ("/usr/bin", "/bin"))
    monkeypatch.setenv("PATH", f"{tools}:/usr/bin:/bin")
    assert profile_mod.resolve_tool("mytool") is None


def _exe(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_a_planted_binary_never_becomes_the_daemons_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workers may write ~/.local/bin and /opt/homebrew/bin. A git planted there must not be
    picked as git (only the system copy is), and a gh planted ahead of the daemon's must not
    leave the real one unprotected. (The daemon's own git is runner.git_path, /usr/bin/git.)"""
    home = tmp_path / "h"
    planted_git = _exe(home / ".local" / "bin" / "git")
    planted_gh = _exe(home / ".local" / "bin" / "gh")
    brew = tmp_path / "brew"
    real_gh = _exe(brew / "Cellar" / "gh" / "2.0" / "bin" / "gh")
    (brew / "bin").mkdir()
    (brew / "bin" / "gh").symlink_to(real_gh)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(profile_mod, "HOST_TOOL_DIRS", (str(brew / "bin"), "/usr/bin", "/bin"))
    assert profile_mod.resolve_tool("gh") == planted_gh.resolve()  # a session tool: fine
    git = profile_mod.resolve_tool("git")
    assert git is not None and not git.is_relative_to(home.resolve())
    found = set(default_protected())
    assert {planted_gh, brew / "bin" / "gh", brew / "Cellar" / "gh" / "2.0"} <= found
    assert planted_git in found  # every copy, whichever the daemon would pick


def test_the_configured_tool_paths_are_protected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    gh = _exe(tmp_path / "custom" / "Cellar" / "gh" / "9.9" / "bin" / "gh")
    monkeypatch.setattr(profile_mod, "_configured_tools", lambda: [gh])
    found = set(default_protected())
    assert gh in found and tmp_path / "custom" / "Cellar" / "gh" / "9.9" in found
    # and the real lookup reads the daemon's config
    monkeypatch.undo()
    configured = profile_mod._configured_tools()  # pyright: ignore[reportPrivateUsage]
    assert Path("/usr/bin/git") in configured and Path("/opt/homebrew/bin/gh") in configured


def test_symlinked_home_paths_protect_their_real_targets(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (tmp_path / "state").mkdir()
    (home / ".resonant").symlink_to(tmp_path / "state")
    (tmp_path / "dotfiles").mkdir()
    (tmp_path / "dotfiles" / "zshrc").write_text("")
    (home / ".zshrc").symlink_to(tmp_path / "dotfiles" / "zshrc")
    (tmp_path / "msgs").mkdir()
    (home / "Library").mkdir()
    (home / "Library" / "Messages").symlink_to(tmp_path / "msgs")
    p = params_in(tmp_path).resolved()
    assert p.protect is not None
    assert Path(_real(tmp_path / "state")) in p.protect
    assert Path(_real(tmp_path / "dotfiles" / "zshrc")) in p.protect
    assert Path(_real(tmp_path / "msgs")) in p.deny_read  # read and write, like the rule
    assert p.resolved() == p  # idempotent


def test_run_and_cache_dirs_must_be_writable(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    secrets = home / ".resonant" / "run-secrets"
    daemon = tmp_path / "daemon"
    denied = tmp_path / "denied"
    for bad in (
        home / ".resonant" / "runs" / "R1",
        home / "Library" / "LaunchAgents",
        home / ".claude" / "x",
        home / ".zshrc",
        home / ".ssh",
        home / ".config" / "runs",
        daemon / "R1",
        denied / "R1",
        secrets / "R1",
    ):
        for kw in (
            {"run_dir": bad, "cache_dir": tmp_path / "ok"},
            {"run_dir": tmp_path / "ok", "cache_dir": bad},
        ):
            with pytest.raises(SandboxError):
                SandboxParams(
                    secrets_dir=secrets,
                    home=home,
                    protect=(daemon,),
                    deny_read=(denied,),
                    **kw,  # type: ignore[arg-type]
                ).resolved()
    # secrets inside a writable run dir is a contradiction
    with pytest.raises(SandboxError):
        SandboxParams(
            run_dir=tmp_path / "run",
            cache_dir=tmp_path / "c",
            secrets_dir=tmp_path / "run" / "s",
            home=home,
            protect=(),
        ).resolved()
    # anywhere else is fine, and the run dir no longer has to sit in the work dir (EM-2)
    SandboxParams(
        run_dir=home / "code" / ".worktrees" / "demo" / "R1",
        cache_dir=home / "resonant-runs" / ".cache",
        work_dir=home / "resonant-runs",
        secrets_dir=secrets,
        home=home,
        protect=(daemon,),
    ).resolved()


def test_em2_write_paths_are_checked_like_the_run_dir(tmp_path: Path) -> None:
    """EM-2's worktree and the checkout's .git (or a scratch dir) arrive as write_paths.
    Writes are allowed everywhere, so they grant nothing, but none may sit where the guard
    denies writes."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    daemon = tmp_path / "daemon"
    base = {
        "run_dir": home / "resonant-runs" / "R1",
        "cache_dir": home / "resonant-runs" / ".cache",
        "secrets_dir": home / ".resonant" / "run-secrets",
        "home": home,
        "protect": (daemon,),
    }
    ok = SandboxParams(
        **base,  # type: ignore[arg-type]
        write_paths=(home / "code" / ".worktrees" / "demo" / "R1", home / "code" / "demo" / ".git"),
    ).resolved()
    assert len(ok.write_paths) == 2
    text, _ = render(ok)
    assert "WRITE" not in text  # nothing to allow under allow-default
    for bad in (daemon / "checkout" / ".git", home / ".resonant" / "scratch", home / ".ssh"):
        with pytest.raises(SandboxError):
            SandboxParams(**base, write_paths=(bad,)).resolved()  # type: ignore[arg-type]


def test_scratch_env_is_built_from_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESONANT_SENTINEL", "leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent")
    monkeypatch.setenv("GH_TOKEN", "ghp_leak")
    params = params_in(tmp_path)
    env = scratch_env(params, claude_token="tok")
    run = _real(tmp_path / "work" / "R1")
    assert env["HOME"] == f"{run}/home"
    assert env["TMPDIR"] == env["CLAUDE_CODE_TMPDIR"] == f"{run}/tmp"
    assert env["CLAUDE_CONFIG_DIR"] == f"{run}/home/.claude"
    assert env["PATH"].startswith(f"{run}/bin:")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert "ANTHROPIC_API_KEY" not in scratch_env(params)
    assert not {"RESONANT_SENTINEL", "SSH_AUTH_SOCK", "GH_TOKEN"} & set(env)
    assert all(v != "leak" for v in env.values())


def test_link_tools_refuses_resonant(tmp_path: Path) -> None:
    params = params_in(tmp_path)
    prepare_dirs(params)
    tool = tmp_path / "tool"
    tool.write_text("#!/bin/sh\n")
    link_tools(params, {"uv": tool})
    assert (params.resolved().run_dir / "bin" / "uv").is_symlink()
    with pytest.raises(SandboxError):
        link_tools(params, {"resonant": tool})
    entry = tmp_path / "resonant"
    entry.write_text("#!/bin/sh\n")
    with pytest.raises(SandboxError):
        link_tools(params, {"x": entry})


def test_secret_findings_report_names_only() -> None:
    env = {
        "PATH": "/usr/bin",
        "GH_TOKEN": "x",
        "A": "ghp_abc",
        "B": "prefix github_pat_zzz",
        "C": "sk-ant-api03-q",
        "RESONANT_SECRET_GITHUB": "v",
        "CLAUDE_CODE_OAUTH_TOKEN": "t",
        "OK": "fine",
    }
    assert secret_findings(env) == sorted(
        ["GH_TOKEN", "A", "B", "C", "RESONANT_SECRET_GITHUB", "CLAUDE_CODE_OAUTH_TOKEN"]
    )
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in secret_findings(env, allow=("CLAUDE_CODE_OAUTH_TOKEN",))
    assert secret_findings({"PATH": "/bin"}) == []


def test_cli_help_lists_check() -> None:
    result = CliRunner().invoke(app, ["sandbox", "--help"])
    assert result.exit_code == 0
    assert "check" in result.output


# --- real sandbox (macOS) --------------------------------------------------------------------


@pytest.fixture(scope="module")
def ctx(tmp_path_factory: pytest.TempPathFactory) -> Iterator[ProbeContext]:
    if sys.platform != "darwin":
        pytest.skip("Seatbelt is macOS only")
    # PER-271: a temp home, never the real one: a module fixture runs before the per-test
    # HOME redirect, and the probes make run dirs in ~/resonant-runs and use its cache
    home = tmp_path_factory.mktemp("probe-home")
    with probe_context(home=home, secrets_dir=home / ".resonant" / "run-secrets") as c:
        yield c


@on_mac
def test_ci_probes_all_pass(ctx: ProbeContext) -> None:
    results = run_probes(ctx, ("ci",))
    assert len(results) == sum(p.scope == "ci" for p in PROBES)
    failed = [f"{r.name}: {r.detail}" for r in results if r.status == "fail"]
    assert not failed


@on_mac
def test_known_limitation_is_informational_not_a_failure(ctx: ProbeContext) -> None:
    res = [r for r in run_probes(ctx, ("ci",)) if "known limitation" in r.name]
    assert res
    assert res[0].status in ("info", "pass", "skip")


ProbeFn = Callable[[ProbeContext], tuple[Status, str]]
_STATE = '(subpath (string-append (param "HOME") "/.resonant"))\n'
# Each deny the guard rests on, and the probe that must catch its loss: the probes are only
# worth something if they fail when a rule goes missing. (old text, new text, probe)
_MUTATIONS: dict[str, tuple[str, str, ProbeFn]] = {
    "state": (_STATE, "", p_state_writes),
    # the protected paths turned into an allow: the venv becomes writable
    "protect": (";; @PROTECT@\n  )", "  )\n(allow file-write*\n;; @PROTECT@\n  )", p_state_writes),
    # the ancestors turned into an allow: a parent dir can be renamed away
    "ancestors": (
        "(deny file-write*\n;; @ANCESTORS@",
        "(allow file-write*\n;; @ANCESTORS@",
        p_state_writes,
    ),
    "rc files": ('(literal (string-append (param "HOME") "/.zshrc"))', "", p_persistence),
    "library": ('(subpath (string-append (param "HOME") "/Library"))\n', "", p_persistence),
    "secrets": ('(subpath (param "SECRETS"))', "", p_unreadable),
    "messages": ('(subpath (string-append (param "HOME") "/Library/Messages"))', "", p_unreadable),
    "securityd": ('(global-name-prefix "com.apple.securityd")', "", p_mach),
    "imessage": ('(global-name-prefix "com.apple.imagent")', "", p_mach),
    "signal": ("(deny signal (require-not (target same-sandbox)))", "", p_signal),
    "port": ('(deny network-outbound (remote ip "*:7777"))', "", p_ports),
}


@on_mac
@pytest.mark.skipif(bool(os.environ.get("CI")), reason="mounts a disk image; local only")
def test_mount_probe_fails_without_the_mount_deny(
    ctx: ProbeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    status, detail = p_mount(ctx)
    if status == "skip":
        pytest.skip(detail)
    assert status == "pass", detail
    template = profile_mod.template_text()
    broken = template.replace("(deny file-mount)", "")
    assert broken != template
    monkeypatch.setattr(profile_mod, "template_text", lambda: broken)
    status, detail = p_mount(ctx)
    assert status == "fail", detail


@on_mac
@pytest.mark.parametrize("name", list(_MUTATIONS))
def test_a_probe_fails_when_its_deny_is_removed(
    ctx: ProbeContext, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    old, new, probe = _MUTATIONS[name]
    assert probe(ctx)[0] == "pass"
    template = profile_mod.template_text()
    broken = template.replace(old, new)
    assert broken != template, old
    monkeypatch.setattr(profile_mod, "template_text", lambda: broken)
    status, detail = probe(ctx)
    assert status == "fail", detail


@on_mac
def test_self_check_passes(ctx: ProbeContext) -> None:
    ok, detail = self_check(ctx.params)
    assert ok, detail
    assert detail == _DETAIL


@on_mac
@pytest.mark.parametrize(
    ("rule", "needle"),
    [
        ('(global-name-prefix "com.apple.securityd")', "securityd"),
        (_STATE, ".resonant"),
        ('(deny network-outbound (remote ip "*:7777"))', "7777"),
    ],
)
def test_self_check_fails_closed_when_a_deny_is_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rule: str, needle: str
) -> None:
    home = tmp_path / "home"
    (home / ".resonant").mkdir(parents=True)  # a fake home: a hole writes here, not in ~
    params = params_in(tmp_path)
    template = profile_mod.template_text()
    broken = template.replace(rule, "")
    assert broken != template
    monkeypatch.setattr(profile_mod, "template_text", lambda: broken)
    ok, detail = self_check(params)
    assert not ok
    assert needle in detail
    assert list((home / ".resonant").iterdir()) == []  # the hole's write was undone


@on_mac
def test_cached_self_check_caches_passes_not_failures(ctx: ProbeContext, tmp_path: Path) -> None:
    cache = tmp_path / "cache" / "sandbox.json"
    clock = [1000.0]
    assert cached_self_check(ctx.params, cache, now=lambda: clock[0]) == (True, _DETAIL)
    assert cache.is_file()
    clock[0] += 60
    assert cached_self_check(ctx.params, cache, now=lambda: clock[0]) == (True, "cached")
    clock[0] += 7200  # past the TTL: probes again
    assert cached_self_check(ctx.params, cache, now=lambda: clock[0])[1] != "cached"


_DETAIL = "~/.resonant and daemon writes, keychain lookup and API port denied"


# --- the worker CLI's control dirs (EM-3) ----------------------------------------------------

_CLIENT = """
import socket, sys
for path in sys.argv[1:]:
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(path)
        print("ok", flush=True)
    except OSError:
        print("denied", flush=True)
"""


@on_mac
def test_a_session_reaches_only_its_own_control_dir(tmp_path: Path) -> None:
    """Real sandbox-exec: the session connects to its own run's sockets and reads its own
    token; another run's socket, token and control dir are out of reach, and no control dir
    is writable (so no run can stand in for the daemon on another's socket)."""
    import shutil
    import socket
    import subprocess
    import tempfile

    short = Path(tempfile.mkdtemp(prefix="ctl", dir="/private/tmp"))  # sun_path is short
    try:
        work = short / "w"
        own, other = work / ".ctl" / "R1", work / ".ctl" / "R2"
        for d in (own, other, work / "R1" / "tmp", work / "R2" / "tmp"):
            d.mkdir(parents=True)
        (own / "token").write_text("mine")
        (other / "token").write_text("theirs")
        paths = [own / "sock", other / "sock", work / "R1" / "tmp" / "s", work / "R2" / "tmp" / "s"]
        servers: list[socket.socket] = []
        for p in paths:
            srv = socket.socket(socket.AF_UNIX)
            srv.bind(str(p))
            srv.listen(4)
            servers.append(srv)
        params = SandboxParams(
            run_dir=work / "R1", cache_dir=work / ".cache", secrets_dir=short / "secrets",
            work_dir=work, ctl_root=work / ".ctl", ctl_dir=own, protect=(),
        )  # fmt: skip
        argv = wrap([sys.executable, "-I", "-c", _CLIENT, *map(str, paths)], params)
        out = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        assert out.stdout.split() == ["ok", "denied", "ok", "denied"], out.stderr
        shell = f"cat {own}/token; cat {other}/token; : > {own}/x; mv {other} {other}x"
        out = subprocess.run(wrap(["/bin/sh", "-c", shell], params), capture_output=True,
                             text=True, timeout=60)  # fmt: skip
        assert out.stdout == "mine"
        assert not (own / "x").exists() and other.exists()
        for srv in servers:
            srv.close()
    finally:
        shutil.rmtree(short, ignore_errors=True)


def test_the_control_dir_defs_stay_out_of_the_profile_hash(tmp_path: Path) -> None:
    from dataclasses import replace

    work = tmp_path / "work"
    base = params_in(tmp_path, work_dir=work, ctl_root=work / ".ctl", ctl_dir=work / ".ctl" / "R1")
    other = replace(base, run_dir=work / "R2", ctl_dir=work / ".ctl" / "R2")
    assert profile_hash(base) == profile_hash(other)
    text, defs = render(base)
    assert defs["CTL_OWN"].endswith("/.ctl/R1") and "remote unix-socket" in text
    with pytest.raises(SandboxError, match="go together"):
        render(replace(base, ctl_dir=None))
    with pytest.raises(SandboxError, match="isn't a control dir"):
        render(replace(base, ctl_dir=tmp_path / "x"))


def test_cli_check_with_the_guard_off_skips_every_probe_and_says_so() -> None:
    """GD-1: the default ``runner.guard: off`` runs no probe (nothing would be wrapped)."""
    from resonant.sandbox.probes import PROBES

    result = CliRunner().invoke(app, ["sandbox", "check"])
    assert result.exit_code == 0, result.output
    assert result.output.startswith("guard: off. runner.guard is off")
    assert "--thin" in result.output
    assert f"0 passed, 0 failed, {len(PROBES)} skipped" in result.output
    local = CliRunner().invoke(app, ["sandbox", "check", "--scope", "ci", "--json"])
    assert local.exit_code == 0
    import json

    rows = json.loads(local.output)
    assert rows and {r["status"] for r in rows} == {"skip"} and {r["scope"] for r in rows} == {"ci"}
