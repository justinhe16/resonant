"""ExtensionHost (P3-6): pins, registration, refusals, approvers, alerts, step limit."""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any

import httpx
import pytest

from resonant.api import DaemonState, create_app
from resonant.components import build_components
from resonant.config import Settings, load_settings
from resonant.e2e.extensions import install_fixture
from resonant.extensions.host import ALERTS_KEY, ExtensionHost, send_alerts, tampered_text
from resonant.extensions.process import FakeExtProcess
from resonant.gate import PolicyGate
from resonant.gateway.channel import ChannelError, ChannelRefused
from resonant.loop import Loop, SlotPool
from resonant.ops.fakes import FakeOpsRunner
from resonant.runners.local import MAX_TOOL_ITERATIONS
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store.db import kv_get, utcnow
from resonant.store.extensions import get_extension
from resonant.tools import ToolRegistry
from resonant.wiring import local_step_limit
from resonant_sdk import Effect, Manifest, ToolSpec

CLI = "import json\nprint(json.dumps({'ok': True}))\n"
MANIFEST = """\
name: example
version: 1
description: time and notes
approvers: [maria, ghost, justin]
secrets: [token]
tools:
  get_time:
    run: ./cli.py time {tz}
    effect: read
    args: {tz: string}
  write_note:
    run: ./cli.py note {text}
    effect: write
    level: L3
    args: {text: string}
    secrets: [token]
jobs:
  - id: long-job
    schedule: "0 3 * * *"
    run: ./cli.py job
    effect: read
    timeout_s: 1800
"""
PRINCIPALS = """\
principals:
  justin:
    role: owner
    identities: ["imessage:+15551234567"]
  maria:
    identities: ["imessage:+15557654321"]
    grants: {example: [request, approve]}
  ghost:
    identities: ["imessage:+15550000000"]
"""


@pytest.fixture
def settings() -> Settings:
    s = load_settings()
    s.principals_path.write_text(PRINCIPALS)
    return s


def host_for(settings: Settings, db: sqlite3.Connection) -> ExtensionHost:
    host = ExtensionHost.from_settings(settings, db, ToolRegistry(), runner=FakeExtProcess())
    host.load()
    return host


def actions(db: sqlite3.Connection, action: str) -> list[sqlite3.Row]:
    return db.execute("SELECT * FROM audit_log WHERE action = ?", (action,)).fetchall()


# --- load and register ----------------------------------------------------------------


def test_registers_tools_and_jobs_keeping_jobs_out_of_chat(
    settings: Settings, db: sqlite3.Connection
) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    host = host_for(settings, db)
    assert sorted(host.loaded) == ["example"] and not host.refused and not host.tampered
    names = [s.name for s in host.registry.specs("example")]
    assert names == ["get_time", "job_long_job", "write_note"]
    assert [s.name for s in host.tool_specs()] == ["get_time", "write_note"]
    assert [s.name for s in host.job_specs("example")] == ["job_long_job"]
    job = host.registry.get("example", "job_long_job")
    assert job is not None and host.is_job(job) and job.timeout_s == 1800
    tool = host.registry.get("example", "write_note")
    assert tool is not None and not host.is_job(tool)
    assert tool.secrets == ("token",) and tool.source == "ext:example"
    assert set(host.handlers) == {(s.extension, s.name) for s in host.registry.specs()}
    assert host.handlers[("example", "job_long_job")].job


async def test_components_merge_handlers_and_api_shows_specs(
    settings: Settings, db: sqlite3.Connection
) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=utcnow())
    c = build_components(
        settings,
        db,
        state,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(FakeKeychain()),
        ext_runner=FakeExtProcess(),
    )
    state.components = c
    assert c.extensions is not None
    assert ("example", "get_time") in c.executor.handlers
    assert ("example", "job_long_job") in c.executor.handlers
    assert ("example", "get_time") not in c.handlers  # builtins only (the fast path)
    assert c.executor.result_max_bytes == settings.extensions.result_max_bytes
    chat = c.chat_specs()
    assert any(s.name == "get_time" for s in chat)
    assert not any(s.name == "job_long_job" for s in chat)
    app = create_app(state)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
    ) as h:
        tools = (await h.get("/api/tools")).json()
    ext = {t["name"]: t for t in tools if t["extension"] == "example"}
    assert set(ext) == {"get_time", "write_note", "job_long_job"}
    assert ext["write_note"]["level"] == "L3" and ext["job_long_job"]["effect"] == "read"
    # The gate's approvers come from the manifest, filtered by grants.
    assert isinstance(c.gate, PolicyGate)
    assert c.gate.approvers("example") == ("justin", "maria")
    assert c.gate.approvers(None) == ("justin",)


