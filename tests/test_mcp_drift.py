"""PER-275: MCP tool drift, accepted or asked. The host's re-pins and questions, the
``core/mcp`` job, and the replies that answer them, against ``fixtures/fake_mcp_server.py``.

Never touched here: the manifest, a tool's effect or level, URL, secrets (asserted below).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import AsyncIterator
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from test_ext_mcp import API, LISTED, OTHER, TOKEN, install_mail, pins_for, request, write_ext
from test_local_runner_gated import FakeChannel

from resonant.brain.notices import item_key
from resonant.config import Settings, load_settings
from resonant.executor import ToolFailed
from resonant.extensions.host import (
    ALERTS_KEY,
    ExtensionHealth,
    ExtensionHost,
    pending_alerts,
)
from resonant.extensions.mcp import McpError, SchemaChange, discover, parse_pins
from resonant.extensions.mcp_drift import NOTES_KEY, pending_notes, repin_summary, run_once
from resonant.extensions.process import FakeExtProcess
from resonant.gate import ApprovalReply, PolicyGate
from resonant.principals import Principals
from resonant.runners.approve import remind_due
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store import approvals as ap
from resonant.store import extensions as ext_store
from resonant.store import pulse as items
from resonant.store.db import atomic, kv_get, utcnow
from resonant.tools import ToolRegistry

OWNER = "justin"
LIVE = "x" * 64


@dataclass
class Clock:
    now: datetime = field(default_factory=lambda: (utcnow() + timedelta(days=1)).replace(hour=12))

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw: float) -> None:
        self.now += timedelta(**kw)


@dataclass
class World:
    settings: Settings
    db: sqlite3.Connection
    tmp_path: Path
    clock: Clock = field(default_factory=Clock)
    channel: FakeChannel = field(default_factory=FakeChannel)
    hosts: list[ExtensionHost] = field(default_factory=list[ExtensionHost])
    events: int = 0

    @property
    def host(self) -> ExtensionHost:
        return self.hosts[-1]

    def start(self) -> ExtensionHost:
        h = ExtensionHost.from_settings(
            self.settings, self.db, ToolRegistry(), runner=FakeExtProcess()
        )
        h.secrets = SecretStore(
            FakeKeychain(items={"mail/token": TOKEN, "mail/api": API, "mail/other": OTHER})
        )
        h.owner, h.clock = OWNER, self.clock
        h.load()
        self.hosts.append(h)
        return h

    async def restart(self) -> ExtensionHost:
        await self.host.aclose()
        h = self.start()
        await h.start_mcp()
        return h

    def knob(self, name: str, value: str = "v") -> None:
        data = self.settings.ext_data_dir / "mail"
        data.mkdir(parents=True, exist_ok=True)
        (data / name).write_text(value)

    async def relist(self) -> None:
        """The server lists again (a ``list_changed``): the runtime re-checks."""
        rt = self.host.loaded["mail"].mcp["fake"]
        data = self.settings.ext_data_dir / "mail"
        tools = await discover(request(self.host.loaded["mail"].root, data))
        rt._check(tools)  # pyright: ignore[reportPrivateUsage]

    async def job(self, advance: float = 0) -> dict[str, Any]:
        self.clock.advance(seconds=advance)
        return await run_once(self.db, self.host, self.channel, OWNER, self.settings, self.clock())

    def gate(self) -> PolicyGate:
        principals = Principals.model_validate(
            {
                "principals": {
                    OWNER: {"role": "owner", "identities": ["imessage:+15551234567"]},
                    "sam": {"role": "member", "identities": ["imessage:+15557654321"]},
                }
            }
        )
        return PolicyGate(
            self.db,
            principals,
            self.host.registry,
            dry_run=False,
            approval_ttl_s=1800,
            clock=self.clock,
        )

    def reply(self, text: str, *, actor: str = OWNER, code: str | None = None) -> Any:
        self.events += 1
        self.clock.advance(seconds=5)
        return self.gate().resolve_reply(
            ApprovalReply(
                actor=actor,
                actor_identity="imessage:+15551234567",
                channel="imessage",
                raw_text=text,
                decision="deny" if text.startswith("no") else "approve",
                path="parser",
                event_id=f"e{self.events}",
                occurred_at=self.clock(),
                code=code,
            )
        )

    def rows(self) -> list[ap.Approval]:
        return ap.repin_rows(
            self.db, "mail", "sleep", since=datetime.fromtimestamp(0, tz=self.clock().tzinfo)
        )

    def audits(self, action: str) -> list[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT tool, extension, effect, detail FROM audit_log WHERE action = ? ORDER BY id",
            (action,),
        ).fetchall()
        return [{"tool": r[0], "extension": r[1], "effect": r[2], **json.loads(r[3])} for r in rows]

    def texts(self) -> list[str]:
        return [t for _, t, _ in self.channel.sent]


@pytest.fixture
def settings() -> Settings:
    s = load_settings(dry_run=False)
    s.principals_path.write_text(
        'principals:\n  justin:\n    role: owner\n    identities: ["imessage:+15551234567"]\n'
    )
    s.timezone = "UTC"
    s.pulse.quiet_hours = "03:00-03:01"
    return s


@pytest.fixture
async def world(settings: Settings, db: sqlite3.Connection, tmp_path: Path) -> AsyncIterator[World]:
    await install_mail(settings, db, tmp_path)
    w = World(settings, db, tmp_path)
    w.start()
    yield w
    for h in w.hosts:
        await h.aclose()


def stored_pin(db: sqlite3.Connection, tool: str) -> Any:
    record = ext_store.get_extension(db, "mail")
    assert record is not None
    return parse_pins(record.mcp_pins)[tool]


def pulse_titles(db: sqlite3.Connection) -> list[str]:
    return [i.title for i in items.open_items(db, utcnow() + timedelta(days=1))]


# --- a read tool: re-pinned, said once --------------------------------------------------------


async def test_read_drift_is_repinned_audited_and_not_alerted(world: World) -> None:
    record = ext_store.get_extension(world.db, "mail")
    assert record is not None
    before = stored_pin(world.db, "echo")
    world.knob("schema")
    await world.host.start_mcp()
    host = world.host
    assert host.disabled_tools("mail") == {}
    after = stored_pin(world.db, "echo")
    assert after.pin != before.pin and after.version == 2
    assert "extra" in after.input_schema["properties"]
    # The model and the gate see the new schema; the description is still the manifest's.
    spec = host.registry.get("mail", "echo")
    assert spec is not None and spec.args_schema == after.input_schema
    assert spec.description == "Echo some text back."
    [listed] = [s for s in host.tool_specs("mail") if s.name == "echo"]
    assert listed.args_schema == after.input_schema
    out = await host.mcp_handlers[("mail", "echo")]({"text": "x", "extra": 2}, _ctx())
    assert out["text"] == "x"
    [audit] = world.audits("ext.mcp_repinned")
    assert audit["tool"] == "echo" and audit["how"] == "auto"
    assert audit["old_pin"] == before.pin and audit["new_pin"] == after.pin
    assert audit["change"]["added_optional"] == ["extra"]
    assert world.audits("ext.mcp_drift") == []
    assert pending_alerts(world.db) == {} and pending_notes(world.db) == {}
    assert kv_get(world.db, NOTES_KEY) is None
    title = "Mail updated echo; I accepted it (read-only)"
    assert title in pulse_titles(world.db)
    item = items.get(world.db, "core", item_key(f"mcp/mail/echo/{after.pin[:12]}"))
    assert item is not None and item.urgency == "normal"
    health = await ExtensionHealth(host).evaluate(utcnow())
    result = health["ext:mail"]
    assert result is not None and result[0].ok
    # Only the pin, its version and the input schema of that one tool changed.
    now = ext_store.get_extension(world.db, "mail")
    assert now is not None
    assert now.manifest == record.manifest and now.argv0_paths == record.argv0_paths
    assert {k: v for k, v in now.mcp_pins.items() if k != "echo"} == {
        k: v for k, v in record.mcp_pins.items() if k != "echo"
    }
    assert now.mcp_pins["echo"]["server"] == record.mcp_pins["echo"]["server"]
    # A restart finds the pin saved: no new audit, no new pulse line, the tool works.
    await world.restart()
    assert len(world.audits("ext.mcp_repinned")) == 1
    assert pulse_titles(world.db).count(title) == 1
    assert world.host.disabled_tools("mail") == {}


def _ctx() -> Any:
    from resonant.executor import CallContext

    return CallContext(secrets={}, idempotency_key="k", task_id="t")


# --- a write tool: benign re-pins, otherwise asked -------------------------------------------


async def test_write_tool_with_an_optional_field_is_repinned(world: World) -> None:
    world.knob("sleep_variant", "optional")
    await world.host.start_mcp()
    assert world.host.disabled_tools("mail") == {} and world.rows() == []
    spec = world.host.registry.get("mail", "sleep")
    assert spec is not None and "note" in spec.args_schema["properties"]
    assert spec.effect.value == "write" and spec.effective_level.value == "L2"  # untouched
    [audit] = world.audits("ext.mcp_repinned")
    assert audit["effect"] == "write" and audit["change"]["added_optional"] == ["note"]
    assert "Mail updated sleep; I accepted it (optional field added)" in pulse_titles(world.db)


async def test_write_tool_output_only_change_is_repinned(world: World) -> None:
    world.knob("sleep_variant", "output")
    await world.host.start_mcp()
    assert world.rows() == [] and len(world.audits("ext.mcp_repinned")) == 1
    assert "Mail updated sleep; I accepted it (no change to what it takes)" in pulse_titles(
        world.db
    )


async def test_write_tool_with_a_required_field_keeps_working_and_asks_once(
    world: World,
) -> None:
    world.knob("sleep_variant", "required")
    old = stored_pin(world.db, "sleep")
    await world.host.start_mcp()
    host = world.host
    assert host.disabled_tools("mail") == {} and world.audits("ext.mcp_repinned") == []
    assert stored_pin(world.db, "sleep").pin == old.pin  # not re-pinned
    spec = host.registry.get("mail", "sleep")
    assert spec is not None and spec.args_schema == old.input_schema
    out = await host.mcp_handlers[("mail", "sleep")]({"seconds": 0}, _ctx())
    assert out["text"] == "slept"  # still callable on the old schema
    [row] = world.rows()
    live = host.loaded["mail"].mcp["fake"].pending["sleep"].pin
    assert row.status == "pending" and row.task_id is None and row.tool == ap.REPIN_TOOL
    assert row.intent_hash == live and row.approvers == (OWNER,) and row.extension == "mail"
    assert row.effect.value == "write" and row.level.value == "L2" and not row.require_code
    assert row.summary == "Mail changed sleep (+required field team). Accept"
    assert row.expires_at - row.created_at == timedelta(hours=24)
    [asked] = world.audits("ext.mcp_repin_asked")
    assert asked["live_pin"] == live and asked["old_pin"] == old.pin
    assert asked["change"]["added_required"] == ["team"]
    # The job sends the prompt once.
    await world.job()
    await world.job()
    assert [p for _, p in world.channel.prompts] == [
        f"[{row.code}] Mail changed sleep (+required field team). Accept? Reply yes / no"
    ]
    # A restart (and another start) neither asks again nor audits again.
    await world.restart()
    await world.host.start_mcp()
    assert len(world.rows()) == 1 and len(world.audits("ext.mcp_repin_asked")) == 1
    await world.job()
    assert len(world.channel.prompts) == 1
    assert pending_alerts(world.db) == {}


async def test_a_second_distinct_change_supersedes_the_first_question(world: World) -> None:
    world.knob("sleep_variant", "required")
    await world.host.start_mcp()
    world.knob("sleep_variant", "retype")
    await world.relist()
    first, second = world.rows()
    assert first.status == "expired" and second.status == "pending"
    assert first.intent_hash != second.intent_hash
    assert len(world.audits("ext.mcp_repin_asked")) == 2


async def test_a_server_that_goes_back_ends_the_question(world: World) -> None:
    world.knob("sleep_variant", "required")
    await world.host.start_mcp()
    await world.job()
    world.knob("sleep_variant", "")  # the original schema again
    await world.relist()
    assert world.host.loaded["mail"].mcp["fake"].pending == {}
    assert (await world.job())["expired"] == 1
    [row] = world.rows()
    assert row.status == "expired"
    assert world.reply("yes").ok is False  # nothing left to answer


# --- the owner's answer ------------------------------------------------------------------------


async def test_yes_repins_the_tool_and_says_so_once(world: World) -> None:
    world.knob("sleep_variant", "required")
    await world.host.start_mcp()
    await world.job()
    [row] = world.rows()
    assert world.reply("yes").ok
    sent_before = len(world.texts())
    assert (await world.job())["settled"] == 1
    host = world.host
    live = row.intent_hash
    assert stored_pin(world.db, "sleep").pin == live
    assert stored_pin(world.db, "sleep").input_schema["required"] == ["team"]
    spec = host.registry.get("mail", "sleep")
    assert spec is not None and spec.args_schema["required"] == ["team"]
    handler = host.mcp_handlers[("mail", "sleep")]
    with pytest.raises(ToolFailed, match="schema"):
        await handler({"seconds": 0}, _ctx())  # the new rule is enforced
    assert (await handler({"team": "t", "seconds": 0}, _ctx()))["text"] == "slept"
    assert world.texts()[sent_before:] == ["Re-pinned sleep."]
    done = ap.get_approval(world.db, row.id)
    assert done is not None and done.status == "approved" and done.used_at is not None
    [audit] = world.audits("ext.mcp_repinned")
    assert audit["how"] == "owner" and audit["new_pin"] == live
    assert audit["approval_id"] == row.id
    # Said once: the same event again, and another run, change nothing.
    assert world.gate().resolve_reply(_again(world)).ok
    assert (await world.job())["settled"] == 0 and len(world.texts()) == sent_before + 1
    await world.restart()
    assert world.host.disabled_tools("mail") == {} and len(world.audits("ext.mcp_repinned")) == 1


def _again(world: World) -> ApprovalReply:
    return ApprovalReply(
        actor=OWNER,
        actor_identity="imessage:+15551234567",
        channel="imessage",
        raw_text="yes",
        decision="approve",
        path="parser",
        event_id=f"e{world.events}",
        occurred_at=world.clock(),
    )


async def test_no_turns_the_tool_off_until_ext_update(world: World) -> None:
    world.knob("sleep_variant", "required")
    await world.host.start_mcp()
    await world.job()
    assert world.reply("no").ok
    # It bites on the very next call, before the job has run.
    with pytest.raises(McpError, match="disabled"):
        await world.host.mcp_handlers[("mail", "sleep")]({"seconds": 0}, _ctx())
    sent_before = len(world.texts())
    await world.job()
    assert world.host.disabled_tools("mail") == {"sleep": "declined"}
    assert world.texts()[sent_before:] == [
        "Okay: sleep is off until you run resonant ext update mail."
    ]
    assert pending_alerts(world.db) == {} and world.audits("ext.mcp_drift") == []
    assert len(world.audits("ext.mcp_repin_declined")) == 1
    assert "sleep" not in {s.name for s in world.host.tool_specs("mail")}
    health = await ExtensionHealth(world.host).evaluate(utcnow())
    result = health["ext:mail"]
    assert result is not None and result[0].ok  # the owner's own answer is not a degrade
    # A restart turns it off again with no new question or text.
    await world.restart()
    assert world.host.disabled_tools("mail") == {"sleep": "declined"}
    assert len(world.rows()) == 1 and len(world.texts()) == sent_before + 1
    # ext update: a new install time and the pins it approved; the "no" is behind it.
    record = ext_store.get_extension(world.db, "mail")
    assert record is not None
    stage = write_ext(world.tmp_path / "stage2")
    fresh = await pins_for(stage, world.tmp_path / "discover2")
    world.clock.advance(minutes=1)
    with atomic(world.db):
        ext_store.save_extension(
            world.db, replace(record, installed_at=world.clock(), mcp_pins=fresh)
        )
    await world.restart()
    assert world.host.disabled_tools("mail") == {}
    out = await world.host.mcp_handlers[("mail", "sleep")]({"team": "t", "seconds": 0}, _ctx())
    assert out["text"] == "slept"


async def test_yes_given_before_the_server_verified_is_accepted_at_the_first_check(
    world: World,
) -> None:
    world.knob("sleep_variant", "required")
    await world.host.start_mcp()
    await world.job()
    [row] = world.rows()
    assert world.reply("yes").ok
    await world.restart()  # no job in between: the row is approved and unused
    assert world.host.loaded["mail"].mcp["fake"].schemas["sleep"]["required"] == ["team"]
    [audit] = world.audits("ext.mcp_repinned")
    assert audit["how"] == "owner" and audit["approval_id"] == row.id
    sent_before = len(world.texts())
    assert (await world.job())["settled"] == 1
    assert world.texts()[sent_before:] == ["Re-pinned sleep."]


async def test_an_expired_question_leaves_the_tool_as_it_is_and_is_reminded_once(
    world: World,
) -> None:
    world.knob("sleep_variant", "required")
    await world.host.start_mcp()
    await world.job()
    [row] = world.rows()
    world.clock.advance(hours=12, minutes=1)
    assert await remind_due(world.db, world.channel, world.clock()) == 1
    assert await remind_due(world.db, world.channel, world.clock()) == 0
    assert f"[{row.code}] still waiting" in world.texts()[-1]
    world.clock.advance(hours=12)
    await world.job()
    [expired] = world.rows()
    assert expired.status == "expired"
    assert world.host.disabled_tools("mail") == {}
    old = stored_pin(world.db, "sleep")
    spec = world.host.registry.get("mail", "sleep")
    assert spec is not None and spec.args_schema == old.input_schema
    # A restart doesn't ask again about the same change.
    await world.restart()
    assert len(world.rows()) == 1 and len(world.audits("ext.mcp_repin_asked")) == 1
    assert world.host.disabled_tools("mail") == {}
    assert (await world.host.mcp_handlers[("mail", "sleep")]({"seconds": 0}, _ctx()))[
        "text"
    ] == "slept"


async def test_only_the_owner_can_answer_and_a_task_less_other_approval_binds_nothing(
    world: World,
) -> None:
    world.knob("sleep_variant", "required")
    await world.host.start_mcp()
    await world.job()
    [row] = world.rows()
    assert world.reply("yes", actor="sam", code=row.code).ok is False
    other = ap.insert_approval(
        world.db,
        ap.NewApproval(
            task_id=None,
            intent_hash="h",
            tool="something_else",
            effect=row.effect,
            level=row.level,
            summary="x",
            approvers=(OWNER,),
            code="Z9",
            expires_at=world.clock() + timedelta(hours=1),
        ),
        world.clock(),
    )
    assert world.reply("yes", code="Z9").ok is False
    assert ap.get_approval(world.db, other).status == "pending"  # type: ignore[union-attr]
    assert world.reply("yes", code=row.code).ok


# --- a tool that is gone ----------------------------------------------------------------------


async def test_a_removed_tool_is_one_non_urgent_heads_up(world: World) -> None:
    world.knob("sleep_variant", "gone")
    await world.host.start_mcp()
    assert world.host.disabled_tools("mail") == {"sleep": "removed"}
    assert pending_alerts(world.db) == {}  # not the urgent path
    [note] = pending_notes(world.db).values()
    assert "no longer offers sleep" in note["title"]
    assert len(world.audits("ext.mcp_drift")) == 1
    health = await ExtensionHealth(world.host).evaluate(utcnow())
    result = health["ext:mail"]
    assert result is not None and result[0].ok
    await world.job()
    heads_up = [t for t in world.texts() if "no longer offers sleep" in t]
    assert len(heads_up) == 1 and "resonant ext update mail" in heads_up[0]
    assert pending_notes(world.db) == {}
    await world.job()
    await world.restart()
    await world.job()
    assert len([t for t in world.texts() if "no longer offers sleep" in t]) == 1
    assert pending_notes(world.db) == {} and len(world.audits("ext.mcp_drift")) == 1


async def test_a_removed_tool_heads_up_is_held_in_quiet_hours(world: World) -> None:
    world.settings.pulse.quiet_hours = "00:00-23:59"
    world.knob("sleep_variant", "gone")
    await world.host.start_mcp()
    await world.job()
    assert [t for t in world.texts() if "no longer offers" in t] == []
    assert any("no longer offers sleep" in t for t in pulse_titles(world.db))
    assert pending_notes(world.db) == {}


async def test_a_tool_listed_twice_still_alerts_urgently(world: World) -> None:
    world.knob("sleep_variant", "dup")
    await world.host.start_mcp()
    assert world.host.disabled_tools("mail") == {"sleep": "changed"}
    assert "mail/sleep" in pending_alerts(world.db) and kv_get(world.db, ALERTS_KEY)
    health = await ExtensionHealth(world.host).evaluate(utcnow())
    result = health["ext:mail"]
    assert result is not None and not result[0].ok


# --- the texts ---------------------------------------------------------------------------------


async def test_a_no_holds_when_the_server_reverts(world: World) -> None:
    world.knob("sleep_variant", "required")
    await world.host.start_mcp()
    await world.job()
    assert world.reply("no").ok
    world.knob("sleep_variant", "")  # the original schema again, before the job ran
    await world.relist()
    with pytest.raises(McpError, match="disabled"):
        await world.host.mcp_handlers[("mail", "sleep")]({"seconds": 0}, _ctx())
    await world.job()
    assert world.host.disabled_tools("mail") == {"sleep": "declined"}
    await world.relist()  # and a later re-list doesn't bring it back
    assert world.host.disabled_tools("mail") == {"sleep": "declined"}


async def test_a_no_holds_across_a_restart_with_the_server_already_reverted(
    world: World,
) -> None:
    world.knob("sleep_variant", "required")
    await world.host.start_mcp()
    await world.job()
    assert world.reply("no").ok
    world.knob("sleep_variant", "")
    await world.restart()  # no job ran; the server is back to what was approved
    assert world.host.disabled_tools("mail") == {"sleep": "declined"}
    with pytest.raises(McpError, match="disabled"):
        await world.host.mcp_handlers[("mail", "sleep")]({"seconds": 0}, _ctx())


async def test_at_most_three_questions_a_day_per_tool(world: World) -> None:
    await world.host.start_mcp()
    for name in ("a", "b", "c", "d", "e"):
        world.knob("sleep_variant", f"req:{name}")
        await world.relist()
    assert len(world.rows()) == 3 and len(world.audits("ext.mcp_repin_asked")) == 3
    [capped] = world.audits("ext.mcp_repin_capped")[:1]
    assert capped["tool"] == "sleep"
    assert len(world.audits("ext.mcp_repin_capped")) == 2  # once per live pin: d and e
    await world.relist()
    assert len(world.audits("ext.mcp_repin_capped")) == 2
    await world.job()
    assert world.channel.prompts == []  # c's question is stale (the server moved on): expired
    assert world.rows()[2].status == "expired"
    assert world.host.disabled_tools("mail") == {}
    world.clock.advance(hours=25)  # a new day, a newer change: asks again
    world.knob("sleep_variant", "req:f")
    await world.relist()
    assert len(world.rows()) == 4


async def test_a_capped_tool_is_asked_again_once_the_window_passes(world: World) -> None:
    await world.host.start_mcp()
    for name in ("a", "b", "c", "d"):
        world.knob("sleep_variant", f"req:{name}")
        await world.relist()
    assert len(world.rows()) == 3  # "d" was capped and nothing is pending for it
    assert "sleep" not in world.host.loaded["mail"].mcp["fake"].pending
    world.clock.advance(hours=25)
    await world.relist()  # the same live pin, a later check
    assert len(world.rows()) == 4 and world.rows()[-1].status == "pending"


async def test_a_yes_that_no_longer_applies_says_so(world: World) -> None:
    world.knob("sleep_variant", "required")
    await world.host.start_mcp()
    await world.job()
    assert world.reply("yes").ok
    world.knob("sleep_variant", "retype")  # it moved on before the job ran
    await world.relist()
    before = len(world.texts())
    await world.job()
    assert world.texts()[before] == "sleep changed again or is off, so that yes didn't apply."
    assert world.audits("ext.mcp_repinned") == []
    await world.job()
    assert [t for t in world.texts() if "didn't apply" in t] == [world.texts()[before]]


def test_summary_names_are_identifiers_only_and_capped() -> None:
    change = SchemaChange(
        (), ("team_id", "x; Reply yes A1", "ok-2", "d", "e"), ("gone",), (), ("old",), True
    )
    text = repin_summary("linear", "save_issue", change)
    assert text == (
        "Linear changed save_issue (+required field team_id, a field, ok-2 +2 more; "
        "removed field gone; no longer required: old; other input rules changed). Accept"
    )
    assert "\n" not in repin_summary("mail", "t", SchemaChange((), ("a\nb",), (), (), (), False))


def test_summary_says_when_new_text_needs_a_look() -> None:
    change = SchemaChange((), (), (), (), (), False, unplain=True)
    assert repin_summary("mail", "echo", change) == (
        "Mail changed echo (new field text needs a look). Accept"
    )


def test_the_listed_tools_are_all_pinned() -> None:
    assert "sleep" in LISTED


def test_replace_swaps_only_the_args_schema(world_registry: ToolRegistry) -> None:
    spec = world_registry.get("mail", "echo")
    assert spec is not None
    new = spec.model_copy(update={"args_schema": {"type": "object", "properties": {}}})
    world_registry.replace(new)
    assert world_registry.get("mail", "echo") == new
    with pytest.raises(ValueError, match="only args_schema"):
        world_registry.replace(new.model_copy(update={"description": "other"}))
    with pytest.raises(ValueError, match="not registered"):
        world_registry.replace(new.model_copy(update={"name": "nothing"}))


@pytest.fixture
def world_registry(world: World) -> ToolRegistry:
    return world.host.registry


def test_core_mcp_job_is_registered_and_needs_a_channel(world: World) -> None:
    from types import SimpleNamespace

    from resonant.extensions.mcp_drift import mcp_drift_job
    from resonant.extensions.scheduler import CORE_JOBS

    assert any(f.__name__ == "mcp_drift_job" for f in CORE_JOBS)
    comps = SimpleNamespace(extensions=world.host)
    deps: Any = SimpleNamespace(
        settings=world.settings, conn=world.db, components=comps, channel=None, owner=OWNER
    )
    assert mcp_drift_job(deps) is None
    deps.channel = world.channel
    job = mcp_drift_job(deps)
    assert job is not None and job.id == "mcp" and not job.runs_while_paused
