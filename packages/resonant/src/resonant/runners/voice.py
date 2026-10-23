"""Proactive texts in Claude's voice, the call (PER-273, docs/brain-plan.md).

``ClaudeVoice`` writes the prose of a run's outcome, the hourly pulse and the briefing with
ONE one-shot ``claude -p`` call per text; ``brain/voice.py`` holds the facts, the prompt and
the guard. Why a one-shot and not the Claude brain's long-lived session (``claude_session``):
that session is serial under a lock (a pulse would delay the owner's turn and the other way
round), the text needs no tools, its inputs include outside content (worker summaries, PR
titles) that would taint the shared session, and every call would use up its turn budget
before the session rotates. The one-shot is stateless: voice continuity comes from the prompt
and from the brains seeing the texts afterwards.

The call::

    claude -p --restricted --tools "" --permission-mode dontAsk --strict-mcp-config
        --setting-sources "" --no-session-persistence --output-format stream-json --verbose
        --max-turns 1 --append-system-prompt <the voice prompt>       (the facts on stdin)

No tool, no MCP server, no settings file, no session. The ``init`` event is checked: a
session that lists a tool or an MCP server is killed and the fallback goes out. Auth is
``AskRunner``'s: ``runner/claude_token``, else ``runner/anthropic_key`` (with
``--max-budget-usd``), else the owner's login when ``runner.guard`` is ``off``; the
environment is built from nothing.

``write`` never raises and never makes a text worse: disabled, paused, a usage limit (kv
``brain.ask_claude.limited_until``, shared with asks both ways), the daily cap (kv
``voice.calls:<date>``, separate from ``ask_claude.daily_cap``), no Claude, a timeout, a
crash, a refusal or a guard miss all return the code-built ``fallback`` unchanged. A call is
counted before it starts (fail closed). With a ``ref`` the result is kept (kv
``voice.out:<ref>``, two days) so a retry of the same text (a refused send, a rerun) reuses it
instead of calling again.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import signal
import sqlite3
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast
from zoneinfo import ZoneInfo

import psutil

from resonant import killswitch
from resonant.brain import voice
from resonant.brain.voice import Facts, VoiceResult
from resonant.config import VoiceConfig
from resonant.redact import Redactor
from resonant.runner.claude_auth import auth_source, status_env
from resonant.runners import reaper
from resonant.runners.ask import usage_of
from resonant.runners.claude import LimitHit, read_lines, usage_limit, wake_time
from resonant.runners.claude_session import LIMIT_KNOWN_KV, LIMIT_KV
from resonant.runners.reaper import MARKER_ENV, Target
from resonant.sandbox import resolve_tool
from resonant.secrets import SecretError, SecretStore, load_runner_secrets
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow

log = logging.getLogger(__name__)

CallStatus = Literal["answered", "usage_limit", "failed", "timeout", "refused", "killed"]
COUNT_KV = "voice.calls:"  # + the local date -> calls that day
OUT_KV = "voice.out:"  # + a ref -> {digest, text, used, reason, at}
KEEP = timedelta(days=2)
COUNT_KEEP_DAYS = 7
OUTCOME_LIMIT_EXTRA = 400  # the whole run-outcome text may run this far past the prose
TEXT_LIMIT = 1000  # a pulse or briefing (``pulse_job.MAX_CHARS``)
POLL_S = 0.1
KILL_GRACE_S = 2.0


@dataclass(frozen=True)
class CallResult:
    """How a call ended: ``answered`` (``text`` is Claude's), ``usage_limit``, ``failed`` (no
    answer, an error result, a crash), ``timeout``, ``refused`` (the init event listed a tool
    or an MCP server) or ``killed`` (Resonant was paused). ``result``: the final stream-json
    ``result`` line."""

    status: CallStatus
    text: str = ""
    result: dict[str, Any] | None = None


# argv, env, cwd, the prompt (stdin), timeout seconds
Call = Callable[[list[str], dict[str, str], Path, str, float], Awaitable[CallResult]]


def default_claude_cmd() -> list[str] | None:
    """The ``claude`` on the daemon's PATH, or None (tests patch this to None)."""
    found = resolve_tool("claude")
    return None if found is None else [str(found)]


