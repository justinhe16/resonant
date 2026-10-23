"""Proactive texts in Claude's voice (PER-273): the call, ``runners/voice.py``.

The real ``ClaudeVoice`` over ``e2e/fake_claude.py`` (a ``claude -p`` stand-in that acts out a
plan), in a temp home. Nothing here starts the real ``claude``.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest

from resonant import killswitch
from resonant.brain.voice import Facts, Item
from resonant.config import VoiceConfig
from resonant.e2e import fake_claude
from resonant.runners import reaper
from resonant.runners.claude_session import LIMIT_KV
from resonant.runners.reaper import MARKER_ENV, Target
from resonant.runners.voice import (
    COUNT_KV,
    OUT_KV,
    Call,
    CallResult,
    ClaudeVoice,
    classify,
    voice_argv,
)
from resonant.secrets import ANTHROPIC_KEY_REF, CLAUDE_TOKEN_REF, FakeKeychain, SecretStore
from resonant.store.db import atomic, iso, kv_get, kv_set

FAKE = Path(fake_claude.__file__)
TOKEN = "sk-ant-oat01-" + "Zy9Xw8Vu7T" * 9
URL = "https://github.com/o/site/pull/18"
GOOD = "R14 opened PR #18 on site: the about page's footer links pointed at the old team path."
FALLBACK = "R14 opened PR #18 (CI green): " + URL
NOW = datetime(2026, 10, 22, 17, 0, tzinfo=UTC)
APPROVAL = "A7"


def facts() -> Facts:
    item = Item(
        ref="R14", outcome="opened_pr", repo="site", ask="fix the footer links", pr=18, pr_url=URL,
        ci="green", summary="Fixed the three footer links.", needs="/merge R14 to merge PR #18",
    )  # fmt: skip
    return Facts("outcome", (item,), approvals=(APPROVAL,))


class Rig:
    """A ``ClaudeVoice`` over the fake, recording every call it makes."""

    def __init__(self, tmp: Path, db: sqlite3.Connection, **kw: Any) -> None:
        tmp.mkdir(parents=True, exist_ok=True)
        self.tmp = tmp
        self.db = db
        self.calls: list[tuple[list[str], dict[str, str], Path, str, float]] = []
        self.plan_path = tmp / "plan.json"
        self.notes = tmp / "notes"
        self.notes.mkdir(exist_ok=True)
        self.keychain = FakeKeychain()
        if kw.pop("token", True):
            self.keychain.items[CLAUDE_TOKEN_REF] = TOKEN
        self.cfg: VoiceConfig = kw.pop("cfg", VoiceConfig(timeout_s=10))
        self.now = NOW
        self.plan({"answer": GOOD})
        cmd: list[str] = kw.pop("cmd", None) or [
            sys.executable, "-I", str(FAKE), "--script", str(self.plan_path),
            "--notes", str(self.notes),
        ]  # fmt: skip
        self.voice = ClaudeVoice(
            db,
            cfg=self.cfg,
            secrets=SecretStore(self.keychain),
            cwd=tmp / "voice",
            timezone="America/Los_Angeles",
            claude_cmd=cmd,
            home=tmp / "home",
            call=self._call,
            clock=lambda: self.now,
            poll_s=0.02,
            kill_grace_s=0.3,
            **kw,
        )
        (tmp / "home").mkdir(exist_ok=True)

    def plan(self, plan: dict[str, Any]) -> None:
        self.plan_path.write_text(json.dumps(plan))

    async def _call(
        self, argv: list[str], env: dict[str, str], cwd: Path, prompt: str, timeout_s: float
    ) -> CallResult:
        self.calls.append((argv, env, cwd, prompt, timeout_s))
        return await self.voice.spawn(argv, env, cwd, prompt, timeout_s)

    def session(self) -> dict[str, Any]:
        return json.loads((self.notes / "session.json").read_text())


@pytest.fixture
def rig(tmp_path: Path, db: sqlite3.Connection) -> Rig:
    return Rig(tmp_path, db)


async def test_a_good_text_is_used_with_the_closing_line(rig: Rig) -> None:
    out = await rig.voice.write(facts(), FALLBACK)
    assert out.used and out.reason == "ok"
    assert out.text == f"{GOOD}\nNeeds you: approve A7; /merge R14 to merge PR #18."
    assert out.usage == {"input_tokens": 1200, "output_tokens": 80, "duration_ms": 40}
    assert len(rig.calls) == 1


async def test_the_call_is_locked_down(rig: Rig) -> None:
    await rig.voice.write(facts(), FALLBACK)
    argv, _env, cwd, _prompt, _t = rig.calls[0]
    session = rig.session()["argv"]
    for flag in (
        "-p",
        "--restricted",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--verbose",
    ):
        assert flag in session
    pairs = dict(pairwise(session))
    assert pairs["--tools"] == "" and pairs["--permission-mode"] == "dontAsk"
    assert pairs["--setting-sources"] == "" and pairs["--max-turns"] == "1"
    assert pairs["--output-format"] == "stream-json"
    assert "at most 600 characters" in pairs["--append-system-prompt"]
    assert "--mcp-config" not in argv and "--max-budget-usd" not in argv  # a subscription
    assert "--model" not in argv
    assert cwd == rig.tmp / "voice" and cwd.is_dir() and (cwd.stat().st_mode & 0o777) == 0o700


async def test_the_environment_is_built_from_nothing(rig: Rig) -> None:
    await rig.voice.write(facts(), FALLBACK)
    _argv, env, _cwd, _p, _t = rig.calls[0]
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == TOKEN and "ANTHROPIC_API_KEY" not in env
    assert env["HOME"] == str(rig.tmp / "home")
    assert env[MARKER_ENV].startswith("voice-")
    assert set(env) <= {
        "CLAUDE_CODE_OAUTH_TOKEN", "HOME", "PATH", "LANG", "DISABLE_AUTOUPDATER", "USER",
        "LOGNAME", MARKER_ENV,
    }  # fmt: skip


async def test_the_facts_are_on_stdin_and_no_approval_code_is(rig: Rig) -> None:
    await rig.voice.write(facts(), FALLBACK)
    prompt = rig.session()["prompt"]
    assert '"ref":"R14"' in prompt and "fix the footer links" in prompt
    assert APPROVAL not in prompt and "/merge" not in prompt and FALLBACK not in prompt
    assert APPROVAL not in json.dumps(rig.session()["argv"])


async def test_api_billing_and_model_flags(tmp_path: Path, db: sqlite3.Connection) -> None:
    r = Rig(tmp_path, db, token=False, cfg=VoiceConfig(model="sonnet", max_budget_usd=0.2))
    r.keychain.items[ANTHROPIC_KEY_REF] = "sk-ant-api03-" + "Ab1" * 20
    await r.voice.write(facts(), FALLBACK)
    argv, env, *_ = r.calls[0]
    assert argv[argv.index("--max-budget-usd") + 1] == "0.2"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "ANTHROPIC_API_KEY" in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    plain = voice_argv(["claude"], VoiceConfig(), api_billing=False)
    assert plain[0] == "claude" and "--model" not in plain


async def test_a_guard_miss_sends_exactly_the_fallback(rig: Rig) -> None:
    rig.plan({"answer": "btw: R14 opened PR #18."})
    out = await rig.voice.write(facts(), FALLBACK)
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "guard:format")
    rig.plan({"answer": "R14 opened PR #18 and changed 7 files."})
    out = await rig.voice.write(facts(), FALLBACK + ".")
    assert (out.text, out.used, out.reason) == (FALLBACK + ".", False, "guard:numbers")


async def test_a_secret_in_the_answer_never_reaches_the_text(rig: Rig) -> None:
    rig.plan({"answer": f"R14 opened PR #18. Token {TOKEN}"})
    out = await rig.voice.write(facts(), FALLBACK)
    assert TOKEN not in out.text and TOKEN not in json.dumps(out.reason)


async def test_a_crash_is_the_fallback(rig: Rig) -> None:
    rig.plan({"crash": "always"})
    out = await rig.voice.write(facts(), FALLBACK)
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "failed")


async def test_output_that_is_not_json_is_the_fallback(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    r = Rig(tmp_path, db, cmd=[sys.executable, "-I", "-c", "print('hello'); print('{oops')"])
    out = await r.voice.write(facts(), FALLBACK)
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "failed")


async def test_an_error_result_is_the_fallback(rig: Rig) -> None:
    result = {"type": "result", "subtype": "error_max_turns", "is_error": True}
    assert classify(result, rig.voice.tz, NOW).status == "failed"
    assert classify({"subtype": "success", "result": "  "}, rig.voice.tz, NOW).status == "failed"
    ok = classify({"subtype": "success", "is_error": False, "result": "hi"}, rig.voice.tz, NOW)
    assert ok.status == "answered" and ok.text == "hi"


async def test_a_hang_times_out_and_the_process_group_is_gone(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    r = Rig(tmp_path, db, cfg=VoiceConfig(timeout_s=0.6))
    r.plan({"hang": True})
    started = time.monotonic()
    out = await r.voice.write(facts(), FALLBACK)
    assert time.monotonic() - started < 6
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "timeout")
    marker = r.calls[0][1][MARKER_ENV]
    assert reaper.find(Target(None, None, None, marker=marker))[0] == []


async def test_a_session_with_tools_is_refused_and_killed(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    r = Rig(tmp_path, db)
    r.plan({"init_tools": ["Bash"], "hang": True})
    started = time.monotonic()
    out = await r.voice.write(facts(), FALLBACK)
    assert time.monotonic() - started < 6
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "refused")
    assert reaper.find(Target(None, None, None, marker=r.calls[0][1][MARKER_ENV]))[0] == []


async def test_a_usage_limit_is_remembered_for_asks_too(rig: Rig) -> None:
    reset = int((NOW + timedelta(hours=2)).timestamp())
    rig.plan({"limit": reset})
    out = await rig.voice.write(facts(), FALLBACK)
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "limited")
    until = rig.voice.limited_until()
    assert until is not None and timedelta(hours=2) <= until - NOW <= timedelta(hours=2, minutes=5)
    assert kv_get(rig.db, LIMIT_KV) == iso(until)  # the key asks read
    again = await rig.voice.write(facts(), FALLBACK)
    assert again.reason == "limited" and len(rig.calls) == 1  # no second call
    rig.now = NOW + timedelta(hours=3)  # past the reset: Claude is used again
    rig.plan({"answer": GOOD})
    assert (await rig.voice.write(facts(), FALLBACK)).used


async def test_a_limit_hit_by_an_ask_stops_the_voice(rig: Rig) -> None:
    with atomic(rig.db):
        kv_set(rig.db, LIMIT_KV, iso(NOW + timedelta(hours=1)))
    out = await rig.voice.write(facts(), FALLBACK)
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "limited") and not rig.calls


async def test_no_process_when_disabled_capped_paused_or_not_set_up(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    off = Rig(tmp_path / "a", db, cfg=VoiceConfig(enabled=False))
    assert (await off.voice.write(facts(), FALLBACK)).reason == "disabled" and not off.calls
    kinds = Rig(tmp_path / "b", db, cfg=VoiceConfig(kinds=["pulse"]))
    out = await kinds.voice.write(facts(), FALLBACK)
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "disabled") and not kinds.calls
    capped = Rig(tmp_path / "c", db, cfg=VoiceConfig(daily_cap=2, timeout_s=10))
    assert [(await capped.voice.write(facts(), FALLBACK + str(n))).reason for n in range(3)] == [
        "ok", "ok", "cap",
    ]  # fmt: skip
    assert len(capped.calls) == 2 and capped.voice.calls_today() == 2
    capped.now = NOW + timedelta(days=1)  # tomorrow: the count starts again
    assert (await capped.voice.write(facts(), FALLBACK + "x")).reason == "ok"
    nokey = Rig(tmp_path / "d", db, token=False)
    out = await nokey.voice.write(facts(), FALLBACK)
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "not_set_up") and not nokey.calls
    paused = Rig(tmp_path / "e", db)
    with atomic(db):
        killswitch.engage(db, actor="test")
    out = await paused.voice.write(facts(), FALLBACK)
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "paused") and not paused.calls


async def test_the_login_is_used_only_with_the_guard_off(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    async def ok() -> bool:
        return True

    r = Rig(tmp_path, db, token=False, login_check=ok)
    assert (await r.voice.write(facts(), FALLBACK)).used
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in r.calls[0][1]
    thin = Rig(tmp_path / "t", db, token=False, login_check=ok, guard="thin")
    assert (await thin.voice.write(facts(), FALLBACK)).reason == "not_set_up"


async def test_the_counter_is_kept_before_the_call_and_old_ones_pruned(
    rig: Rig, db: sqlite3.Connection
) -> None:
    with atomic(db):
        kv_set(db, COUNT_KV + "2026-10-01", 5)
        kv_set(db, OUT_KV + "old", {"digest": "x", "text": "t", "used": True, "reason": "ok",
                                    "at": iso(NOW - timedelta(days=3))})  # fmt: skip
    rig.plan({"crash": "always"})
    await rig.voice.write(facts(), FALLBACK)
    assert kv_get(db, COUNT_KV + "2026-10-22") == 1  # counted though it crashed
    assert kv_get(db, COUNT_KV + "2026-10-01") is None and kv_get(db, OUT_KV + "old") is None
    row = db.execute("SELECT detail FROM audit_log WHERE action = 'voice.write'").fetchone()
    detail = json.loads(row["detail"])
    assert detail == {"kind": "outcome", "used": False, "reason": "failed", "calls_today": 1}


async def test_a_retry_with_the_same_ref_reuses_the_text(rig: Rig) -> None:
    first = await rig.voice.write(facts(), FALLBACK, ref="run:R14:ready")
    assert first.used and len(rig.calls) == 1
    rig.plan({"answer": "R14 opened PR #18, a different way."})
    again = await rig.voice.write(facts(), FALLBACK, ref="run:R14:ready")
    assert (again.text, again.used, again.reason) == (first.text, True, "cached")
    assert len(rig.calls) == 1 and rig.voice.calls_today() == 1
    # changed facts (a new fallback) call again
    changed = await rig.voice.write(facts(), FALLBACK + " (CI red)", ref="run:R14:ready")
    assert changed.used and len(rig.calls) == 2
    # another ref is its own text
    await rig.voice.write(facts(), FALLBACK, ref="run:R15:ready")
    assert len(rig.calls) == 3


async def test_a_fallback_is_kept_for_a_retry_too(rig: Rig) -> None:
    rig.plan({"answer": "btw: no"})
    first = await rig.voice.write(facts(), FALLBACK, ref="run:R14:failed")
    again = await rig.voice.write(facts(), FALLBACK, ref="run:R14:failed")
    assert (again.text, again.used, again.reason) == (FALLBACK, False, "guard:format")
    assert first.reason == again.reason and len(rig.calls) == 1


async def test_a_custom_call_that_raises_is_the_fallback(
    tmp_path: Path, db: sqlite3.Connection
) -> None:
    async def boom(*_a: Any) -> CallResult:
        raise RuntimeError("boom")

    call: Call = boom
    r = Rig(tmp_path, db)
    v = ClaudeVoice(db, cfg=VoiceConfig(), secrets=SecretStore(r.keychain), cwd=tmp_path / "v",
                    claude_cmd=["claude"], call=call)  # fmt: skip
    out = await v.write(facts(), FALLBACK)
    assert (out.text, out.used, out.reason) == (FALLBACK, False, "failed")


def test_voice_argv_has_no_tools_or_servers() -> None:
    argv = voice_argv(["claude"], VoiceConfig(max_chars=300), api_billing=True)
    assert argv[:2] == ["claude", "-p"]
    i = argv.index("--tools")
    assert argv[i + 1] == ""
    assert "--mcp-config" not in argv and "--add-dir" not in argv and "--allowedTools" not in argv
    assert argv[argv.index("--append-system-prompt") + 1].count("300 characters") == 1


async def test_transient_results_are_not_kept_for_a_retry(rig: Rig) -> None:
    with atomic(rig.db):
        kv_set(rig.db, LIMIT_KV, iso(NOW + timedelta(hours=1)))
    first = await rig.voice.write(facts(), FALLBACK, ref="run:R14:ready")
    assert first.reason == "limited" and kv_get(rig.db, OUT_KV + "run:R14:ready") is None
    rig.now = NOW + timedelta(hours=2)  # the limit cleared: the same text can be voiced
    again = await rig.voice.write(facts(), FALLBACK, ref="run:R14:ready")
    assert again.used and len(rig.calls) == 1
    rig.plan({"crash": "always"})
    await rig.voice.write(facts(), FALLBACK + "!", ref="run:R15:ready")
    assert kv_get(rig.db, OUT_KV + "run:R15:ready") is None  # a crash isn't kept either


async def test_the_default_login_check_reaches_the_owner_login(
    tmp_path: Path, db: sqlite3.Connection, never_the_real_claude_login: list[tuple[str, ...]]
) -> None:
    """Built as ``build_imessage_stack`` builds it (no ``login_check``), with guard off and no
    token, the voice asks the owner-login probe (stubbed by conftest: never the real CLI)."""
    r = Rig(tmp_path, db, token=False)
    v = ClaudeVoice(
        db, cfg=VoiceConfig(), secrets=SecretStore(r.keychain), cwd=tmp_path / "v",
        claude_cmd=["claude-cmd"], home=tmp_path / "home",
    )  # fmt: skip
    out = await v.write(facts(), FALLBACK)
    assert out.reason == "not_set_up"  # the probe says "not logged in"
    assert never_the_real_claude_login == [("claude-cmd", str(tmp_path / "home"))]
