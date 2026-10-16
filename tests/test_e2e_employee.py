"""EM-9: the employee model end to end (docs/employee-plan.md section 5, the exit criteria).

Each test is one scenario as the owner lives it over iMessage: the real ``Daemon`` (router,
relay, gate, loop, ``ClaudeRunner``, worker desk and CLI, trust rules, pulse), the fake claude
(``tests/fakes/claude``) acting through the real ``resonant ask``/``check`` shim, a local bare
repo for GitHub and a fake ``gh``. The model is the scripted one plus the phrasings below. The
guard scenario runs the session under the real ``sandbox-exec`` and is local only.

What the other e2e files already cover is not repeated here: ``test_e2e_runner.py`` (``/run``
to a PR, cancel and kill, crash and resume, refusals, fork mode, chat merges against a seeded
rule) and ``test_e2e_relay.py`` (which-one prompts, options, ``tell R1``, ask timeouts).

- "spawn a claude" to a PR in the digest, three workers at once: ``test_three_workers_*``
- the thin guard: ``test_the_thin_guard_*``
- a first deploy, ``yes always``: ``test_a_first_deploy_*``
- "always ask before merging in trading": ``test_a_rule_taught_*``
- a scratch run: ``test_a_scratch_run_*``
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
import test_e2e_runner
from test_e2e_relay import RelayModel, ask, prompts, text_like
from test_e2e_runner import PR_URL, REPO, FakeChatDB, World, chat, make_world, running, settled
from test_runner_claude import FAKE, git, until

from resonant.models import ChatMessage, ModelReply, SpanFactory, ToolCall
from resonant.runners import claude as claude_mod
from resonant.store import pulse as pulse_store
from resonant.store import trust as trust_store
from resonant.store.db import utcnow
from resonant_sdk import ToolSpec

__all__ = ["chat"]  # the fixture

TRADING = "trading-api"


class EmployeeModel(RelayModel):
    """The relay's model plus the employee plan's phrasings: "spawn a claude to <ask> in
    <repo>" and "do a manual db update …" (no repo) start runs, "always ask before merging in
    <area>" adds a rule, and "merge R<n>" merges that run."""

    def __init__(self) -> None:
        super().__init__()
        self.rules = (
            ("spawn a claude", "run_project"),
            ("manual db update", "run_project"),
            ("always ask before", "rules"),
            ("merge r", "run_project"),
            *self.rules,
        )

    async def chat_tools(
        self,
        messages: list[ChatMessage],
        *,
        tools: list[ToolSpec],
        max_tokens: int = 512,
        spans: SpanFactory | None = None,
    ) -> ModelReply:
        names = {t.name for t in tools}
        current = next(
            (m["content"] for m in reversed(messages) if "<message>" in m["content"]), ""
        )
        text = current.split("<message>", 1)[-1].split("</message>", 1)[0].strip()
        asked = any(
            m["role"] == "assistant" and m["content"].startswith('{"tool_call"') for m in messages
        )
        call: ToolCall | None = None
        if not asked:
            if (m := re.match(r"spawn a claude to (.+) in (\S+)$", text)) and "start_run" in names:
                call = ToolCall("start_run", {"repo": m[2], "ask": m[1]})
            elif text.startswith("do a manual db update") and "start_run" in names:
                call = ToolCall("start_run", {"ask": text})
            elif (m := re.match(r"always ask before merging in (\S+)$", text)) and (
                "add_trust_rule" in names
            ):
                call = ToolCall("add_trust_rule", {"scope": m[1], "action": "merge",
                                                   "effect": "ask"})  # fmt: skip
            elif (m := re.match(r"merge (R\d+)$", text)) and "merge_run" in names:
                call = ToolCall("merge_run", {"code": m[1]})
        if call is None:
            return await super().chat_tools(messages, tools=tools, max_tokens=max_tokens)
        self.calls["tools"] += 1
        return ModelReply(None, [call], None, 1, 10, 5)


