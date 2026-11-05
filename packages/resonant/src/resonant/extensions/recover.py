"""Extension self-recovery (PER-284): the manifest's ``recover: {run, timeout, secrets}``.

launchd restarts a process that exits; this covers one that is alive but stuck (a session
that keeps its heartbeat but stopped deciding after a network drop). ``ExtensionHealth``
hands every fresh ``health.check`` result to :meth:`Recovery.observe`:

- **Trigger.** ``FAILS_TO_RECOVER`` (2) failed health runs in a row (non-zero exit, timeout,
  a missing secret). An ok run resets the count; so does a recover run (the next one needs
  two fresh failures).
- **Limits.** At most one run per extension per ``SPACING`` (15 min) and ``PER_DAY`` (3) per
  local day (``settings.timezone``). The first trigger past the cap texts once
  (``<ext>: recovered 3 times today; not trying again until tomorrow``, audited
  ``ext.recover`` ``decision=capped``) and nothing more runs until the next local day. Never
  while the kill switch is engaged (paused; unknown counts as paused) or autonomy is off.
- **Run.** Like a job command: the compiled, pinned ``recover.run`` (``host._prepare``) in
  the extension's approved copy, a scratch environment with only ``recover.secrets``, the
  host's process runner (so ``/kill`` ends it) and ``recover.timeout`` (default 120s, at
  most 300s). It runs in the background so the monitor never waits on it.
- **Record.** Audited ``ext.recover`` with ``exit_code`` and ``duration_ms``
  (``decision`` ``ok`` or ``failed``, with a code-built ``reason`` when it didn't exit).
- **Text.** One line to the owner: ``<ext>: recovering (health failed twice): <first stdout
  line>`` (redacted of the run's secret values, cleaned with ``notify.clean_text``, URLs
  dropped, at most 160 chars) or ``... recover failed (exit N)``. It goes through
  ``brain.notices.send_notice`` like a watch's notice: held while paused or in quiet hours
  (and over the hourly cap) and carried by the pulse, but outside the daily notice budget
  (this has its own cap of 3).

State is one kv row per extension, ``ext.recover.<name>`` (no migration):
``{streak, last_at, day, count, capped_told}``. It is written before the run starts, so a
crash mid-run never runs it again sooner than the limits allow.
"""

from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from resonant import killswitch
from resonant.brain.notices import send_notice
from resonant.config import Settings
from resonant.extensions.notify import clean_text
from resonant.extensions.process import (
    ExtAmbiguous,
    ExtFailed,
    ExtRequest,
    ensure_data_dir,
    scratch_env,
)
from resonant.gateway.channel import ChannelError
from resonant.monitor import scrub
from resonant.ops import Sender
from resonant.redact import Redactor
from resonant.secrets import SecretError, SecretStore, values_of
from resonant.store import pulse as pulse_items
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set

if TYPE_CHECKING:
    from resonant.extensions.host import ExtensionHost, LoadedExtension

log = logging.getLogger(__name__)

ACTION = "ext.recover"
KV_PREFIX = "ext.recover."
FAILS_TO_RECOVER = 2
SPACING = timedelta(minutes=15)
PER_DAY = 3
LINE_MAX = 160
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")  # terminal colour and cursor codes


@dataclass
class RecoverState:
    streak: int = 0  # failed health runs in a row since the last ok or recover run
    last_at: datetime | None = None  # the last recover run's start
    day: str = ""  # the local date ``count`` is for
    count: int = 0  # recover runs on ``day``
    capped_told: bool = False  # the cap text went out for ``day``

    def to_kv(self) -> dict[str, Any]:
        return {
            "streak": self.streak,
            "last_at": iso(self.last_at) if self.last_at else None,
            "day": self.day,
            "count": self.count,
            "capped_told": self.capped_told,
        }

    @classmethod
    def from_kv(cls, raw: object) -> RecoverState:
        if not isinstance(raw, dict):
            return cls()
        d = cast(Mapping[str, object], raw)
        last_at: datetime | None = None
        if isinstance(v := d.get("last_at"), str):
            try:
                last_at = datetime.fromisoformat(v)
            except ValueError:
                last_at = None
        streak, count, day = d.get("streak"), d.get("count"), d.get("day")
        return cls(
            streak=streak if isinstance(streak, int) else 0,
            last_at=last_at,
            day=day if isinstance(day, str) else "",
            count=count if isinstance(count, int) else 0,
            capped_told=d.get("capped_told") is True,
        )


