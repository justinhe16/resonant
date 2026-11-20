"""PER-290: two daemons, one checkout.

Personal Resonant and Cori run from the same checkout with different homes. Shared agents
stay personal Resonant's, a deploy restarts every sibling (each says "Back up" itself),
personal Resonant's kill switch reaches Cori, backups in a shared ``extra_dir`` stay apart,
and neither brain can write the other's home. No real launchctl, git, uv or Keychain call:
launchd and the ops runner are faked, and HOME is a temp dir (conftest), so
``~/.resonant-cori`` is never the owner's.
"""

from __future__ import annotations

import os
import plistlib
import shlex
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from test_router import fast_router, handlers, msg, principals, registry, tool_calls
from typer.testing import CliRunner

from resonant import backup, cli, instance, killswitch, launchd
from resonant.api import DaemonState
from resonant.components import build_components, shell_policy
from resonant.config import Settings, load_settings
from resonant.executor import ToolFailed
from resonant.gate import Deny, Intent, PolicyGate
from resonant.gate.shell import HOME_WRITE, ShellPolicy
from resonant.loop import Loop, SlotPool
from resonant.ops import LAUNCHCTL, ProcResult, back_up, restart_pending
from resonant.ops import siblings as sib
from resonant.ops.fakes import FakeOpsRunner
from resonant.router import Router
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store.db import kv_get, kv_set, open_db, transaction
from resonant.toolcore.reports import REPORTS
from resonant.tools.ops import GIT, MIGRATIONS_DIR, SELF_UPDATE_OFF, Ops

# fixtures of test_router's, used by the /kill test below
__all__ = ["fast_router", "handlers", "principals", "registry", "tool_calls"]

OWNER = "justin"
UID = 501
T0 = datetime(2026, 11, 20, 15, 0, tzinfo=UTC)
UV = "/opt/homebrew/bin/uv"
BRANCH = (GIT, "rev-parse", "--abbrev-ref", "HEAD")
STATUS = (GIT, "status", "--porcelain")
HEAD = (GIT, "rev-parse", "--short=7", "HEAD")
PULL = (GIT, "pull", "--ff-only")
OLD, NEW = "a1b2c3d", "e4f5a6b"
DIFF = (GIT, "diff", "--name-only", OLD, NEW, "--", MIGRATIONS_DIR)
CORI_LABEL = "com.resonant.cori.daemon"
PRINT_CORI = (LAUNCHCTL, "print", f"gui/{UID}/{CORI_LABEL}")
KICK_CORI = (LAUNCHCTL, "kickstart", "-k", f"gui/{UID}/{CORI_LABEL}")
HELPER = (sys.executable, "-m", "resonant.ops.restart_later", "com.resonant.daemon", "3")
PRINCIPALS = """\
principals:
  justin:
    role: owner
    identities: ["imessage:+15551234567"]
"""


def ok(stdout: str = "") -> ProcResult:
    return ProcResult(0, stdout, "")


@pytest.fixture
def cori_home(user_home: Path) -> Path:
    """Cori installed beside personal Resonant: her home, store and LaunchAgent plist."""
    home = user_home / ".resonant-cori"
    home.mkdir()
    (home / "config.yaml").write_text("instance:\n  name: cori\napi:\n  port: 7778\n")
    open_db(home / "resonant.db").close()
    agents = user_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    plist = {"Label": CORI_LABEL, "EnvironmentVariables": {"RESONANT_HOME": str(home),
             "RESONANT_INSTANCE": "cori"}}  # fmt: skip
    (agents / f"{CORI_LABEL}.plist").write_bytes(plistlib.dumps(plist))
    return home


def cori_settings(cori_home: Path, **kw: Any) -> Settings:
    return load_settings(home=cori_home, **kw)


# --- siblings -----------------------------------------------------------------------------


