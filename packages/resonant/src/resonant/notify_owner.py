"""The notify-owner bridge (PER-301): another instance's urgent alerts reach the owner's
iMessage through personal Resonant.

Cori (any instance with ``notify_owner:`` in its config) runs on the same macOS user as
personal Resonant but has no iMessage. Rather than script Messages herself, she hands every
urgent item to personal Resonant's loopback API, which texts it as its own: one sender, one
outbound table, one per-hour cap, one ``/kill`` and ``/ext mute``, and the text lands in
personal Resonant's transcript (``system_texts``, ``outside``) so its brain can answer "what's
that?".

**Personal side** (``api.py``, ``POST /api/notify-owner``). ``Authorization: Bearer <token>``
must match ``core/notify_token_<instance>`` in personal Resonant's Keychain (same user, so
this guards against mistakes, not attackers; docs/limits.md). The body is exactly
:class:`NotifyOwnerBody` (``{instance, key, title, urgency: "urgent", clear}``, at most
``MAX_BODY`` bytes); anything else is 422 and stores nothing. A missing or wrong token is
401, an instance without a token on personal's side (or a daemon with no channel) 404. A
valid call goes to ``extensions.pulse.Pulse.ingest_instance``: an item of source
``instance:<name>``, texted ``[Cori] 🔴 <title>`` once per fingerprint (``✓`` for ``clear``)
under the usual pause, mute and 20-an-hour cap.

**Instance side** (:class:`OwnerBridge`). ``forward(key, title, clear=)`` puts the alert in
a small outbox (kv ``notify_owner.outbox``, one entry per key, the latest wins, at most
``MAX_OUTBOX``, dropped after ``KEEP``) and wakes :meth:`OwnerBridge.run`, which POSTs each
due entry with the token from this instance's Keychain (``notify_owner.token_ref``). A 2xx
removes it; a 422 drops it (a bug: retrying can't help); anything else backs it off
(``BACKOFF_MIN_S`` doubling to ``BACKOFF_MAX_S``) and retries. Callers: the pulse's urgent
path (extension reports), the health monitor's alerts and recoveries, and a run's urgent
failure. The item stays in this instance's own pulse whatever happens.

While the bridge fails, the owner gets **one** DM on this instance's own channel (Slack for
Cori) per problem (kv ``notify_owner.down``): personal Resonant unreachable, ``notify token
mismatch`` (401: the token was rotated on one side only), not set up on personal's side
(404), or no token here. A delivery clears it, so the next outage says so again.

``resonant notify-owner test`` (owner-run) sends a fixed title through the same POST.

Titles are message bodies: they are never logged at INFO.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal, cast

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError

from resonant.config import NotifyOwnerConfig
from resonant.extensions.notify import clean_text
from resonant.gateway.channel import ChannelError, ChannelRefused
from resonant.instance import NAME_RE
from resonant.ops import Sender
from resonant.secrets import InvalidSecretRef, SecretError, SecretMissing, SecretStore, secret_ref
from resonant.store import pulse as items
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, utcnow

log = logging.getLogger(__name__)

PATH = "/api/notify-owner"
MAX_BODY = 2048  # bytes; a full body is about 400
TITLE_MAX = 200
KEY_MAX = items.KEY_MAX  # 120, the pulse store's key rule
TOKEN_NAME = "notify_token"  # noqa: S105 - a Keychain item name, not a value
OUTBOX_KV = "notify_owner.outbox"
DOWN_KV = "notify_owner.down"
MAX_OUTBOX = 50
KEEP = timedelta(hours=24)
BACKOFF_MIN_S = 5.0
BACKOFF_MAX_S = 300.0
IDLE_S = 60.0
TEST_KEY = "notify-owner/test"
TEST_TITLE = "Test alert: the notify-owner bridge works"

Problem = Literal["unreachable", "mismatch", "not_set_up", "no_token"]
_BAD_KEY_CHARS = re.compile(r"[^a-z0-9/._:-]+")


# --- the contract ---------------------------------------------------------------------------


class NotifyOwnerBody(BaseModel):
    """The one body ``POST /api/notify-owner`` takes. No other fields."""

    model_config = ConfigDict(extra="forbid", strict=True)

    instance: str = Field(pattern=f"^{NAME_RE.pattern}$", max_length=32)
    key: str = Field(min_length=1, max_length=KEY_MAX)
    title: str = Field(min_length=1, max_length=TITLE_MAX)
    urgency: Literal["urgent"]
    clear: StrictBool = False


def parse_body(raw: bytes) -> NotifyOwnerBody | str:
    """The body, or why it is refused (422): too big, not JSON, or not exactly the schema."""
    if len(raw) > MAX_BODY:
        return f"body over {MAX_BODY} bytes"
    try:
        body = NotifyOwnerBody.model_validate_json(raw)
    except ValidationError as e:
        errors = e.errors()
        if not errors:
            return "body: invalid"
        where = ".".join(str(p) for p in errors[0]["loc"]) or "body"
        return f"{where}: {errors[0]['msg']}"[:200]
    if not items.valid_key(body.key):
        return "key: want 1..120 characters of [a-z0-9/._:-]"
    if not clean_text(body.title):  # what the text would say: nothing printable is blank
        return "title: must not be blank"
    return body


def token_ref_for(instance: str) -> str | None:
    """Personal Resonant's Keychain ref for ``instance``'s token
    (``core/notify_token_cori``), or None when the name can't make a ref. ``-`` becomes
    ``_`` (a ref name has no ``-``); instance names have no ``_``, so two can't collide."""
    try:
        return secret_ref("core", f"{TOKEN_NAME}_{instance.replace('-', '_')}")
    except InvalidSecretRef:
        return None


