"""A scripted Claude for the voice tests (PER-273): the real ``ClaudeVoice`` (auth, quota,
guard, assembly) with the process swapped for a ``Script`` that answers from a list."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from resonant.config import VoiceConfig
from resonant.runners.voice import CallResult, ClaudeVoice
from resonant.secrets import CLAUDE_TOKEN_REF, FakeKeychain, SecretStore

TOKEN = "sk-ant-oat01-" + "Zy9Xw8Vu7T" * 9


class Script:
    """Answers each call in turn (the last one repeats). A ``CallResult`` is returned as it
    is; a string is Claude's answer. ``prompts`` are the stdin of every call."""

    def __init__(self, *answers: str | CallResult) -> None:
        self.answers = list(answers)
        self.prompts: list[str] = []

    @property
    def calls(self) -> int:
        return len(self.prompts)

    async def __call__(
        self, argv: list[str], env: dict[str, str], cwd: Path, prompt: str, timeout_s: float
    ) -> CallResult:
        n = len(self.prompts)
        self.prompts.append(prompt)
        answer = self.answers[min(n, len(self.answers) - 1)]
        return CallResult("answered", answer) if isinstance(answer, str) else answer


def scripted_voice(
    conn: sqlite3.Connection,
    tmp: Path,
    script: Script,
    *,
    cfg: VoiceConfig | None = None,
    clock: Callable[[], datetime] | None = None,
    timezone: str = "UTC",
) -> ClaudeVoice:
    keychain = FakeKeychain()
    keychain.items[CLAUDE_TOKEN_REF] = TOKEN
    return ClaudeVoice(
        conn,
        cfg=cfg or VoiceConfig(),
        secrets=SecretStore(keychain),
        cwd=tmp / "voice",
        timezone=timezone,
        claude_cmd=["claude"],
        home=tmp,
        call=script,
        clock=clock or (lambda: datetime.now(UTC)),
    )
