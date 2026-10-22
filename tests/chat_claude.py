"""A fake Claude brain for tests that drive plain-words chat (PER-256: the local model only
routes, so every plain-words text that isn't small talk goes to Claude).

``attach_claude(daemon, root=...)`` registers a real ``AskRunner`` (the Claude brain's tool
bridge, gate and executor) over ``e2e/fake_claude.py`` in a started-but-not-running daemon.
It acts out the plan in the returned file: ``{"calls": [{"name", "args"}], "answer": "..."}``
(write it with ``json.dumps`` before the text is sent). Nothing reaches a real claude.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from resonant.config import DEFAULT_READ_DENY
from resonant.daemon import Daemon
from resonant.runners.ask import AskRunner, AskScope
from resonant.secrets import CLAUDE_TOKEN_REF, FakeKeychain, SecretStore
from resonant.wiring import tool_access

FAKE_CLAUDE = (
    Path(__file__).parent.parent / "packages" / "resonant" / "src" / "resonant" / "e2e"
    / "fake_claude.py"
)  # fmt: skip
TOKEN = "sk-ant-oat01-FAKETOKENVALUE0123456789"


def attach_claude(daemon: Daemon, *, root: Path, answer: str = "Nothing to report.") -> Path:
    """Register the fake Claude brain on ``daemon``; the plan file it reads (initially just
    ``answer``)."""
    assert daemon.imessage is not None and daemon.state.components is not None
    brain = daemon.imessage.brain
    assert brain is not None
    brain.runner_gaps = None
    root.mkdir(parents=True, exist_ok=True)
    plan = root / "claude-plan.json"
    plan.write_text(json.dumps({"answer": answer}))
    keychain = FakeKeychain()
    keychain.items[CLAUDE_TOKEN_REF] = TOKEN
    settings = daemon.settings
    asks = AskRunner(
        daemon.conn,
        cfg=settings.brain.ask_claude,
        scope=AskScope((root,), DEFAULT_READ_DENY),
        ask_root=root / "ask",
        channel=daemon.imessage.channel,
        secrets=SecretStore(keychain),
        owner="justin",
        create_task=daemon.loop.create_task,
        claude_cmd=[sys.executable, "-I", str(FAKE_CLAUDE), "--script", str(plan)],
        home=root / "claude-home",
        tools=tool_access(daemon.state.components, brain, settings),
        poll_s=0.05,
        kill_grace_s=0.5,
    )
    daemon.state.components.runners[asks.name] = asks
    daemon.loop.runners[asks.name] = asks
    return plan


def write_plan(plan: Path, **fields: Any) -> None:
    plan.write_text(json.dumps(fields))
