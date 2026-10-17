"""The status relay's pusher (docs/dashboard-plan.md D4, DB-3).

While ``relay.enabled``, the daemon runs :meth:`SnapshotPusher.run` as a background task: every
``relay.every_s`` it builds the dashboard snapshot, keeps only its ``remote`` tier
(:func:`~resonant.dashboard.snapshot.to_remote`, plan D3) and POSTs it to the Supabase RPC
``relay_ingest`` (``supabase/migrations``). Push only: nothing connects into the Mini.

- **Credentials.** The request carries the project's anon key (``relay.anon_key``, public) as
  ``apikey`` and the ingest token from the Keychain (``relay/supabase_key``) in the JSON body.
  The token is read on every push (``SecretStore`` never caches) and is never logged, put in
  a URL or a header, or stored anywhere else. Redirects are not followed.
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
from resonant.redact import Redactor
from resonant.secrets import SecretError, SecretMissing, SecretStore, secret_ref
from resonant.store.db import iso, kv_get, kv_set, utcnow

log = logging.getLogger(__name__)

RELAY_EXT: Final = "relay"
SUPABASE_KEY_REF: Final = secret_ref(RELAY_EXT, "supabase_key")
INGEST_PATH: Final = "/rest/v1/rpc/relay_ingest"
# The migration's cap on ``octet_length(data::text)``; measured here the same way.
MAX_BYTES: Final = 65536
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


def clear_relay_health(conn: sqlite3.Connection) -> None:
    """Drop the ``relay`` health row (the relay is off: no row, rather than a stale one)."""
    try:
        conn.execute("DELETE FROM kv WHERE key = ?", (RELAY_HEALTH_KV,))
    except sqlite3.Error:
        log.warning("could not clear the relay health row")


class SnapshotPusher:
    """Pushes the remote tier of ``snapshot()`` to Supabase. See the module doc.

    ``snapshot`` returns the local-tier snapshot (``api.local_snapshot``); this class applies
    ``to_remote`` itself, so a caller can't hand it anything wider. ``client``, ``sleep``,
    ``clock``, ``monotonic`` and ``jitter`` are injectable for tests.
    """

    def __init__(
        self,
        conn: sqlite3.Connection,
        config: RelayConfig,
        *,
        snapshot: Callable[[], Awaitable[Snapshot]],
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
        self.client = client or httpx.AsyncClient(follow_redirects=False)
        self._sleep = sleep
        self._clock = clock
        self._monotonic = monotonic
        self._jitter = jitter
        self.failures = 0  # failed pushes in a row

    async def run(self) -> None:
        """Push every ``every_s`` (measured start to start) until cancelled."""
        try:
            while True:
                started = self._monotonic()
                try:
                    await self.push_once()
                except Exception as e:  # never let the task die; never log a message text
                    log.error("status relay push crashed: %s", type(e).__name__)
                elapsed = self._monotonic() - started
                await self._sleep(max(0.0, self.config.every_s - elapsed))
        finally:
            if self._owns_client:
                await self.client.aclose()

    async def push_once(self) -> PushOutcome:
        """One push with its retries; records the health row. Returns the outcome."""
        outcome = await self._push()
        self._record(outcome)
        return outcome

    async def _push(self) -> PushOutcome:
        try:
            data = to_remote(await self._snapshot()).model_dump(mode="json")
        except Exception as e:
            return PushOutcome(False, False, f"snapshot failed ({type(e).__name__})")
        size = data_size(data)
        if size > MAX_BYTES:
            return PushOutcome(False, True, f"snapshot too large ({size} > {MAX_BYTES} bytes)")
        try:
            token = await self.secrets.get(SUPABASE_KEY_REF)
        except SecretMissing:
            return PushOutcome(
                False, True, f"missing secret {SUPABASE_KEY_REF} (resonant secret set it)"
            )
        except SecretError as e:
            return PushOutcome(False, False, f"keychain: {e.reason}")
        redactor = Redactor([token, self.anon_key])
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
            "authorization": f"Bearer {self.anon_key}",
            "content-type": "application/json",
            "prefer": "return=minimal",
        }
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
