"""The status relay's pusher (docs/dashboard-plan.md D4, DB-3).

While ``relay.enabled``, the daemon runs :meth:`SnapshotPusher.run` as a background task: every
``relay.every_s`` it builds the dashboard snapshot, keeps only its ``remote`` tier
(:func:`~resonant.dashboard.snapshot.to_remote`, plan D3) and POSTs it to the Supabase RPC
``relay_ingest`` (``supabase/migrations``). Push only: nothing connects into the Mini.

- **Credentials.** The request carries the project's anon key (``relay.anon_key``, public) as
  ``apikey`` (and as ``Authorization: Bearer`` too only when it is a legacy JWT key; Supabase
  wants an ``sb_publishable_`` key on ``apikey`` alone) and the ingest token from the Keychain
  (``relay/supabase_key``) in the JSON body. The token is read on every push (``SecretStore``
  never caches) and is never logged, put in a URL or a header, or stored anywhere else.
  Redirects are not followed.
- **Bounded.** At most ``relay.max_attempts`` attempts per push, each capped at
  ``relay.timeout_s``, with exponential backoff between them, for network errors, timeouts,
  429 and 5xx. A rejected credential (401/403) or a refused snapshot (any other 4xx) is not
  retried. The response body is never read. The task only awaits, so the loop never blocks on
  it, and a push that fails just waits for the next one.
- **Health.** kv ``health.relay`` ``{state, since, detail}`` is the ``relay`` row of the
  snapshot: ``ok`` after a push lands; ``degraded`` after a failed push; ``failing`` after
  ``FAIL_AFTER`` failed pushes in a row, or at once when retrying can't help (rejected
  credentials, a refused snapshot, a missing secret). The row in a pushed snapshot is the state
  before that push. ``detail`` is built by code (an HTTP status or an exception type, never a
  response body or a URL), redacted of the token and key, and capped.
- **Kill switch.** ``/kill`` doesn't stop the relay: it is outbound status, not an action, and
  a paused Mini is exactly what the owner wants to see from away. The snapshot says
  ``paused: true``. ``dry_run`` doesn't stop it either. Only ``relay.enabled: false`` does,
  and then the daemon clears the health row (:func:`clear_relay_health`).
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import random
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal, cast

import httpx

from resonant.config import RelayConfig
from resonant.dashboard.snapshot import RELAY_HEALTH_KV, Snapshot, to_remote
from resonant.monitor import scrub
from resonant.redact import Redactor, redact_secrets, scrub_rc_links
from resonant.secrets import SecretError, SecretMissing, SecretStore, secret_ref
from resonant.store.db import iso, kv_get, kv_set, utcnow

log = logging.getLogger(__name__)

RELAY_EXT: Final = "relay"
SUPABASE_KEY_REF: Final = secret_ref(RELAY_EXT, "supabase_key")
INGEST_PATH: Final = "/rest/v1/rpc/relay_ingest"
# The migration caps ``octet_length(data::text)`` at 262144 (256 KiB). Measured here the same
# way, with a margin for Postgres's own number formatting, so a refused 413 never surprises us.
MAX_BYTES: Final = 240 * 1024
SHRINK_FLOOR: Final = 5  # step 2 keeps at least this many of each list
EXT_TOOLS_TRIMMED: Final = 10  # step 1 keeps this many of each extension's tools
MIN_GAP_S: Final = 1.0  # between pushes, even when one overran every_s
FAIL_AFTER: Final = 3
BACKOFF_S: Final = 2.0
BACKOFF_MAX_S: Final = 20.0

RelayState = Literal["ok", "degraded", "failing"]


@dataclass(frozen=True)
class PushOutcome:
    ok: bool
    fatal: bool  # retrying won't help: the owner has to fix something
    detail: str  # code-built; never a response body, URL or secret value


_OK = PushOutcome(ok=True, fatal=False, detail="pushed")


def data_size(data: Any) -> int:
    """The size Postgres gives ``data::text`` for this JSON (``", "`` and ``": "``)."""
    return len(json.dumps(data, ensure_ascii=False, separators=(", ", ": ")).encode())


def _items(data: dict[str, Any], section: str) -> list[dict[str, Any]]:
    node = data.get(section)
    items = cast(dict[str, Any], node).get("items") if isinstance(node, dict) else None
    return cast(list[dict[str, Any]], items) if isinstance(items, list) else []


def _approval_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    node = data.get("approvals")
    if not isinstance(node, dict):
        return []
    rows = cast(dict[str, Any], node)
    return [
        r for key in ("items", "recent") for r in cast(list[dict[str, Any]], rows.get(key) or [])
    ]


def _drop_outside(row: dict[str, Any], *names: str) -> None:
    if isinstance(row.get("outside"), list):
        row["outside"] = [o for o in cast(list[str], row["outside"]) if o not in names]


def _lists(data: dict[str, Any]) -> list[tuple[dict[str, Any], str]]:
    """The (parent, key) of every list step 2 halves and step 4 empties."""
    found: list[tuple[dict[str, Any], str]] = []
    for section, key in (
        ("runs", "items"),
        ("tasks", "items"),
        ("approvals", "recent"),
        ("pulse", "items"),
        ("questions", "items"),
    ):
        node = data.get(section)
        if isinstance(node, dict) and isinstance(cast(dict[str, Any], node).get(key), list):
            found.append((cast(dict[str, Any], node), key))
    brain = data.get("claude_brain")
    if isinstance(brain, dict) and isinstance(cast(dict[str, Any], brain).get("tools"), list):
        found.append((cast(dict[str, Any], brain), "tools"))
    return found


def _cut_history(out: dict[str, Any]) -> None:
    for run in _items(out, "runs"):
        run["history"] = run.get("history", [])[-4:]
        run["did"] = run.get("did", [])[-3:]
        run["effects"] = run.get("effects", [])[-3:]
    for task in _items(out, "tasks"):
        task["history"] = task.get("history", [])[-3:]
    for row in _approval_rows(out):
        if isinstance(row.get("changes"), list):
            row["changes"] = row["changes"][:3]
    # An extension's tool list is the biggest block a snapshot can carry (40 tools with their
    # descriptions each): cut it before any run or task row has to go.
    for ext in cast(list[dict[str, Any]], out.get("extensions") or []):
        if isinstance(ext.get("tools"), list):
            ext["tools"] = ext["tools"][:EXT_TOOLS_TRIMMED]
    # The diagram's children (PER-277) keep their boxes but lose their sentences.
    for node in _diagram_nodes(out):
        if node.get("kind") in _DIAGRAM_CHILDREN:
            node["text"] = ""


# The diagram nodes that belong to an extension: step 1 drops their sentences, step 3 them.
_DIAGRAM_CHILDREN: Final = frozenset({"tool", "server", "repo", "job"})


def _diagram_nodes(out: dict[str, Any]) -> list[dict[str, Any]]:
    diagram = out.get("diagram")
    if not isinstance(diagram, dict):
        return []
    nodes = cast(dict[str, Any], diagram).get("nodes")
    return cast(list[dict[str, Any]], nodes) if isinstance(nodes, list) else []


def _cut_diagram_children(out: dict[str, Any]) -> None:
    diagram = out.get("diagram")
    if not isinstance(diagram, dict):
        return
    node = cast(dict[str, Any], diagram)
    kept = [
        n
        for n in _diagram_nodes(out)
        if n.get("kind") not in _DIAGRAM_CHILDREN or n.get("ext") == "core"
    ]
    ids = {n.get("id") for n in kept}
    node["nodes"] = kept
    edges = node.get("edges")
    node["edges"] = [
        e
        for e in (cast(list[dict[str, Any]], edges) if isinstance(edges, list) else [])
        if e.get("source") in ids and e.get("target") in ids
    ]


def _halve_lists(out: dict[str, Any]) -> None:
    lists = _lists(out)
    while data_size(out) > MAX_BYTES and any(len(p[k]) > SHRINK_FLOOR for p, k in lists):
        for parent, key in lists:
            parent[key] = parent[key][: max(SHRINK_FLOOR, len(parent[key]) // 2)]


def _cut_detail(out: dict[str, Any]) -> None:
    for run in _items(out, "runs"):
        run.update(summary=None, failure=None, did=[], effects=[], history=[], flagged_paths=[],
                   detail=False)  # fmt: skip
        _drop_outside(run, "summary", "failure", "did", "effects", "flagged_paths")
    for task in _items(out, "tasks"):
        task.update(error=None, history=[])
        _drop_outside(task, "error")
    for ext in cast(list[dict[str, Any]], out.get("extensions") or []):
        ext["tools"] = []
    _cut_diagram_children(out)
    for row in _approval_rows(out):
        if row.get("changes") is not None:
            row["changes"] = None
            _drop_outside(row, "changes")


def _cut_items(out: dict[str, Any]) -> None:
    for parent, key in _lists(out):
        parent[key] = []
    approvals_node = out.get("approvals")
    if isinstance(approvals_node, dict):
        cast(dict[str, Any], approvals_node)["items"] = []
    out["trust_rules"] = []
    out["repos"] = []


# (what ``trimmed`` says, the step). Each step works on a copy and is applied after the ones
# before it; ``_halve_lists`` stops by itself as soon as the JSON fits.
SHRINK_STEPS: Final[tuple[tuple[str, Callable[[dict[str, Any]], None]], ...]] = (
    ("history", _cut_history),
    ("lists", _halve_lists),
    ("detail", _cut_detail),
    ("items", _cut_items),
)


def shrink(data: dict[str, Any]) -> dict[str, Any]:
    """``data`` cut to fit ``MAX_BYTES``, by the first of ``SHRINK_STEPS`` (each includes the
    ones before it) that does, with ``trimmed`` saying which; ``data`` itself when it already
    fits, the last step's result when none does. Deterministic, and every step's output is
    still a valid snapshot.

    1. ``history``: a run's last 4 events, 3 did notes and 3 effects, a task's last 3 events,
       a batch's first 3 changes, an extension's first ``EXT_TOOLS_TRIMMED`` tools, and no
       sentence on the diagram's extension children (tools, servers, jobs, repos).
    2. ``lists``: the runs, tasks, recently decided approvals, pulse items, brain tool calls
       and questions, halved together (each keeps at least ``SHRINK_FLOOR``) until it fits.
    3. ``detail``: every run's summary, failure, did, effects, history and flagged paths, every
       task's error and history, every extension's tools and every batch's changes dropped,
       and the diagram's extension children (core jobs stay).
    4. ``items``: every one of those lists, pending approvals, trust rules and repos emptied;
       the counts, health, extensions and jobs stay.
    """
    if data_size(data) <= MAX_BYTES:
        return data
    out = copy.deepcopy(data)
    for name, step in SHRINK_STEPS:
        out["trimmed"] = name
        step(out)
        if data_size(out) <= MAX_BYTES:
            break
    return out


def _scrub(data: dict[str, Any], known: Redactor) -> dict[str, Any]:
    """``data`` with the values ``known`` holds replaced in every string and key, then every
    string value cleaned of credential-shaped text (the last check before the wire)."""

    def walk(node: Any) -> Any:
        if isinstance(node, str):
            return redact_secrets(scrub_rc_links(node))
        if isinstance(node, dict):
            return {k: walk(v) for k, v in cast(dict[str, Any], node).items()}
        if isinstance(node, list):
            return [walk(v) for v in cast(list[Any], node)]
        return node

    return cast(dict[str, Any], walk(known.deep(data)))


def clear_relay_health(conn: sqlite3.Connection) -> None:
    """Drop the ``relay`` health row (the relay is off: no row, rather than a stale one)."""
    try:
        conn.execute("DELETE FROM kv WHERE key = ?", (RELAY_HEALTH_KV,))
    except sqlite3.Error:
        log.warning("could not clear the relay health row")


class SnapshotPusher:
    """Pushes the remote tier of ``snapshot()`` to Supabase. See the module doc.

    ``snapshot(redactor)`` returns the local-tier snapshot (``api.local_snapshot``) built with
    the relay's own secrets as known values; this class applies ``to_remote`` itself, so a
    caller can't hand it anything wider. ``client``, ``sleep``,
    ``clock``, ``monotonic`` and ``jitter`` are injectable for tests.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: RelayConfig,
        *,
        snapshot: Callable[[Redactor], Awaitable[Snapshot]],
        secrets: SecretStore,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], datetime] = utcnow,
        monotonic: Callable[[], float] = time.monotonic,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        if config.supabase_url is None or config.anon_key is None:
            raise ValueError("relay needs supabase_url and anon_key")
        self.conn = conn
        self.config = config
        self.url = config.supabase_url + INGEST_PATH
        self.anon_key = config.anon_key
        self._snapshot = snapshot
        self.secrets = secrets
        self._owns_client = client is None
        # Created on first use (run() closes it), so a pusher that never runs holds nothing.
        # No proxy or CA settings from the environment: one fixed https endpoint.
        self._client = client
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self._jitter = jitter
        self.failures = 0  # failed pushes in a row

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(follow_redirects=False, trust_env=False)
        return self._client

    async def run(self) -> None:
        """Push every ``every_s`` (start to start, at least ``MIN_GAP_S`` apart) until cancelled."""
        try:
            while True:
                started = self._monotonic()
                try:
                    await self.push_once()
                except Exception as e:  # never let the task die; never log a message text
                    log.error("status relay push crashed: %s", type(e).__name__)
                elapsed = self._monotonic() - started
                await self._sleep(max(MIN_GAP_S, self.config.every_s - elapsed))
        finally:
            if self._owns_client and self._client is not None:
                await self._client.aclose()

    async def push_once(self) -> PushOutcome:
        """One push with its retries; records the health row. Returns the outcome."""
        outcome = await self._push()
        self._record(outcome)
        return outcome

    async def _push(self) -> PushOutcome:
        try:
            token = await self.secrets.get(SUPABASE_KEY_REF)
        except SecretMissing:
            return PushOutcome(
                False, True, f"missing secret {SUPABASE_KEY_REF} (resonant secret set it)"
            )
        except SecretError as e:
            return PushOutcome(False, False, f"keychain: {e.reason}")
        redactor = Redactor([token, self.anon_key])
        try:
            snapshot = await self._snapshot(redactor)
            data = _scrub(to_remote(snapshot).model_dump(mode="json"), redactor)
        except Exception as e:
            return PushOutcome(False, False, f"snapshot failed ({type(e).__name__})")
        size = data_size(data)
        if size > MAX_BYTES:
            data = shrink(data)  # the counts and health still go out
            size = data_size(data)
        if size > MAX_BYTES:
            return PushOutcome(False, True, f"snapshot too large ({size} > {MAX_BYTES} bytes)")
        body = json.dumps({"p_token": token, "p_data": data}).encode()
        outcome = _OK
        for attempt in range(1, self.config.max_attempts + 1):
            outcome = await self._post(body)
            if outcome.ok or outcome.fatal or attempt == self.config.max_attempts:
                break
            await self._sleep(self._backoff(attempt))
        if outcome.ok:
            return outcome
        detail = scrub(redactor.text(outcome.detail))
        if self.config.max_attempts > 1 and not outcome.fatal:
            detail = f"{detail} (after {self.config.max_attempts} attempts)"
        return PushOutcome(False, outcome.fatal, detail)

    def _backoff(self, attempt: int) -> float:
        base = min(BACKOFF_MAX_S, BACKOFF_S * 2 ** (attempt - 1))
        return base * (1 + 0.25 * self._jitter())

    async def _post(self, body: bytes) -> PushOutcome:
        headers = {
            "apikey": self.anon_key,
            "content-type": "application/json",
            "prefer": "return=minimal",
        }
        # Bearer only for a legacy anon JWT: an sb_publishable_ key isn't a JWT and Supabase
        # wants it on apikey alone (the Fly relay's source.auth_headers does the same).
        if self.anon_key.count(".") == 2:
            headers["authorization"] = f"Bearer {self.anon_key}"
        try:
            async with asyncio.timeout(self.config.timeout_s):
                # stream: the status line is all we need; the body is never read.
                async with self.client.stream(
                    "POST",
                    self.url,
                    content=body,
                    headers=headers,
                    timeout=self.config.timeout_s,
                ) as response:
                    code = response.status_code
        except (TimeoutError, httpx.TimeoutException):
            return PushOutcome(False, False, "timed out")
        except httpx.HTTPError as e:
            # str(e) can carry the URL or more: the type only.
            return PushOutcome(False, False, f"network error ({type(e).__name__})")
        except Exception as e:  # anything else from the transport: a failed attempt, not a crash
            return PushOutcome(False, False, f"request failed ({type(e).__name__})")
        if 200 <= code < 300:
            return _OK
        if code in (401, 403):
            return PushOutcome(
                False,
                True,
                f"HTTP {code}: credentials rejected (check {SUPABASE_KEY_REF} and relay.anon_key)",
            )
        if code == 429 or code >= 500:
            return PushOutcome(False, False, f"HTTP {code}")
        if code == 404:
            return PushOutcome(False, True, "HTTP 404: relay_ingest not found (migration applied?)")
        return PushOutcome(False, True, f"HTTP {code}: snapshot refused")

    def _record(self, outcome: PushOutcome) -> None:
        if outcome.ok:
            self.failures = 0
            state: RelayState = "ok"
            detail = None
        else:
            self.failures += 1
            failing = outcome.fatal or self.failures >= FAIL_AFTER
            state = "failing" if failing else "degraded"
            detail = outcome.detail
        try:
            raw = kv_get(self.conn, RELAY_HEALTH_KV)
            prev = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
            if prev.get("state") == state and prev.get("detail") == detail:
                return
            if state == "ok":
                log.info("status relay pushing")
            else:
                log.warning("status relay %s: %s", state, detail)
            since = prev.get("since") if prev.get("state") == state else iso(self._clock())
            kv_set(self.conn, RELAY_HEALTH_KV, {"state": state, "since": since, "detail": detail})
        except sqlite3.Error as e:
            log.warning("could not record the relay health row: %s", type(e).__name__)
