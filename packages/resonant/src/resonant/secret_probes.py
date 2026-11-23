"""Credential probes (PER-314): one cheap, read-only call a day per probe-able secret.

A step of ``core/secrets`` (``resonant.secret_expiry``), after the expiry step. For each ref in
:func:`plan` that the Keychain holds, one request with the value in **one request header**
(``Authorization``, or ``x-api-key`` for Anthropic) and nowhere else: never in a URL, argv,
log, audit, kv, error or message. ``httpx``, 10 s timeout, no redirects, ``User-Agent:
resonant-probe``. Only the status, a rate-limit header and the one expiry field are read; the
body is never logged or stored.

**Which call** (:data:`BUILTIN`, then ``runner.session_env`` by env var, then config
``secrets.probes: {<ref>: {kind, app?}}`` overriding or adding):

============================  ============================================  =================
kind                          call                                          expiry
============================  ============================================  =================
``github`` (github/token,     ``GET api.github.com/user``                   header
runner/github_token,                                                        ``github-authenti
runner/github_public_token)                                                 cation-token-expi
                                                                            ration``
``github_app``                JWT (``make_jwt``), ``GET api.github.com/app`` none
``linear`` (linear/token)     ``POST api.linear.app/graphql {viewer{id}}``  none
``fly`` (FLY_API_TOKEN,       ``GET api.machines.dev/v1/apps/<app>``        none (owner sets)
relay/fly_deploy_token)
``vercel`` (VERCEL_TOKEN)     ``GET api.vercel.com/v2/user``, then          ``expiresAt``
                              ``/v5/user/tokens/current``
``supabase``                  ``GET api.supabase.com/v1/projects``          none
(SUPABASE_ACCESS_TOKEN)
``anthropic``                 ``GET api.anthropic.com/v1/models``           none
============================  ============================================  =================

Not probed (:data:`NOT_PROBED`): ``runner/claude_token`` (no quota-free server check: ``claude
auth status`` reads local state only; it keeps the 365-day assumed expiry), and the refs
already checked all the time (``relay/supabase_key`` by the pusher, ``slack/*`` by
``auth.test``, ``core/notify_token*`` by the bridge). A Fly ref with no app is ``not probed:
no app`` (``relay/fly_deploy_token`` takes ``relay.fly_app``); ``runner/github_app_key``
needs ``runner.github_app``.

**Outcomes**, kv ``secrets.probe:<ref>`` = ``{state, status, at}``: 2xx ``ok`` (an expiry the
API reports is upserted with source ``probe``; an ``owner`` row wins only when sooner); 401, a
403 that isn't a rate limit, or Linear's ``AUTHENTICATION_ERROR``: ``rejected``; a timeout,
network error, rate limit, 5xx or any other status: ``unknown`` (no alert; logged with the
ref and the status). A rejection alerts once per incident (kv
``secrets.probe_incident:<ref>`` = ``{since, alerted}``; dedupe key
``probe:<ref>:rejected:<since>``) through the expiry job's :class:`Delivery`; the next ``ok``
sends one ``✓ <ref> works again``. A ``rejected`` ref makes the ``secrets`` health row
``failing``. Nothing probe-able present: no request, no write. A locked Keychain skips every
probe this run (logged once).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import sqlite3
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Literal, cast
from zoneinfo import ZoneInfo

import httpx

from resonant.config import ProbeKind
from resonant.ops.relay_deploy import FLY_DEPLOY_TOKEN_REF
from resonant.secret_expiry import BRIDGE_KEY, RELAY_KEY_REF, Entry, known_refs, renew_command
from resonant.secrets import (
    ANTHROPIC_KEY_REF,
    CLAUDE_TOKEN_REF,
    GITHUB_APP_KEY_REF,
    GITHUB_PUBLIC_TOKEN_REF,
    GITHUB_TOKEN_REF,
    SecretMissing,
    SecretStore,
    SecretUnavailable,
)
from resonant.store import secret_expiry as rows_store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set

if TYPE_CHECKING:
    from resonant.config import Settings
    from resonant.secret_expiry import Delivery

log = logging.getLogger(__name__)

Kind = Literal["github", "github_app", "linear", "fly", "vercel", "supabase", "anthropic"]
State = Literal["ok", "rejected", "unknown"]

PROBE_KV = "secrets.probe:"  # + ref: {state, status, at} (PER-315 reads it)
INCIDENT_KV = "secrets.probe_incident:"  # + ref: {since, alerted}
AUDIT_ACTION = "secrets.probe_alert"
TIMEOUT_S = 10.0
USER_AGENT = "resonant-probe"
LOCKED = "keychain unavailable"  # a Result's why when the Keychain didn't answer

GITHUB_API = "https://api.github.com"
LINEAR_API = "https://api.linear.app/graphql"
FLY_API = "https://api.machines.dev/v1/apps/"
VERCEL_API = "https://api.vercel.com"
SUPABASE_API = "https://api.supabase.com/v1/projects"
ANTHROPIC_API = "https://api.anthropic.com/v1/models"
GITHUB_EXPIRY_HEADER = "github-authentication-token-expiration"

BUILTIN: dict[str, Kind] = {
    "github/token": "github",
    GITHUB_TOKEN_REF: "github",
    GITHUB_PUBLIC_TOKEN_REF: "github",
    GITHUB_APP_KEY_REF: "github_app",
    "linear/token": "linear",
    FLY_DEPLOY_TOKEN_REF: "fly",
    ANTHROPIC_KEY_REF: "anthropic",
}
# runner.session_env refs by the env var they fill
BY_ENV: dict[str, Kind] = {
    "FLY_API_TOKEN": "fly",
    "VERCEL_TOKEN": "vercel",
    "SUPABASE_ACCESS_TOKEN": "supabase",
}
NOT_PROBED: dict[str, str] = {
    CLAUDE_TOKEN_REF: "no free check",
    RELAY_KEY_REF: "checked by the relay pusher",
}
NOT_PROBED_PREFIXES: tuple[tuple[str, str], ...] = (
    ("slack/", "checked by auth.test"),
    ("core/notify_token", "checked by the bridge"),
)


@dataclass(frozen=True)
class Target:
    """One ref and how it's probed. ``why``: not probed, and why (then ``kind`` may be None)."""

    ref: str
    kind: Kind | None
    app: str | None = None  # fly: the app it may read
    app_id: int | None = None  # github_app: the app's id
    why: str | None = None

    @property
    def probed(self) -> bool:
        return self.kind is not None and self.why is None