def test_siblings_come_from_the_launch_agents(cori_home: Path, user_home: Path) -> None:
    agents = user_home / "Library" / "LaunchAgents"
    (agents / "com.resonant.daemon.plist").write_bytes(plistlib.dumps({"Label": "x"}))
    (agents / "com.resonant.ollama.plist").write_bytes(b"not a daemon")
    (agents / "com.resonant.gone.daemon.plist").write_bytes(
        plistlib.dumps({"EnvironmentVariables": {"RESONANT_HOME": str(user_home / "missing")}})
    )
    (agents / "com.resonant.junk.daemon.plist").write_bytes(b"\x00 not a plist")
    personal = instance.siblings("resonant")
    assert [(s.name, s.label, s.home, s.display_name) for s in personal] == [
        ("cori", CORI_LABEL, cori_home, "Cori")
    ]  # missing home and unreadable plist: skipped
    (user_home / ".resonant").mkdir()
    assert [(s.name, s.home) for s in instance.siblings("cori")] == [
        ("resonant", user_home / ".resonant")  # no RESONANT_HOME in its plist: the default
    ]


def test_no_launch_agents_no_siblings() -> None:
    assert instance.siblings("resonant") == []


# --- shared agents: personal Resonant owns them -------------------------------------------


@dataclass
class FakeLaunchd:
    installed: list[str]
    removed: list[str]


@pytest.fixture
def fake_launchd(monkeypatch: pytest.MonkeyPatch) -> FakeLaunchd:
    seen = FakeLaunchd([], [])

    def install(plist: dict[str, Any]) -> Path:
        seen.installed.append(plist["Label"])
        return Path("/nowhere") / f"{plist['Label']}.plist"

    def uninstall(label: str) -> bool:
        seen.removed.append(label)
        return True

    monkeypatch.setattr(launchd, "install", install)
    monkeypatch.setattr(launchd, "uninstall", uninstall)
    monkeypatch.setattr(launchd, "find_ollama", lambda: "/opt/homebrew/bin/ollama")
    monkeypatch.setattr(cli, "_port_problem", lambda settings: None)  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    return seen


SHARED = {launchd.CAFFEINATE_LABEL, launchd.OLLAMA_LABEL}


