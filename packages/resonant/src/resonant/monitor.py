"""Brain health: periodic checks, owner alerts over iMessage, and dead-man reasons.

``HealthMonitor`` evaluates four checks every ``interval_s`` (60s):

- ``model``: ``ModelClient.probe()`` every ``model_every_s`` (5 min). The result is cached
  in kv ``model.probe`` (the builtin ``model_status`` tool reads it).
- ``imessage``: the channel's reader health plus the last self-test (kv
  ``imessage.selftest``). The self-test is re-run every ``selftest_every_s`` (6h), after
  ``selftest_after_errors`` (2) consecutive reader errors, and every
  ``selftest_retry_s`` (30 min) while the last one failed.
- ``loop``: ``Daemon.healthy()``, dead-lettered events and failed tasks in the last hour.
- ``store``: the DB is writable and free disk is above ``min_free_bytes`` (2GB).

Each check is a small state machine ``ok -> degraded -> ok`` persisted in kv
``health.<check>`` = ``{state, since, detail, checked_at, ...}``. The owner gets one
iMessage per incident once a check has been degraded for ``alert_after`` (2) consecutive
evaluations, at most one per check per ``alert_cooldown_s`` (6h), and one "resolved: ..."
message when an alerted incident clears. While the ``imessage`` check itself is degraded
(or there is no channel), no iMessage is attempted: :meth:`HealthMonitor.deadman_reason`
returns the reason and the healthchecks.io ``/fail`` ping carries it instead.

Alert text and dead-man reasons are built by code from short check details. URLs are
scrubbed and the text is capped, so no secret or ping URL ever leaves in a message.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from resonant.gateway.channel import ChannelError, MessageRef, SelfTestResult
from resonant.models import ProbeResult
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow

log = logging.getLogger(__name__)

CheckName = Literal["model", "imessage", "loop", "store"]
CHECKS: tuple[CheckName, ...] = ("model", "imessage", "loop", "store")
HEALTH_KV_PREFIX = "health."
MODEL_PROBE_KV = "model.probe"  # same key and shape as `resonant model probe`
SELFTEST_KV = "imessage.selftest"  # written by IMessageChannel.self_test
DEAD_LETTER_ACTION = "event.dead_letter"

GB = 1024**3
_MAX_DETAIL = 120
_URL_RE = re.compile(r"\b[a-z][a-z0-9+.-]*://\S+", re.IGNORECASE)
# Fields of ProbeResult.as_dict() plus what the CLI adds. Nothing else is cached.
_PROBE_FIELDS = ("ok", "model_present", "ctx_ok", "thinking_off_ok", "latency_ms", "detail")

State = Literal["ok", "degraded", "unknown"]


def scrub(text: str, limit: int = _MAX_DETAIL) -> str:
    """One line, URLs replaced by ``<url>``, capped at ``limit`` characters."""
    one_line = " ".join(_URL_RE.sub("<url>", text).split())
    return one_line if len(one_line) <= limit else one_line[: limit - 1] + "…"


class ModelProber(Protocol):
    async def probe(self) -> ProbeResult: ...


class MonitoredChannel(Protocol):
    """The parts of a ``Channel`` the monitor uses (``IMessageChannel`` satisfies it)."""

    @property
    def name(self) -> str: ...

    def health(self) -> dict[str, Any]: ...

    async def self_test(self, *, timeout_s: float = ...) -> SelfTestResult: ...

    async def send(
        self,
        principal: str,
        text: str,
        *,
        thread: MessageRef | None = None,
        dedupe_key: str | None = None,
    ) -> MessageRef: ...


@dataclass(frozen=True)
class CheckResult:
    ok: bool
    detail: str  # short, code-built; scrubbed before it reaches a message


@dataclass
class CheckState:
    state: State = "unknown"
    since: datetime | None = None
    detail: str | None = None
    checked_at: datetime | None = None
    consecutive: int = 0  # consecutive degraded evaluations in this incident
    alerted: bool = False  # an alert went out for the current incident
    alert_window: int | None = None  # dedupe window of that alert
    last_alert_at: datetime | None = None

    def public(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "since": None if self.since is None else iso(self.since),
            "detail": self.detail,
            "checked_at": None if self.checked_at is None else iso(self.checked_at),
        }

    def to_kv(self) -> dict[str, Any]:
        return {
            **self.public(),
            "consecutive": self.consecutive,
            "alerted": self.alerted,
            "alert_window": self.alert_window,
            "last_alert_at": None if self.last_alert_at is None else iso(self.last_alert_at),
        }

    @classmethod
    def from_kv(cls, raw: object) -> CheckState:
        if not isinstance(raw, dict):
            return cls()
        d = cast(dict[str, Any], raw)
        state = d.get("state")
        window = d.get("alert_window")
        return cls(
            state=state if state in ("ok", "degraded") else "unknown",
            since=_parse_dt(d.get("since")),
            detail=d["detail"] if isinstance(d.get("detail"), str) else None,
            checked_at=_parse_dt(d.get("checked_at")),
            consecutive=d["consecutive"] if isinstance(d.get("consecutive"), int) else 0,
            alerted=d.get("alerted") is True,
            alert_window=window if isinstance(window, int) else None,
            last_alert_at=_parse_dt(d.get("last_alert_at")),
        )


def _parse_dt(v: object) -> datetime | None:
    if not isinstance(v, str):
        return None
    try:
        return datetime.fromisoformat(v)
    except ValueError:
        return None


def _disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


# --- read side (API / status), usable without a running monitor ----------------------


def health_snapshot(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """``{check: {state, since, detail, checked_at}}`` from kv; ``unknown`` if never run."""
    return {c: CheckState.from_kv(kv_get(conn, HEALTH_KV_PREFIX + c)).public() for c in CHECKS}


def channels_snapshot(
    conn: sqlite3.Connection, *, imessage_enabled: bool, channel: MonitoredChannel | None
) -> list[dict[str, Any]]:
    """``GET /api/channels``: ``[{name, enabled, ok, last_read_at, last_selftest}]``.

    Without a live channel, ``ok`` and ``last_read_at`` are null; ``last_selftest`` comes
    from kv either way (``resonant selftest imessage`` also writes it).
    """
    raw: object = kv_get(conn, SELFTEST_KV)
    last_selftest: dict[str, Any] | None = None
    if isinstance(raw, dict):
        st = cast(dict[str, Any], raw)
        detail = st.get("detail")
        last_selftest = {
            "ok": st.get("ok") is True,
            "at": st.get("at") if isinstance(st.get("at"), str) else None,
            "detail": scrub(detail, 300) if isinstance(detail, str) else None,
        }
    ok: bool | None = None
    last_read_at: str | None = None
    if channel is not None:
        h = channel.health()
        ok = h.get("ok") is True
        read_at = h.get("last_read_at")
        last_read_at = read_at if isinstance(read_at, str) else None
    return [
        {
            "name": "imessage",
            "enabled": imessage_enabled or channel is not None,
            "ok": ok,
            "last_read_at": last_read_at,
            "last_selftest": last_selftest,
        }
    ]


# --- the monitor -------------------------------------------------------------------------


class HealthMonitor:
    """Evaluates the checks, persists their state, and alerts the owner. See module doc.

    Dependencies are injected: ``model`` (anything with ``probe()``), ``channel`` (the
    iMessage channel, or None: no alerts and no imessage check), ``owner`` (the principal
    alerted), ``daemon_healthy`` (``Daemon.healthy``), ``clock`` and ``sleep``.
    ``model_meta`` (e.g. ``{"model": ..., "api": ...}``) is merged into the cached probe.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        model: ModelProber | None,
        channel: MonitoredChannel | None,
        owner: str | None,
        daemon_healthy: Callable[[], bool],
        db_path: Path | None = None,
        model_meta: Mapping[str, str] | None = None,
        clock: Callable[[], datetime] = utcnow,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        disk_free: Callable[[Path], int] = _disk_free,
        interval_s: float = 60.0,
        model_every_s: float = 300.0,
        selftest_every_s: float = 6 * 3600.0,
        selftest_retry_s: float = 1800.0,
        selftest_after_errors: int = 2,
        selftest_timeout_s: float = 10.0,
        alert_after: int = 2,
        alert_cooldown_s: float = 6 * 3600.0,
        min_free_bytes: int = 2 * GB,
        max_dead_letters_1h: int = 0,
        max_failed_tasks_1h: int = 2,
    ) -> None:
        self.conn = conn
        self.model = model
        self.channel = channel
        self.owner = owner
        self.daemon_healthy = daemon_healthy
        self.db_path = db_path
        self.model_meta = dict(model_meta or {})
        self.clock = clock
        self.sleep = sleep
        self.disk_free = disk_free
        self.interval_s = interval_s
        self.model_every = timedelta(seconds=model_every_s)
        self.selftest_every = timedelta(seconds=selftest_every_s)
        self.selftest_retry = timedelta(seconds=selftest_retry_s)
        self.selftest_after_errors = selftest_after_errors
        self.selftest_timeout_s = selftest_timeout_s
        self.alert_after = alert_after
        self.alert_cooldown = timedelta(seconds=alert_cooldown_s)
        self.min_free_bytes = min_free_bytes
        self.max_dead_letters_1h = max_dead_letters_1h
        self.max_failed_tasks_1h = max_failed_tasks_1h
        self.states: dict[CheckName, CheckState] | None = None  # loaded on first evaluate
        self._model_result: CheckResult | None = None
        self._model_probed_at: datetime | None = None
        self._reader_errors = 0
        self._stop = asyncio.Event()

    # --- lifecycle -----------------------------------------------------------------------

    async def run(self) -> None:
        """Evaluate every ``interval_s`` until :meth:`stop`. One bad round never ends it."""
        while not self._stop.is_set():
            try:
                await self.evaluate()
            except Exception:
                log.exception("health monitor evaluation failed")
            await self.sleep(self.interval_s)

    def stop(self) -> None:
        self._stop.set()

    # --- one round -----------------------------------------------------------------------

    async def evaluate(self) -> dict[CheckName, CheckState]:
        """Run every check once, update state, persist it, then send alerts/recoveries."""
        states = self._load()
        now = self.clock()
        results: dict[CheckName, CheckResult | None] = {
            "model": await self._check_model(now),
            "imessage": await self._check_imessage(now),
            "loop": self._check_loop(now),
            "store": self._check_store(),
        }
        for name, result in results.items():
            if result is not None:
                self._transition(states[name], result, now)
        self._persist(states)
        await self._notify(states, now)
        return states

    def deadman_reason(self) -> str | None:
        """Reason for the dead-man ``/fail`` body, or None when the owner can be texted.

        Set while the ``imessage`` check is degraded (alerts can't go out), or when any
        check is degraded and there is no channel at all.
        """
        if self.states is None:
            return None
        degraded = [(n, s) for n, s in self.states.items() if s.state == "degraded"]
        if not degraded:
            return None
        im = self.states["imessage"]
        if self.channel is not None and self.owner is not None and im.state != "degraded":
            return None
        # The imessage reason first: it's why nobody was texted.
        degraded.sort(key=lambda ns: ns[0] != "imessage")
        return scrub("; ".join(f"{n}: {s.detail or 'degraded'}" for n, s in degraded), 300)

    # --- checks --------------------------------------------------------------------------

    async def _check_model(self, now: datetime) -> CheckResult | None:
        if self.model is None:
            return None
        due = self._model_probed_at is None or now - self._model_probed_at >= self.model_every
        if due or self._model_result is None:
            probe = await self.model.probe()  # never raises
            self._model_probed_at = now
            self._model_result = _model_result(probe)
            cached = {k: v for k, v in probe.as_dict().items() if k in _PROBE_FIELDS}
            cached["detail"] = scrub(probe.detail, 300)
            self._kv_write(MODEL_PROBE_KV, {**cached, **self.model_meta, "checked_at": iso(now)})
        return self._model_result

    async def _check_imessage(self, now: datetime) -> CheckResult | None:
        if self.channel is None:
            return None
        h = self.channel.health()
        err = h.get("last_error")
        if err is not None or h.get("fda_ok") is False:
            self._reader_errors += 1
        else:
            self._reader_errors = 0

        last = self._last_selftest()
        due = last is None
        if last is not None:
            ok, at = last[0], last[1]
            age = now - at if at is not None else self.selftest_every
            due = age >= (self.selftest_every if ok else self.selftest_retry)
        if self._reader_errors >= self.selftest_after_errors:
            due = True
        if due:
            try:
                await self.channel.self_test(timeout_s=self.selftest_timeout_s)
            except Exception as e:  # a self-test must never take the monitor down
                log.warning("imessage self-test raised %s", type(e).__name__)
                self._kv_write(
                    SELFTEST_KV,
                    {
                        "ok": False,
                        "at": iso(now),
                        "detail": f"self-test error ({type(e).__name__})",
                    },
                )
            self._reader_errors = 0
            last = self._last_selftest()

        if h.get("fda_ok") is False:
            return CheckResult(False, "imessage reader: Full Disk Access missing")
        if err is not None:
            return CheckResult(False, f"imessage reader error: {scrub(str(err), 80)}")
        if last is not None and not last[0]:
            return CheckResult(False, f"imessage selftest failed: {scrub(last[2], 90)}")
        return CheckResult(True, "ok")

    def _last_selftest(self) -> tuple[bool, datetime | None, str] | None:
        raw: object = kv_get(self.conn, SELFTEST_KV)
        if not isinstance(raw, dict):
            return None
        d = cast(dict[str, Any], raw)
        detail = d.get("detail")
        return (
            d.get("ok") is True,
            _parse_dt(d.get("at")),
            detail if isinstance(detail, str) else "",
        )

    def _check_loop(self, now: datetime) -> CheckResult:
        cutoff = iso(now - timedelta(hours=1))
        problems: list[str] = []
        if not self.daemon_healthy():
            problems.append("loop is not ticking")
        try:
            dead = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM audit_log WHERE action = ? AND at >= ?",
                    (DEAD_LETTER_ACTION, cutoff),
                ).fetchone()[0]
            )
            failed = int(
                self.conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status = 'failed' AND updated_at >= ?",
                    (cutoff,),
                ).fetchone()[0]
            )
        except sqlite3.Error as e:
            problems.append(f"store unreadable ({type(e).__name__})")
            dead = failed = 0
        if dead > self.max_dead_letters_1h:
            problems.append(f"{dead} events dead-lettered in the last hour")
        if failed > self.max_failed_tasks_1h:
            problems.append(f"{failed} tasks failed in the last hour")
        return CheckResult(not problems, "; ".join(problems) or "ok")

    def _check_store(self) -> CheckResult:
        problems: list[str] = []
        try:
            with atomic(self.conn):
                kv_set(self.conn, "health.store.write_check", iso(self.clock()))
        except sqlite3.Error as e:
            problems.append(f"database not writable ({type(e).__name__})")
        if self.db_path is not None:
            try:
                free = self.disk_free(self.db_path.parent)
            except OSError as e:
                problems.append(f"disk usage unreadable ({type(e).__name__})")
            else:
                if free < self.min_free_bytes:
                    problems.append(f"low disk: {free / GB:.1f}GB free")
        return CheckResult(not problems, "; ".join(problems) or "ok")

    # --- state machine -------------------------------------------------------------------

    def _transition(self, s: CheckState, r: CheckResult, now: datetime) -> None:
        s.checked_at = now
        if r.ok:
            if s.state != "ok":
                s.state, s.since = "ok", now
            s.detail = r.detail
            s.consecutive = 0
            return
        if s.state != "degraded":
            s.state, s.since, s.consecutive = "degraded", now, 0
            s.alerted, s.alert_window = False, None
        s.consecutive += 1
        s.detail = scrub(r.detail)

    def _can_text_owner(self) -> bool:
        assert self.states is not None
        return (
            self.channel is not None
            and self.owner is not None
            and self.states["imessage"].state != "degraded"
        )

    async def _notify(self, states: dict[CheckName, CheckState], now: datetime) -> None:
        if not self._can_text_owner():
            return
        changed = False
        for name, s in states.items():
            if s.state == "degraded" and not s.alerted and s.consecutive >= self.alert_after:
                cooled = s.last_alert_at is None or now - s.last_alert_at >= self.alert_cooldown
                if not cooled:
                    continue
                window = int(now.timestamp() // self.alert_cooldown.total_seconds())
                text = f"hey, some issues here: {name}: {s.detail}"
                if await self._send(name, text, f"health:{name}:{window}", "alert"):
                    s.alerted, s.alert_window, s.last_alert_at = True, window, now
                    changed = True
            elif s.state == "ok" and s.alerted:
                text = f"resolved: {name} is healthy again"
                key = f"health:{name}:{s.alert_window}:resolved"
                if await self._send(name, text, key, "resolved"):
                    s.alerted, s.alert_window = False, None
                    changed = True
        if changed:
            self._persist(states)

    async def _send(self, check: str, text: str, dedupe_key: str, kind: str) -> bool:
        assert self.channel is not None and self.owner is not None
        try:
            await self.channel.send(self.owner, scrub(text, 300), dedupe_key=dedupe_key)
        except ChannelError as e:
            log.warning("health %s for %s not sent: %s", kind, check, type(e).__name__)
            self._audit(f"health.{kind}", check=check, decision="failed", error=type(e).__name__)
            return False
        log.info("health %s sent for %s", kind, check)
        self._audit(f"health.{kind}", check=check, decision="sent", idempotency_key=dedupe_key)
        return True

    # --- persistence ---------------------------------------------------------------------

    def _load(self) -> dict[CheckName, CheckState]:
        if self.states is None:
            self.states = {
                c: CheckState.from_kv(kv_get(self.conn, HEALTH_KV_PREFIX + c)) for c in CHECKS
            }
        return self.states

    def _persist(self, states: dict[CheckName, CheckState]) -> None:
        try:
            with atomic(self.conn):
                for name, s in states.items():
                    kv_set(self.conn, HEALTH_KV_PREFIX + name, s.to_kv())
        except sqlite3.Error as e:
            log.warning("health state not persisted: %s", type(e).__name__)

    def _kv_write(self, key: str, value: object) -> None:
        try:
            with atomic(self.conn):
                kv_set(self.conn, key, value)
        except sqlite3.Error as e:
            log.warning("kv %s not written: %s", key, type(e).__name__)

    def _audit(self, action: str, *, check: str, decision: str, **detail: Any) -> None:
        idem = detail.pop("idempotency_key", None)
        try:
            with atomic(self.conn):
                audit(
                    self.conn,
                    action,
                    decision=decision,
                    idempotency_key=idem,
                    check=check,
                    **detail,
                )
        except sqlite3.Error as e:
            log.warning("health audit not written: %s", type(e).__name__)


def _model_result(p: ProbeResult) -> CheckResult:
    if p.ok:
        return CheckResult(True, "ok")
    if not p.model_present:
        return CheckResult(False, "model unreachable or not pulled")
    if not p.ctx_ok:
        return CheckResult(False, "model context too small")
    if not p.thinking_off_ok:
        return CheckResult(False, "model thinking is not off")
    return CheckResult(False, "model probe failed")