@dataclass(frozen=True)
class Outcome:
    """What one probe found. ``reason``: why ``unknown`` (``timeout``, ``HTTP 503``, ...)."""

    state: State
    status: int | None = None
    reason: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True)
class Result:
    """A target's outcome, or None with ``why`` when it wasn't probed."""

    target: Target
    outcome: Outcome | None
    why: str | None = None

    @property
    def ref(self) -> str:
        return self.target.ref


# --- which refs ------------------------------------------------------------------------------


def _covered(ref: str) -> str | None:
    """Why a ref already checked elsewhere isn't probed again, or None."""
    return next((why for p, why in NOT_PROBED_PREFIXES if ref.startswith(p)), None)


def _resolve(ref: str, settings: Settings) -> Target:
    by_var = {r: var for var, r in settings.runner.session_env.items()}
    override = settings.secrets.probes.get(ref)
    kind: ProbeKind | None
    app: str | None = None
    if override is not None:
        kind, app = override.kind, override.app
        if kind == "off":
            return Target(ref, None, why="off in config")
    elif ref in NOT_PROBED:
        return Target(ref, None, why=NOT_PROBED[ref])
    elif (skip := _covered(ref)) is not None:
        return Target(ref, None, why=skip)
    elif ref in BUILTIN:
        kind = BUILTIN[ref]
    elif (var := by_var.get(ref)) is not None and var in BY_ENV:
        kind = BY_ENV[var]
    else:
        return Target(ref, None, why="no probe for this ref")
    k: Kind = kind
    if k == "fly":
        if app is None and ref == FLY_DEPLOY_TOKEN_REF:
            app = settings.relay.fly_app
        if app is None:
            return Target(ref, k, why="no app")
    if k == "github_app":
        gh_app = settings.runner.github_app
        if gh_app is None:
            return Target(ref, k, why="no runner.github_app")
        return Target(ref, k, app_id=gh_app.app_id)
    return Target(ref, k, app=app)