def test_cori_install_never_touches_the_shared_agents(
    cori_home: Path, fake_launchd: FakeLaunchd, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("RESONANT_HOME")
    monkeypatch.setenv("RESONANT_INSTANCE", "cori")
    result = CliRunner().invoke(cli.app, ["daemon", "install"])
    assert result.exit_code == 0, result.output
    assert fake_launchd.installed == [CORI_LABEL]
    assert fake_launchd.removed == []
    assert "personal Resonant owns them" in result.output
    result = CliRunner().invoke(cli.app, ["daemon", "uninstall"])
    assert result.exit_code == 0, result.output
    assert fake_launchd.removed == [CORI_LABEL]
    assert not SHARED & set(fake_launchd.installed + fake_launchd.removed)


def test_personal_install_is_unchanged(fake_launchd: FakeLaunchd) -> None:
    result = CliRunner().invoke(cli.app, ["daemon", "install"])
    assert result.exit_code == 0, result.output
    assert fake_launchd.installed == [
        launchd.OLLAMA_LABEL, launchd.CAFFEINATE_LABEL, "com.resonant.daemon",
    ]  # fmt: skip
    CliRunner().invoke(cli.app, ["daemon", "uninstall"])
    assert fake_launchd.removed == [
        "com.resonant.daemon", launchd.OLLAMA_LABEL, launchd.CAFFEINATE_LABEL,
    ]  # fmt: skip


@pytest.mark.parametrize("key", ["power:\n  keep_awake: true", "model:\n  manage: true"])
def test_cori_refuses_the_shared_agents(cori_home: Path, key: str) -> None:
    (cori_home / "config.yaml").write_text(f"instance:\n  name: cori\n{key}\n")
    with pytest.raises(ValidationError, match="personal Resonant owns it"):
        cori_settings(cori_home)


def test_cori_reads_the_shared_agents_as_off(cori_home: Path) -> None:
    s = cori_settings(cori_home)
    assert s.power.keep_awake is False and s.model.manage is False
    assert s.model.base_url == "http://127.0.0.1:11434/v1"  # the owner's Ollama
    personal = load_settings()
    assert personal.power.keep_awake is True and personal.model.manage is True
    assert personal.ops.self_update is True and personal.runner.self_repo is True


def test_the_cori_example_turns_off_deploys_and_the_self_repo(cori_home: Path) -> None:
    example = Path(__file__).resolve().parents[1] / "config" / "cori.example.yaml"
    (cori_home / "config.yaml").write_text(example.read_text())
    s = cori_settings(cori_home)
    assert s.ops.self_update is False and s.runner.self_repo is False
    assert s.power.keep_awake is False and s.model.manage is False


def test_restart_ollama_is_personals(cori_home: Path) -> None:
    db = open_db(cori_home / "resonant.db")
    for settings, registered in ((cori_settings(cori_home), False), (load_settings(), True)):
        loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
        state = DaemonState(settings, db, loop, started_at=T0)
        secrets = SecretStore(FakeKeychain())
        c = build_components(settings, db, state, ops_runner=FakeOpsRunner(), secrets=secrets)
        assert (c.registry.get(None, "restart_ollama") is not None) is registered
    db.close()


# --- deploys --------------------------------------------------------------------------------


def personal_ops(
    db: sqlite3.Connection, runner: FakeOpsRunner, repo: Path, **kw: Any
) -> tuple[Ops, Settings]:
    settings = load_settings(ops={"repo_path": repo, "uv_path": UV}, **kw)
    return Ops(db, settings, runner, owner=OWNER, clock=lambda: T0, uid=UID), settings


def scripted(*, migration: bool, cori_loaded: bool = True, kick: ProcResult | None = None
             ) -> FakeOpsRunner:  # fmt: skip
    runner = FakeOpsRunner().ok(*BRANCH, stdout="main\n").ok(*STATUS, stdout="")
    runner.on(*HEAD, results=[ok(OLD + "\n"), ok(NEW + "\n")])
    runner.ok(*DIFF, stdout=f"{MIGRATIONS_DIR}/031_x.sql\n" if migration else "")
    runner.on(*PRINT_CORI, results=[ok() if cori_loaded else ProcResult(113, "", "nope")])
    runner.on(*KICK_CORI, results=[kick or ok()])
    return runner


class Sent:
    def __init__(self) -> None:
        self.texts: list[tuple[str, str]] = []

    async def send(self, principal: str, text: str, *, dedupe_key: str | None = None) -> object:
        self.texts.append((principal, text))
        return None


async def test_personal_update_backs_up_both_and_restarts_cori_then_itself(
    cori_home: Path, tmp_path: Path, resonant_home: Path
) -> None:
    db = open_db(resonant_home / "resonant.db")
    runner = scripted(migration=True)
    ops, _ = personal_ops(db, runner, tmp_path)
    out = await ops.update_resonant()
    argvs = runner.argvs()
    assert argvs.count(PULL) == 1
    assert sum(1 for a in argvs if a[:2] == (UV, "sync")) == 1
    assert argvs.index(DIFF) < argvs.index(PRINT_CORI) < argvs.index(KICK_CORI)
    assert runner.spawned == [HELPER]  # personal restarts last, after Cori's kickstart
    assert out["migration"] is True
    assert out["backups"] == [{"name": "Resonant", "ok": True}, {"name": "Cori", "ok": True}]
    assert out["siblings"] == [{"name": "Cori", "state": "restarted"}]
    # each store has its own copy, named as its own
    assert [p.name for _, p in backup.copies(resonant_home / "backups" / "daily")] == [
        "resonant-2026-11-20.db"
    ]
    assert [p.name for _, p in backup.copies(cori_home / "backups" / "daily", "cori")] == [
        "cori-2026-11-20.db"
    ]
    # each says "Back up" on its own channel, to its own owner
    cori_db = open_db(cori_home / "resonant.db")
    marker = restart_pending(cori_db)
    assert marker is not None and marker["commit"] == NEW and marker["by"] == ""
    cori_channel, personal_channel = Sent(), Sent()
    assert await back_up(cori_db, cori_channel, "cori-owner", clock=lambda: T0)
    assert await back_up(db, personal_channel, OWNER, clock=lambda: T0)
    assert cori_channel.texts == [("cori-owner", f"Back up ({NEW}), down 0s")]
    assert personal_channel.texts == [(OWNER, f"Back up ({NEW}), down 0s")]
    reply = REPORTS["update_resonant"](out)
    assert reply == f"Updated {OLD} → {NEW}, restarting…\nCori: restarting"


async def test_no_migration_no_backups(
    cori_home: Path, tmp_path: Path, resonant_home: Path
) -> None:
    db = open_db(resonant_home / "resonant.db")
    ops, _ = personal_ops(db, scripted(migration=False), tmp_path)
    out = await ops.update_resonant()
    assert "backups" not in out and out["siblings"] == [{"name": "Cori", "state": "restarted"}]
    assert not (cori_home / "backups").exists()


async def test_a_stopped_cori_stays_stopped(cori_home: Path, tmp_path: Path) -> None:
    db = open_db(tmp_path / "p.db")
    runner = scripted(migration=False, cori_loaded=False)
    ops, _ = personal_ops(db, runner, tmp_path)
    out = await ops.update_resonant()
    assert KICK_CORI not in runner.argvs()
    assert out["siblings"] == [{"name": "Cori", "state": "stopped"}]
    assert restart_pending(open_db(cori_home / "resonant.db")) is None
    assert (REPORTS["update_resonant"](out) or "").endswith("\nCori: stopped, not started")
    assert runner.spawned == [HELPER]


async def test_cori_failing_to_restart_still_restarts_personal(
    cori_home: Path, tmp_path: Path
) -> None:
    db = open_db(tmp_path / "p.db")
    ops, _ = personal_ops(db, scripted(migration=False, kick=ProcResult(5, "", "x")), tmp_path)
    out = await ops.update_resonant()
    assert out["siblings"] == [{"name": "Cori", "state": "failed", "detail": "exit 5"}]
    assert out["restarting"] is True and ops.runner.spawned == [HELPER]  # type: ignore[attr-defined]  # pyright: ignore[reportAttributeAccessIssue, reportUnknownMemberType]
    assert restart_pending(open_db(cori_home / "resonant.db")) is None  # no stale "Back up"
    assert (REPORTS["update_resonant"](out) or "").endswith("\nCori didn't come back (exit 5)")


async def test_a_sibling_with_a_missing_home_is_skipped(user_home: Path, tmp_path: Path) -> None:
    agents = user_home / "Library" / "LaunchAgents"
    agents.mkdir(parents=True)
    (agents / f"{CORI_LABEL}.plist").write_bytes(
        plistlib.dumps({"EnvironmentVariables": {"RESONANT_HOME": str(user_home / "nope")}})
    )
    db = open_db(tmp_path / "p.db")
    runner = scripted(migration=True)
    ops, _ = personal_ops(db, runner, tmp_path)
    out = await ops.update_resonant()
    assert "siblings" not in out and DIFF not in runner.argvs()  # as with no siblings
    assert runner.spawned == [HELPER]


# --- Cori's deploys come from personal Resonant -------------------------------------------


async def test_cori_refuses_code_updates(cori_home: Path, tmp_path: Path) -> None:
    settings = cori_settings(cori_home, ops={"repo_path": tmp_path, "uv_path": UV,
                                             "self_update": False})  # fmt: skip
    db = open_db(settings.db_path)
    runner = FakeOpsRunner()
    ops = Ops(db, settings, runner, owner="cori-owner", clock=lambda: T0, uid=UID)
    with pytest.raises(ToolFailed, match=SELF_UPDATE_OFF):
        await ops.update_resonant()  # /update, update_resonant and a worker's resonant update
    assert runner.calls == [] and runner.spawned == []
    restarted = await ops.restart_resonant()  # resonant restart: Cori only
    assert restarted["label"] == CORI_LABEL
    assert runner.spawned == [(sys.executable, "-m", "resonant.ops.restart_later", CORI_LABEL, "3")]


def test_cori_runtime_state_stays_hers(cori_home: Path) -> None:
    """``self_update: false`` is code deploys only: watchers and trust rules (and later the
    registered jobs) are still hers to change through her gate."""
    settings = cori_settings(cori_home, dry_run=False, ops={"self_update": False})
    settings.principals_path.write_text(PRINCIPALS)
    db = open_db(settings.db_path)
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=T0)
    secrets = SecretStore(FakeKeychain())
    c = build_components(settings, db, state, ops_runner=FakeOpsRunner(), secrets=secrets)
    assert isinstance(c.gate, PolicyGate)
    names = ("create_watch", "cancel_watch", "add_trust_rule", "allow_trust_rule",
             "remove_trust_rule")  # fmt: skip
    for name in names:
        spec = c.registry.get(None, name)
        assert spec is not None, name
        intent = Intent.for_tool(spec, task_id="t", principal=OWNER, args={}, origin="explicit")
        assert not isinstance(c.gate.evaluate(intent), Deny), name
    assert c.registry.get(None, "restart_ollama") is None
    db.close()


