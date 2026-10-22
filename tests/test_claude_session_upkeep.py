"""PER-262: the Claude brain's upkeep, unit level: old transcripts, stale tool bridge dirs,
the ``Read()`` deny rules' case variants and rate-limit events. Temp dirs only."""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import time
from pathlib import Path

import psutil
import pytest

from resonant.config import ClaudeSessionConfig
from resonant.runners.ask import AskScope, case_variants
from resonant.runners.ask_tools import McpServer, sweep_bridge_dirs
from resonant.runners.claude_session import (
    ClaudeSession,
    project_dir_name,
    sweep_transcripts,
)
from resonant.store.claude_brain import SESSION_KV
from resonant.store.db import kv_set

A = "11111111-1111-4111-8111-111111111111"
B = "22222222-2222-4222-8222-222222222222"
C = "33333333-3333-4333-8333-333333333333"
D = "44444444-4444-4444-8444-444444444444"
DAY = 86400.0


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t), follow_symlinks=False)


# --- transcripts --------------------------------------------------------------------------


def test_project_dir_name_matches_claude_codes_naming(tmp_path: Path) -> None:
    cwd = tmp_path / ".resonant" / "ask" / "session"
    cwd.mkdir(parents=True)
    real = os.path.realpath(cwd)
    name = project_dir_name(cwd)
    assert name == "".join(c if c.isascii() and c.isalnum() else "-" for c in real)
    assert "--resonant-ask-session" in name  # "/." -> "--"


def test_old_transcripts_go_and_the_rest_stay(tmp_path: Path) -> None:
    where = tmp_path / "proj"
    where.mkdir()
    old, new, current = where / f"{A}.jsonl", where / f"{B}.jsonl", where / f"{C}.jsonl"
    for f in (old, new, current):
        f.write_text("{}\n")
    tool_dir = where / A  # a session's own dir (tool results)
    tool_dir.mkdir()
    (tool_dir / "x.txt").write_text("x")
    memory = where / "memory"  # not a session: never touched
    memory.mkdir()
    other = where / "notes.jsonl"
    other.write_text("{}\n")
    outside = tmp_path / "outside.jsonl"
    outside.write_text("keep")
    link = where / f"{D}.jsonl"  # a symlink: never followed nor removed
    link.symlink_to(outside)
    for p in (old, current, tool_dir, memory, other, link, outside):
        _age(p, 30 * DAY)
    _age(new, 1 * DAY)

    n = sweep_transcripts(where, time.time() - 14 * DAY, keep={C})

    assert n == 2  # the old transcript and its dir
    assert not old.exists() and not tool_dir.exists()
    assert new.exists() and current.exists() and memory.is_dir() and other.exists()
    assert link.is_symlink() and outside.read_text() == "keep"


def test_a_missing_or_linked_dir_is_left_alone(tmp_path: Path) -> None:
    assert sweep_transcripts(tmp_path / "nope", time.time(), keep=set()) == 0
    real = tmp_path / "real"
    real.mkdir()
    (real / f"{A}.jsonl").write_text("{}")
    _age(real / f"{A}.jsonl", 30 * DAY)
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)
    assert sweep_transcripts(linked, time.time(), keep=set()) == 0
    assert (real / f"{A}.jsonl").exists()


def _session(db: sqlite3.Connection, tmp_path: Path, **cfg: float) -> ClaudeSession:
    cwd = tmp_path / "ask" / "session"
    cwd.mkdir(parents=True)
    return ClaudeSession(
        db,
        cfg=ClaudeSessionConfig.model_validate(cfg),
        cwd=cwd,
        server=McpServer(),
        home=tmp_path / "claude-home",
    )


