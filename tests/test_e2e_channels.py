"""PER-292: the channel hub, end to end.

The real daemon (router, gate, loop, ``ClaudeRunner``, worker CLI) as in ``test_e2e_runner``,
talked to over iMessage (``FakeChatDB`` + ``RecordingOsaRunner``) or over a fake Slack
adapter (``e2e.FakeChannel("slack")``).

- **Golden texts.** One script (a reply, an approval prompt answered ``yes <code>``, a run's
  outcome, a worker question answered ``R1 …``) runs over iMessage and its texts are pinned
  exactly as they were before the hub (only the random approval code, run name and timings
  are masked). The same script over Slack sends the same texts, each in the thread of the
  message it answers.
- **iMessage off.** With only the Slack adapter the whole stack runs (router, runners,
  monitor), and iMessage never sends.
- **Two adapters.** iMessage and Slack together: a reply goes back on the channel the message
  came from; a proactive text goes to the owner's first identity.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import AsyncGenerator, Callable
from pathlib import Path
from typing import Any

import pytest
from test_e2e_runner import (
    REPO,
    FakeChatDB,
    RunModel,
    World,
    attach_claude,
    chat,
    make_world,
    settled,
)
from test_runner_claude import until

from resonant.config import ChannelsConfig
from resonant.daemon import Daemon
from resonant.e2e import FakeChannel
from resonant.gateway.channel import ChannelRefused
from resonant.ops.fakes import FakeOpsRunner
from resonant.principals import load_principals
from resonant.secrets import SecretStore
from resonant.store.db import iso, kv_set, utcnow

__all__ = ["chat"]  # the fixture

OWNER_PHONE = "+15551234567"
SLACK_UID = "U1"
CHECK = "dori-api:deploy:deploy dori-api to prod"
QUESTION = "Fix both READMEs or just the top one?"
RUN_ASK = f"/run {REPO} fix the typo in the README"

# The texts as they were before the hub (iMessage-only), masked by ``mask``.
GOLDEN_REPLY = ["Nothing is waiting for approval."]
OPENED = (
    "R1 opened PR #41 (CI green, 1 file, <t>): <url>\n/merge R1 to merge it.\n"
    "R1: Fixed it. Added claude-fix.txt."
)
STARTED = "On it: R1 · demo. Up to <t>. /runs to check, /cancel R1 to stop."
GOLDEN_CHECK = [
    STARTED,
    '[<code>] R1 wants to deploy on dori-api: "deploy dori-api to prod" (new system; yes always '
    "= don't ask before deploy in dori-api). Reply yes / yes always / no",
    OPENED,
]
GOLDEN_RELAY = [STARTED, f"R1 asks: {QUESTION}", "→ R1", OPENED]
# PER-327: on Slack the start line says where the end goes (and never "text"); the rest is
# the same as iMessage's
SLACK_STARTED = f"{STARTED} I'll post here when it's done."
SLACK_CHECK = [SLACK_STARTED, *GOLDEN_CHECK[1:]]
SLACK_RELAY = [SLACK_STARTED, *GOLDEN_RELAY[1:]]


def mask(text: str) -> str:
    """A text with what changes run to run masked: the approval code, the run's name in its
    end text, timings and URLs."""
    out = re.sub(r"^\[[A-Z0-9]+\]", "[<code>]", text)
    out = re.sub(r"^(R[0-9]+) \(.*?\) ", r"\1 ", out)  # the run's name (PER-268)
    out = re.sub(r"https?://\S+", "<url>", out)
    out = re.sub(r"\b\d+(\.\d+)?(s|m|h|min)\b", "<t>", out)
    return re.sub(r"\$\d+(\.\d+)?", "$<n>", out)


class Transport:
    """How the test talks to the daemon: ``say`` sends as the owner (optionally in a thread),
    ``sent`` is every text the daemon sent the owner on this transport, with its thread."""

    name: str

    def say(self, text: str, *, thread: str | None = None) -> str | None:
        raise NotImplementedError

    def sent(self) -> list[tuple[str, str | None]]:
        raise NotImplementedError


class IMessage(Transport):
    name = "imessage"

    def __init__(self, w: World) -> None:
        self.w = w

    def say(self, text: str, *, thread: str | None = None) -> str | None:
        self.w.chat.add_message(handle=OWNER_PHONE, text=text)
        return None

    def sent(self) -> list[tuple[str, str | None]]:
        return [(m.text, None) for m in self.w.osa.sent]


class Slack(Transport):
    name = "slack"

    def __init__(self, fake: FakeChannel) -> None:
        self.fake = fake
        self.last = ""

    def say(self, text: str, *, thread: str | None = None) -> str | None:
        """Post as slack:U1; the message's ref (``slack-in:<n>``)."""
        assert self.fake.inject(SLACK_UID, text, thread=thread)
        self.last = f"slack-in:{self.fake.injected}"
        return self.last

    def sent(self) -> list[tuple[str, str | None]]:
        return [(m.text, m.thread) for m in self.fake.sent]