# --- the kill switch across instances -----------------------------------------------------


class CliOpsRunner(FakeOpsRunner):
    """Runs a sibling's ``/usr/bin/env K=V … python -m resonant <args>`` in-process with
    Typer's runner, so the fan-out reaches the real ``resonant pause``/``resume``."""

    def __init__(self, fail: bool = False) -> None:
        super().__init__()
        self.failing = fail
        self.outputs: list[str] = []

    async def run(
        self, argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None
    ) -> ProcResult:
        await super().run(argv, timeout_s=timeout_s, cwd=cwd)
        if self.failing:
            return ProcResult(1, "", "boom")
        assert argv[0] == sib.ENV
        env = dict(a.split("=", 1) for a in argv[1:] if "=" in a and not a.startswith("/"))
        args = list(argv[list(argv).index("resonant") + 1 :])
        result = CliRunner().invoke(cli.app, args, env=env)
        self.outputs.append(result.output)
        return ProcResult(result.exit_code, result.output, "")


def cori_db(cori_home: Path) -> sqlite3.Connection:
    return open_db(cori_home / "resonant.db")


async def test_personal_kill_pauses_cori_and_resume_resumes_her(cori_home: Path) -> None:
    runner = CliOpsRunner()
    fan = sib.kill_switch_fan_out("resonant", runner)
    assert fan is not None
    assert await fan(True) == ["Cori: paused"]
    (argv,) = runner.argvs()
    assert argv[:4] == (sib.ENV, f"RESONANT_HOME={cori_home}", "RESONANT_INSTANCE=cori",
                        sys.executable)  # fmt: skip
    assert shlex.join(argv).endswith("-m resonant pause --by sibling:resonant")
    conn = cori_db(cori_home)
    assert killswitch.is_paused(conn) and killswitch.paused_by(conn) == "sibling:resonant"
    assert kv_get(conn, "autonomy") == "off"
    assert await fan(False) == ["Cori: resumed"]
    assert not killswitch.is_paused(conn) and killswitch.paused_by(conn) is None