def voice_argv(head: Sequence[str], cfg: VoiceConfig, *, api_billing: bool) -> list[str]:
    """The one-shot's argv (the facts go on stdin). No tools, no MCP, no settings, no
    session; one turn."""
    argv = [
        *head,
        "-p",
        "--restricted",
        "--tools",
        "",
        "--permission-mode",
        "dontAsk",
        "--strict-mcp-config",
        "--setting-sources",
        "",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--max-turns",
        "1",
        "--append-system-prompt",
        voice.system_prompt(cfg.max_chars),
    ]
    if cfg.model:
        argv += ["--model", cfg.model]
    if api_billing:  # a subscription has no dollar budget
        argv += ["--max-budget-usd", f"{cfg.max_budget_usd:g}"]
    return argv


def classify(result: dict[str, Any], tz: ZoneInfo, now: datetime) -> CallResult:
    """A final ``result`` line as a :class:`CallResult`."""
    if usage_limit(result, tz=tz, now=now) is not None:
        return CallResult("usage_limit", result=result)
    text = result.get("result")
    if result.get("is_error") is True or result.get("subtype") != "success":
        return CallResult("failed", result=result)
    if not isinstance(text, str) or not text.strip():
        return CallResult("failed", result=result)
    return CallResult("answered", text, result)


def _digest(prompt: str, fallback: str) -> str:
    return hashlib.sha256(f"{prompt}\0{fallback}".encode()).hexdigest()


