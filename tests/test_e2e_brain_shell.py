"""PER-258: the Claude brain's own tools (Bash, Edit, Write) end to end.

The whole daemon in process, as in ``test_e2e_claude_session.py`` (its ``start``): the fake
Claude (``resonant/e2e/fake_claude.py``) uses its own tools from a plan's ``shell`` list. A
command a ``Bash(<prefix>:*)`` rule covers runs at once; anything else is a permission
request to Resonant's permission tool over the real MCP relay, decided by the real gate.
Allowed commands really run, in a scratch repo under ``tmp_path`` with a bare remote. Nothing
touches the real ~/.resonant, ~/code, Keychain or claude.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_e2e_claude_session import CLAUDE, World, start

from resonant.e2e import wait_until
from resonant.gateway.imessage.fixtures import FakeChatDB
from resonant.store import trust as trust_store
from resonant.store.claude_brain import GRANTS_KV, recent_tools
from resonant.store.db import atomic, kv_get, utcnow


@pytest.fixture
def chat(tmp_path: Path) -> Iterator[FakeChatDB]:
    db = FakeChatDB(tmp_path / "chat.db")
    yield db
    db.close()


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True, timeout=30
    )
    return done.stdout.strip()


def scratch_repo(tmp_path: Path) -> tuple[Path, Path]:
    """``code/r`` on branch ``feat`` (one commit ahead of ``main``), and its bare remote."""
    code = tmp_path / "code"
    code.mkdir(exist_ok=True)
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", "-b", "main", str(remote))
    repo = code / "r"
    git(code, "init", "-q", "-b", "main", "r")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "T")
    git(repo, "commit", "-q", "--allow-empty", "-m", "one")
    git(repo, "remote", "add", "origin", str(remote))
    git(repo, "push", "-q", "-u", "origin", "main")
    git(repo, "remote", "set-head", "origin", "main")
    git(repo, "checkout", "-q", "-b", "feat")
    git(repo, "commit", "-q", "--allow-empty", "-m", "two")
    return repo, remote


def remote_head(remote: Path, branch: str) -> str | None:
    done = subprocess.run(
        ["git", "rev-parse", "--verify", "-q", branch],
        cwd=remote,
        capture_output=True,
        text=True,
        check=False,
    )
    return done.stdout.strip() or None


async def prompt_recorded(w: World) -> None:
    def recorded() -> bool:
        rows = w.rows("SELECT channel_refs FROM approvals WHERE status = 'pending'")
        return bool(rows) and all(json.loads(r["channel_refs"]) for r in rows)

    await wait_until(recorded, 10)
    await asyncio.sleep(0.01)


def tools(w: World) -> list[dict[str, Any]]:
    """The ``brain.tool`` audit rows, oldest first."""
    return list(reversed(recent_tools(w.conn, 100)))


def shell_gated(w: World) -> list[tuple[str, str]]:
    """The gate's decisions on the shell changes, with their origin."""
    return [(a["decision"], a["detail"]["origin"]) for a in w.audits("gate.evaluate")
            if a["tool"] == "shell"]  # fmt: skip


# --- reads ------------------------------------------------------------------------------------