async def test_an_approvers_pause_on_cori_survives_personal_resume(cori_home: Path) -> None:
    conn = cori_db(cori_home)
    killswitch.engage(conn, actor="slack:U123")  # an approver's Cori-only pause (PER-293)
    fan = sib.kill_switch_fan_out("resonant", CliOpsRunner())
    assert fan is not None
    assert await fan(True) == ["Cori: paused"]
    assert killswitch.paused_by(conn) == "slack:U123"  # the first source stays
    assert await fan(False) == ["Cori: still paused (not paused by Resonant)"]
    assert killswitch.is_paused(conn) and killswitch.paused_by(conn) == "slack:U123"


async def test_a_sibling_cli_failure_is_reported(cori_home: Path) -> None:
    fan = sib.kill_switch_fan_out("resonant", CliOpsRunner(fail=True))
    assert fan is not None
    assert await fan(True) == ["Cori: couldn't pause (exit 1)"]
    timeout = FakeOpsRunner().on(sib.ENV, results=[TimeoutError()])
    fan = sib.kill_switch_fan_out("resonant", timeout)
    assert fan is not None
    assert await fan(True) == ["Cori: couldn't pause (timed out after 20s)"]


def test_only_personal_fans_out() -> None:
    assert sib.kill_switch_fan_out("cori", FakeOpsRunner()) is None