def load_state(conn: sqlite3.Connection, name: str) -> RecoverState:
    return RecoverState.from_kv(kv_get(conn, KV_PREFIX + name))


def _save(conn: sqlite3.Connection, name: str, state: RecoverState) -> None:
    with atomic(conn):
        kv_set(conn, KV_PREFIX + name, state.to_kv())


def blocked(conn: sqlite3.Connection) -> str | None:
    """``paused`` or ``autonomy off`` when nothing may run on its own, else None."""
    try:
        if killswitch.is_paused(conn):
            return "paused"
    except killswitch.PausedStateUnknownError:
        return "paused"
    if kv_get(conn, "autonomy", "on") == "off":
        return "autonomy off"
    return None


def first_line(stdout: str) -> str:
    """The first non-blank stdout line, made safe to text: one line, cleaned like an
    extension's report, URLs dropped, at most ``LINE_MAX`` characters."""
    line = next((ln for ln in stdout.splitlines() if ln.strip()), "")
    flat = scrub(clean_text(_ANSI.sub("", line)), 100_000)
    # "..." not "…": send_notice cleans the title again, and that drops non-ASCII.
    return flat if len(flat) <= LINE_MAX else flat[: LINE_MAX - 3].rstrip() + "..."


def recovering_text(name: str, detail: str) -> str:
    """The one-line text; ``detail`` is cut to fit a pulse title (``TITLE_MAX``) whole."""
    head = f"{name}: recovering (health failed twice)"
    if not detail:
        return head
    # send_notice cleans the title again, which may quote a reply word or two: keep a margin.
    room = pulse_items.TITLE_MAX - len(head) - 2 - 8
    if len(detail) > room:
        detail = detail[: room - 3].rstrip() + "..."
    return f"{head}: {detail}"


def capped_text(name: str) -> str:
    return f"{name}: recovered {PER_DAY} times today; not trying again until tomorrow"