def principals_yaml(*identities: str) -> str:
    quoted = ", ".join(f'"{i}"' for i in identities)
    return f"principals:\n  alice:\n    role: owner\n    identities: [{quoted}]\n"


@contextlib.asynccontextmanager
async def running(
    w: World, home: Path, *identities: str, imessage: bool = True, slack: bool = True
) -> AsyncGenerator[tuple[Daemon, FakeChannel | None]]:
    """The daemon with the hub's adapters: iMessage from the world's settings (or off), and a
    fake Slack adapter."""
    (home / "principals.yaml").write_text(principals_yaml(*identities))
    if not imessage:
        w.settings = w.settings.model_copy(update={"channels": ChannelsConfig()})
    fake = FakeChannel("slack", load_principals(home / "principals.yaml")) if slack else None
    extra: dict[str, Any] = {} if fake is None else {"channels": {"slack": fake}}
    daemon = w.daemon = Daemon(
        w.settings,
        model=RunModel(),
        osa_runner=w.osa,
        ops_runner=FakeOpsRunner(),
        secrets=SecretStore(w.keychain),
        **extra,
    )
    kv_set(daemon.conn, "imessage.selftest", {"ok": True, "at": iso(utcnow()), "detail": "ok"})
    attach_claude(w, daemon)
    task = asyncio.create_task(daemon.run())
    assert daemon.imessage is not None  # the stack's pre-PER-292 name (the golden tests)
    hub = daemon.imessage.channel
    await until(lambda: hub.health()["last_read_at"] is not None, 10, "the channels to read")
    try:
        yield daemon, fake
    finally:
        if not task.done():
            daemon.stop()
        assert await asyncio.wait_for(task, 20) == 0
        w.daemon = None


async def next_sent(t: Transport, n: int, what: str) -> tuple[str, str | None]:
    """The ``n``-th text sent on ``t`` (1-based)."""
    await until(lambda: len(t.sent()) >= n, 60, what)
    return t.sent()[n - 1]


async def wait_text(t: Transport, pred: Callable[[str], bool], what: str) -> int:
    """How many texts were sent once one matching ``pred`` is there."""
    await until(lambda: any(pred(x) for x, _ in t.sent()), 60, what)
    return next(i for i, (x, _) in enumerate(t.sent(), 1) if pred(x))


async def script_check(t: Transport, w: World) -> tuple[list[str], set[str | None]]:
    """A reply, then a run whose check asks: answered ``yes <code>``; the run ends."""
    first = t.say("/approvals")
    await next_sent(t, 1, "the reply")
    t.say(RUN_ASK, thread=None)
    run_thread = None if t.name == "imessage" else _last_ref(t)
    prompt_at = await wait_text(t, lambda x: x.startswith("["), "the check's prompt")
    code = re.match(r"^\[([A-Z0-9]+)\]", t.sent()[prompt_at - 1][0])
    assert code is not None
    await until(settled(w, "R1", "waiting"), 20, "the check wait")
    t.say(f"yes {code.group(1)}", thread=run_thread)
    await wait_text(t, lambda x: mask(x).startswith("R1 opened PR #41"), "the run's end")
    texts = [mask(x) for x, _ in t.sent()]
    threads = {th for _, th in t.sent()[1:]}
    assert first is None or t.sent()[0][1] == first  # the reply in the message's thread
    return texts, threads


async def script_relay(t: Transport, w: World) -> tuple[list[str], set[str | None]]:
    """A run asks a question; the owner answers ``R1 …`` in the run's thread."""
    t.say(RUN_ASK)
    run_thread = None if t.name == "imessage" else _last_ref(t)
    await wait_text(t, lambda x: x == f"R1 asks: {QUESTION}", "the question")
    await until(settled(w, "R1", "waiting"), 20, "the question wait")
    t.say("R1 just the top one", thread=run_thread)
    await wait_text(t, lambda x: mask(x).startswith("R1 opened PR #41"), "the run's end")
    return [mask(x) for x, _ in t.sent()], {th for _, th in t.sent()}


