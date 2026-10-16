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
        secrets_dir=home / ".resonant" / "run-secrets",
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
            run_dir=home / ".resonant" / "runs" / "R1",
            cache_dir=tmp_path / "c",
            secrets_dir=tmp_path / "s",
            home=home,
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
        secrets_dir=home / ".resonant" / "run-secrets",
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


def test_render_secret_denies_come_last_and_carry_deny_read(tmp_path: Path) -> None:
    extra = tmp_path / "keys"
    extra.mkdir()
    tools = tmp_path / "tools"
    tools.mkdir()
    text, defs = render(params_in(tmp_path, deny_read=(extra,), read_paths=(tools,)))
    assert defs["DENY_READ_0"] == os.path.realpath(extra)
    assert str(extra) not in text
    assert ";; @" not in text  # every placeholder was filled
    # the name rules sit after every allow, and spare the run dir, cache and read paths
    last_allow = text.rindex("(allow ")
    names = text.index('(regex #"/\\.[eE][nN][vV]$")')
    assert names > last_allow
    assert text.index('(param "DENY_READ_0")') > last_allow
    exempt = text[names:]
    assert "(require-not (require-any" in exempt
    for key in ("RUN", "CACHE", "READ_0"):
        assert f'(subpath (param "{key}"))' in exempt
    # case-insensitive filesystem: every letter is a class, and the template allow-list too
    assert "[eE][nN][vV]\\.([eE][xX][aA][mM][pP][lL][eE]|" in text
    assert "[kK][eE][yY]" in text and "[pP][fF][xX]" in text and "[jJ][kK][sS]" in text
    assert "[eE][nN][vV][rR][cC]" in text and "[iI][dD]_([rR][sS][aA]|" in text
    # every HOME_SECRETS entry is rendered, none is a leftover literal in the template
    for rel in profile_mod.HOME_SECRETS:
        assert f'(subpath (string-append (param "HOME") "/{rel}"))' in text[names:]
    for rel in (".git-credentials", ".pgpass", ".config/gcloud", ".azure"):
        assert rel in profile_mod.HOME_SECRETS


def test_deny_read_is_checked_like_other_paths(tmp_path: Path) -> None:
    with pytest.raises(SandboxError):
        params_in(tmp_path, deny_read=(Path("relative"),)).resolved()
    with pytest.raises(SandboxError):
        params_in(tmp_path, deny_read=(tmp_path / 'a"b',)).resolved()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    with pytest.raises(SandboxError):  # a run dir inside ~/.kube would be unreadable
        SandboxParams(
            run_dir=home / ".kube" / "r",
            cache_dir=tmp_path / "c",
            secrets_dir=home / ".resonant" / "s",
            home=home,
        ).resolved()


@on_mac
def test_secret_denies_in_a_fake_home(tmp_path: Path) -> None:
    """With read paths put right on top of credential files, the final denies still win, and
    the exempt paths (a read path, the run dir) keep their .env and .pem files."""
    home = tmp_path / "home"
    for rel in (".docker/config.json", ".docker/ok.txt", ".kube/config", ".gnupg/k", ".ssh/id"):
        (home / rel).parent.mkdir(parents=True, exist_ok=True)
        (home / rel).write_text("secret")
    for rel in (".netrc", ".npmrc", ".pypirc"):
        (home / rel).write_text("secret")
    tools = tmp_path / "tools"
    tools.mkdir()
    (tools / "cacert.pem").write_text("cert")
    (tools / ".env").write_text("tool")
    extra = tmp_path / "extra"
    extra.mkdir()
    (extra / "f").write_text("secret")
    interp = os.path.realpath(getattr(sys, "_base_executable", sys.executable))
    params = SandboxParams(
        run_dir=tmp_path / "run",
        cache_dir=tmp_path / "cache",
        secrets_dir=home / ".resonant" / "run-secrets",
        home=home,
        read_paths=(
            home / ".docker",
            home / ".kube",
            home / ".gnupg",
            home / ".ssh",
            home / ".netrc",
            home / ".npmrc",
            home / ".pypirc",
            tools,
            extra,
            Path(sys.base_prefix),
            Path(interp).parent,
        ),
        deny_read=(extra,),
    )
    prepare_dirs(params)
    (tmp_path / "run" / "repo" / ".env").write_text("mine")
    code = (
        "import json,sys\n"
        "r={}\n"
        "for p in sys.argv[1:]:\n"
        "    try: r[p]=open(p,'rb').read(4).decode()\n"
        "    except OSError as e: r[p]='errno:%d'%e.errno\n"
        "print(json.dumps(r))\n"
    )
    denied = [
        *(home / rel for rel in (".docker/config.json", ".kube/config", ".gnupg/k", ".ssh/id")),
        *(home / rel for rel in (".netrc", ".npmrc", ".pypirc")),
        extra / "f",
    ]
    allowed = [
        home / ".docker" / "ok.txt",
        tools / "cacert.pem",
        tools / ".env",
        tmp_path / "run" / "repo" / ".env",
    ]
    paths = [str(p) for p in (*denied, *allowed)]
    proc = subprocess.run(
        wrap([interp, "-I", "-c", code, *paths], params),
        cwd="/",
        capture_output=True,
        text=True,
        check=True,
    )
    got = json.loads(proc.stdout)
    assert {str(p): got[str(p)] for p in denied} == {str(p): "errno:1" for p in denied}, got
    assert not any(got[str(p)].startswith("errno") for p in allowed), got


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