def test_approvers_for_fails_closed(settings: Settings, db: sqlite3.Connection) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    host = host_for(settings, db)
    assert host.approvers_for("example") == ("maria", "ghost", "justin")
    assert host.approvers_for("nope") == ()

    class Broken(ExtensionHost):
        def manifest(self, name: str) -> Manifest | None:
            raise RuntimeError("x")

    broken = Broken.from_settings(settings, db, ToolRegistry(), runner=FakeExtProcess())
    assert isinstance(broken, Broken)
    broken.load()
    assert broken.approvers_for("example") == ()


# --- tamper -----------------------------------------------------------------------------


@pytest.mark.parametrize("change", ["edit", "add", "remove", "symlink"])
def test_tamper_disables_and_alerts_once(
    settings: Settings, db: sqlite3.Connection, change: str
) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    root = settings.extensions_dir / "example"
    if change == "edit":
        (root / "cli.py").write_text(CLI + "# evil\n")
    elif change == "add":
        (root / "extra.py").write_text("")
    elif change == "remove":
        (root / "cli.py").unlink()
    else:
        (root / "link").symlink_to("/etc")
    host = host_for(settings, db)
    assert host.tampered == ["example"] and not host.loaded
    assert host.registry.specs("example") == [] and not host.handlers
    rec = get_extension(db, "example")
    assert rec is not None and rec.status == "disabled_tampered"
    assert len(actions(db, "ext.tampered")) == 1
    alerts: dict[str, Any] = kv_get(db, ALERTS_KEY)
    assert alerts["example"]["text"] == tampered_text("example")
    assert tampered_text("example") == (
        "example changed on disk since you approved it; disabled. "
        "resonant ext update example to review"
    )
    # A restart doesn't load it, re-flip it or alert again.
    again = host_for(settings, db)
    assert not again.loaded and not again.tampered
    assert len(actions(db, "ext.tampered")) == 1


class RecordingSender:
    def __init__(self, fail: Exception | None = None) -> None:
        self.sent: list[tuple[str, str, str | None]] = []
        self.fail = fail

    async def send(self, principal: str, text: str, *, dedupe_key: str | None = None) -> None:
        if self.fail is not None:
            raise self.fail
        self.sent.append((principal, text, dedupe_key))


async def test_alert_sent_once_and_kept_on_transient_error(
    settings: Settings, db: sqlite3.Connection
) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    (settings.extensions_dir / "example" / "cli.py").write_text("x")
    host_for(settings, db)
    assert await send_alerts(db, RecordingSender(), None) == 0  # no owner yet: kept
    assert await send_alerts(db, RecordingSender(ChannelError("down")), "justin") == 0
    assert "example" in kv_get(db, ALERTS_KEY)
    ok = RecordingSender()
    assert await send_alerts(db, ok, "justin") == 1
    assert ok.sent[0][:2] == ("justin", tampered_text("example"))
    assert ok.sent[0][2] is not None and ok.sent[0][2].startswith("ext-tampered:example:")
    assert await send_alerts(db, ok, "justin") == 0 and len(ok.sent) == 1


async def test_refused_alert_is_dropped(settings: Settings, db: sqlite3.Connection) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    (settings.extensions_dir / "example" / "cli.py").write_text("x")
    host_for(settings, db)
    assert await send_alerts(db, RecordingSender(ChannelRefused("no id")), "justin") == 0
    assert kv_get(db, ALERTS_KEY) == {}


# --- refusals -----------------------------------------------------------------------------


GH_MANIFEST = """\
name: hub
version: 1
tools:
  prs:
    run: gh pr list
    effect: read
"""

BINS_MANIFEST = """\
name: hub
version: 1
bins: [gh]
tools:
  prs:
    run: ./cli.py prs
    effect: read
health: {check: ./cli.py health, every: 30m}
"""


@pytest.mark.parametrize(
    ("manifest", "files", "argv0", "reason"),
    [
        (
            MANIFEST.replace("run: ./cli.py job", "run: ./cli.py job {when}"),
            {"cli.py": CLI},
            None,
            "job_long_job: undeclared placeholders ['when']",
        ),
        (GH_MANIFEST, {}, {"prs": "/nonexistent/gh"}, "prs: pinned command /nonexistent/gh"),
        (GH_MANIFEST, {}, None, "prs: 'gh' must run by its pinned absolute path"),
        (BINS_MANIFEST, {"cli.py": CLI}, None, "bins: gh isn't pinned; resonant ext update hub"),
        (
            BINS_MANIFEST,
            {"cli.py": CLI},
            {"bin:gh": "/nonexistent/gh"},
            "bins: pinned command /nonexistent/gh is missing",
        ),
        (MANIFEST, {"cli.py": CLI, "requirements.txt": "x"}, None, "requirements.txt: "),
    ],
)
def test_load_refusals_register_nothing(
    settings: Settings,
    db: sqlite3.Connection,
    manifest: str,
    files: dict[str, str],
    argv0: dict[str, str] | None,
    reason: str,
) -> None:
    rec = install_fixture(settings, db, manifest, files, argv0_paths=argv0)
    host = host_for(settings, db)
    assert not host.loaded and not host.handlers
    assert host.registry.specs(rec.name) == []
    assert host.refused[rec.name].startswith(reason)
    assert len(actions(db, "ext.load_refused")) == 1
    stored = get_extension(db, rec.name)
    assert stored is not None and stored.status == "enabled"  # not tampering


