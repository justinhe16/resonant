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


def test_default_protected_covers_path_dirs_state_kegs_and_the_uv_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    local_bin = tmp_path / "home" / ".local" / "bin"
    state = tmp_path / "state"
    cache = tmp_path / "uvcache"
    cellar = tmp_path / "brew" / "Cellar" / "gh" / "2.0" / "bin"
    cellar.mkdir(parents=True)
    (cellar / "gh").write_text("#!/bin/sh\n")
    (cellar / "gh").chmod(0o755)
    brew_bin = tmp_path / "brew" / "bin"
    brew_bin.mkdir()
    (brew_bin / "gh").symlink_to(cellar / "gh")
    monkeypatch.setenv("PATH", f"{local_bin}:{brew_bin}:/usr/bin:/bin:{Path.home()}")
    monkeypatch.setenv("RESONANT_HOME", str(state))
    monkeypatch.setenv("UV_CACHE_DIR", str(cache))
    found = set(default_protected())
    assert {local_bin, brew_bin, state, cache} <= found
    assert brew_bin / "gh" in found
    assert tmp_path / "brew" / "Cellar" / "gh" / "2.0" in found  # the whole keg
    # system dirs and $HOME itself are never protected (a worker must write under $HOME)
    assert not {Path("/usr/bin"), Path("/bin"), Path.home()} & found


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
def ctx() -> Iterator[ProbeContext]:
    if sys.platform != "darwin":
        pytest.skip("Seatbelt is macOS only")
    with probe_context(secrets_dir=Path.home() / ".resonant" / "run-secrets") as c:
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