def test_secrets_dir_overlapping_a_readable_path_is_refused(tmp_path: Path) -> None:
    run = tmp_path / "work" / "R1"
    cache = tmp_path / "work" / ".cache"
    tools = tmp_path / "tools"
    tools.mkdir()
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)

    def build(secrets: Path) -> SandboxParams:
        return SandboxParams(
            run_dir=run, cache_dir=cache, home=home, read_paths=(tools,), secrets_dir=secrets
        )

    for bad in (run / "s", cache / "s", tools / "s", tmp_path / "work", tools, run):
        with pytest.raises(SandboxError):
            build(bad).resolved()
    ok = build(home / ".resonant" / "run-secrets").resolved()
    _, defs = render(ok)
    assert defs["SECRETS"] == os.path.realpath(home / ".resonant" / "run-secrets")


def test_persistence_paths_are_refused_for_run_and_cache_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    secrets = home / ".resonant" / "run-secrets"
    for bad in (
        home,
        tmp_path,
        home / "Library" / "LaunchAgents",
        home / ".claude" / "x",
        home / ".zshrc",
        home / ".ssh",
    ):
        for kw in (
            {"run_dir": bad, "cache_dir": tmp_path / "ok"},
            {"run_dir": tmp_path / "ok", "cache_dir": bad},
        ):
            with pytest.raises(SandboxError):
                SandboxParams(secrets_dir=secrets, home=home, **kw).resolved()  # type: ignore[arg-type]
    SandboxParams(
        run_dir=home / "resonant-runs" / "R1",
        cache_dir=home / "resonant-runs" / ".cache",
        secrets_dir=secrets,
        home=home,
    ).resolved()


def test_final_denies_come_after_every_allow(tmp_path: Path) -> None:
    text, _ = render(params_in(tmp_path))
    lines = [ln for ln in text.splitlines() if not ln.lstrip().startswith(";")]
    body = "\n".join(lines)
    last_allow_file = max(body.rfind("(allow file-write*"), body.rfind("(allow file-read*"))
    last_allow = body.rfind("(allow ")
    for marker in (
        '(subpath (param "SECRETS"))',
        '(regex #"/\\.[eE][nN][vV]$")',
        '(subpath (string-append (param "HOME") "/.netrc"))',
        '(literal (string-append (param "HOME") "/.zshrc"))',
        '(subpath (string-append (param "HOME") "/.claude"))',
    ):
        assert body.rfind(marker) > last_allow_file
    assert body.rfind('(param "SECRETS")') > last_allow
    assert '(regex #"^/dev/tty")' not in body
    assert '(literal "/dev/tty")' in body


def test_scratch_env_is_non_interactive(tmp_path: Path) -> None:
    env = scratch_env(params_in(tmp_path))
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"


# --- other runs' dirs, wherever the work dir sits (docs/repos-plan.md D10) ------------------------