class ClaudeVoice:
    """The :class:`~resonant.brain.voice.Voice` that asks Claude (see the module docstring).

    ``claude_cmd``/``call``/``login_check`` are seams for tests; by default the ``claude``
    on the PATH, a real subprocess and the owner's login check (``runner.guard: off`` only).
    ``home`` is the ``HOME`` the call runs with (the owner's, for their login)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        cfg: VoiceConfig,
        secrets: SecretStore,
        cwd: Path,
        guard: str = "off",
        timezone: str = "UTC",
        claude_cmd: Sequence[str] | None = None,
        home: Path | None = None,
        login_check: Callable[[], Awaitable[bool]] | None = None,
        call: Call | None = None,
        clock: Callable[[], datetime] = utcnow,
        poll_s: float = POLL_S,
        kill_grace_s: float = KILL_GRACE_S,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.secrets = secrets
        self.cwd = cwd
        self.guard = guard
        self.timezone = timezone
        self.tz = ZoneInfo(timezone)
        self._claude_cmd = None if claude_cmd is None else tuple(claude_cmd)
        self.home = home or Path.home()
        self._login_check = login_check
        self._call: Call = call or self.spawn
        self.clock = clock
        self.poll_s = poll_s
        self.kill_grace_s = kill_grace_s

    # -- state --

    def _today(self) -> str:
        return self.clock().astimezone(self.tz).date().isoformat()

    def calls_today(self) -> int:
        n = kv_get(self.conn, COUNT_KV + self._today())
        return n if isinstance(n, int) and not isinstance(n, bool) else 0

    def limited_until(self) -> datetime | None:
        """When Claude may be used again after a usage limit (shared with asks), or None."""
        raw = kv_get(self.conn, LIMIT_KV)
        if not isinstance(raw, str):
            return None
        try:
            until = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return until if until.tzinfo is not None and until > self.clock() else None

    def _paused(self) -> bool:
        try:
            return killswitch.is_paused(self.conn)
        except killswitch.PausedStateUnknownError:
            return True  # unknown counts as paused

    def _head(self) -> list[str] | None:
        if self._claude_cmd is not None:
            return list(self._claude_cmd)
        return default_claude_cmd()

    def _limit(self, hit: LimitHit) -> None:
        """Record a usage limit as asks do (so both stop until the reset); no notice."""
        if self.limited_until() is not None:
            return
        until = wake_time(hit, self.clock())
        with atomic(self.conn):
            kv_set(self.conn, LIMIT_KV, iso(until))
            kv_set(self.conn, LIMIT_KNOWN_KV, hit.reset_at is not None)

    def _count(self) -> int:
        """One call, counted before it starts; old counters and kept texts are pruned."""
        now = self.clock()
        today = self._today()
        cutoff = (now.astimezone(self.tz) - timedelta(days=COUNT_KEEP_DAYS)).date().isoformat()
        with atomic(self.conn):
            n = self.calls_today() + 1
            kv_set(self.conn, COUNT_KV + today, n)
            for (key,) in self.conn.execute(
                "SELECT key FROM kv WHERE key LIKE ?", (COUNT_KV + "%",)
            ).fetchall():
                if str(key).removeprefix(COUNT_KV) < cutoff:
                    self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))
            for (key,) in self.conn.execute(
                "SELECT key FROM kv WHERE key LIKE ?", (OUT_KV + "%",)
            ).fetchall():
                rec: Any = kv_get(self.conn, str(key))
                at = _rec_at(rec)
                if at is None or at < now - KEEP:
                    self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))
        return n

    def _kept(self, ref: str | None, digest: str) -> VoiceResult | None:
        if ref is None:
            return None
        rec: Any = kv_get(self.conn, OUT_KV + ref)
        if not isinstance(rec, dict):
            return None
        d = cast(dict[str, Any], rec)
        text, used = d.get("text"), d.get("used")
        if d.get("digest") != digest or not isinstance(text, str) or not isinstance(used, bool):
            return None
        reason = "cached" if used else str(d.get("reason") or "failed")
        return VoiceResult(text, used, reason)

    def _keep(self, ref: str | None, digest: str, result: VoiceResult) -> None:
        if ref is None:
            return
        rec = {
            "digest": digest,
            "text": result.text,
            "used": result.used,
            "reason": result.reason,
            "at": iso(self.clock()),
        }
        with atomic(self.conn):
            kv_set(self.conn, OUT_KV + ref, rec)

    # -- the write --

    async def write(self, facts: Facts, fallback: str, *, ref: str | None = None) -> VoiceResult:
        """Claude's text for ``facts``, or ``fallback`` (see the module docstring). Never
        raises."""
        started = time.monotonic()
        try:
            result = await self._write(facts, fallback, ref)
        except Exception:
            log.exception("voice write failed")
            result = VoiceResult(fallback, False, "failed")
        log.info(
            "voice",
            extra={
                "kind": facts.kind,
                "used": result.used,
                "reason": result.reason,
                "s": round(time.monotonic() - started, 1),
                "calls_today": self.calls_today(),
                "cap": self.cfg.daily_cap,
            },
        )
        return result

    def _unused(self, fallback: str, reason: str) -> VoiceResult:
        return VoiceResult(fallback, False, reason)

    async def _write(self, facts: Facts, fallback: str, ref: str | None) -> VoiceResult:
        cfg = self.cfg
        if not cfg.enabled or facts.kind not in cfg.kinds:
            return self._unused(fallback, "disabled")
        prompt = voice.prompt(facts, cfg.max_chars)
        digest = _digest(prompt, fallback)
        if (kept := self._kept(ref, digest)) is not None:
            return kept
        if self._paused():
            return self._unused(fallback, "paused")
        if self.limited_until() is not None:
            return self._unused(fallback, "limited")
        if cfg.daily_cap and self.calls_today() >= cfg.daily_cap:
            return self._unused(fallback, "cap")
        try:
            secrets = await load_runner_secrets(self.secrets, claude=False)
        except SecretError:
            return self._unused(fallback, "not_set_up")
        login = (
            not (secrets.claude_token or secrets.anthropic_key)
            and self.guard == "off"
            and await self._login_ok()
        )
        source = auth_source(secrets, login_ok=login)
        head = self._head()
        if source is None or head is None:
            return self._unused(fallback, "not_set_up")
        env = status_env(self.home)
        if source == "token" and secrets.claude_token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = secrets.claude_token
        elif source == "api_key" and secrets.anthropic_key:
            env["ANTHROPIC_API_KEY"] = secrets.anthropic_key
        env[MARKER_ENV] = f"voice-{uuid.uuid4().hex[:12]}"
        argv = voice_argv(head, cfg, api_billing=source == "api_key")
        self.cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        calls = self._count()  # before the spawn: a crash can't make it free
        out = await self._call(argv, env, self.cwd, prompt, cfg.timeout_s)
        result = self._finish(out, facts, fallback, Redactor(secrets.values()))
        self._keep(ref, digest, result)
        with atomic(self.conn):
            audit(
                self.conn,
                "voice.write",
                kind=facts.kind,
                used=result.used,
                reason=result.reason,
                calls_today=calls,
            )
        return result

    def _finish(
        self, out: CallResult, facts: Facts, fallback: str, redactor: Redactor
    ) -> VoiceResult:
        usage = usage_of(out.result) if out.result is not None else None
        now = self.clock()
        match out.status:
            case "usage_limit":
                hit = usage_limit(out.result or {}, tz=self.tz, now=now) or LimitHit(None)
                self._limit(hit)
                return VoiceResult(fallback, False, "limited", usage)
            case "answered":
                limit = TEXT_LIMIT
                if facts.kind == "outcome":
                    limit = self.cfg.max_chars + OUTCOME_LIMIT_EXTRA
                composed = voice.compose(
                    redactor.text(out.text),
                    facts,
                    fallback,
                    now=now,
                    tz=self.timezone,
                    max_chars=self.cfg.max_chars,
                    limit=limit,
                )
                return VoiceResult(composed.text, composed.used, composed.reason, usage)
            case status:
                reason = {"timeout": "timeout", "refused": "refused", "killed": "paused"}
                return VoiceResult(fallback, False, reason.get(status, "failed"), usage)

    async def _login_ok(self) -> bool:
        if self._login_check is None:
            return False
        try:
            return await self._login_check()
        except Exception:
            log.exception("claude login check failed")
            return False

    # -- the process --

    async def spawn(
        self, argv: list[str], env: dict[str, str], cwd: Path, prompt: str, timeout_s: float
    ) -> CallResult:
        """Run the call: always ends with its whole process group gone."""
        started = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv,
                cwd=cwd,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except OSError:
            return CallResult("failed")
        try:
            created: float | None = psutil.Process(proc.pid).create_time()
        except psutil.Error:
            created = None  # gone or unreadable: the env marker and a direct kill still end it
        target = Target(proc.pid, proc.pid, created, marker=env.get(MARKER_ENV))
        result: dict[str, Any] | None = None
        refused = False
        assert proc.stdin and proc.stdout and proc.stderr

        async def feed() -> None:
            assert proc.stdin
            with contextlib.suppress(OSError, ConnectionError):
                proc.stdin.write(prompt.encode())
                await proc.stdin.drain()
            with contextlib.suppress(OSError, ConnectionError):
                proc.stdin.close()

        async def pump_out() -> None:
            nonlocal result, refused
            assert proc.stdout
            async for raw, truncated in read_lines(proc.stdout):
                if truncated or not raw.startswith(b"{"):
                    continue
                try:
                    msg: Any = json.loads(raw)
                except ValueError:
                    continue
                if not isinstance(msg, dict):
                    continue
                m = cast(dict[str, Any], msg)
                if m.get("type") == "system" and m.get("subtype") == "init":
                    tools, servers = m.get("tools"), m.get("mcp_servers")
                    if (isinstance(tools, list) and tools) or (
                        isinstance(servers, list) and servers
                    ):
                        refused = True  # a session with tools is not the one we asked for
                elif m.get("type") == "result":
                    result = m

        async def pump_err() -> None:
            assert proc.stderr
            async for _raw, _truncated in read_lines(proc.stderr, 8192):
                pass  # drained, never logged: it can quote what Claude was given

        pumps = [asyncio.create_task(c) for c in (feed(), pump_out(), pump_err())]
        exited = asyncio.create_task(proc.wait())
        stop: str | None = None
        try:
            while True:
                done, _ = await asyncio.wait({exited}, timeout=self.poll_s)
                if done:
                    break
                reaper.sample(target)
                if refused:
                    stop = "refused"
                elif self._paused():
                    stop = "killed"
                elif time.monotonic() - started >= timeout_s:
                    stop = "timeout"
                if stop is not None:
                    break
        finally:
            try:
                await reaper.reap(target, grace_s=self.kill_grace_s)
            except asyncio.CancelledError:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(proc.pid, signal.SIGKILL)
                raise
            finally:
                if proc.returncode is None:  # our own unreaped child: its pid can't be reused
                    with contextlib.suppress(ProcessLookupError):
                        proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), 5)
            exited.cancel()
            done_pumps, pending = await asyncio.wait(pumps, timeout=5)
            for t in pending:
                t.cancel()
            await asyncio.wait([*done_pumps, *pending, exited])
        if refused:
            return CallResult("refused")
        if stop == "killed":
            return CallResult("killed")
        if stop == "timeout":
            return CallResult("timeout")
        if result is None:
            return CallResult("failed")
        return classify(result, self.tz, self.clock())


def _rec_at(rec: Any) -> datetime | None:
    try:
        at = datetime.fromisoformat(str(cast(dict[str, Any], rec)["at"]))
    except (KeyError, ValueError, TypeError):
        return None
    return at if at.utcoffset() is not None else None


__all__ = [
    "COUNT_KV",
    "KEEP",
    "OUT_KV",
    "Call",
    "CallResult",
    "CallStatus",
    "ClaudeVoice",
    "classify",
    "default_claude_cmd",
    "voice_argv",
]