def test_cli_kill_fans_out_and_personal_pauses_even_when_cori_fails(
    cori_home: Path, resonant_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(cli, "_siblings_runner", lambda: CliOpsRunner(fail=True))
    result = CliRunner().invoke(cli.app, ["kill"])
    assert result.exit_code == 0, result.output
    assert "Cori: couldn't pause (exit 1)" in result.output
    assert killswitch.is_paused(open_db(resonant_home / "resonant.db"))
    monkeypatch.setattr(cli, "_siblings_runner", lambda: CliOpsRunner())
    CliRunner().invoke(cli.app, ["kill"])
    assert killswitch.paused_by(cori_db(cori_home)) == "sibling:resonant"
    result = CliRunner().invoke(cli.app, ["resume"])
    assert result.exit_code == 0 and "Cori: resumed" in result.output


def test_pause_and_resume_if_by_on_the_cli(resonant_home: Path) -> None:
    conn = open_db(resonant_home / "resonant.db")
    r = CliRunner().invoke(cli.app, ["resume", "--if-by", "sibling:resonant"])
    assert r.exit_code == 0 and "not paused" in r.output
    assert CliRunner().invoke(cli.app, ["pause", "--by", "not a source"]).exit_code == 2
    killswitch.engage(conn, actor=OWNER)
    r = CliRunner().invoke(cli.app, ["resume", "--if-by", "sibling:resonant"])
    assert r.exit_code == sib.EXIT_KEPT and killswitch.is_paused(conn)
    killswitch.release(conn, actor=OWNER)
    assert CliRunner().invoke(cli.app, ["pause", "--by", "sibling:resonant"]).exit_code == 0
    assert killswitch.paused_by(conn) == "sibling:resonant"


async def test_a_pause_from_another_process_fires_the_hooks_on_the_next_tick(
    tmp_path: Path,
) -> None:
    """Personal's fan-out writes Cori's ``paused`` from another process: her scheduler's next
    tick runs the pause hooks (runs cancelled, the brain session stopped), once."""
    from test_scheduler import FakeExtProcess, at_local, harness

    heard: list[bool] = []
    async with harness(tmp_path, FakeExtProcess()) as h:
        remove = killswitch.add_pause_hook(lambda conn, paused: heard.append(paused))
        try:
            await h.tick(at_local(7))
            assert heard == []
            with transaction(h.db):
                kv_set(h.db, "paused", True)  # as `resonant pause` in another process does
            await h.tick(at_local(7, 1))
            assert heard == [True]
            await h.tick(at_local(7, 2))
            assert heard == [True]  # once
            killswitch.release(h.db, actor=OWNER)  # in-process: the hook fires right away
            await h.tick(at_local(7, 3))
            assert heard == [True, False]
        finally:
            remove()


async def test_personal_slash_kill_names_each_sibling(
    db: sqlite3.Connection, fast_router: Router
) -> None:
    from resonant.router.fast_path import KILL_REPLY, RESUME_REPLY

    calls: list[bool] = []

    async def fan(paused: bool) -> list[str]:
        calls.append(paused)
        return ["Cori: couldn't pause (exit 1)"] if paused else ["Cori: resumed"]

    fast_router.fast_path.siblings = fan
    task = await fast_router.route(msg("/kill"))
    assert task is not None
    assert task.checkpoint["fast_reply"] == f"{KILL_REPLY}\nCori: couldn't pause (exit 1)"
    assert kv_get(db, "paused") is True  # personal paused whatever Cori said
    task = await fast_router.route(msg("/resume"))
    assert task is not None
    assert task.checkpoint["fast_reply"] == f"{RESUME_REPLY}\nCori: resumed"
    assert calls == [True, False]

    async def broken(paused: bool) -> list[str]:
        raise RuntimeError("x")

    fast_router.fast_path.siblings = broken
    task = await fast_router.route(msg("/kill"))
    assert task is not None
    assert task.checkpoint["fast_reply"].startswith(KILL_REPLY + "\n")
    assert kv_get(db, "paused") is True


# --- backups in a shared extra_dir ---------------------------------------------------------


def test_two_instances_share_an_extra_dir(cori_home: Path, tmp_path: Path) -> None:
    extra = tmp_path / "extra"
    extra.mkdir()
    cfg = {"extra_dir": extra, "keep_daily": 2, "keep_weekly": 0}
    personal = load_settings(backup=cfg)
    cori = cori_settings(cori_home, backup=cfg)
    open_db(personal.db_path).close()
    # older copies of both, plus an unrelated file
    for d in (date(2026, 11, 1), date(2026, 11, 2), date(2026, 11, 3)):
        (extra / backup.backup_name(d)).write_bytes(b"old")
        (extra / backup.backup_name(d, "cori")).write_bytes(b"old")
    (extra / "notes.db").write_bytes(b"keep")
    backup.create_backup(cori, now=T0)
    names = sorted(p.name for p in extra.iterdir() if not p.name.startswith("."))
    assert names == [
        "cori-2026-11-03.db", "cori-2026-11-20.db", "notes.db",
        "resonant-2026-11-01.db", "resonant-2026-11-02.db", "resonant-2026-11-03.db",
    ]  # fmt: skip
    backup.create_backup(personal, now=T0)
    names = sorted(p.name for p in extra.iterdir() if not p.name.startswith("."))
    assert names == [
        "cori-2026-11-03.db", "cori-2026-11-20.db", "notes.db",
        "resonant-2026-11-03.db", "resonant-2026-11-20.db",
    ]  # fmt: skip
    assert [p.name for _, p in backup.copies(extra, backup.prefix_of(cori))] == [
        "cori-2026-11-20.db", "cori-2026-11-03.db",
    ]  # fmt: skip


def test_backup_list_shows_only_its_own(cori_home: Path, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:  # fmt: skip
    daily = cori_home / "backups" / "daily"
    daily.mkdir(parents=True)
    (daily / "cori-2026-11-19.db").write_bytes(b"x")
    (daily / "resonant-2026-11-19.db").write_bytes(b"x")
    monkeypatch.setenv("RESONANT_HOME", str(cori_home))
    result = CliRunner().invoke(cli.app, ["backup", "list"])
    assert "cori-2026-11-19.db" in result.output
    assert "resonant-2026-11-19.db" not in result.output


# --- neither brain writes the other's home -------------------------------------------------


def _policy(home: Path, user_home: Path, others: Sequence[Path] = ()) -> ShellPolicy:
    return ShellPolicy(read_ok=lambda p: False, home=home, scratch=[home / "ask"],
                       user_home=user_home, daemon_pid=4242, temps=[], others=others)  # fmt: skip


def _never(policy: ShellPolicy, command: str, cwd: Path) -> bool:
    v = policy.judge({"tool": "Bash", "command": command, "cwd": str(cwd)})
    return v.kind == "never" and v.why == HOME_WRITE


def _write_never(policy: ShellPolicy, path: Path) -> bool:
    v = policy.judge({"tool": "Write", "command": str(path), "cwd": "/"})
    return v.kind == "never"


@pytest.fixture
def homes(user_home: Path) -> Iterator[tuple[Path, Path]]:
    personal, cori = user_home / ".resonant", user_home / ".resonant-cori"
    for h in (personal, cori):
        (h / "ask").mkdir(parents=True)
    yield personal, cori


def test_personals_brain_cant_write_coris_home(homes: tuple[Path, Path], user_home: Path) -> None:
    personal, cori = homes
    p = _policy(personal, user_home)
    assert _write_never(p, cori / "config.yaml")
    assert _write_never(p, cori / "ask" / "x")  # her scratch is not personal's
    assert _never(p, f"rm {cori}/resonant.db", user_home)
    assert _never(p, "sqlite3 resonant.db 'delete from kv'", cori)
    assert _never(p, "echo x > ~/.resonant-cori/config.yaml", user_home)
    assert not _write_never(p, personal / "ask" / "notes.md")  # its own scratch: allowed


def test_coris_brain_cant_write_personals_home(homes: tuple[Path, Path], user_home: Path) -> None:
    personal, cori = homes
    c = _policy(cori, user_home)
    assert _write_never(c, personal / "resonant.db")
    assert _never(c, f"cp x {personal}/config.yaml", user_home)
    assert _never(c, "touch ~/.resonant/x", user_home)
    assert not _write_never(c, cori / "ask" / "notes.md")


def test_a_sibling_home_anywhere_is_never_written(user_home: Path, tmp_path: Path) -> None:
    odd = tmp_path / "elsewhere" / "cori-home"
    odd.mkdir(parents=True)
    p = _policy(user_home / ".resonant", user_home, others=[odd])
    assert _write_never(p, odd / "config.yaml")
    assert not _write_never(_policy(user_home / ".resonant", user_home), odd / "config.yaml")


def test_shell_policy_takes_the_installed_siblings(cori_home: Path, user_home: Path) -> None:
    p = shell_policy(load_settings())
    assert p.others == (str(cori_home.resolve()),)
    assert _write_never(p, cori_home / "principals.yaml")


# --- the restore guard sees only its own instance -------------------------------------------


class FakeProc:
    def __init__(self, pid: int, env: dict[str, str] | None) -> None:
        self.info: dict[str, Any] = {
            "pid": pid,
            "uids": type("U", (), {"real": os.getuid()})(),
            "cmdline": ["/venv/bin/python", "-m", "resonant", "daemon", "run"],
        }
        self.env = env

    def environ(self) -> dict[str, str]:
        if self.env is None:
            import psutil

            raise psutil.AccessDenied(self.info["pid"])
        return self.env


def test_restore_guard_ignores_the_other_instances_daemon(
    cori_home: Path, user_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import psutil

    procs = [
        FakeProc(101, {"RESONANT_HOME": str(cori_home), "RESONANT_INSTANCE": "cori"}),
        FakeProc(102, {}),  # personal: ~/.resonant by default
        FakeProc(103, None),  # env unreadable: counts
    ]
    monkeypatch.setattr(psutil, "process_iter", lambda attrs: iter(procs))  # pyright: ignore[reportUnknownLambdaType, reportUnknownArgumentType]
    assert cli._daemon_pids(user_home / ".resonant") == [102, 103]  # pyright: ignore[reportPrivateUsage]
    assert cli._daemon_pids(cori_home) == [101, 103]  # pyright: ignore[reportPrivateUsage]