def test_render_denies_the_work_dir_except_the_runs_own_paths(tmp_path: Path) -> None:
    tools = tmp_path / "work" / "tools"
    tools.mkdir(parents=True)
    params = SandboxParams(
        run_dir=tmp_path / "work" / "R1",
        work_dir=tmp_path / "work",
        cache_dir=tmp_path / "work" / ".cache",
        secrets_dir=tmp_path / "secrets",
        home=tmp_path / "home",
        read_paths=(tools,),
    )
    text, defs = render(params)
    assert defs["WORK"] == os.path.realpath(tmp_path / "work")
    start = text.index('(subpath (param "WORK"))')
    rule = text[start : text.index("secret files: denied", start)]
    for key in ("RUN", "CACHE", "READ_0"):
        assert f'(subpath (param "{key}"))' in rule
    assert '(literal (param "META_0"))' in rule  # stat on the ancestors still works
    assert text.rindex("(allow ") < start  # after every allow: last match wins
    # the default is the run dir's parent; a run dir outside the work dir is refused
    default = SandboxParams(
        run_dir=tmp_path / "w2" / "R1", cache_dir=tmp_path / "c", secrets_dir=tmp_path / "s"
    ).resolved()
    assert default.work_dir == Path(os.path.realpath(tmp_path / "w2"))
    with pytest.raises(SandboxError, match="inside the work dir"):
        SandboxParams(
            run_dir=tmp_path / "elsewhere" / "R1",
            work_dir=tmp_path / "work",
            cache_dir=tmp_path / "c",
            secrets_dir=tmp_path / "s",
        ).resolved()


def _outside_home_work_dir() -> Path:
    import tempfile

    # /private/var/tmp is outside every read deny of the template
    return Path(os.path.realpath(tempfile.mkdtemp(prefix="resonant-work-", dir="/private/var/tmp")))


@on_mac
def test_a_run_cannot_read_another_runs_dir_with_the_work_dir_outside_home(
    tmp_path: Path,
) -> None:
    """Only the WORK rule keeps ``<work>/R2/creds/token`` away from run R1 here. The cache
    inside the work dir stays usable."""
    import shutil

    work = _outside_home_work_dir()
    try:
        for code in ("R1", "R2"):
            (work / code / "creds").mkdir(parents=True)
            (work / code / "creds" / "token").write_text(f"ghs_{code}\n")
            (work / code / "repo").mkdir()
        (work / ".cache").mkdir()
        (work / ".cache" / "pkg.txt").write_text("cached")
        home = tmp_path / "home"
        home.mkdir(exist_ok=True)
        interp = os.path.realpath(getattr(sys, "_base_executable", sys.executable))
        params = SandboxParams(
            run_dir=work / "R1",
            work_dir=work,
            cache_dir=work / ".cache",
            secrets_dir=tmp_path / "secrets",
            home=home,
            read_paths=(Path(sys.base_prefix), Path(interp).parent),
        )
        code = (
            "import json,os,sys\n"
            "r={}\n"
            "for p in sys.argv[1:]:\n"
            "    try:\n"
            "        d=os.path.isdir(p)\n"
            "        r[p]='ok:'+(os.listdir(p)[0] if d else open(p).read().strip())\n"
            "    except OSError as e: r[p]='errno:%d'%e.errno\n"
            "r['cwd']=os.path.realpath(os.getcwd())\n"
            "print(json.dumps(r))\n"
        )
        paths = [
            str(work / "R1" / "creds" / "token"),
            str(work / ".cache" / "pkg.txt"),
            str(work / "R2" / "creds" / "token"),
            str(work / "R2" / "creds"),
            str(work / "R2"),
        ]
        proc = subprocess.run(
            wrap([interp, "-I", "-c", code, *paths], params),
            cwd=work / "R1" / "repo",
            capture_output=True,
            text=True,
            check=True,
        )
        got = json.loads(proc.stdout)
        assert got[paths[0]] == "ok:ghs_R1" and got[paths[1]] == "ok:cached"
        assert got[paths[2]] == got[paths[3]] == got[paths[4]] == "errno:1"
        assert got["cwd"] == str(work / "R1" / "repo")
    finally:
        shutil.rmtree(work, ignore_errors=True)


@on_mac
def test_probes_pass_with_the_work_dir_outside_home(tmp_path: Path) -> None:
    import shutil

    work = _outside_home_work_dir()
    try:
        with probe_context(secrets_dir=tmp_path / "secrets", work_dir=work) as c:
            res = {r.name: r for r in run_probes(c, ("ci",))}
        mine = res["work dir and sibling run dirs"]
        assert mine.status == "pass", mine.detail
        failed = [f"{r.name}: {r.detail}" for r in res.values() if r.status == "fail"]
        assert not failed
    finally:
        shutil.rmtree(work, ignore_errors=True)
