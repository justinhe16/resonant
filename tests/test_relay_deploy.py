"""``resonant relay deploy`` (PER-316): a Keychain Fly deploy token, only in the child's env.

No real ``fly``, ``git`` or Keychain: a fake runner and a ``FakeKeychain`` under a real
``SecretStore``. The token is a sentinel that must appear nowhere but the child's env.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import stat
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from resonant import cli
from resonant.cli import app
from resonant.config import RelayConfig, load_settings
from resonant.ops import relay_deploy as rd
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store.db import open_db

TOKEN = "FlyV1-sentinel-deploy-token-7f3a9c2e"
APP = "resonant-relay-test"
SHA = "0123456789abcdef0123456789abcdef01234567"


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    cwd: Path
    env: dict[str, str]
    timeout_s: float
    echo: bool
    mask: tuple[str, ...]


@dataclass
class FakeDeployRunner:
    """Scripted results by the argv's second word (``rev-parse``, ``deploy``, ``status``)."""

    results: dict[str, rd.RunResult] = field(default_factory=dict[str, rd.RunResult])
    calls: list[Call] = field(default_factory=list[Call])

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        echo: bool,
        mask: Sequence[str] = (),
    ) -> rd.RunResult:
        self.calls.append(Call(tuple(argv), cwd, dict(env), timeout_s, echo, tuple(mask)))
        default = {
            "rev-parse": rd.RunResult(0, SHA + "\n"),
            "deploy": rd.RunResult(0, "==> Building image\n"),
            "status": rd.RunResult(0, json.dumps({"Machines": [{"id": "m1"}]})),
        }
        return self.results.get(argv[1], default[argv[1]])


def _fly(tmp_path: Path) -> Path:
    fly = tmp_path / "bin" / "fly"
    fly.parent.mkdir(parents=True)
    fly.write_text("#!/bin/sh\nexit 99\n")
    fly.chmod(fly.stat().st_mode | stat.S_IXUSR)
    return fly


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "checkout"
    (repo / "relay").mkdir(parents=True)
    (repo / "relay" / "fly.toml").write_text("app = 'x'\n")
    (repo / "relay" / "Dockerfile").write_text("FROM scratch\n")
    (repo / "dashboard" / "dist").mkdir(parents=True)
    return repo


@dataclass
class Env:
    runner: FakeDeployRunner
    keychain: FakeKeychain
    fly: Path
    repo: Path
    conn: sqlite3.Connection


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Env:
    fly, repo = _fly(tmp_path), _repo(tmp_path)
    monkeypatch.setenv("RESONANT_RELAY__FLY_APP", APP)
    monkeypatch.setenv("RESONANT_RELAY__FLY_PATH", str(fly))
    monkeypatch.setenv("RESONANT_OPS__REPO_PATH", str(repo))
    monkeypatch.setenv("FLY_API_TOKEN", "inherited-api-token-should-not-pass")
    monkeypatch.setenv("FLY_ACCESS_TOKEN", "inherited-access-token-should-not-pass")
    keychain = FakeKeychain(items={rd.FLY_DEPLOY_TOKEN_REF: TOKEN})
    monkeypatch.setattr(cli, "_secret_store", lambda: SecretStore(keychain))
    runner = FakeDeployRunner()
    monkeypatch.setattr(cli, "_deploy_runner", lambda: runner)
    conn = open_db(load_settings().db_path)
    return Env(runner, keychain, fly, repo, conn)


def invoke(*args: str) -> tuple[int, str]:
    result = CliRunner().invoke(app, ["relay", "deploy", *args])
    return result.exit_code, result.output


def _audits(conn: sqlite3.Connection) -> list[tuple[str, str]]:
    return conn.execute(
        "SELECT action, detail FROM audit_log WHERE action = 'relay.deploy'"
    ).fetchall()


