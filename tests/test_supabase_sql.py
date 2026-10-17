"""The relay's Supabase migration on a real Postgres (supabase/migrations, DB-3).

Runs the migration on a throwaway cluster, after ``tests/fixtures/supabase_stub.sql`` (the API
roles, ``auth.uid()``/``auth.jwt()`` and Supabase's default privileges), and checks who can do
what as PostgREST would: ``SET ROLE`` plus the request's JWT claims.

Needs the Postgres server binaries (``initdb``, ``pg_ctl``, ``psql``): ``PG_TEST_BINDIR``, else
``pg_config --bindir``, else a Debian/Homebrew install dir. Without them every test skips,
unless ``PG_TEST_REQUIRED=1`` (the ``supabase-sql`` CI job), where that is a failure.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from resonant.config import load_settings
from resonant.dashboard.snapshot import build_snapshot, to_remote
from resonant.store.db import open_db

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
STUB = ROOT / "tests" / "fixtures" / "supabase_stub.sql"

OWNER = "11111111-1111-4111-8111-111111111111"
STRANGER = "22222222-2222-4222-8222-222222222222"
INGEST = "i" * 8 + "0123456789abcdef" * 3  # 56 chars; the real one is 64 hex
WATCH = "w" * 8 + "fedcba9876543210" * 3


def _bindir() -> Path | None:
    candidates: list[str] = []
    if env := os.environ.get("PG_TEST_BINDIR"):
        candidates.append(env)
    if pg_config := shutil.which("pg_config"):
        out = subprocess.run([pg_config, "--bindir"], capture_output=True, text=True, check=False)
        candidates.append(out.stdout.strip())
    candidates += sorted(glob.glob("/usr/lib/postgresql/*/bin"), reverse=True)
    candidates += sorted(glob.glob("/opt/homebrew/opt/postgresql@*/bin"), reverse=True)
    for c in candidates:
        if c and all((Path(c) / b).is_file() for b in ("initdb", "pg_ctl", "psql")):
            return Path(c)
    return None


BINDIR = _bindir()
if BINDIR is None:
    if os.environ.get("PG_TEST_REQUIRED") == "1":
        raise RuntimeError("PG_TEST_REQUIRED=1 but no Postgres binaries were found")
    pytest.skip("no Postgres server binaries (set PG_TEST_BINDIR)", allow_module_level=True)


@dataclass(frozen=True)
class Result:
    ok: bool
    out: str
    err: str

    @property
    def sqlstate(self) -> str | None:
        # psql with VERBOSITY=verbose prints "ERROR:  PT403: relay: bad token".
        m = re.search(r"ERROR:\s+([0-9A-Z]{5}):", self.err)
        return None if m is None else m.group(1)


class Pg:
    def __init__(self, bindir: Path, sockdir: Path, port: int) -> None:
        self.bindir, self.sockdir, self.port = bindir, sockdir, port

    def run(
        self, sql: str, *, role: str | None = None, claims: dict[str, Any] | None = None
    ) -> Result:
        script = ""
        if claims is not None:
            script += f"set request.jwt.claims = '{json.dumps(claims)}';\n"
        if role is not None:
            script += f"set role {role};\n"
        script += sql
        proc = subprocess.run(
            [
                str(self.bindir / "psql"),
                "-X",
                "-q",
                "-t",
                "-A",
                "-h",
                str(self.sockdir),
                "-p",
                str(self.port),
                "-U",
                "postgres",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-v",
                "VERBOSITY=verbose",
                "-f",
                "-",
            ],
            input=script,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return Result(proc.returncode == 0, proc.stdout.strip(), proc.stderr)

    def ok(self, sql: str, **kw: Any) -> str:
        r = self.run(sql, **kw)
        assert r.ok, r.err
        return r.out


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture(scope="module")
def pg() -> Iterator[Pg]:
    assert BINDIR is not None
    assert MIGRATIONS, "no migrations"
    # A short path: a Unix socket path must fit in ~100 bytes.
    base = Path(tempfile.mkdtemp(prefix="rpg", dir="/tmp"))
    data, sock = base / "data", base / "sock"
    sock.mkdir()
    subprocess.run(
        [str(BINDIR / "initdb"), "-D", str(data), "-U", "postgres", "--auth=trust", "-E", "UTF8"],
        capture_output=True,
        check=True,
    )
    port = _free_port()
    opts = f"-c listen_addresses='' -k {sock} -p {port} -c fsync=off"
    subprocess.run(
        [
            str(BINDIR / "pg_ctl"),
            "-D",
            str(data),
            "-o",
            opts,
            "-w",
            "-l",
            str(base / "log"),
            "start",
        ],
        capture_output=True,
        check=True,
    )
    try:
        db = Pg(BINDIR, sock, port)
        db.ok(STUB.read_text())
        for m in MIGRATIONS:
            db.ok(m.read_text())
        yield db
    finally:
        subprocess.run(
            [str(BINDIR / "pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(base, ignore_errors=True)


def _sha(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


@pytest.fixture
def db(pg: Pg) -> Pg:
    """A configured project with no snapshots."""
    pg.ok(
        "truncate public.snapshots;"
        f"update relay_private.config set owner_id = '{OWNER}',"
        f" ingest_token_sha256 = decode('{_sha(INGEST)}', 'hex'),"
        f" watch_token_sha256 = decode('{_sha(WATCH)}', 'hex');"
    )
    return pg


def remote_snapshot(tmp_path: Path, **changes: Any) -> dict[str, Any]:
    conn = open_db(tmp_path / "s.db")
    try:
        snap = build_snapshot(
            conn, load_settings(), started_at=datetime.now(UTC), version="abcdef12"
        )
    finally:
        conn.close()
    data = to_remote(snap).model_dump(mode="json")
    data.update(changes)
    return data


def _lit(value: Any) -> str:
    return "'" + json.dumps(value).replace("'", "''") + "'"


def ingest(db: Pg, data: Any, token: str = INGEST, role: str = "anon") -> Result:
    return db.run(f"select public.relay_ingest('{token}', {_lit(data)}::jsonb);", role=role)


def count(db: Pg) -> int:
    return int(db.ok("select count(*) from public.snapshots;"))


OWNER_AAL2 = {"sub": OWNER, "role": "authenticated", "aal": "aal2"}


# --- anon (the public key) -------------------------------------------------------------------


def test_anon_cannot_touch_the_table_or_config(db: Pg, tmp_path: Path) -> None:
    db.ok("insert into public.snapshots (taken_at, data) values (now(), '{}');")
    for sql in (
        "select * from public.snapshots;",
        "insert into public.snapshots (taken_at, data) values (now(), '{}');",
        "update public.snapshots set data = '{}';",
        "delete from public.snapshots;",
        "select * from relay_private.config;",
        "select relay_private.is_owner();",
        "select nextval('public.snapshots_id_seq');",
    ):
        r = db.run(sql, role="anon")
        assert not r.ok and r.sqlstate == "42501", (sql, r.err)
    assert count(db) == 1


def test_authenticated_has_select_only_and_no_rpc(db: Pg, tmp_path: Path) -> None:
    for sql in (
        "insert into public.snapshots (taken_at, data) values (now(), '{}');",
        "update public.snapshots set data = '{}';",
        "delete from public.snapshots;",
        "select * from relay_private.config;",
        f"select public.relay_ingest('{INGEST}', '{{}}');",
        f"select * from public.relay_watch('{WATCH}');",
    ):
        r = db.run(sql, role="authenticated", claims=OWNER_AAL2)
        assert not r.ok and r.sqlstate == "42501", (sql, r.err)


# --- the ingest path -------------------------------------------------------------------------


def test_ingest_stores_a_remote_snapshot(db: Pg, tmp_path: Path) -> None:
    data = remote_snapshot(tmp_path)
    assert ingest(db, data).ok
    row = db.ok(
        "select taken_at = (data->>'taken_at')::timestamptz, data->>'tier' from public.snapshots;"
    )
    assert row == "t|remote"
    # The writer still can't read what it wrote.
    assert db.run("select count(*) from public.snapshots;", role="anon").sqlstate == "42501"


@pytest.mark.parametrize("token", ["", "short", INGEST + "x", WATCH])
def test_ingest_refuses_a_bad_token(db: Pg, tmp_path: Path, token: str) -> None:
    r = ingest(db, remote_snapshot(tmp_path), token=token)
    assert r.sqlstate == "PT403", r.err
    assert count(db) == 0


def test_ingest_refuses_everything_until_configured(db: Pg, tmp_path: Path) -> None:
    db.ok("update relay_private.config set ingest_token_sha256 = null;")
    assert ingest(db, remote_snapshot(tmp_path)).sqlstate == "PT403"


@pytest.mark.parametrize(
    "change",
    [
        {"tier": "local"},
        {"schema": 2},
        {"trust_rules": [{"n": 1, "text": "never push to main"}]},
        {"repos": []},
        {"runs": {"by_state": {}, "items": []}},
        {"approvals": {"open": 1, "items": []}},
        {"questions": {"open": 0, "items": []}},
        {"pulse": {"open": 0, "items": []}},
        {"runner": {"set_up": False, "missing": ["x"]}},
        {"health": [{"name": "model", "state": "ok", "since": None, "reason": "detail"}]},
        {"taken_at": "not a time"},
        {"taken_at": None},
        {"taken_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()},
    ],
)
def test_ingest_refuses_anything_but_a_remote_tier(
    db: Pg, tmp_path: Path, change: dict[str, Any]
) -> None:
    r = ingest(db, remote_snapshot(tmp_path, **change))
    assert r.sqlstate == "PT400", r.err
    assert count(db) == 0


def _local_only_paths(local: Any, remote: Any, path: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    """Every key the local tier has and the remote one lacks, at any depth (list items by
    position), from real dumps: so a new LOCAL_ONLY field shows up here by itself."""
    out: list[tuple[Any, ...]] = []
    if isinstance(local, dict) and isinstance(remote, dict):
        loc, rem = cast(dict[str, Any], local), cast(dict[str, Any], remote)
        for k, v in loc.items():
            if k not in rem:
                out.append((*path, k))
            else:
                out += _local_only_paths(v, rem[k], (*path, k))
    elif isinstance(local, list) and isinstance(remote, list):
        for i, (lv, rv) in enumerate(
            zip(cast(list[Any], local), cast(list[Any], remote), strict=False)
        ):
            out += _local_only_paths(lv, rv, (*path, i))
    return out


def _busy_dumps(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    from test_dashboard_snapshot import _busy  # pyright: ignore[reportPrivateUsage]

    from resonant.dashboard.snapshot import RELAY_HEALTH_KV
    from resonant.store.db import kv_set

    conn = open_db(tmp_path / "busy.db")
    try:
        _busy(conn, datetime.now(UTC))
        kv_set(conn, RELAY_HEALTH_KV, {"state": "failing", "since": None, "detail": "HTTP 503"})
        snap = build_snapshot(
            conn, load_settings(), started_at=datetime.now(UTC), version="abcdef12"
        )
    finally:
        conn.close()
    return snap.model_dump(mode="json"), to_remote(snap).model_dump(mode="json")


def test_every_local_only_field_is_refused(db: Pg, tmp_path: Path) -> None:
    local, remote = _busy_dumps(tmp_path)
    paths = _local_only_paths(local, remote)
    # The busy store fills every local-only field the models declare.
    assert {p[-1] for p in paths} >= {"reason", "items", "missing", "trust_rules", "repos"}
    for path in paths:
        data = json.loads(json.dumps(remote))
        target, src = data, local
        for key in path[:-1]:
            target, src = target[key], src[key]
        target[path[-1]] = src[path[-1]]
        r = ingest(db, data)
        assert r.sqlstate == "PT400", (path, r.err)
    assert count(db) == 0


def test_a_busy_remote_snapshot_is_accepted(db: Pg, tmp_path: Path) -> None:
    _, remote = _busy_dumps(tmp_path)
    assert any(h["name"] == "relay" for h in remote["health"])
    assert ingest(db, remote).ok
    out = db.ok(f"select health from public.relay_watch('{WATCH}');", role="anon")
    assert {"name": "relay", "state": "failing"} in json.loads(out)


def test_ingest_refuses_a_non_object(db: Pg) -> None:
    assert ingest(db, [1, 2]).sqlstate == "PT400"


def test_ingest_caps_the_size(db: Pg, tmp_path: Path) -> None:
    big = remote_snapshot(tmp_path, padding="x" * 33_000)
    assert ingest(db, big).sqlstate == "PT413"
    assert count(db) == 0


def test_ingest_rate_limit(db: Pg, tmp_path: Path) -> None:
    data = remote_snapshot(tmp_path)
    for _ in range(3):
        assert ingest(db, data).ok
    assert ingest(db, data).sqlstate == "PT429"
    assert count(db) == 3


def test_ingest_caps_the_row_count(db: Pg, tmp_path: Path) -> None:
    # Within 7 days and outside the rate window, so only the row cap removes them.
    db.ok(
        "insert into public.snapshots (taken_at, received_at, data)"
        " select now(), now() - interval '2 minutes', jsonb_build_object('n', g)"
        " from generate_series(1, 12005) g;"
    )
    assert ingest(db, remote_snapshot(tmp_path)).ok
    assert count(db) == 12000
    # The oldest went; the new one stayed.
    assert db.ok("select min((data->>'n')::int) from public.snapshots;") == "7"
    assert db.ok("select count(*) from public.snapshots where data->>'tier' = 'remote';") == "1"


def test_ingest_keeps_seven_days(db: Pg, tmp_path: Path) -> None:
    db.ok(
        "insert into public.snapshots (taken_at, received_at, data) values"
        " (now() - interval '8 days', now() - interval '8 days', '{}'),"
        " (now() - interval '6 days', now() - interval '6 days', '{}');"
    )
    assert ingest(db, remote_snapshot(tmp_path)).ok
    assert (
        db.ok(
            "select count(*) from public.snapshots where received_at < now() - interval '7 days';"
        )
        == "0"
    )
    assert count(db) == 2


def test_the_table_caps_size_even_for_postgres(db: Pg) -> None:
    big = json.dumps({"x": "y" * 33_000})
    r = db.run(f"insert into public.snapshots (taken_at, data) values (now(), '{big}');")
    assert r.sqlstate == "23514"


# --- the owner's read ------------------------------------------------------------------------


def _visible(db: Pg, claims: dict[str, Any]) -> int:
    return int(db.ok("select count(*) from public.snapshots;", role="authenticated", claims=claims))


def test_only_the_owner_with_mfa_reads(db: Pg, tmp_path: Path) -> None:
    assert ingest(db, remote_snapshot(tmp_path)).ok
    assert _visible(db, OWNER_AAL2) == 1
    latest = db.ok(
        "select data->>'tier' from public.snapshots order by taken_at desc limit 1;",
        role="authenticated",
        claims=OWNER_AAL2,
    )
    assert latest == "remote"
    assert _visible(db, {**OWNER_AAL2, "aal": "aal1"}) == 0  # password only
    assert _visible(db, {"sub": OWNER, "role": "authenticated"}) == 0
    assert _visible(db, {**OWNER_AAL2, "sub": STRANGER}) == 0  # another user, even with MFA
    assert _visible(db, {}) == 0
    db.ok("update relay_private.config set owner_id = null;")
    assert _visible(db, OWNER_AAL2) == 0


# --- the watch path (DB-4) -------------------------------------------------------------------


def test_watch_returns_times_and_health_states_only(db: Pg, tmp_path: Path) -> None:
    empty = db.ok(f"select count(*) from public.relay_watch('{WATCH}');", role="anon")
    assert empty == "0"
    health = [
        {"name": "model", "state": "ok", "since": None},
        {"name": "relay", "state": "failing", "since": None},
    ]
    assert ingest(db, remote_snapshot(tmp_path, health=health)).ok
    out = db.ok(f"select health from public.relay_watch('{WATCH}');", role="anon")
    assert json.loads(out) == [
        {"name": "model", "state": "ok"},
        {"name": "relay", "state": "failing"},
    ]
    # Exactly three columns: no snapshot data beyond the health states.
    row = db.ok(f"select * from public.relay_watch('{WATCH}');", role="anon")
    assert len(row.split("|")) == 3


@pytest.mark.parametrize("token", ["", INGEST, WATCH + "x"])
def test_watch_refuses_other_tokens(db: Pg, token: str) -> None:
    r = db.run(f"select * from public.relay_watch('{token}');", role="anon")
    assert r.sqlstate == "PT403", r.err


def test_one_token_cannot_be_both(db: Pg) -> None:
    r = db.run(
        f"update relay_private.config set watch_token_sha256 = decode('{_sha(INGEST)}', 'hex');"
    )
    assert r.sqlstate == "23514"