@pytest.fixture(autouse=True)
def employee_model(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(test_e2e_runner, "RunModel", EmployeeModel)


def cli_calls(w: World, code: str) -> list[dict[str, Any]]:
    path = w.tmp(code) / "cli.json"
    return json.loads(path.read_text()) if path.exists() else []


def starts(prefix: str) -> Callable[[str], bool]:
    return lambda t: t.startswith(prefix)


def line_is(w: World, code: str, pred: Callable[[str], bool]) -> Callable[[], bool]:
    """The run's digest line (its pulse item) is written and ``pred`` accepts its title."""

    def check() -> bool:
        item = pulse_store.get(w.conn, "core", f"run/{code.lower()}")
        return item is not None and pred(item.title)

    return check


# --- section 5, bullets 1 and 4: three workers at once, then "spawn a claude" -----------------

TYPO_Q = "Fix both READMEs or just the top one?"
CHANGELOG_Q = "Should the changelog keep the 2019 entries?"
FLAKY_Q = "Skip the flaky test or rewrite it?"
SEARCH_Q = "Postgres full text or a search service?"
ASKS = {"typo": TYPO_Q, "changelog": CHANGELOG_Q, "flaky": FLAKY_Q, "search page": SEARCH_Q}


async def test_three_workers_ask_at_once_then_spawn_a_claude_to_a_pr_in_the_digest(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    extra = ["--ask-map", json.dumps(ASKS)]
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, extra=extra,
                   runner_opts={"ask_block_s": 0.5})  # fmt: skip
    async with running(w):
        # three workers, each with its own question, all waiting at the same time
        for code, ask_text in (("R1", "fix the typo in the README"),
                               ("R2", "tidy the changelog"),
                               ("R3", "fix the flaky test")):  # fmt: skip
            await ask(w, f"/run {REPO} {ask_text}", starts(f"On it: {code} · {REPO}."), code)
            await text_like(w, starts(f"{code} asks:"), f"{code}'s question")
            await until(settled(w, code, "waiting"), 20, f"{code} waits")
        assert sorted(q.run_code for q in w.runner.open_questions()) == ["R1", "R2", "R3"]
        assert [r for r in ("R1", "R2", "R3") if w.run(r).wait_reason != "question"] == []
        asked = [t for t in w.texts() if " asks: " in t]
        assert asked[0] == f"R1 asks: {TYPO_Q}"  # nobody else was waiting yet
        others = "\nOther runs are waiting too: reply {} <answer>."
        assert asked[1:] == [
            f"R2 asks: {CHANGELOG_Q}" + others.format("R2"),
            f"R3 asks: {FLAKY_Q}" + others.format("R3"),
        ]

        # replies out of order: by code, by content, and the last one left needs neither
        assert await ask(w, "R2: drop them", starts("→"), "by code") == "→ R2"
        assert await ask(w, "both READMEs please", starts("→"), "by content") == (
            f"→ R1 ({TYPO_Q})"
        )
        assert (await ask(w, "rewrite it", starts("→"), "the only one left")).startswith("→ R3")
        for code, q, answer in (("R1", TYPO_Q, "both READMEs please"),
                                ("R2", CHANGELOG_Q, "drop them"),
                                ("R3", FLAKY_Q, "rewrite it")):  # fmt: skip
            await text_like(w, starts(f"{code} done: PR #"), f"{code} done")
            assert prompts(w, code)[1].startswith(
                f'The owner answered your question "{q}": {answer}'
            ), code
        assert w.runner.open_questions() == []

        # "spawn a claude to build out XYZ": the ack, the question, the answer, the PR
        started = await ask(w, f"spawn a claude to build out the search page in {REPO}",
                            starts("Working on it."), "the ack")  # fmt: skip
        assert started.endswith(
            f"On it: R4 · {REPO}. Up to 45m. /runs to check, /cancel R4 to stop."
        )
        assert w.run("R4").ask == "build out the search page"
        assert await text_like(w, starts("R4 asks:"), "R4's question") == f"R4 asks: {SEARCH_Q}"
        await until(settled(w, "R4", "waiting"), 20, "R4 waits")
        assert await ask(w, "postgres is fine", starts("→"), "the answer") == "→ R4"
        await until(settled(w, "R4", "ready"), 60, "R4's PR")
        assert prompts(w, "R4")[1].startswith(
            f'The owner answered your question "{SEARCH_Q}": postgres is fine'
        )
        # a chat run's PR is a digest line, not a text
        assert not [t for t in w.texts() if t.startswith("R4 done")]
        line = f"R4 done: PR #44 (CI green) · {PR_URL[:-2]}44"  # the fourth PR on the fake
        await until(line_is(w, "R4", lambda t: t == line), 20, "R4's digest line")
        assert [i.key for i in pulse_store.pending(w.conn, ["normal"], utcnow())] == ["run/r4"]
        digest = await ask(w, "/pulse now", starts("btw:"), "the digest")
        assert digest == f"btw:\nWorkers:\n{line}"
        assert len([t for t in w.texts() if "R4 done" in t]) == 1


# --- section 5, bullet 2: the thin guard (real sandbox-exec, local only) ---------------------


@pytest.fixture
def scratch_keychain() -> Iterator[tuple[str, str, str]]:
    """A throwaway keychain file (never the login keychain or its search list) with one item:
    ``(path, service, secret)``. Readable outside the sandbox; the guard must refuse it."""
    security = "/usr/bin/security"
    root = Path(os.path.realpath(tempfile.mkdtemp(prefix="resonant-e2e-kc-")))
    kc = str(root / "e2e.keychain-db")
    password, secret, service = uuid.uuid4().hex, uuid.uuid4().hex, "resonant-e2e-probe"

    def sec(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run([security, *argv], capture_output=True, text=True, check=False)

    try:
        if sec("create-keychain", "-p", password, kc).returncode != 0:
            pytest.skip("can't create a scratch keychain here")
        sec("unlock-keychain", "-p", password, kc)
        sec("add-generic-password", "-s", service, "-a", "e2e", "-w", secret, "-T", security, kc)
        if sec("find-generic-password", "-s", service, "-w", kc).stdout.strip() != secret:
            pytest.skip("the scratch keychain item isn't readable outside the sandbox")
        yield kc, service, secret
    finally:
        sec("delete-keychain", kc)
        shutil.rmtree(root, ignore_errors=True)


@pytest.mark.skipif(
    bool(os.environ.get("CI"))
    or sys.platform != "darwin"
    or not os.access("/usr/bin/sandbox-exec", os.X_OK),
    reason="runs the session under the real sandbox-exec; local macOS only",
)
async def test_the_thin_guard_lets_a_worker_read_env_but_not_touch_resonant(
    tmp_path: Path,
    resonant_home: Path,
    chat: FakeChatDB,
    monkeypatch: pytest.MonkeyPatch,
    scratch_keychain: tuple[str, str, str],
) -> None:
    """The worker reads the checkout's ``.env`` (linked into its worktree), but can't write the
    daemon's state (the fake ``~/.resonant``, ``$RESONANT_HOME``), signal the daemon or read
    the Keychain. Docker and iMessage are covered by ``resonant sandbox check``'s probes."""
    kc, service, secret = scratch_keychain
    root = await asyncio.to_thread(tmp_path.resolve)
    python = await asyncio.to_thread(os.path.realpath, sys.executable)
    cache = root / "rr" / "runs" / ".cache"
    cache.mkdir(parents=True)
    fake = cache / "fake-claude"
    shutil.copy(FAKE, fake)
    probe = {"env": ".env", "state": str(resonant_home), "pid": os.getpid(),
             "keychain": kc, "service": service}  # fmt: skip
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, runner_opts={
        "wrap": claude_mod._sandbox_wrap,  # pyright: ignore[reportPrivateUsage]
        "claude_cmd": [python, str(fake), "--no-push", "--guard-probe", json.dumps(probe)],
        "read_paths": [Path(sys.base_prefix), Path(python).parent],
    })  # fmt: skip
    # the owner's own checkout, with its .env (untracked, as in real life)
    checkout = w.settings.runner.code_dir / REPO
    git(w.root, "clone", "-q", str(w.remote), str(checkout))
    git(checkout, "remote", "set-url", "origin", f"https://github.com/own/{REPO}")
    (checkout / ".env").write_text("DATABASE_URL=postgres://owner@localhost/demo\n")
    async with running(w):
        assert (await w.say(f"/run {REPO} fix it")).startswith("On it: R1")
        done = await w.next_text()
        assert done.startswith("R1: no changes."), done
        seen = json.loads((w.tmp() / "guard.json").read_text())
        assert seen["env"] == "ok DATABASE_URL=postgres://owner@localhost/demo", seen
        assert seen["state"].startswith("denied "), seen
        assert not (resonant_home / "worker-was-here").exists()
        assert seen["signal"].startswith("denied "), seen
        assert not seen["keychain"].startswith("rc 0") and secret not in seen["keychain"], seen
        assert secret not in w.everything()


# --- section 5, bullet 3: a first deploy asks, yes always, the next doesn't -------------------


async def test_a_first_deploy_asks_and_yes_always_means_the_next_one_does_not(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Irreversible, so a plain ``yes`` would ask again next time: only ``yes always`` (the
    rule it adds) keeps the second deploy from asking."""
    check = ["--check", "dori-api:deploy:deploy dori-api to prod", "--check-flag=--irreversible"]
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, extra=check,
                   runner_opts={"ask_block_s": 0.5})  # fmt: skip
    async with running(w):
        assert (await w.say(f"/run {REPO} ship the fix")).startswith("On it: R1")
        prompt = await w.next_text()
        assert re.match(
            r'\[\w\w\] R1 wants to deploy on dori-api: "deploy dori-api to prod" ', prompt
        )
        assert (
            "irreversible" in prompt
            and "yes always = don't ask before deploy in dori-api" in prompt
        )
        assert prompt.endswith("Reply yes / yes always / no"), prompt
        await until(settled(w, "R1", "waiting"), 20, "the check's wait")
        await asyncio.sleep(1.1)  # chat.db times are whole seconds: reply after the prompt
        added = await ask(w, "yes always", starts("Rule added:"), "the rule")
        assert added == "Rule added: don't ask before deploy in dori-api (rule 1)"
        await text_like(w, starts("R1 done: PR #41"), "R1 done")
        assert prompts(w, "R1")[1].startswith(
            "Your check (deploy on dori-api: deploy dori-api to prod (irreversible)) is ok: "
            "go ahead."
        )
        [rule] = trust_store.list_rules(w.conn)
        assert (rule.scope, rule.action, rule.effect, rule.created_from) == (
            "dori-api", "deploy", "allow", "yes always",
        )  # fmt: skip

        # the next deploy runs straight through: no prompt, the check answers ok at once
        before = len(w.texts())
        await ask(w, f"/run {REPO} ship the next fix", starts("On it: R2"), "R2 started")
        await text_like(w, starts("R2 done: PR #"), "R2 done")
        [call] = cli_calls(w, "R2")
        assert call["argv"][:5] == ["check", "--system", "dori-api", "--action", "deploy"]
        assert call["rc"] == 0, call
        assert not [t for t in w.texts()[before:] if "wants to" in t]
        assert len(prompts(w, "R2")) == 1  # one turn: it never waited


# --- section 5, bullet 3: "always ask before merging in trading" ----------------------------


async def test_a_rule_taught_over_chat_makes_trading_merges_ask_and_only_those(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch)
    w.settings.runner.repos[TRADING] = w.settings.runner.repos[REPO].model_copy(
        update={"remote": f"github.com/own/{TRADING}"}
    )
    async with running(w):
        taught = await ask(w, "always ask before merging in trading", lambda t: "Rule" in t,
                           "the rule")  # fmt: skip
        assert "Rule added: ask before merge in trading (rule 1)." in taught, taught
        [rule] = trust_store.list_rules(w.conn)
        assert (rule.scope, rule.action, rule.effect) == ("trading", "merge", "ask")

        await ask(w, f"/run {TRADING} fix it", starts(f"On it: R1 · {TRADING}."), "R1 started")
        await text_like(w, starts("R1 done: PR #41"), "R1 done")
        await ask(w, f"/run {REPO} fix it", starts(f"On it: R2 · {REPO}."), "R2 started")
        await text_like(w, starts("R2 done: PR #42"), "R2 done")

        # trading-api is in the trading area: a chat merge asks a plain yes, nothing merged
        prompt = await ask(w, "merge R1", lambda t: "merge R1" in t, "the merge ask")
        assert f"merge R1: PR #41 in {TRADING} " in prompt and prompt.endswith("Reply yes / no")
        assert w.gh_calls("merge") == [] and w.run("R1").status == "ready"
        await asyncio.sleep(1.1)
        assert await ask(w, "no", lambda t: t == "Not done.", "the no") == "Not done."
        assert w.run("R1").status == "ready"
        # demo isn't: the same words merge it without asking
        merged = await ask(w, "merge R2", lambda t: "R2 merged" in t, "the demo merge")
        assert "R2 merged: PR #42." in merged
        assert len(w.gh_calls("merge")) == 1 and w.run("R2").status == "merged"


# --- the scratch run: an ask with no repo --------------------------------------------------


async def test_a_scratch_run_has_no_repo_no_token_and_reports_in_the_digest(
    tmp_path: Path, resonant_home: Path, chat: FakeChatDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    w = make_world(tmp_path, resonant_home, chat, monkeypatch, scenario="scratch")
    async with running(w):
        started = await ask(w, "do a manual db update in dori: end acme's trial on friday",
                            starts("Working on it."), "the ack")  # fmt: skip
        assert started.endswith(
            "On it: R1 · no repo (scratch dir). Up to 45m. /runs to check, /cancel R1 to stop."
        )
        await until(settled(w, "R1", "done"), 60, "R1 done")
        await until(line_is(w, "R1", starts("R1 done")), 20, "R1's digest line")
        run = w.run()
        assert not run.repo and run.ask.startswith("do a manual db update in dori")
        scratch = w.settings.runner.code_dir / ".scratch" / "R1"
        assert json.loads((w.tmp() / "argv.json").read_text())[0]["cwd"] == str(scratch)
        assert (scratch / "notes.md").exists()
        # no repo: no App token, no checkout, no push, no PR
        assert w.api.mints == [] and w.gh_calls("create") == []
        assert not (w.settings.runner.code_dir / REPO).exists()
        # reported in the digest with Claude's summary, not texted
        assert not [t for t in w.texts() if t.startswith("R1 done")]
        digest = await ask(w, "/pulse now", starts("btw:"), "the digest")
        assert digest == (
            "btw:\nWorkers:\nR1 done: Checked the disk: 80% full, the biggest dir is ~/Downloads."
        )
        assert pulse_store.pending(w.conn, ["normal", "urgent"], utcnow()) == []