@dataclass
class Recovery:
    """Runs an extension's ``recover`` command when its health fails twice (module doc)."""

    host: ExtensionHost
    channel: Sender
    owner: str
    settings: Settings
    secrets: SecretStore | None = None
    monotonic: Callable[[], float] = time.monotonic
    _tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict[str, asyncio.Task[None]])

    @property
    def conn(self) -> sqlite3.Connection:
        return self.host.conn

    async def observe(self, name: str, ok: bool, now: datetime) -> None:
        """One fresh health result for a loaded extension; may start a recover run."""
        ext = self.host.loaded.get(name)
        if ext is None or ext.recover is None or ext.manifest.recover is None:
            return
        state = load_state(self.conn, name)
        if ok:
            if state.streak:
                state.streak = 0
                _save(self.conn, name, state)
            return
        state.streak += 1
        if state.streak < FAILS_TO_RECOVER or self._running(name) or blocked(self.conn):
            _save(self.conn, name, state)
            return
        day = now.astimezone(ZoneInfo(self.settings.timezone)).date().isoformat()
        if state.day != day:
            state.day, state.count, state.capped_told = day, 0, False
        if state.count >= PER_DAY:
            tell = not state.capped_told
            state.capped_told = True
            _save(self.conn, name, state)
            if tell:
                with atomic(self.conn):
                    audit(self.conn, ACTION, extension=name, decision="capped", day=day)
                await self._tell(f"recover/{name}/{day}/capped", capped_text(name), now)
            return
        if state.last_at is not None and now - state.last_at < SPACING:
            _save(self.conn, name, state)
            return
        state.count += 1
        state.last_at = now
        state.streak = 0
        _save(self.conn, name, state)
        key = f"recover/{name}/{day}/{state.count}"
        self._tasks[name] = asyncio.create_task(self._recover(name, ext, key, now))

    def _running(self, name: str) -> bool:
        task = self._tasks.get(name)
        return task is not None and not task.done()

    async def wait(self) -> None:
        """Wait for every recover run in flight (tests, shutdown)."""
        await asyncio.gather(*self._tasks.values(), return_exceptions=True)

    async def _recover(self, name: str, ext: LoadedExtension, key: str, now: datetime) -> None:
        started = self.monotonic()
        try:
            exit_code, reason, line = await self._run(name, ext)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a recover run never takes the monitor down
            log.warning("recover run raised %s", type(e).__name__, extra={"extension": name})
            exit_code, reason, line = None, f"error ({type(e).__name__})", ""
        duration_ms = int((self.monotonic() - started) * 1000)
        ok = exit_code == 0
        why: dict[str, Any] = {"reason": reason} if reason else {}
        with atomic(self.conn):
            audit(
                self.conn,
                ACTION,
                extension=name,
                decision="ok" if ok else "failed",
                exit_code=exit_code,
                duration_ms=duration_ms,
                **why,
            )
        if ok:
            detail = line
        elif exit_code is not None and exit_code > 0:
            detail = f"recover failed (exit {exit_code})"
        elif exit_code is not None:
            detail = f"recover failed (signal {-exit_code})"
        else:
            detail = f"recover failed ({reason})"
        await self._tell(key, recovering_text(name, detail), now)

    async def _run(self, name: str, ext: LoadedExtension) -> tuple[int | None, str, str]:
        """``(exit code or None, why it didn't exit, first stdout line)``."""
        recover, command = ext.manifest.recover, ext.recover
        assert recover is not None and command is not None  # checked by observe
        env_secrets: dict[str, str] = {}
        if recover.secrets:
            try:
                if self.secrets is None:
                    raise SecretError(f"{name}/{recover.secrets[0]}", "unavailable")
                env_secrets = await self.secrets.env_for(name, recover.secrets)
            except SecretError:
                return None, "a declared secret is missing", ""
        data_dir = self.host.data_dir / name
        ensure_data_dir(data_dir)
        env = scratch_env(
            ext=name, data_dir=data_dir, secrets=env_secrets, path=self.host.path_env, bins=ext.bins
        )
        redactor = Redactor(values_of(env))
        req = ExtRequest(
            argv=command.render({}),
            cwd=ext.root,
            env=env,
            timeout_s=recover.timeout_s,
            label=f"{name}/recover",
        )
        try:
            raw = await self.host.runner.run_raw(req, redact=redactor.text)
        except ExtFailed as e:
            return None, "paused" if e.reason == "paused" else "could not start", ""
        except ExtAmbiguous as e:
            return None, "timed out" if e.reason == "timeout" else "cut short", ""
        return raw.returncode, "", first_line(redactor.text(raw.stdout))

    async def _tell(self, key: str, title: str, now: datetime) -> None:
        try:
            await send_notice(
                self.conn,
                self.channel,
                self.owner,
                self.settings,
                key=key,
                title=title,
                now=now,
                daily_cap=False,
            )
        except ChannelError as e:  # held in the pulse already; the next pulse carries it
            log.warning("recover text not sent (%s)", type(e).__name__)


__all__ = [
    "ACTION",
    "FAILS_TO_RECOVER",
    "PER_DAY",
    "SPACING",
    "RecoverState",
    "Recovery",
    "blocked",
    "capped_text",
    "first_line",
    "load_state",
    "recovering_text",
]