def bearer(header: str | None) -> str | None:
    """The token of an ``Authorization: Bearer <token>`` header, else None."""
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    return token if scheme.lower() == "bearer" and token else None


def owner_key(raw: str) -> str:
    """``raw`` made a valid pulse key: lowercased, other characters as ``-``, and a long one
    clipped with a short hash so two long keys stay apart."""
    key = _BAD_KEY_CHARS.sub("-", raw.lower()).strip("-") or "alert"
    if len(key) <= KEY_MAX:
        return key
    digest = hashlib.sha256(raw.encode()).hexdigest()[:16]
    return f"{key[: KEY_MAX - 17]}-{digest}"


# --- one POST -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Delivery:
    """One POST's result: ``status`` None when personal Resonant couldn't be reached."""

    status: int | None
    outcome: str | None = None  # personal's answer: sent, held, told or unreachable
    reason: str | None = None  # held: why; else the error, for the log

    @property
    def ok(self) -> bool:
        return self.status is not None and 200 <= self.status < 300

    @property
    def problem(self) -> Problem | None:
        if self.ok or self.status == 422:
            return None
        if self.status == 401:
            return "mismatch"
        if self.status == 404:
            return "not_set_up"
        return "unreachable"


async def post_alert(
    cfg: NotifyOwnerConfig,
    token: str,
    body: NotifyOwnerBody,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Delivery:
    """POST one alert to personal Resonant. Never raises for a network error."""
    try:
        async with httpx.AsyncClient(
            base_url=cfg.url, timeout=cfg.timeout_s, transport=transport
        ) as client:
            r = await client.post(
                PATH,
                content=body.model_dump_json().encode(),
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
    except httpx.HTTPError as e:
        return Delivery(None, reason=type(e).__name__)
    data: Any = None
    with contextlib.suppress(ValueError):
        data = r.json()
    answer = cast(dict[str, Any], data) if isinstance(data, dict) else {}
    outcome, reason = answer.get("outcome"), answer.get("reason")
    return Delivery(
        r.status_code,
        outcome if isinstance(outcome, str) else None,
        reason if isinstance(reason, str) else None,
    )


async def send_test(
    cfg: NotifyOwnerConfig,
    instance: str,
    secrets: SecretStore,
    *,
    clear: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Delivery:
    """``resonant notify-owner test``: the fixed test alert (or its all-clear)."""
    token = await secrets.get(cfg.token_ref)
    body = NotifyOwnerBody(
        instance=instance, key=TEST_KEY, title=TEST_TITLE, urgency="urgent", clear=clear
    )
    return await post_alert(cfg, token, body, transport=transport)


# --- the instance side ----------------------------------------------------------------------

_TELL: dict[Problem, str] = {
    "unreachable": (
        "I can't reach Resonant to text you an urgent alert, so it's only here for now. "
        "I'll keep trying."
    ),
    "mismatch": (
        "notify token mismatch: Resonant refused my urgent alert. The token here "
        "({ref}) and Resonant's core/notify_token_{name} need the same value."
    ),
    "not_set_up": (
        "Resonant isn't set up to text you my urgent alerts (no core/notify_token_{name} "
        "there). They're only here for now."
    ),
    "no_token": (
        "I can't text you urgent alerts through Resonant: {ref} isn't set here. "
        "They're only here for now."
    ),
}


class OwnerBridge:
    """The instance side (see the module docstring). ``channel``/``owner``: where the one
    outage DM goes (this instance's hub and owner); None: no DM. ``transport`` replaces
    the HTTP transport (tests: an ``httpx.ASGITransport`` over personal Resonant's app)."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: NotifyOwnerConfig,
        *,
        instance: str,
        secrets: SecretStore,
        channel: Sender | None = None,
        owner: str | None = None,
        clock: Callable[[], datetime] = utcnow,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.conn = conn
        self.cfg = cfg
        self.instance = instance
        self.secrets = secrets
        self.channel = channel
        self.owner = owner
        self.clock = clock
        self.transport = transport
        self._wake = asyncio.Event()
        self._stop = asyncio.Event()

    # -- queueing --

    def forward(self, key: str, title: str, *, clear: bool = False) -> None:
        """Queue one urgent alert for personal Resonant (the latest per key wins). Never
        raises: the alert is still on this instance's own pulse."""
        key = owner_key(key)
        title = " ".join(title.split())[:TITLE_MAX]
        if not clean_text(title):  # personal Resonant would refuse it as blank
            title = "urgent alert"
        now = self.clock()
        try:
            with atomic(self.conn):
                box = self._outbox()
                box.pop(key, None)
                box[key] = {"title": title, "clear": clear, "seq": uuid.uuid4().hex,
                            "queued_at": iso(now), "next_at": iso(now), "attempts": 0}  # fmt: skip
                while len(box) > MAX_OUTBOX:
                    oldest = min(box, key=lambda k: str(box[k]["queued_at"]))
                    del box[oldest]
                    log.warning("notify-owner outbox full: dropped the oldest alert")
                kv_set(self.conn, OUTBOX_KV, box)
        except sqlite3.Error as e:
            log.warning("notify-owner alert not queued: %s", type(e).__name__)
            return
        self._wake.set()

    def pending(self) -> dict[str, dict[str, Any]]:
        """The outbox (key -> entry), for tests and status."""
        return self._outbox()

    def _outbox(self) -> dict[str, dict[str, Any]]:
        raw: object = kv_get(self.conn, OUTBOX_KV)
        if not isinstance(raw, dict):
            return {}
        box = cast(dict[str, Any], raw)
        return {k: cast(dict[str, Any], v) for k, v in box.items() if isinstance(v, dict)}

    # -- delivery --

    async def flush(self) -> int:
        """Try every due alert once (oldest first); the number personal Resonant took."""
        now = self.clock()
        box = self._outbox()
        expired = [k for k, e in box.items() if _when(e.get("queued_at"), now) < now - KEEP]
        due = sorted(
            (k for k, e in box.items() if k not in expired and _when(e.get("next_at"), now) <= now),
            key=lambda k: str(box[k].get("queued_at")),
        )
        done = {k: str(box[k].get("seq")) for k in expired}  # key -> seq of the entry read
        backoff: dict[str, str] = {}
        delivered = 0
        problem: Problem | None = None
        if due:
            try:
                token: str | None = await self.secrets.get(self.cfg.token_ref)
            except SecretMissing:
                token, problem = None, "no_token"
            except SecretError as e:
                log.warning("notify-owner token unreadable: %s", e)
                token = None
            if token is None:
                backoff = {k: str(box[k].get("seq")) for k in due}
            else:
                for i, key in enumerate(due):
                    entry = box[key]
                    body = NotifyOwnerBody(
                        instance=self.instance,
                        key=key,
                        title=str(entry.get("title") or "urgent alert"),
                        urgency="urgent",
                        clear=entry.get("clear") is True,
                    )
                    d = await post_alert(self.cfg, token, body, transport=self.transport)
                    if d.ok or d.status == 422:
                        if d.status == 422:
                            log.warning("notify-owner alert refused as invalid: %s", d.reason)
                        else:
                            delivered += 1
                        done[key] = str(entry.get("seq"))
                        self._audit(key, d)
                        continue
                    problem = d.problem
                    log.warning("notify-owner alert not delivered (%s, %s)", d.status, d.reason)
                    backoff = {k: str(box[k].get("seq")) for k in due[i:]}
                    break
        if expired:
            log.warning("notify-owner: %d alert(s) expired undelivered", len(expired))
        self._settle(done, backoff, now)
        if problem is not None:
            await self._tell(problem, now)
        elif delivered:
            self._recovered()
        return delivered

    def _settle(self, done: dict[str, str], backoff: dict[str, str], now: datetime) -> None:
        """Apply a flush to the outbox as it is now: an entry ``forward`` replaced meanwhile
        (another seq) is left alone."""
        if not done and not backoff:
            return
        try:
            with atomic(self.conn):
                box = self._outbox()
                for key, seq in done.items():
                    if key in box and box[key].get("seq") == seq:
                        del box[key]
                for key, seq in backoff.items():
                    entry = box.get(key)
                    if entry is None or entry.get("seq") != seq:
                        continue
                    attempts = int(entry.get("attempts") or 0) + 1
                    wait = min(BACKOFF_MAX_S, BACKOFF_MIN_S * 2 ** (attempts - 1))
                    entry["attempts"] = attempts
                    entry["next_at"] = iso(now + timedelta(seconds=wait))
                kv_set(self.conn, OUTBOX_KV, box)
        except sqlite3.Error as e:
            log.warning("notify-owner outbox not updated: %s", type(e).__name__)

    async def _tell(self, problem: Problem, now: datetime) -> None:
        """One DM to the owner per problem until a delivery clears it."""
        raw: object = kv_get(self.conn, DOWN_KV)
        down = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        if down.get("problem") == problem and down.get("told") is True:
            return
        since = down.get("since") if down.get("problem") == problem else iso(now)
        told = self.channel is None or self.owner is None
        if not told:
            assert self.channel is not None and self.owner is not None
            text = _TELL[problem].format(ref=self.cfg.token_ref, name=self.instance)
            try:
                await self.channel.send(
                    self.owner, text, dedupe_key=f"notify-owner:{problem}:{since}"
                )
                told = True
            except ChannelRefused:
                told = True  # no identity to reach: nothing more to try
            except ChannelError as e:
                log.warning("notify-owner outage DM not sent: %s", type(e).__name__)
        self._kv(DOWN_KV, {"problem": problem, "since": since, "told": told})

    def _recovered(self) -> None:
        if kv_get(self.conn, DOWN_KV) is not None:
            log.info("notify-owner: personal Resonant reachable again")
            self._kv(DOWN_KV, None)

    def _kv(self, key: str, value: object) -> None:
        try:
            with atomic(self.conn):
                if value is None:
                    self.conn.execute("DELETE FROM kv WHERE key = ?", (key,))
                else:
                    kv_set(self.conn, key, value)
        except sqlite3.Error as e:
            log.warning("kv %s not written: %s", key, type(e).__name__)

    def _audit(self, key: str, d: Delivery) -> None:
        try:
            with atomic(self.conn):
                audit(self.conn, "notify_owner.sent" if d.ok else "notify_owner.refused",
                      key=key, status=d.status, outcome=d.outcome, reason=d.reason)  # fmt: skip
        except sqlite3.Error as e:
            log.warning("notify-owner audit not written: %s", type(e).__name__)

    # -- the loop --

    def next_due_s(self) -> float:
        """Seconds until the next entry is due (``IDLE_S`` with none)."""
        now = self.clock()
        box = self._outbox()
        if not box:
            return IDLE_S
        soonest = min(_when(e.get("next_at"), now) for e in box.values())
        return max(0.0, min(IDLE_S, (soonest - now).total_seconds()))

    async def run(self) -> None:
        """Flush whenever an alert is queued or one comes due, until :meth:`stop`."""
        while not self._stop.is_set():
            try:
                await self.flush()
            except Exception:
                log.exception("notify-owner flush failed")
            self._wake.clear()
            if self._stop.is_set():
                break
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(max(0.05, self.next_due_s())):
                    await self._wake.wait()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()


def _when(raw: object, default: datetime) -> datetime:
    if isinstance(raw, str):
        with contextlib.suppress(ValueError):
            return datetime.fromisoformat(raw)
    return default


__all__ = [
    "MAX_BODY",
    "PATH",
    "TEST_KEY",
    "TEST_TITLE",
    "Delivery",
    "NotifyOwnerBody",
    "OwnerBridge",
    "bearer",
    "owner_key",
    "parse_body",
    "post_alert",
    "send_test",
    "token_ref_for",
]
