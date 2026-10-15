"""The runner sandbox: profile building (any host) and the CI-safe probes (macOS)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from typer.testing import CliRunner

from resonant.cli import app
from resonant.sandbox import (
    SandboxError,
    SandboxParams,
    checked_path,
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
    cached_self_check,
    probe_context,
    run_probes,
    secret_findings,
    self_check,
)

on_mac = pytest.mark.skipif(sys.platform != "darwin", reason="Seatbelt is macOS only")


def params_in(tmp_path: Path, **kw: object) -> SandboxParams:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    return SandboxParams(
        run_dir=tmp_path / "work" / "R1",
        cache_dir=tmp_path / "work" / ".cache",
        home=home,
        **kw,  # type: ignore[arg-type]
    )


# --- profile building ------------------------------------------------------------------------


def test_paths_are_realpathed_and_checked(tmp_path: Path) -> None:
    link = tmp_path / "link"
    link.symlink_to(tmp_path)
    assert checked_path(link / "x") == os.path.realpath(tmp_path / "x")
    with pytest.raises(SandboxError):
        checked_path("relative/path")
    for bad in ('/tmp/a"b', "/tmp/a\\b", "/tmp/a)b", "/tmp/a\nb", "/tmp/a\x00b"):
        with pytest.raises(SandboxError):
            checked_path(bad)


def test_render_puts_every_path_in_a_define_never_in_the_text(tmp_path: Path) -> None:
    params = params_in(tmp_path)
    text, defs = render(params)
    assert os.path.realpath(tmp_path) not in text
    assert "@READ_ALLOW@" not in text
    assert "@META_ALLOW@" not in text
    assert "@DENY_EXEC@" not in text
    assert defs["RUN"] == os.path.realpath(tmp_path / "work" / "R1")
    assert defs["HOME"] == os.path.realpath(tmp_path / "home")
    # ancestors of the run dir get stat-only access
    metas = {v for k, v in defs.items() if k.startswith("META_")}
    assert os.path.realpath(tmp_path / "work") in metas
    assert "/" in metas


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
    monkeypatch.setattr(profile_mod, "template_text", lambda: "(version 1)\n(allow default)\n")
    with pytest.raises(SandboxError):  # a template that lost its placeholders is refused
        profile_hash(params)


def test_run_dir_inside_a_never_readable_dir_is_refused(tmp_path: Path) -> None:
    home = tmp_path / "home"
    (home / ".resonant").mkdir(parents=True)
    with pytest.raises(SandboxError):
        SandboxParams(
            run_dir=home / ".resonant" / "runs" / "R1", cache_dir=tmp_path / "c", home=home
        ).resolved()


def test_scratch_env_is_built_from_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RESONANT_SENTINEL", "leak")
    monkeypatch.setenv("SSH_AUTH_SOCK", "/tmp/agent")
    monkeypatch.setenv("GH_TOKEN", "ghp_leak")
    params = params_in(tmp_path)
    env = scratch_env(params, claude_token="tok")
    run = os.path.realpath(tmp_path / "work" / "R1")
    assert env["HOME"] == f"{run}/home"
    assert env["TMPDIR"] == env["CLAUDE_CODE_TMPDIR"] == f"{run}/tmp"
    assert env["CLAUDE_CONFIG_DIR"] == f"{run}/home/.claude"
    assert env["PATH"].startswith(f"{run}/bin:")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "tok"
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
    with probe_context() as c:
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


@on_mac
def test_always_denied_dirs_win_over_read_paths(tmp_path: Path) -> None:
    """SBPL is last-match-wins: a read path inside ~/.ssh stays unreadable, one outside the
    denied dirs but under a denied parent (the temp dir) is readable."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    (home / ".ssh" / "id").write_text("key")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "t").write_text("tool")
    interp = os.path.realpath(getattr(sys, "_base_executable", sys.executable))
    params = SandboxParams(
        run_dir=tmp_path / "run",
        cache_dir=tmp_path / "cache",
        home=home,
        read_paths=(home / ".ssh", tmp_path / "tools", Path(sys.base_prefix), Path(interp).parent),
    )
    prepare_dirs(params)
    code = (
        "import json,sys\n"
        "r={}\n"
        "for p in sys.argv[1:]:\n"
        "    try: open(p,'rb').read(1); r[p]='ok'\n"
        "    except OSError as e: r[p]='errno:%d'%e.errno\n"
        "print(json.dumps(r))\n"
    )
    paths = [str(home / ".ssh" / "id"), str(tmp_path / "tools" / "t")]
    proc = subprocess.run(
        wrap([interp, "-I", "-c", code, *paths], params),
        cwd="/",
        capture_output=True,
        text=True,
        check=True,
    )
    got = json.loads(proc.stdout)
    assert got[paths[0]] == "errno:1"
    assert got[paths[1]] == "ok"


@on_mac
def test_self_check_passes(ctx: ProbeContext) -> None:
    ok, detail = self_check(ctx.params)
    assert ok, detail


@on_mac
def test_self_check_fails_closed_when_a_deny_is_missing(
    ctx: ProbeContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    template = profile_mod.template_text()
    broken = template.replace('(global-name-prefix "com.apple.securityd")', "")
    assert broken != template
    monkeypatch.setattr(profile_mod, "template_text", lambda: broken)
    ok, detail = self_check(ctx.params)
    assert not ok
    assert "securityd" in detail


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


_DETAIL = "read, keychain lookup and API port denied"
