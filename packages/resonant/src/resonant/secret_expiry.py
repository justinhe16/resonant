"""Secret expiry (PER-313): when each Keychain secret expires, and the daily watch.

**Metadata only.** A secret's value is never read for this. An expiry is a row in the store
(``store/secret_expiry.py``, migration 035) or, for a ref with a known default
(:data:`DEFAULTS`: ``runner/claude_token`` lasts 365 days), the Keychain item's ``mdat``
(modified) attribute plus that default, shown as ``assumed``. ``SecretStore.modified_at``
reads ``mdat`` from ``security find-generic-password`` without ``-w``; when it doesn't parse,
the expiry is unknown, never guessed. An expiry never goes in the Keychain value, argv, logs,
audit, kv or a message beyond the ref, the date and the renew command.

**Which refs.** :func:`known_refs`: the core refs every instance may hold (``runner/*``,
``relay/supabase_key``), the ones its config names (``notify_owner.token_ref``, personal's
``core/notify_token_<instance>`` for each of ``instances``, Slack's two tokens when
``channels.slack`` is on, every ``runner.session_env`` value), plus every ref with a row.
``resonant secret list`` shows them with the extensions' declared refs.

**The daily job** ``core/secrets`` (10:00 in ``settings.timezone``, 20 h catch-up, not while
paused): for each present ref with an expiry, the days left (local dates) pick the tightest
threshold reached of 30, 7, 1 and 0 (expired). Each ``(ref, expires_at, threshold)`` is told
once (kv ``secrets.alerted:<ref>``; a new ``expires_at``, a renewal, starts fresh), so a
missed 30-day run still sends the 7-day one and never three at once. Where it goes: an
instance with ``notify_owner`` forwards it to personal Resonant (``OwnerBridge``, key
``secrets/<ref>``), posts it to ``pulse.heartbeat.room`` when set and DMs the owner on its own
channel; personal Resonant texts the owner (dedupe ``expiry:<ref>:<date>:<threshold>``).
Every command in the text carries ``--instance <name>`` on a non-default instance. Audit
``secrets.expiry_alert`` (ref, threshold, decision; never the text). A locked Keychain skips
the presence checks: the job alerts on rows only and logs it once. Nothing known: the run is
quiet (records nothing).

**The health row** ``secrets`` (:class:`SecretsHealth`): ``ok`` (``soonest <ref> in Nd``, or
``no expiries known``), ``degraded`` when a present ref expires within 7 days, ``failing``
once one has expired. The monitor never alerts, bridges or dead-mans on it: the job owns the
alerting. Keychain presence and ``mdat`` are re-read at most hourly; the rows every time.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from resonant import instance as inst
from resonant.gateway.channel import ChannelError
from resonant.ops.relay_deploy import FLY_DEPLOY_TOKEN_REF
from resonant.secrets import (
    ANTHROPIC_KEY_REF,
    CLAUDE_TOKEN_REF,
    GITHUB_APP_KEY_REF,
    GITHUB_PUBLIC_TOKEN_REF,
    GITHUB_TOKEN_REF,
    InvalidSecretRef,
    SecretStore,
    SecretUnavailable,
    secret_ref,
    validate_ref,
)
from resonant.store import secret_expiry as rows_store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow
from resonant.store.secret_expiry import ExpiryRow, Source

if TYPE_CHECKING:
    from resonant.config import Settings
    from resonant.executor import CallContext
    from resonant.extensions.scheduler import CoreDeps, CoreJob
    from resonant.gateway.channel import Channel
    from resonant.gateway.hub import RoomPosting
    from resonant.monitor import CheckResult
    from resonant.notify_owner import OwnerBridge

log = logging.getLogger(__name__)

JOB_ID = "secrets"
SCHEDULE = "0 10 * * *"
CATCHUP = timedelta(hours=20)
TIMEOUT_S = 120.0
THRESHOLDS = (30, 7, 1, 0)  # days left; 0 is "expired"
DEGRADED_DAYS = 7
ALERTED_KV = "secrets.alerted:"  # + ref: {"expires_at", "sent": [thresholds]}
AUDIT_ACTION = "secrets.expiry_alert"
TEXT_MAX = 300
BRIDGE_KEY = "secrets/"  # + ref
HEALTH_REFRESH = timedelta(hours=1)
MAX_DAYS = 3650  # --expires-in at most about ten years

RELAY_KEY_REF = secret_ref("relay", "supabase_key")  # dashboard.pusher.SUPABASE_KEY_REF

# Known defaults: the lifetime assumed from the Keychain item's ``mdat`` when no row says
# otherwise. Keep it small, and say "assumed" wherever it shows.
DEFAULTS: dict[str, timedelta] = {CLAUDE_TOKEN_REF: timedelta(days=365)}
# What to do before ``resonant secret set`` for a ref (code-built, no value).
RENEW_HINTS: dict[str, str] = {
    CLAUDE_TOKEN_REF: "claude setup-token",
    FLY_DEPLOY_TOKEN_REF: "fly tokens create deploy -a <relay.fly_app> --expiry 8760h",
}

CORE_REFS: tuple[str, ...] = (
    CLAUDE_TOKEN_REF,
    ANTHROPIC_KEY_REF,
    GITHUB_TOKEN_REF,
    GITHUB_APP_KEY_REF,
    GITHUB_PUBLIC_TOKEN_REF,
    RELAY_KEY_REF,
)

_EXPIRES_IN = re.compile(r"(\d{1,5})d")


# --- refs and parsing ------------------------------------------------------------------------


def known_refs(settings: Settings) -> list[str]:
    """The refs this instance may hold besides the extensions' declared ones, sorted. Names
    only: nothing is read."""
    from resonant.notify_owner import token_ref_for

    refs: set[str] = set(CORE_REFS)
    if settings.notify_owner is not None:
        refs.add(settings.notify_owner.token_ref)
    for other in settings.instances:
        if (ref := token_ref_for(other.name)) is not None:
            refs.add(ref)
    slack = settings.channels.slack
    if slack.enabled:
        refs.update((slack.bot_token_ref, slack.app_token_ref))
    refs.update(settings.runner.session_env.values())
    if settings.relay.fly_app is not None and inst.is_default(settings.instance.name):
        refs.add(FLY_DEPLOY_TOKEN_REF)  # PER-316: `resonant relay deploy`, personal only
    valid: list[str] = []
    for ref in sorted(refs):
        try:
            valid.append(validate_ref(ref))
        except InvalidSecretRef:
            continue
    return valid


def resonant_cmd(settings: Settings) -> str:
    """``resonant``, or ``resonant --instance <name>`` on a non-default instance."""
    name = settings.instance.name
    return "resonant" if inst.is_default(name) else f"resonant --instance {name}"


def parse_expires(raw: str, *, tz: str, now: datetime) -> datetime:
    """``--expires YYYY-MM-DD``: the start of that day in ``tz``, as UTC. A date before today
    is refused (ValueError with the reason)."""
    try:
        day = date.fromisoformat(raw.strip())
    except ValueError:
        raise ValueError(f"--expires {raw!r}: want a date like 2027-11-23") from None
    zone = ZoneInfo(tz)
    if day < now.astimezone(zone).date():
        raise ValueError(f"--expires {day.isoformat()} is in the past")
    return datetime.combine(day, time(), zone).astimezone(ZoneInfo("UTC"))


def parse_expires_in(raw: str, *, now: datetime) -> datetime:
    """``--expires-in <N>d`` (N whole days, 1..3650): ``now`` plus N days."""
    m = _EXPIRES_IN.fullmatch(raw.strip())
    days = int(m[1]) if m is not None else 0
    if m is None or not 1 <= days <= MAX_DAYS:
        raise ValueError(f"--expires-in {raw!r}: want whole days from 1d to {MAX_DAYS}d, like 365d")
    return now + timedelta(days=days)


def days_left(expires_at: datetime, now: datetime, tz: str) -> int:
    """Calendar days from today to the expiry date, both in ``tz`` (negative once past)."""
    zone = ZoneInfo(tz)
    return (expires_at.astimezone(zone).date() - now.astimezone(zone).date()).days


def threshold(days: int, expires_at: datetime, now: datetime) -> int | None:
    """The tightest threshold reached: 0 once expired (or expiring today), else 1, 7 or 30;
    None while more than 30 days are left."""
    if days <= 0 or expires_at <= now:
        return 0
    for t in (1, 7, 30):
        if days <= t:
            return t
    return None


# --- what is known -------------------------------------------------------------------------


@dataclass(frozen=True)
class Entry:
    """One ref: whether the Keychain has it (None: couldn't tell) and its expiry, if known."""

    ref: str
    present: bool | None
    expires_at: datetime | None = None
    source: Source | None = None
    set_at: datetime | None = None

    @property
    def watched(self) -> bool:
        """Has an expiry and isn't known to be missing (a locked Keychain: rows still count)."""
        return self.expires_at is not None and self.present is not False


@dataclass(frozen=True)
class Probe:
    """Keychain facts by ref, names and ``mdat`` only. ``locked``: the Keychain couldn't
    answer, so the rest were not asked."""

    present: dict[str, bool | None]
    modified: dict[str, datetime | None]
    locked: bool = False
    reason: str | None = None  # why it was locked (``SecretError.reason``: no value, no output)


async def probe(store: SecretStore, refs: Iterable[str]) -> Probe:
    """Presence for every ref and ``mdat`` for the ones with a default. Never ``-w``. After
    one ``SecretUnavailable`` (locked, timed out) nothing more is asked."""
    present: dict[str, bool | None] = {}
    modified: dict[str, datetime | None] = {}
    locked = False
    reason: str | None = None
    for ref in refs:
        if locked:
            present[ref] = None
            continue
        try:
            present[ref] = await store.exists(ref)
            if present[ref] and ref in DEFAULTS:
                modified[ref] = await store.modified_at(ref)
        except SecretUnavailable as e:
            locked, reason = True, e.reason
            present[ref] = None
    return Probe(present, modified, locked, reason)


def entries(refs: Iterable[str], rows: Iterable[ExpiryRow], facts: Probe) -> list[Entry]:
    """Every ref (the given ones and every row's), by ref: the row wins, then a default
    counted from ``mdat``, else no expiry."""
    by_ref = {r.ref: r for r in rows}
    out: list[Entry] = []
    for ref in sorted({*refs, *by_ref}):
        present = facts.present.get(ref)
        row = by_ref.get(ref)
        if row is not None:
            out.append(Entry(ref, present, row.expires_at, row.source, row.set_at))
        elif ref in DEFAULTS and (mdat := facts.modified.get(ref)) is not None:
            out.append(Entry(ref, present, mdat + DEFAULTS[ref], "assumed", mdat))
        else:
            out.append(Entry(ref, present))
    return out


async def survey(
    conn: sqlite3.Connection, store: SecretStore, refs: Iterable[str]
) -> tuple[list[Entry], bool]:
    """``(entries, locked)`` for ``refs`` plus every row's ref."""
    rows = rows_store.all(conn)
    wanted = sorted({*refs, *(r.ref for r in rows)})
    facts = await probe(store, wanted)
    return entries(wanted, rows, facts), facts.locked


def describe(e: Entry, now: datetime, tz: str) -> str:
    """The ``secret list`` expires column: ``2027-11-23 (365d, assumed)``, ``expired 2d
    ago``, ``expires today``, ``unknown``; ``(expiry kept)`` for a row whose item is gone."""
    if e.expires_at is None:
        return "unknown" if e.present is not False else ""
    if e.present is False:
        return "(expiry kept)"
    days = days_left(e.expires_at, now, tz)
    if e.expires_at <= now:
        return "expired today" if days == 0 else f"expired {-days}d ago"
    if days == 0:
        return f"expires today ({e.source})"
    local = e.expires_at.astimezone(ZoneInfo(tz)).date().isoformat()
    return f"{local} ({days}d, {e.source})"


def renew_command(e: Entry, settings: Settings) -> str:
    """The exact renew command: the ref's hint (``claude setup-token``), then ``resonant
    secret set <ref>`` with the expiry flag it should get again."""
    cmd = f"{resonant_cmd(settings)} secret set {e.ref}"
    if e.ref in DEFAULTS:
        cmd += f" --expires-in {DEFAULTS[e.ref].days}d"
    elif e.expires_at is not None and e.set_at is not None:
        span = (e.expires_at - e.set_at).days
        cmd += f" --expires-in {span}d" if span >= 1 else " --expires YYYY-MM-DD"
    hint = RENEW_HINTS.get(e.ref)
    return f"{hint}, then {cmd}" if hint else cmd


def _when(e: Entry, days: int, now: datetime) -> str:
    assert e.expires_at is not None
    if e.expires_at <= now:
        return (
            "expired today" if days >= 0 else f"expired {-days} day{'s' if days != -1 else ''} ago"
        )
    if days <= 0:
        return "expires today"
    return f"expires in {days} day{'s' if days != 1 else ''}"


def alert_text(e: Entry, settings: Settings, now: datetime) -> str:
    """``🔑 runner/claude_token expires in 7 days (2027-11-23, assumed). Renew: …``, at most
    ``TEXT_MAX`` characters. Code-built: the ref, the date, the source and the command."""
    assert e.expires_at is not None
    tz = settings.timezone
    days = days_left(e.expires_at, now, tz)
    local = e.expires_at.astimezone(ZoneInfo(tz)).date().isoformat()
    text = (
        f"🔑 {e.ref} {_when(e, days, now)} ({local}, {e.source}). "
        f"Renew: {renew_command(e, settings)}"
    )
    return text if len(text) <= TEXT_MAX else text[: TEXT_MAX - 1] + "…"


def bridge_text(e: Entry, settings: Settings, now: datetime) -> str:
    """The short form for personal Resonant (title cap 200): the ref, days and the command."""
    assert e.expires_at is not None
    days = days_left(e.expires_at, now, settings.timezone)
    return f"{e.ref} {_when(e, days, now)}. Renew: {renew_command(e, settings)}"


# --- the daily job ---------------------------------------------------------------------------


def _alerted(conn: sqlite3.Connection, ref: str, expires_at: datetime) -> set[int]:
    raw: object = kv_get(conn, ALERTED_KV + ref)
    if not isinstance(raw, dict):
        return set()
    d = cast(dict[str, Any], raw)
    if d.get("expires_at") != iso(expires_at):
        return set()  # renewed: a new expiry starts fresh
    sent: object = d.get("sent")
    if not isinstance(sent, list):
        return set()
    return {t for t in cast(list[object], sent) if isinstance(t, int)}


def dedupe_key(ref: str, expires_at: datetime, tz: str, t: int) -> str:
    local = expires_at.astimezone(ZoneInfo(tz)).date().isoformat()
    return f"expiry:{ref}:{local}:{t}"


@dataclass
class Delivery:
    """Where an alert goes. ``bridge``: personal Resonant (an instance with
    ``notify_owner``); ``rooms``/``room``: the pulse room; ``channel``/``owner``: a DM."""

    channel: Channel | None
    owner: str | None
    bridge: OwnerBridge | None = None
    rooms: RoomPosting | None = None
    room: str | None = None

    @property
    def any(self) -> bool:
        return self.bridge is not None or self.room is not None or self.owner is not None

    async def send(self, e: Entry, key: str, settings: Settings, now: datetime) -> bool:
        """True when at least one path took it. Never raises for a channel error."""
        text = alert_text(e, settings, now)
        took = False
        # The bridge never fails here (it queues and retries on its own), so with a bridge the
        # alert counts as told even when the room post and the DM failed.
        if self.bridge is not None:
            self.bridge.forward(BRIDGE_KEY + e.ref, bridge_text(e, settings, now))
            took = True
        if self.rooms is not None and self.room is not None:
            try:
                await self.rooms.post_room(self.room, text, dedupe_key=f"{key}:room")
                took = True
            except ChannelError as err:
                log.warning("secret expiry: room post failed (%s)", type(err).__name__)
        if self.channel is not None and self.owner is not None:
            try:
                await self.channel.send(self.owner, text, dedupe_key=key)
                took = True
            except ChannelError as err:
                log.warning("secret expiry: owner DM failed (%s)", type(err).__name__)
        return took


async def watch(
    conn: sqlite3.Connection,
    store: SecretStore,
    settings: Settings,
    delivery: Delivery,
    now: datetime,
) -> dict[str, Any]:
    """One run of ``core/secrets``. ``{"quiet": True}`` when nothing is watched; else
    ``{"alerts": [{ref, threshold, decision}]}`` (names only, never the text)."""
    found, locked = await survey(conn, store, known_refs(settings))
    if locked:
        log.warning("secret expiry: keychain unavailable; alerting on stored expiries only")
    tz = settings.timezone
    alerts: list[dict[str, Any]] = []
    for e in found:
        if not e.watched:
            continue
        assert e.expires_at is not None
        t = threshold(days_left(e.expires_at, now, tz), e.expires_at, now)
        if t is None:
            continue
        sent = _alerted(conn, e.ref, e.expires_at)
        if any(s <= t for s in sent):
            continue  # this threshold (or a tighter one) was already told
        key = dedupe_key(e.ref, e.expires_at, tz, t)
        took = await delivery.send(e, key, settings, now)
        decision = "sent" if took else "failed"
        with atomic(conn):
            if took:
                kv_set(conn, ALERTED_KV + e.ref,
                       {"expires_at": iso(e.expires_at), "sent": sorted({*sent, t})})  # fmt: skip
            audit(conn, AUDIT_ACTION, decision=decision, idempotency_key=key, ref=e.ref,
                  threshold=t)  # fmt: skip
        alerts.append({"ref": e.ref, "threshold": t, "decision": decision})
    if not alerts:
        return {"quiet": True}
    return {"alerts": alerts}


def secrets_job(
    deps: CoreDeps,
    *,
    clock: Callable[[], datetime] = utcnow,
    store: SecretStore | None = None,
) -> CoreJob | None:
    """The ``CORE_JOBS`` factory. None when no alert could go anywhere (no owner to DM, no
    bridge, no room)."""
    from resonant.extensions.scheduler import CoreJob
    from resonant.gateway.hub import RoomPosting

    settings, conn = deps.settings, deps.conn
    room = settings.pulse.heartbeat.room if deps.bridge is not None else None
    rooms = deps.channel if isinstance(deps.channel, RoomPosting) else None
    delivery = Delivery(
        channel=deps.channel,
        owner=deps.owner if deps.channel is not None else None,
        bridge=deps.bridge,
        rooms=rooms,
        room=room if rooms is not None else None,
    )
    if not delivery.any:
        return None
    keychain = (
        store or deps.components.executor.secrets or SecretStore(service=settings.keychain_service)
    )

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        del args, ctx
        return await watch(conn, keychain, settings, delivery, clock())

    return CoreJob(
        id=JOB_ID, schedule=SCHEDULE, run=run, catchup=CATCHUP, timeout_s=TIMEOUT_S,
        quiet_idle=True,
    )  # fmt: skip


# --- the health row --------------------------------------------------------------------------


class SecretsHealth:
    """The monitor's ``secrets`` check (see the module doc). The rows are read every
    evaluation; Keychain presence and ``mdat`` at most every ``refresh``."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        store: SecretStore,
        settings: Settings,
        *,
        refresh: timedelta = HEALTH_REFRESH,
    ) -> None:
        self.conn = conn
        self.store = store
        self.settings = settings
        self.refresh = refresh
        self._facts: Probe | None = None
        self._probed_at: datetime | None = None
        self._probed_refs: frozenset[str] = frozenset()

    async def __call__(self, now: datetime) -> CheckResult:
        rows = rows_store.all(self.conn)
        refs = frozenset({*known_refs(self.settings), *(r.ref for r in rows)})
        stale = (
            self._facts is None
            or self._probed_at is None
            or now - self._probed_at >= self.refresh
            or not refs <= self._probed_refs
        )
        if stale:
            self._facts = await probe(self.store, sorted(refs))
            self._probed_at, self._probed_refs = now, refs
        assert self._facts is not None
        return health_result(entries(refs, rows, self._facts), now, self.settings.timezone)


def health_result(found: Iterable[Entry], now: datetime, tz: str) -> CheckResult:
    """``failing`` once a watched ref has expired, ``degraded`` within 7 days, else ``ok``
    with the soonest one (or ``no expiries known``)."""
    from resonant.monitor import CheckResult

    watched = sorted((e for e in found if e.watched), key=lambda e: cast(datetime, e.expires_at))
    if not watched:
        return CheckResult(True, "no expiries known")
    soonest = watched[0]
    assert soonest.expires_at is not None
    days = days_left(soonest.expires_at, now, tz)
    # failing only once the time has passed; "expires today" is degraded (the alert says it)
    expired = [e for e in watched if cast(datetime, e.expires_at) <= now]
    if expired:
        names = ", ".join(e.ref for e in expired[:3])
        more = f" +{len(expired) - 3} more" if len(expired) > 3 else ""
        return CheckResult(False, f"expired: {names}{more}", failing=True)
    if days <= DEGRADED_DAYS:
        return CheckResult(False, f"{soonest.ref} expires in {days}d")
    return CheckResult(True, f"soonest {soonest.ref} in {days}d")


__all__ = [
    "CATCHUP",
    "DEFAULTS",
    "JOB_ID",
    "SCHEDULE",
    "THRESHOLDS",
    "Delivery",
    "Entry",
    "Probe",
    "SecretsHealth",
    "alert_text",
    "bridge_text",
    "days_left",
    "dedupe_key",
    "describe",
    "entries",
    "health_result",
    "known_refs",
    "parse_expires",
    "parse_expires_in",
    "probe",
    "renew_command",
    "resonant_cmd",
    "secrets_job",
    "survey",
    "threshold",
    "watch",
]