def _last_ref(t: Transport) -> str:
    assert isinstance(t, Slack)
    return t.last


def world(tmp_path: Path, home: Path, chat: FakeChatDB, mp: pytest.MonkeyPatch, **kw: Any) -> World:
    return make_world(tmp_path, home, chat, mp, scenario="happy",
                      runner_opts={"ask_block_s": 0.5}, **kw)  # fmt: skip


# --- golden: iMessage only, texts as before the hub ------------------------------------------


async def test_golden_imessage_texts_reply_approval_and_run_outcome(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_path, resonant_home, chat, monkeypatch, extra=["--check", CHECK])
    async with running(w, resonant_home, f"imessage:{OWNER_PHONE}", slack=False):
        texts, _ = await script_check(IMessage(w), w)
    assert texts[: len(GOLDEN_REPLY)] == GOLDEN_REPLY
    assert texts[1:] == GOLDEN_CHECK, texts


async def test_golden_imessage_texts_relay(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_path, resonant_home, chat, monkeypatch, extra=["--ask", QUESTION])
    async with running(w, resonant_home, f"imessage:{OWNER_PHONE}", slack=False):
        texts, _ = await script_relay(IMessage(w), w)
    assert texts == GOLDEN_RELAY, texts


# --- Slack only: iMessage off, the full stack runs --------------------------------------------


async def test_slack_only_routes_replies_in_thread_approves_with_a_code(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A message from slack:U1 routes; its reply goes to its thread; the run it starts asks in
    that thread, ``yes <code>`` there approves it, and the run's end lands there too. The same
    texts as over iMessage."""
    w = world(tmp_path, resonant_home, chat, monkeypatch, extra=["--check", CHECK])
    async with running(w, resonant_home, f"slack:{SLACK_UID}", imessage=False) as (d, fake):
        assert d.stack is not None and d.stack.channel.names == ("slack",)
        assert d.state.monitor is not None and set(d.state.monitor.channels) == {"slack"}
        assert fake is not None
        t = Slack(fake)
        texts, threads = await script_check(t, w)
        await d.state.monitor.evaluate()
        assert d.state.monitor.states is not None
        assert d.state.monitor.states["slack"].state == "ok"
    assert texts[0] == GOLDEN_REPLY[0] and fake.sent[0].thread == "slack-in:1"
    assert texts[1:] == SLACK_CHECK, texts
    assert threads == {"slack-in:2"}  # every run text in the thread of the /run message
    assert w.osa.sent == []  # iMessage is off: nothing went out there


async def test_slack_only_worker_question_answered_r1_in_thread(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_path, resonant_home, chat, monkeypatch, extra=["--ask", QUESTION])
    async with running(w, resonant_home, f"slack:{SLACK_UID}", imessage=False) as (_, fake):
        assert fake is not None
        texts, threads = await script_relay(Slack(fake), w)
    assert texts == SLACK_RELAY, texts
    assert threads == {"slack-in:1"}
    assert w.osa.sent == []


# --- two adapters ------------------------------------------------------------------------------


async def test_two_adapters_reply_on_the_channel_asked_proactive_to_the_first_identity(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_path, resonant_home, chat, monkeypatch)
    ids = (f"imessage:{OWNER_PHONE}", f"slack:{SLACK_UID}")
    async with running(w, resonant_home, *ids) as (d, fake):
        assert d.stack is not None and fake is not None
        hub = d.stack.channel
        assert hub.names == ("imessage", "slack") and hub.name == "imessage+slack"
        im, sl = IMessage(w), Slack(fake)
        sl.say("/approvals")
        assert await next_sent(sl, 1, "the slack reply") == (GOLDEN_REPLY[0], "slack-in:1")
        im.say("/approvals")
        assert (await next_sent(im, 1, "the imessage reply"))[0] == GOLDEN_REPLY[0]
        assert len(fake.sent) == 1  # the iMessage reply didn't go to Slack
        # a proactive text (no thread): the owner's first identity, iMessage
        await hub.send("alice", "hey, some issues here", dedupe_key="t:proactive")
        assert w.osa.sent[-1].text == "hey, some issues here" and len(fake.sent) == 1


async def test_a_principal_with_no_running_identity_is_refused(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = world(tmp_path, resonant_home, chat, monkeypatch)
    async with running(w, resonant_home, f"imessage:{OWNER_PHONE}", imessage=False) as (d, _):
        assert d.stack is not None
        with pytest.raises(ChannelRefused):
            await d.stack.channel.send("alice", "hello", dedupe_key="t:refused")
    assert w.osa.sent == []