def test_one_bad_extension_does_not_block_others(
    settings: Settings, db: sqlite3.Connection
) -> None:
    install_fixture(settings, db, GH_MANIFEST, {})
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    host = host_for(settings, db)
    assert list(host.loaded) == ["example"] and list(host.refused) == ["hub"]


def test_pinned_gh_runs_by_its_path(settings: Settings, db: sqlite3.Connection) -> None:
    fake_gh = settings.home / "bin" / "gh"
    fake_gh.parent.mkdir()
    fake_gh.write_text("#!/bin/sh\n")
    fake_gh.chmod(0o755)
    install_fixture(settings, db, GH_MANIFEST, {}, argv0_paths={"prs": str(fake_gh)})
    host = host_for(settings, db)
    assert "hub" in host.loaded
    assert host.handlers[("hub", "prs")].command.render({}) == (str(fake_gh), "pr", "list")


def test_pinned_bins_reach_every_process_as_env(settings: Settings, db: sqlite3.Connection) -> None:
    fake_gh = settings.home / "bin" / "gh"
    fake_gh.parent.mkdir()
    fake_gh.write_text("#!/bin/sh\n")
    fake_gh.chmod(0o755)
    install_fixture(
        settings, db, BINS_MANIFEST, {"cli.py": CLI}, argv0_paths={"bin:gh": str(fake_gh)}
    )
    host = host_for(settings, db)
    pins = {"RESONANT_BIN_GH": str(fake_gh)}
    assert host.handlers[("hub", "prs")].bins == pins
    assert host.loaded["hub"].bins == pins  # the health check's env


# --- step limit ---------------------------------------------------------------------------


def test_step_limit_ignores_jobs_and_caps_extension_tools(
    settings: Settings, db: sqlite3.Connection
) -> None:
    loop = Loop(db, {}, settings.loop, SlotPool(2, 1))
    state = DaemonState(settings, db, loop, started_at=utcnow())
    builtin_only = local_step_limit(
        settings.model,
        build_components(settings, db, state, ops_runner=FakeOpsRunner()).chat_specs(),
    )
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    c = build_components(settings, db, state, ops_runner=FakeOpsRunner())
    assert any(s.timeout_s == 1800 for s in c.registry.specs())  # the job is registered
    assert local_step_limit(settings.model, c.chat_specs()) == builtin_only  # 1800s ignored
    long_tool = ToolSpec(name="t", effect=Effect.READ, extension="x", timeout_s=1000)
    model_s = (MAX_TOOL_ITERATIONS + 1) * settings.model.timeout_s
    assert local_step_limit(settings.model, [long_tool]) == max(model_s, 300) + 15


def test_no_extensions_dir_is_fine(settings: Settings, db: sqlite3.Connection) -> None:
    assert not settings.extensions_dir.exists()
    host = host_for(settings, db)
    assert host.loaded == {} and host.registry.specs() == []
    assert os.environ.get("RESONANT_HOME") == str(settings.home)


def test_snapshot_must_match_the_pinned_manifest(
    settings: Settings, db: sqlite3.Connection
) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})
    row = db.execute("SELECT manifest_json FROM extensions").fetchone()
    snap = json.loads(row["manifest_json"])
    snap["tools"]["write_note"]["level"] = "L0"
    snap["tools"]["write_note"]["effect"] = "read"
    db.execute("UPDATE extensions SET manifest_json = ?", (json.dumps(snap),))
    host = host_for(settings, db)
    assert not host.loaded
    assert host.refused["example"] == "approved manifest snapshot does not match resonant.yaml"


def test_unexpected_load_error_is_a_refusal(
    settings: Settings, db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    install_fixture(settings, db, MANIFEST, {"cli.py": CLI})

    def boom(root: Path, manifest: Manifest) -> list[str]:
        raise RuntimeError("symlink loop")

    monkeypatch.setattr("resonant.extensions.host.dir_errors", boom)
    host = host_for(settings, db)
    assert host.refused == {"example": "load failed (RuntimeError)"}