def test_deploy_argv_cwd_env_and_audit(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    code, out = invoke()
    assert code == 0, out
    git, deploy, status = env.runner.calls
    assert git.argv == ("/usr/bin/git", "rev-parse", "HEAD") and rd.TOKEN_ENV not in git.env
    assert deploy.argv == (
        str(env.fly),
        "deploy",
        "--app",
        APP,
        "--config",
        "relay/fly.toml",
        "--dockerfile",
        "relay/Dockerfile",
        "--ha=false",
        ".",
    )
    assert deploy.cwd == env.repo and deploy.echo and deploy.timeout_s == 30 * 60
    assert deploy.env["FLY_API_TOKEN"] == TOKEN
    assert "FLY_ACCESS_TOKEN" not in deploy.env
    assert deploy.env["PATH"].split(":")[0] == str(env.fly.parent)
    assert set(deploy.env) <= {"HOME", "USER", "LOGNAME", "TMPDIR", "PATH", "FLY_API_TOKEN"} | {
        "GIT_TERMINAL_PROMPT",
        "GIT_ASKPASS",
        "SSH_ASKPASS",
        "GIT_SSH_COMMAND",
    }
    assert deploy.mask == (TOKEN,)
    assert status.argv == (str(env.fly), "status", "--app", APP, "--json")
    assert status.env["FLY_API_TOKEN"] == TOKEN and status.cwd == env.repo
    assert "Deployed resonant-relay-test: 1 machine" in out
    ((_, detail),) = _audits(env.conn)
    assert json.loads(detail) == {"app": APP, "exit_code": 0, "head": SHA}
    # the sentinel: only ever in a child's env
    for c in env.runner.calls:
        assert all(TOKEN not in a for a in c.argv)
    assert TOKEN not in out and TOKEN not in detail and TOKEN not in caplog.text
    # the value was read once, with -w (the only read)
    assert [a[-1] for a in env.keychain.argvs()] == ["-w"]


def test_inherited_fly_tokens_dont_pass(env: Env) -> None:
    code, _ = invoke()
    assert code == 0
    for c in env.runner.calls:
        assert "inherited-api-token-should-not-pass" not in c.env.values()
        assert "FLY_ACCESS_TOKEN" not in c.env


def test_fly_failure_is_the_exit_code(env: Env) -> None:
    env.runner.results["deploy"] = rd.RunResult(3, "Error: failed to fetch an image\n")
    code, out = invoke()
    assert code == 3
    assert "Fly rejected" not in out
    assert [c.argv[1] for c in env.runner.calls] == ["rev-parse", "deploy"]  # no status
    ((_, detail),) = _audits(env.conn)
    assert json.loads(detail)["exit_code"] == 3


def test_rejected_token_says_renew(env: Env) -> None:
    env.runner.results["deploy"] = rd.RunResult(1, "Error: unauthorized: token has expired\n")
    code, out = invoke()
    assert code == 1
    assert f"Fly rejected the token: renew with fly tokens create deploy -a {APP}" in out
    assert "unauthorized" not in out  # fly's output isn't echoed again
    assert TOKEN not in out


def test_timeout(env: Env) -> None:
    env.runner.results["deploy"] = rd.RunResult(-9, "", timed_out=True)
    code, out = invoke()
    assert code == rd.EXIT_TIMEOUT and "timed out after 30 min" in out


def test_two_machines_warn(env: Env) -> None:
    env.runner.results["status"] = rd.RunResult(
        0, json.dumps({"Machines": [{"id": "a"}, {"id": "b"}]})
    )
    code, out = invoke()
    assert code == 0 and "has 2 machines, not 1" in out


def test_dry_run_never_reads_the_value(env: Env) -> None:
    code, out = invoke("--dry-run")
    assert code == 0, out
    assert env.runner.calls == []
    assert all("-w" not in a for a in env.keychain.argvs()) and env.keychain.argvs()
    assert f"FLY_API_TOKEN=<from relay/fly_deploy_token> {env.fly} deploy --app {APP}" in out
    assert "--ha=false ." in out and TOKEN not in out
    assert _audits(env.conn) == []


def test_repo_option_overrides(env: Env, tmp_path: Path) -> None:
    other = _repo(tmp_path / "wt")
    code, _ = invoke("--repo", str(other))
    assert code == 0
    assert {c.cwd for c in env.runner.calls} == {other}


@pytest.mark.parametrize("dry", [False, True])
def test_missing_token_refuses_with_setup(env: Env, dry: bool) -> None:
    env.keychain.items.clear()
    code, out = invoke(*(["--dry-run"] if dry else []))
    assert code == 1
    assert "relay/fly_deploy_token isn't in the Keychain" in out
    assert f"fly tokens create deploy -a {APP} --expiry 8760h" in out
    assert "resonant secret set relay/fly_deploy_token --expires-in 365d" in out
    assert env.runner.calls == []


def test_missing_app_refuses(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESONANT_RELAY__FLY_APP")
    code, out = invoke()
    assert code == 1 and "relay.fly_app isn't set" in out
    assert env.runner.calls == [] and env.keychain.calls == []


def test_missing_fly_refuses(env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RESONANT_RELAY__FLY_PATH", str(tmp_path / "nope" / "fly"))
    code, out = invoke()
    assert code == 1 and "no fly binary" in out
    assert env.runner.calls == [] and env.keychain.calls == []


def test_missing_dist_refuses(env: Env) -> None:
    (env.repo / "dashboard" / "dist").rmdir()
    code, out = invoke()
    assert code == 1 and "dashboard/dist is missing" in out
    assert env.runner.calls == [] and env.keychain.calls == []


def test_missing_repo_refuses(env: Env, tmp_path: Path) -> None:
    code, out = invoke("--repo", str(tmp_path / "nowhere"))
    assert code == 1 and "is not a directory" in out
    assert env.runner.calls == [] and env.keychain.calls == []


def test_non_default_instance_refuses(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cori = tmp_path / ".resonant-cori"
    cori.mkdir()
    monkeypatch.setenv("RESONANT_HOME", str(cori))
    result = CliRunner().invoke(app, ["--instance", "cori", "relay", "deploy"])
    assert result.exit_code == 2, result.output
    assert "personal Resonant's" in result.output
    assert env.runner.calls == [] and env.keychain.calls == []


# --- helpers ---------------------------------------------------------------------------


def test_find_fly_order(tmp_path: Path) -> None:
    fly = _fly(tmp_path)
    brew = tmp_path / "brew-fly"
    assert rd.find_fly(fly) == fly
    assert rd.find_fly(tmp_path / "missing") is None  # a configured path isn't second-guessed
    assert rd.find_fly(None, which=lambda n: str(fly) if n == "flyctl" else None) == fly
    assert rd.find_fly(None, which=lambda _n: None, homebrew=brew) is None
    brew.write_text("")
    brew.chmod(0o755)
    assert rd.find_fly(None, which=lambda _n: None, homebrew=brew) == brew


def test_child_env_strips_inherited_tokens(tmp_path: Path) -> None:
    fly = tmp_path / "bin" / "fly"
    base = {"HOME": "/h", "FLY_API_TOKEN": "old", "FLY_ACCESS_TOKEN": "old2", "SECRET": "x"}
    assert rd.child_env(fly, None, base) == {
        "HOME": "/h",
        "PATH": f"{fly.parent}:/usr/bin:/bin:/usr/sbin:/sbin",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
        "GIT_SSH_COMMAND": "ssh -o BatchMode=yes",
    }
    assert rd.child_env(fly, "new", base)["FLY_API_TOKEN"] == "new"


def test_machine_count() -> None:
    assert rd.machine_count('{"Machines": [{}, {}]}') == 2
    assert rd.machine_count('note\n{"Machines": []}') == 0
    assert rd.machine_count("not json") is None
    assert rd.machine_count('{"Name": "x"}') is None


def test_subprocess_runner_streams_masks_and_times_out(tmp_path: Path) -> None:
    seen: list[str] = []
    runner = rd.SubprocessDeployRunner(out=seen.append)
    env = {"PATH": "/usr/bin:/bin", "SECRET_V": TOKEN}
    result = runner.run(
        ["/bin/sh", "-c", 'echo one; echo "v=$SECRET_V" >&2; exit 4'],
        cwd=tmp_path,
        env=env,
        timeout_s=30,
        echo=True,
        mask=(TOKEN,),
    )
    assert result.returncode == 4 and not result.timed_out
    assert seen == ["one\n", "v=[redacted]\n"] and TOKEN not in result.tail
    slow = runner.run(
        ["/bin/sh", "-c", "sleep 5"], cwd=tmp_path, env=env, timeout_s=0.2, echo=False
    )
    assert slow.timed_out
    missing = runner.run([str(tmp_path / "nope")], cwd=tmp_path, env=env, timeout_s=5, echo=False)
    assert missing.returncode == rd.EXIT_NO_START


def test_config_validates_fly_app() -> None:
    assert RelayConfig(fly_app="resonant-relay-jhe").fly_app == "resonant-relay-jhe"
    with pytest.raises(ValueError, match="Fly app name"):
        RelayConfig(fly_app="Bad App")


@dataclass
class InterruptedRunner(FakeDeployRunner):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: float,
        echo: bool,
        mask: Sequence[str] = (),
    ) -> rd.RunResult:
        if argv[1] == "deploy":
            raise KeyboardInterrupt
        return super().run(argv, cwd=cwd, env=env, timeout_s=timeout_s, echo=echo, mask=mask)


def test_interrupt_is_audited(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    runner = InterruptedRunner()
    monkeypatch.setattr(cli, "_deploy_runner", lambda: runner)
    code, out = invoke()
    assert code == rd.EXIT_INTERRUPTED and "interrupted" in out
    ((_, detail),) = _audits(env.conn)
    assert json.loads(detail)["exit_code"] == rd.EXIT_INTERRUPTED