async def test_a_commit_count_is_a_read_that_never_asks(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    repo, _ = scratch_repo(tmp_path)
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(
            shell=[
                {"tool": "Bash", "command": f"cd {repo}"},
                {"tool": "Bash", "command": "git rev-list --count HEAD"},
                {"tool": "Bash", "command": f"git -C {repo} rev-list --count main"},
            ],
            answer="resonant has {r1} commits ({r2} on main)",
        )
        (answer,) = await w.say("how many commits does resonant have?")
        await w.settled()
        assert answer == "resonant has 2 commits (1 on main)"
        assert w.rows("SELECT 1 FROM approvals") == []
        rows = tools(w)
        assert [(r["tool"], r["decision"], r["status"]) for r in rows] == [
            ("Bash", "read", "ok"),  # cd into a read root: the gate's L0 read
            ("Bash", "allowed", "ok"),  # on the pre-allowed list: never reached Resonant
            ("Bash", "read", "ok"),  # git -C: not a list prefix, still a read
        ]
        assert rows[1]["command"] == "git rev-list --count HEAD" and rows[1]["cwd"] == str(repo)
        assert all(r["write"] is False for r in rows)
        assert {a["decision"] for a in w.audits("gate.evaluate") if a["tool"] == "shell_read"} == {
            "Allow"
        }
        assert "Did:" not in answer  # no progress line for reads


async def test_a_read_outside_the_read_roots_is_refused(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        secret = tmp_path / "elsewhere.txt"
        secret.write_text("SECRET-OUTSIDE")
        w.model.triage = CLAUDE
        w.claude(shell=[{"tool": "Bash", "command": f"cat {secret} | head -1"}], answer="{r0}")
        (answer,) = await w.say("what's in that file?")
        await w.settled()
        assert "SECRET-OUTSIDE" not in answer and "outside the folders" in answer
        assert w.rows("SELECT 1 FROM approvals") == []


# --- writes -----------------------------------------------------------------------------------


async def test_a_feature_branch_push_runs_and_reports(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """A clean turn: a push of a branch that isn't the default one runs without asking (L3),
    and the reply ends with the code-built progress line."""
    repo, remote = scratch_repo(tmp_path)
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(shell=[{"tool": "Bash", "command": f"cd {repo} && git push -q origin feat"}],
                 answer="Pushed.")  # fmt: skip
        (answer,) = await w.say("push feat")
        await w.settled()
        assert answer == "Pushed.\nDid: pushed feat in r"
        assert remote_head(remote, "feat") == git(repo, "rev-parse", "feat")
        assert shell_gated(w) == [("Allow", "inferred")]  # an untainted L3: no ask
        (row,) = tools(w)
        assert (row["decision"], row["status"], row["write"]) == ("gate", "ok", True)
        assert row["command"] == f"cd {repo} && git push -q origin feat"
        assert row["cwd"] == str(repo)  # where it ran


async def test_a_push_to_main_asks_and_yes_runs_it_in_a_new_turn(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    repo, remote = scratch_repo(tmp_path)
    git(repo, "checkout", "-q", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "three")
    push = f"cd {repo} && git push -q origin main"
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(
            turns=[{"when": "Bash, from", "shell": [{"tool": "Bash", "command": push}],
                    "answer": "Pushed main: {r0}"}],  # the approved turn (fmt: skip)
            shell=[{"tool": "Bash", "command": push}],
            answer="{r0}",
        )  # fmt: skip
        answer, prompt = await w.say("push main", replies=2)
        assert answer.startswith("asked the owner: run `cd ")
        assert prompt.startswith("[") and "&& git push -q origin main` in" in prompt
        assert prompt.rsplit("? Reply", 1)[0].endswith("/code/r")  # where it runs
        assert "Reply yes" in prompt and "yes always" not in prompt
        assert remote_head(remote, "main") != git(repo, "rev-parse", "main")  # not run
        assert shell_gated(w) == [("NeedsApproval", "inferred")] * 2  # in the turn, prompted
        await prompt_recorded(w)
        approved, ran = await w.say("yes", replies=2)
        assert approved.startswith("Approved: `cd ") and approved.endswith(
            "&& git push -q origin main`; Claude runs it now."
        )
        assert ran == "Pushed main: (Bash completed with no output)\nDid: pushed main in r"
        await w.settled()
        assert remote_head(remote, "main") == git(repo, "rev-parse", "main")
        # the approved turn's message is Resonant's, naming the exact command and its dir
        assert w.session()["prompt"].startswith("From Resonant: the owner said yes")
        assert f": {push}" in w.session()["prompt"] and "1. Bash, from " in w.session()["prompt"]
        decisions = [r["decision"] for r in tools(w)]
        assert decisions == ["asked", "granted"]
        assert kv_get(w.conn, GRANTS_KV) == {}  # spent
        assert len(w.audits("brain.shell_approved")) == 1


async def test_no_means_it_never_runs(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    repo, remote = scratch_repo(tmp_path)
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(shell=[{"tool": "Bash", "command": f"cd {repo} && git push -q origin main"}],
                 answer="{r0}")  # fmt: skip
        await w.say("push main", replies=2)
        await prompt_recorded(w)
        assert await w.say("no") == ["Not done."]
        await w.settled()
        assert len(w.ask_tasks()) == 1  # no approved turn
        assert remote_head(remote, "main") == git(repo, "rev-parse", "main")  # unchanged
        assert kv_get(w.conn, GRANTS_KV) is None


@pytest.mark.parametrize(
    "command",
    [
        "resonant secret set x",
        "security find-generic-password -s x",
        "uv run resonant ext add ./evil",
        "pkill -f resonant",
    ],
)
async def test_the_never_list_is_refused_without_asking(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path, command: str
) -> None:
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(shell=[{"tool": "Bash", "command": command}], answer="{r0}")
        (answer,) = await w.say("do the thing")
        await w.settled()
        assert answer.startswith("not run: ") and "Never try another way" in answer
        assert w.rows("SELECT 1 FROM approvals") == []
        assert [d for d, _ in shell_gated(w)] == ["Deny"]
        (row,) = tools(w)
        assert (row["decision"], row["status"]) == ("refused", "error")


async def test_a_write_to_resonant_home_is_refused(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    config = resonant_home / "config.yaml"
    async with start(resonant_home, chat, tmp_path) as w:
        before = config.read_text() if config.exists() else None
        w.model.triage = CLAUDE
        w.claude(shell=[
            {"tool": "Write", "file_path": str(config), "content": "autonomy: on"},
            {"tool": "Bash", "command": f"echo x >> {config}"},
        ], answer="{r0} | {r1}")  # fmt: skip
        (answer,) = await w.say("fix my config")
        await w.settled()
        assert answer.count("not run: writes under ~/.resonant or the keychains aren't") == 2
        assert (config.read_text() if config.exists() else None) == before
        assert w.rows("SELECT 1 FROM approvals") == []


async def test_a_compound_command_asks_and_isnt_a_read(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    repo, remote = scratch_repo(tmp_path)
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(shell=[{"tool": "Bash", "command": f"cd {repo} && git log -1 && git push -q"}],
                 answer="{r0}")  # fmt: skip
        answer, prompt = await w.say("log then push", replies=2)
        assert "asked the owner" in answer and "git log -1 && git push -q" in prompt
        assert shell_gated(w)[0] == ("NeedsApproval", "inferred")
        assert remote_head(remote, "feat") is None


async def test_a_tainted_turn_asks_before_a_shell_write(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """A read first (its output is outside content): the L3 push now asks."""
    repo, remote = scratch_repo(tmp_path)
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        w.claude(shell=[
            {"tool": "Bash", "command": f"git -C {repo} log -1"},
            {"tool": "Bash", "command": f"cd {repo} && git push -q origin feat"},
        ], answer="{r1}")  # fmt: skip
        answer, prompt = await w.say("check the log, then push feat", replies=2)
        assert "asked the owner" in answer and "Reply yes" in prompt
        assert set(shell_gated(w)) == {("NeedsApproval", "tainted")}
        assert remote_head(remote, "feat") is None


async def test_claude_cant_call_the_permission_tool_itself(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    repo, remote = scratch_repo(tmp_path)
    async with start(resonant_home, chat, tmp_path) as w:
        w.model.triage = CLAUDE
        fake = {"tool_name": "Bash", "input": {"command": f"cd {repo} && git push -q origin feat"},
                "tool_use_id": "t-made-up"}  # fmt: skip
        w.claude(calls=[{"name": "permission", "args": fake}], answer="{r0}")
        (answer,) = await w.say("push feat")
        await w.settled()
        assert "doesn't match a tool call" in answer
        assert shell_gated(w) == []
        assert remote_head(remote, "feat") is None


async def test_a_standing_allow_for_a_verb_in_a_repo(
    resonant_home: Path, chat: FakeChatDB, tmp_path: Path
) -> None:
    """ "Don't ask for git push to main in r": ``allow tool:shell.git-push-default in r``
    lets a push to main run without asking there (an untainted turn)."""
    repo, remote = scratch_repo(tmp_path)
    git(repo, "checkout", "-q", "main")
    git(repo, "commit", "-q", "--allow-empty", "-m", "three")
    async with start(resonant_home, chat, tmp_path) as w:
        with atomic(w.conn):
            trust_store.add_rule(w.conn, scope="r", action="tool:shell.git-push-default",
                                 effect="allow", created_from="chat", now=utcnow())  # fmt: skip
        w.model.triage = CLAUDE
        w.claude(shell=[{"tool": "Bash", "command": f"cd {repo} && git push -q origin main"}],
                 answer="Pushed.")  # fmt: skip
        (answer,) = await w.say("push main")
        await w.settled()
        assert answer == "Pushed.\nDid: pushed main in r"
        assert remote_head(remote, "main") == git(repo, "rev-parse", "main")
        (row,) = [a for a in w.audits("gate.evaluate") if a["tool"] == "shell"]
        assert (row["decision"], row["detail"]["standing"]) == ("Allow", True)