async def test_the_session_sweeps_only_its_own_dir_and_spares_the_kv_session(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    session = _session(db, tmp_path)
    where = session.transcripts()
    assert where is not None
    assert where == tmp_path / "claude-home" / ".claude" / "projects" / project_dir_name(
        session.cwd
    )
    neighbour = where.parent / "-some-other-project"
    for d in (where, neighbour):
        d.mkdir(parents=True)
        for sid in (A, B):
            (d / f"{sid}.jsonl").write_text("{}")
            _age(d / f"{sid}.jsonl", 30 * DAY)
    kv_set(db, SESSION_KV, {"session_id": B})

    assert await session.sweep() == 1

    assert not (where / f"{A}.jsonl").exists()
    assert (where / f"{B}.jsonl").exists()  # the current session
    assert (neighbour / f"{A}.jsonl").exists() and (neighbour / f"{B}.jsonl").exists()


async def test_transcript_days_zero_keeps_everything(
    db: sqlite3.Connection, tmp_path: Path
) -> None:
    session = _session(db, tmp_path, transcript_days=0)
    where = session.transcripts()
    assert where is not None
    where.mkdir(parents=True)
    (where / f"{A}.jsonl").write_text("{}")
    _age(where / f"{A}.jsonl", 3650 * DAY)
    assert await session.sweep() == 0
    assert (where / f"{A}.jsonl").exists()


async def test_startup_with_nothing_left_is_a_no_op(db: sqlite3.Connection, tmp_path: Path) -> None:
    session = _session(db, tmp_path)
    await session.startup()  # no kv, no transcripts dir: nothing to do, nothing raised
    assert not session.alive()


# --- bridge dirs --------------------------------------------------------------------------


def _bridge(base: Path, name: str, owner: object | None, *, age: float = 3600) -> Path:
    d = base / f"rsn-mcp-{name}"
    d.mkdir(parents=True)
    if owner is not None:
        (d / "owner").write_text(json.dumps(owner))
    _age(d, age)
    return d


def test_bridge_dirs_of_a_gone_daemon_go_and_live_ones_stay(tmp_path: Path) -> None:
    me = psutil.Process()
    live = _bridge(tmp_path, "live", {"pid": os.getpid(), "created": me.create_time()})
    reused = _bridge(tmp_path, "reused", {"pid": os.getpid(), "created": 1.0})
    gone = _bridge(tmp_path, "gone", {"pid": 2**22 + 12345, "created": 1.0})
    young = _bridge(tmp_path, "young", None, age=0)
    legacy_dead = _bridge(tmp_path, "legacy-dead", None)
    (legacy_dead / "mcp.json").write_text("{}")
    _age(legacy_dead, 3600)  # its socket is gone: nothing listens
    other = tmp_path / "not-a-bridge"
    other.mkdir()
    _age(other, 3600)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "rsn-mcp-link"
    link.symlink_to(target, target_is_directory=True)

    assert sweep_bridge_dirs(tmp_path) == 3

    assert live.is_dir() and young.is_dir() and other.is_dir()
    assert link.is_symlink() and target.is_dir()
    assert not reused.exists() and not gone.exists() and not legacy_dead.exists()


def test_a_legacy_bridge_dir_whose_socket_listens_stays(tmp_path: Path) -> None:
    base = Path(os.path.realpath("/tmp")) / f"rsn-test-{os.getpid()}"
    base.mkdir(exist_ok=True)  # a unix socket path must stay short
    try:
        d = _bridge(base, "legacy-live", None)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as srv:
            srv.bind(str(d / "s"))
            srv.listen(1)
            _age(d, 3600)
            assert sweep_bridge_dirs(base) == 0
        assert d.is_dir()
        assert sweep_bridge_dirs(base) == 1  # nothing listens now
    finally:
        for p in sorted(base.rglob("*"), reverse=True):
            p.unlink() if not p.is_dir() else p.rmdir()
        base.rmdir()


async def test_a_bridge_dir_records_its_owner() -> None:
    server = McpServer()
    config = await server.start()
    try:
        owner = json.loads((config.parent / "owner").read_text())
        assert owner["pid"] == os.getpid()
        assert owner["created"] == pytest.approx(psutil.Process().create_time())
        assert (config.parent / "owner").stat().st_mode & 0o777 == 0o600
    finally:
        await server.aclose()
    assert not config.parent.exists()


# --- deny rules (the CLI probe, docs/brain-plan.md PER-262) -----------------------------------


def test_case_variants() -> None:
    assert case_variants(".env*") == [".env*", ".ENV*", ".Env*"]
    assert case_variants("*.pem") == ["*.pem", "*.PEM", "*.Pem"]
    assert case_variants("id_*") == ["id_*", "ID_*", "Id_*"]
    assert case_variants(".git/config") == [".git/config", ".GIT/CONFIG", ".Git/config"]
    assert case_variants("*") == ["*"]
    assert case_variants("Credentials") == ["Credentials", "credentials", "CREDENTIALS"]


def test_deny_rules_write_globs_per_case_and_paths_once(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    rules = AskScope((tmp_path,), (".env*", "*.pem", str(secret))).deny_rules()
    for g in (".env*", ".ENV*", ".Env*", "*.pem", "*.PEM", "*.Pem"):
        assert f"Read(**/{g})" in rules and f"Read(**/{g}/**)" in rules
    real = Path(os.path.realpath(secret))
    path_rules = [r for r in rules if not r.startswith("Read(**/")]
    assert set(path_rules) == {
        f"Read(/{secret})",
        f"Read(/{secret}/**)",
        f"Read(/{real})",
        f"Read(/{real}/**)",
    }  # a path rule already ignores case (probed): no variants
    assert len(rules) == len(set(rules))


def test_the_default_deny_list_covers_an_upper_case_env_file() -> None:
    from resonant.config import DEFAULT_READ_DENY

    rules = AskScope((), DEFAULT_READ_DENY).deny_rules()
    assert "Read(**/.ENV*)" in rules and "Read(**/.env*)" in rules
    assert "Read(**/*.KEY)" in rules and "Read(**/ID_*)" in rules


# --- rate limits --------------------------------------------------------------------------


def test_a_rate_limit_warning_is_logged_once_per_window(
    db: sqlite3.Connection, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    session = _session(db, tmp_path)
    near = {"type": "rate_limit_event", "rate_limit_info": {
        "status": "allowed_warning", "rateLimitType": "five_hour", "resetsAt": 1760000000,
        "utilization": 0.9}}  # fmt: skip
    fine = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}
    with caplog.at_level(logging.INFO, logger="resonant.runners.claude_session"):
        session.note_rate_limit(fine)
        session.note_rate_limit(near)
        session.note_rate_limit(near)
    warned = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warned) == 1
    assert "allowed_warning" in warned[0].getMessage()