def plan(settings: Settings, refs: Iterable[str] = ()) -> list[Target]:
    """Every ref this instance might probe (the known ones, the built-in table, the config's
    ``secrets.probes``, plus ``refs``), sorted, each with its kind or why not. Names only."""
    wanted = {*known_refs(settings), *BUILTIN, *settings.secrets.probes, *refs}
    return [_resolve(ref, settings) for ref in sorted(wanted)]


def target_for(settings: Settings, ref: str) -> Target:
    return _resolve(ref, settings)


# --- one probe -------------------------------------------------------------------------------


def _bearer(value: str) -> str:
    return f"Bearer {value}"


def _fly_auth(value: str) -> str:
    # ``fly tokens create`` prints ``FlyV1 fm2_...``: that is already the whole header value.
    return value if value.startswith("FlyV1 ") else _bearer(value)


def _linear_auth(value: str) -> str:
    # A personal API key goes bare; an OAuth token is a bearer.
    return value if value.startswith("lin_api_") else _bearer(value)


def parse_github_expiry(raw: str | None) -> datetime | None:
    """``2027-01-01 00:00:00 UTC`` (or ``... -0800``) as UTC; None when absent or odd."""
    if not raw:
        return None
    text = raw.strip()
    try:
        if text.endswith(" UTC"):
            return datetime.strptime(text[:-4], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S %z").astimezone(UTC)
    except ValueError:
        return None


def _json(resp: httpx.Response) -> Any:
    with contextlib.suppress(ValueError):
        return resp.json()
    return None


def _linear_auth_error(resp: httpx.Response) -> bool:
    """Linear answers a bad key with HTTP 400 and ``extensions.code AUTHENTICATION_ERROR``."""
    if resp.status_code != 400:
        return False
    data: object = _json(resp)
    if not isinstance(data, dict):
        return False
    errors: object = cast(dict[str, Any], data).get("errors")
    if not isinstance(errors, list):
        return False
    for err in cast(list[object], errors):
        if isinstance(err, dict):
            ext: object = cast(dict[str, Any], err).get("extensions")
            if isinstance(ext, dict) and cast(dict[str, Any], ext).get("code") == (
                "AUTHENTICATION_ERROR"
            ):
                return True
    return False


def classify(resp: httpx.Response, kind: Kind) -> Outcome:
    """The outcome of a status (and a rate-limit header), nothing else of the reply."""
    s = resp.status_code
    if 200 <= s < 300:
        return Outcome("ok", s)
    if s == 403 and (
        resp.headers.get("x-ratelimit-remaining") == "0" or "retry-after" in resp.headers
    ):
        return Outcome("unknown", s, "rate limit")
    if s in (401, 403) or (kind == "linear" and _linear_auth_error(resp)):
        return Outcome("rejected", s)
    if s == 429:
        return Outcome("unknown", s, "rate limit")
    return Outcome("unknown", s, f"HTTP {s}")


async def _vercel_expiry(client: httpx.AsyncClient, value: str) -> datetime | None:
    """``GET /v5/user/tokens/current`` ``token.expiresAt`` (ms), when it answers with one."""
    try:
        resp = await client.get(
            f"{VERCEL_API}/v5/user/tokens/current", headers={"Authorization": _bearer(value)}
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    data: object = _json(resp)
    token: object = cast(dict[str, Any], data).get("token") if isinstance(data, dict) else None
    ms: object = cast(dict[str, Any], token).get("expiresAt") if isinstance(token, dict) else None
    if isinstance(ms, int | float) and not isinstance(ms, bool) and ms > 0:
        with contextlib.suppress(ValueError, OverflowError, OSError):
            return datetime.fromtimestamp(ms / 1000, UTC)
    return None


async def check(target: Target, value: str, client: httpx.AsyncClient) -> Outcome:
    """One probe. Never raises; never puts ``value`` anywhere but one request header."""
    kind = target.kind
    assert kind is not None and target.probed
    try:
        match kind:
            case "github":
                resp = await client.get(
                    f"{GITHUB_API}/user",
                    headers={"Authorization": _bearer(value),
                             "Accept": "application/vnd.github+json"},
                )  # fmt: skip
            case "github_app":
                from resonant.runner.github_app import GitHubAppError, make_jwt, parse_private_key

                assert target.app_id is not None
                try:
                    jwt = make_jwt(target.app_id, parse_private_key(value), time.time())
                except GitHubAppError:
                    return Outcome("unknown", None, "unreadable key")
                resp = await client.get(
                    f"{GITHUB_API}/app",
                    headers={"Authorization": _bearer(jwt),
                             "Accept": "application/vnd.github+json"},
                )  # fmt: skip
            case "linear":
                resp = await client.post(
                    LINEAR_API,
                    json={"query": "{ viewer { id } }"},
                    headers={"Authorization": _linear_auth(value)},
                )
            case "fly":
                assert target.app is not None
                resp = await client.get(
                    FLY_API + target.app, headers={"Authorization": _fly_auth(value)}
                )
            case "vercel":
                resp = await client.get(
                    f"{VERCEL_API}/v2/user", headers={"Authorization": _bearer(value)}
                )
            case "supabase":
                resp = await client.get(SUPABASE_API, headers={"Authorization": _bearer(value)})
            case "anthropic":
                resp = await client.get(
                    ANTHROPIC_API, headers={"x-api-key": value, "anthropic-version": "2023-06-01"}
                )
    except httpx.TimeoutException:
        return Outcome("unknown", None, "timeout")
    except httpx.HTTPError as e:
        return Outcome("unknown", None, f"network: {type(e).__name__}")
    except Exception as e:  # one odd ref never hides the others' outcomes; type name only
        return Outcome("unknown", None, f"error: {type(e).__name__}")
    out = classify(resp, kind)
    if out.state != "ok":
        return out
    if kind == "github":
        expires = parse_github_expiry(resp.headers.get(GITHUB_EXPIRY_HEADER))
        return Outcome("ok", out.status, expires_at=expires)
    if kind == "vercel":
        return Outcome("ok", out.status, expires_at=await _vercel_expiry(client, value))
    return out


def client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=TIMEOUT_S,
        follow_redirects=False,
        headers={"User-Agent": USER_AGENT},
        transport=transport,
    )


async def probe_all(
    store: SecretStore,
    targets: Sequence[Target],
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[list[Result], bool]:
    """``(results, locked)``: one per target. A target not probed, or whose item is missing,
    gets ``why``; after one ``SecretUnavailable`` nothing more is read and no request made.
    No probe-able item present: no client, no request."""
    values: dict[str, str] = {}
    skipped: dict[str, str] = {}
    locked = False
    for t in targets:
        if not t.probed:
            continue
        if locked:
            skipped[t.ref] = LOCKED
            continue
        try:
            values[t.ref] = await store.get(t.ref)
        except SecretMissing:
            skipped[t.ref] = "not set"
        except SecretUnavailable:
            locked = True
            skipped[t.ref] = LOCKED
    if locked:
        log.warning("secret probes: keychain unavailable; skipped this run")
        values.clear()
        skipped.update({t.ref: LOCKED for t in targets if t.probed})
    outcomes: dict[str, Outcome] = {}
    if values:
        by_ref = {t.ref: t for t in targets}
        async with client(transport) as c:
            refs = sorted(values)
            got = await asyncio.gather(*(check(by_ref[r], values[r], c) for r in refs))
        outcomes = dict(zip(refs, got, strict=True))
        values.clear()
    results: list[Result] = []
    for t in targets:
        if (o := outcomes.get(t.ref)) is not None:
            if o.state != "ok":
                log.info("secret probe %s: %s (%s)", t.ref, o.state, o.reason or o.status)
            results.append(Result(t, o))
        else:
            results.append(Result(t, None, t.why or skipped.get(t.ref, "not set")))
    return results, locked


# --- recording -------------------------------------------------------------------------------


def apply_expiry(
    conn: sqlite3.Connection, ref: str, expires_at: datetime, now: datetime
) -> bool:  # fmt: skip
    """Upsert a ``probe`` row unless an ``owner`` row is as soon or sooner, or it's unchanged.
    True when written."""
    row = rows_store.get(conn, ref)
    if row is not None:
        if row.source == "owner" and row.expires_at <= expires_at:
            return False
        if row.source == "probe" and row.expires_at == expires_at:
            return False
    rows_store.upsert(conn, ref, expires_at=expires_at, source="probe", set_at=now)
    return True


def record(conn: sqlite3.Connection, results: Iterable[Result], now: datetime) -> None:
    """Every probed result's kv ``{state, status, at}``, and the expiries the APIs reported.
    A ref no longer probed (gone from the Keychain, ``off``, no app) loses its kv outcome and
    any open incident, so an old ``rejected`` never keeps the health row ``failing``; a locked
    Keychain forgets nothing."""
    with atomic(conn):
        for r in results:
            if r.outcome is None:
                if r.why != LOCKED:
                    for prefix in (PROBE_KV, INCIDENT_KV):
                        conn.execute("DELETE FROM kv WHERE key = ?", (prefix + r.ref,))
                continue
            o = r.outcome
            kv_set(conn, PROBE_KV + r.ref, {"state": o.state, "status": o.status, "at": iso(now)})
            if o.state == "ok" and o.expires_at is not None:
                apply_expiry(conn, r.ref, o.expires_at, now)


def _stale(conn: sqlite3.Connection, results: Iterable[Result]) -> bool:
    """Whether a ref not probed now still has a kv outcome or incident (only then write)."""
    gone = {r.ref for r in results if r.outcome is None}
    return any(ref in gone for ref in last_states(conn)) or any(
        _incident(conn, ref) is not None for ref in gone
    )


def last_state(conn: sqlite3.Connection, ref: str) -> dict[str, Any] | None:
    raw: object = kv_get(conn, PROBE_KV + ref)
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else None


def rejected_refs(conn: sqlite3.Connection) -> list[str]:
    """Refs still taken as rejected, sorted (the health row's ``failing``): the last probe was
    ``rejected``, or ``unknown`` during an open incident (a timeout doesn't clear it; ``ok``
    does)."""
    out: list[str] = []
    for ref, st in last_states(conn).items():
        state = st.get("state")
        if state == "rejected" or (state == "unknown" and _incident(conn, ref) is not None):
            out.append(ref)
    return out


def last_states(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    """Every ref's last probe outcome (kv ``secrets.probe:<ref>``), by ref."""
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(
        "SELECT key FROM kv WHERE substr(key, 1, ?) = ? ORDER BY key", (len(PROBE_KV), PROBE_KV)
    ):
        ref = str(row["key"]).removeprefix(PROBE_KV)
        if (state := last_state(conn, ref)) is not None:
            out[ref] = state
    return out


def list_note(target: Target, last: dict[str, Any] | None) -> str | None:
    """The ``secret list`` note for a present ref: ``probe ok``, ``probe rejected (401)``,
    ``probe unknown``, ``not probed: no free check``; None when there's nothing to say."""
    if not target.probed:
        return None if target.why == "no probe for this ref" else f"not probed: {target.why}"
    if last is None:
        return None
    state, status = last.get("state"), last.get("status")
    if state == "rejected":
        return f"probe rejected ({status})"
    return f"probe {state}" if state in ("ok", "unknown") else None


def describe(r: Result) -> str:
    """``ok``, ``rejected (401)``, ``unknown (timeout)`` or ``not probed: <why>``."""
    if r.outcome is None:
        return f"not probed: {r.why}"
    o = r.outcome
    if o.state == "ok":
        return "ok"
    if o.state == "rejected":
        return f"rejected ({o.status})"
    return f"unknown ({o.reason or o.status})"


# --- alerting --------------------------------------------------------------------------------


def _entry(conn: sqlite3.Connection, ref: str) -> Entry:
    row = rows_store.get(conn, ref)
    if row is None:
        return Entry(ref, True)
    return Entry(ref, True, row.expires_at, row.source, row.set_at)


def rejected_text(ref: str, status: int | None, renew: str) -> str:
    return f"🔑 {ref} was rejected (HTTP {status}). Renew: {renew}"


def recovered_text(ref: str) -> str:
    return f"✓ {ref} works again"


def _incident(conn: sqlite3.Connection, ref: str) -> dict[str, Any] | None:
    raw: object = kv_get(conn, INCIDENT_KV + ref)
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else None


async def _tell(
    conn: sqlite3.Connection, settings: Settings, delivery: Delivery, r: Result, now: datetime
) -> str | None:
    """Alert a new rejection, or the recovery of a told one. The decision, or None."""
    o = r.outcome
    assert o is not None
    ref = r.ref
    incident = _incident(conn, ref)
    bridge_key = f"{BRIDGE_KEY}probe/{ref}"
    if o.state == "rejected":
        if incident is None:
            since = now.astimezone(ZoneInfo(settings.timezone)).date().isoformat()
            incident = {"since": since, "alerted": False}
            with atomic(conn):
                kv_set(conn, INCIDENT_KV + ref, incident)
        if incident.get("alerted") is True:
            return None
        key = f"probe:{ref}:rejected:{incident.get('since')}"
        text = rejected_text(ref, o.status, renew_command(_entry(conn, ref), settings))
        took = await delivery.send_text(
            key, text, bridge_key=bridge_key, bridge_title=text.removeprefix("🔑 ")
        )
        decision = "sent" if took else "failed"
        with atomic(conn):
            if took:
                kv_set(conn, INCIDENT_KV + ref, {**incident, "alerted": True})
            audit(conn, AUDIT_ACTION, decision=decision, idempotency_key=key, ref=ref,
                  state="rejected", status=o.status)  # fmt: skip
        return decision
    if o.state == "ok" and incident is not None:
        if incident.get("alerted") is not True:
            with atomic(conn):
                conn.execute("DELETE FROM kv WHERE key = ?", (INCIDENT_KV + ref,))
            return None
        key = f"probe:{ref}:recovered:{incident.get('since')}"
        text = recovered_text(ref)
        took = await delivery.send_text(
            key, text, bridge_key=bridge_key, bridge_title=f"{ref} works again", clear=True
        )
        decision = "recovered" if took else "failed"
        with atomic(conn):
            if took:
                conn.execute("DELETE FROM kv WHERE key = ?", (INCIDENT_KV + ref,))
            audit(conn, AUDIT_ACTION, decision=decision, idempotency_key=key, ref=ref,
                  state="ok", status=o.status)  # fmt: skip
        return decision
    return None


async def watch_probes(
    conn: sqlite3.Connection,
    store: SecretStore,
    settings: Settings,
    delivery: Delivery,
    now: datetime,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> list[dict[str, Any]]:
    """The probe step of ``core/secrets``: ``[{ref, state, status, alert?}]`` for every ref
    probed (names only), ``[]`` when none was."""
    results, locked = await probe_all(store, plan(settings), transport=transport)
    probed = [r for r in results if r.outcome is not None]
    if not locked and _stale(conn, results):
        record(conn, [r for r in results if r.outcome is None], now)
    if not probed:
        return []
    record(conn, probed, now)
    out: list[dict[str, Any]] = []
    for r in probed:
        assert r.outcome is not None
        item: dict[str, Any] = {"ref": r.ref, "state": r.outcome.state, "status": r.outcome.status}
        if (decision := await _tell(conn, settings, delivery, r, now)) is not None:
            item["alert"] = decision
        out.append(item)
    return out


__all__ = [
    "BUILTIN",
    "BY_ENV",
    "INCIDENT_KV",
    "NOT_PROBED",
    "PROBE_KV",
    "Outcome",
    "Result",
    "Target",
    "apply_expiry",
    "check",
    "classify",
    "describe",
    "last_state",
    "last_states",
    "list_note",
    "parse_github_expiry",
    "plan",
    "probe_all",
    "record",
    "rejected_refs",
    "target_for",
    "watch_probes",
]
