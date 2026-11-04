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
from typing import Any

import pytest

from resonant.config import load_settings
from resonant.dashboard.snapshot import build_snapshot, to_remote
from resonant.redact import SECRET_SQL_FAMILY
from resonant.store.db import open_db

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = sorted((ROOT / "supabase" / "migrations").glob("*.sql"))
STUB = ROOT / "tests" / "fixtures" / "supabase_stub.sql"

OWNER = "11111111-1111-4111-8111-111111111111"
STRANGER = "22222222-2222-4222-8222-222222222222"
INGEST = "i" * 8 + "0123456789abcdef" * 3  # 56 chars; the real one is 64 hex
STATUS = "w" * 8 + "fedcba9876543210" * 3


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
        "truncate public.snapshots, public.snapshot_latest;"
        f"update relay_private.config set owner_id = '{OWNER}',"
        f" ingest_token_sha256 = decode('{_sha(INGEST)}', 'hex'),"
        f" status_token_sha256 = decode('{_sha(STATUS)}', 'hex');"
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


def count_latest(db: Pg) -> int:
    return int(db.ok("select count(*) from public.snapshot_latest;"))


def latest(db: Pg) -> dict[str, Any]:
    return json.loads(db.ok("select data from public.snapshot_latest;"))


def octet_length(db: Pg, table: str) -> int:
    return int(db.ok(f"select max(octet_length(data::text)) from public.{table};"))


def v1_snapshot(**changes: Any) -> dict[str, Any]:
    """What an old (schema 1) daemon pushes: the text-free tier."""
    data: dict[str, Any] = {
        "schema": 1,
        "tier": "remote",
        "taken_at": datetime.now(UTC).isoformat(),
        "version": "abcdef12",
        "health": [{"name": "model", "state": "ok", "since": None}],
        "runs": {"by_state": {"ready": 1}, "brief": []},
    }
    data.update(changes)
    return data


OWNER_AAL2 = {"sub": OWNER, "role": "authenticated", "aal": "aal2"}


# --- anon (the public key) -------------------------------------------------------------------


def test_anon_cannot_touch_the_table_or_config(db: Pg, tmp_path: Path) -> None:
    db.ok("insert into public.snapshots (taken_at, data) values (now(), '{}');")
    for sql in (
        "select * from public.snapshot_latest;",
        "insert into public.snapshot_latest (taken_at, data) values (now(), '{}');",
        "update public.snapshot_latest set data = '{}';",
        "delete from public.snapshot_latest;",
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
        "insert into public.snapshot_latest (taken_at, data) values (now(), '{}');",
        "update public.snapshot_latest set data = '{}';",
        "delete from public.snapshot_latest;",
        "select * from relay_private.config;",
        f"select public.relay_ingest('{INGEST}', '{{}}');",
        f"select public.relay_status('{STATUS}');",
    ):
        r = db.run(sql, role="authenticated", claims=OWNER_AAL2)
        assert not r.ok and r.sqlstate == "42501", (sql, r.err)


# --- the ingest path -------------------------------------------------------------------------


def test_ingest_stores_the_full_snapshot_as_latest_and_a_slim_history_row(
    db: Pg, tmp_path: Path
) -> None:
    from test_dashboard_snapshot import SECRET  # pyright: ignore[reportPrivateUsage]

    _, remote = _busy_dumps(tmp_path)
    assert remote["schema"] == 2 and SECRET in json.dumps(remote)  # the texts are in it
    assert ingest(db, remote).ok
    # The dashboard's row: the whole snapshot, texts included.
    assert latest(db) == remote
    assert (
        db.ok("select taken_at = (data->>'taken_at')::timestamptz from public.snapshot_latest;")
        == "t"
    )
    # The history row: slim. Health (name, state, since) and counts, and no text.
    assert count(db) == 1
    slim = json.loads(db.ok("select data from public.snapshots;"))
    assert set(slim) == {"schema", "tier", "taken_at", "version", "slim", "health", "counts"}
    assert slim["slim"] is True and slim["schema"] == 2 and slim["tier"] == "remote"
    assert slim["taken_at"] == remote["taken_at"] and slim["version"] == remote["version"]
    assert slim["health"] == [
        {"name": h["name"], "state": h["state"], "since": h["since"]} for h in remote["health"]
    ]
    assert slim["counts"] == {
        "runs": remote["runs"]["by_state"],
        "tasks": remote["tasks"]["by_state"],
        "approvals": remote["approvals"]["open"],
        "questions": remote["questions"]["open"],
        "pulse": remote["pulse"]["open"],
        "untold": remote["pulse"]["untold"],
    }
    assert SECRET not in json.dumps(slim)
    # The writer still can't read what it wrote.
    assert db.run("select count(*) from public.snapshots;", role="anon").sqlstate == "42501"
    assert db.run("select count(*) from public.snapshot_latest;", role="anon").sqlstate == "42501"


def test_ingest_stores_a_schema_1_push_whole(db: Pg) -> None:
    """An old daemon keeps working until it is updated: its push is the history row as it
    always was, and the latest row too (the old page reads the history)."""
    data = v1_snapshot()
    assert ingest(db, data).ok
    assert json.loads(db.ok("select data from public.snapshots;")) == data
    assert latest(db) == data
    assert json.loads(status(db).out)["health"] == [{"name": "model", "state": "ok"}]


def test_a_schema_1_push_keeps_its_old_limits(db: Pg) -> None:
    # Still text-free and still 32 KiB: the old rules for the old shape.
    changes: list[dict[str, Any]] = [
        {"trust_rules": []},
        {"repos": []},
        {"claude_brain": None},
        {"runs": {"by_state": {}, "items": []}},
        {"approvals": {"open": 0, "items": []}},
        {"questions": {"open": 0, "items": []}},
        {"pulse": {"open": 0, "items": []}},
        {"runner": {"set_up": False, "missing": ["x"]}},
        {"health": [{"name": "model", "state": "ok", "since": None, "reason": "detail"}]},
    ]
    for change in changes:
        assert ingest(db, v1_snapshot(**change)).sqlstate == "PT400", change
    assert ingest(db, v1_snapshot(padding="x" * 33_000)).sqlstate == "PT413"
    assert count(db) == 0 and count_latest(db) == 0


def test_the_latest_row_is_replaced_only_by_a_newer_snapshot(db: Pg, tmp_path: Path) -> None:
    newer = datetime.now(UTC).replace(microsecond=0)
    older = newer - timedelta(minutes=3)
    assert ingest(db, remote_snapshot(tmp_path, taken_at=newer.isoformat(), version="new")).ok
    assert ingest(db, remote_snapshot(tmp_path, taken_at=older.isoformat(), version="old")).ok
    assert latest(db)["version"] == "new"  # a late arrival doesn't replace it
    assert count(db) == 2 and count_latest(db) == 1  # but the history keeps it
    again = newer + timedelta(seconds=30)
    assert ingest(db, remote_snapshot(tmp_path, taken_at=again.isoformat(), version="newest")).ok
    assert latest(db)["version"] == "newest" and count_latest(db) == 1
    # The same taken_at (a retry whose first try landed) is just replaced (outside the rate
    # window of the pushes above).
    db.ok("update public.snapshots set received_at = received_at - interval '2 minutes';")
    assert ingest(db, remote_snapshot(tmp_path, taken_at=again.isoformat(), version="retry")).ok
    assert latest(db)["version"] == "retry"


@pytest.mark.parametrize("token", ["", "short", INGEST + "x", STATUS])
def test_ingest_refuses_a_bad_token(db: Pg, tmp_path: Path, token: str) -> None:
    r = ingest(db, remote_snapshot(tmp_path), token=token)
    assert r.sqlstate == "PT403", r.err
    assert count(db) == 0


def test_ingest_refuses_everything_until_configured(db: Pg, tmp_path: Path) -> None:
    db.ok("update relay_private.config set ingest_token_sha256 = null;")
    assert ingest(db, remote_snapshot(tmp_path)).sqlstate == "PT403"


LINK = "https://claude.ai/code/session_abc"


@pytest.mark.parametrize(
    "change",
    [
        {"tier": "local"},
        {"tier": None},
        {"schema": 3},
        {"schema": 0},
        {"schema": "2"},
        {"schema": None},
        {"slots": {"used": 0, "max": 1, "remote_url": LINK}},  # a link, at any depth
        {"claude_brain": {"state": "up", "turns": 1, "tools": [], "remote_url": LINK}},
        {"runs": {"by_state": {}, "items": [{"code": "R1", "remote_url": LINK}]}},
        {"runs": {"by_state": {}, "items": [{"code": "R1", "remote_url": None}]}},  # the key alone
        {"approvals": {"open": 1, "items": [{"id": "a", "code": "K4"}], "recent": []}},  # a code
        {"approvals": {"open": 0, "items": [], "recent": [{"id": "a", "code": "K4"}]}},
        {"approvals": {"open": 1, "items": [{"id": "b"}, {"id": "a", "code": "K4"}]}},
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
    assert count(db) == 0 and count_latest(db) == 0


def test_ingest_accepts_the_text_the_old_tier_refused(db: Pg, tmp_path: Path) -> None:
    """PER-276: trust rules, repos, run items, approval texts and the brain's tool calls are
    on the remote tier now; a null code is no code."""
    data = remote_snapshot(
        tmp_path,
        trust_rules=[{"n": 1, "text": "ask before merge in dori"}],
        repos=[{"name": "dori", "mode": "push", "base": "main", "ci": True, "slug": "me/dori"}],
        claude_brain={"state": "up", "turns": 1, "tools": []},
        runner={"set_up": False, "missing": ["no Claude runner"], "gaps": 1},
        health=[{"name": "model", "state": "ok", "since": None, "reason": "fine"}],
        approvals={"open": 1, "items": [{"id": "a", "summary": "x", "code": None}], "recent": []},
    )
    assert ingest(db, data).ok
    assert latest(db)["trust_rules"] == data["trust_rules"]


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


def test_a_local_tier_is_refused_for_its_links_and_codes(db: Pg, tmp_path: Path) -> None:
    """The one thing the two tiers differ in: a local dump with only its tier relabelled still
    holds approval codes (and a link), and is refused."""
    local, remote = _busy_dumps(tmp_path)
    assert any("code" in a for a in local["approvals"]["items"])
    relabelled = {**local, "tier": "remote"}
    assert ingest(db, relabelled).sqlstate == "PT400"
    with_link = json.loads(json.dumps(remote))
    with_link["runs"]["items"][0]["remote_url"] = LINK
    assert ingest(db, with_link).sqlstate == "PT400"
    assert count(db) == 0 and count_latest(db) == 0
    assert ingest(db, remote).ok


def test_a_busy_remote_snapshot_is_accepted(db: Pg, tmp_path: Path) -> None:
    _, remote = _busy_dumps(tmp_path)
    assert any(h["name"] == "relay" for h in remote["health"])
    assert ingest(db, remote).ok
    out = db.ok(f"select public.relay_status('{STATUS}');", role="anon")
    assert {"name": "relay", "state": "failing"} in json.loads(out)["health"]


def test_ingest_refuses_a_non_object(db: Pg) -> None:
    assert ingest(db, [1, 2]).sqlstate == "PT400"


def test_ingest_caps_the_size_at_256_kib(db: Pg, tmp_path: Path) -> None:
    big = remote_snapshot(tmp_path, padding="x" * 300_000)
    assert ingest(db, big).sqlstate == "PT413"
    assert count(db) == 0 and count_latest(db) == 0
    # Well past the old 32 KiB cap, under the new one: accepted, and the history row stays slim.
    ok = remote_snapshot(tmp_path, padding="x" * 200_000)
    assert ingest(db, ok).ok
    assert octet_length(db, "snapshot_latest") > 200_000
    assert octet_length(db, "snapshots") < 4_000


# The families relay_ingest refuses (``redact.SECRET_SQL_FAMILY``); the fuzzy ``password=``
# check is the daemon's alone.
SQL_FAMILIES = [
    "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2",
    "github_pat_" + "11ABCDEFG0abcdefghij_klmnopq",
    "sk-ant-" + "api03-Zz9Yy8Xx7Ww6Vv5Uu4Tt3Ss2Rr1Qq0Pp",
    "sk-" + "proj1234567890abcdefghijklmnop",
    "xoxb-" + "1234567890-abcdefghijklmnop",
    "AKIA" + "IOSFODNN7EXAMPLE",
    "ASIA" + "IOSFODNN7EXAMPLE",
    "AIza" + "a1B2" * 8 + "xyz",
    "glpat-" + "g1" * 10,
    "npm_" + "n1" * 18,
    "lin_api_" + "l1" * 10,
    "sb_secret_" + "s1" * 5,
    "eyJhbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0NTY3ODkwIn0" + ".SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
    "Bearer abcdefghijklmnop1234567890",
    "bearer   abcdefghijklmnop",
    "-----BEGIN RSA " + "PRIVATE KEY-----\nMIIEowIBAAKCAQEA\n-----END RSA " + "PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY",
]


@pytest.mark.parametrize("secret", SQL_FAMILIES)
def test_ingest_refuses_secret_shaped_text(db: Pg, tmp_path: Path, secret: str) -> None:
    """Whatever the daemon did, a credential in any text is refused (PT400) and nothing is
    stored in either table: in a run's ask, deep in a list, in a health reason."""
    assert re.search(SECRET_SQL_FAMILY.replace("\\y", "\\b"), secret)  # the sample is one
    places: list[dict[str, Any]] = [
        {"runs": {"by_state": {}, "items": [{"code": "R1", "ask": f"fix {secret} now"}]}},
        {"pulse": {"open": 1, "untold": 0, "items": [{"title": "a"}, {"title": secret}]}},
        {"health": [{"name": "model", "state": "ok", "since": None, "reason": f"({secret})"}]},
        {"questions": {"open": 1, "items": [{"options": [["deep", {"x": secret}]]}]}},
    ]
    for change in places:
        r = ingest(db, remote_snapshot(tmp_path, **change))
        assert r.sqlstate == "PT400" and "secret-shaped" in r.err, (change, r.err)
    assert count(db) == 0 and count_latest(db) == 0


def test_ingest_refuses_a_remote_control_link_in_text(db: Pg, tmp_path: Path) -> None:
    link = "see https://claude.ai/code/session_" + "abc123 now"
    change: dict[str, Any] = {"runs": {"by_state": {}, "items": [{"code": "R1", "summary": link}]}}
    r = ingest(db, remote_snapshot(tmp_path, **change))
    assert r.sqlstate == "PT400" and "remote control link" in r.err, r.err
    assert count(db) == 0 and count_latest(db) == 0


@pytest.mark.parametrize(
    "text",
    [
        "password=hunter2hunter2",  # the fuzzy family is the daemon's
        "Bearer token",
        "sk-short",
        "ghp_tooshort",
        "task-force-aaaaaaaaaaaaaaaaaaaaaaaa",  # not at a word start
        "resonant/r12-per-276-dashboard",
        "AKIA short",
    ],
)
def test_ingest_accepts_ordinary_text(db: Pg, tmp_path: Path, text: str) -> None:
    change: dict[str, Any] = {"runs": {"by_state": {}, "items": [{"code": "R1", "ask": text}]}}
    assert ingest(db, remote_snapshot(tmp_path, **change)).ok


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


def test_the_tables_cap_size_even_for_postgres(db: Pg) -> None:
    big = json.dumps({"x": "y" * 33_000})
    r = db.run(f"insert into public.snapshots (taken_at, data) values (now(), '{big}');")
    assert r.sqlstate == "23514"
    big = json.dumps({"x": "y" * 263_000})
    r = db.run(f"insert into public.snapshot_latest (taken_at, data) values (now(), '{big}');")
    assert r.sqlstate == "23514"
    r = db.run(
        "insert into public.snapshot_latest (singleton, taken_at, data) values"
        " (false, now(), '{}');"
    )
    assert r.sqlstate == "23514"  # one row only


# --- the owner's read ------------------------------------------------------------------------


def _visible(db: Pg, claims: dict[str, Any], table: str = "snapshots") -> int:
    return int(db.ok(f"select count(*) from public.{table};", role="authenticated", claims=claims))


def test_only_the_owner_with_mfa_reads(db: Pg, tmp_path: Path) -> None:
    assert ingest(db, remote_snapshot(tmp_path)).ok
    for table in ("snapshots", "snapshot_latest"):
        assert _visible(db, OWNER_AAL2, table) == 1
        assert _visible(db, {**OWNER_AAL2, "aal": "aal1"}, table) == 0  # password only
        assert _visible(db, {"sub": OWNER, "role": "authenticated"}, table) == 0
        assert _visible(db, {**OWNER_AAL2, "sub": STRANGER}, table) == 0  # another user, MFA too
        assert _visible(db, {}, table) == 0
    row = db.ok(
        "select data->>'tier', data->>'schema' from public.snapshot_latest;",
        role="authenticated",
        claims=OWNER_AAL2,
    )
    assert row == "remote|2"
    db.ok("update relay_private.config set owner_id = null;")
    assert _visible(db, OWNER_AAL2) == 0 and _visible(db, OWNER_AAL2, "snapshot_latest") == 0


# --- the status path (DB-4's checker, relay/README.md) ---------------------------------------


def status(db: Pg, token: str = STATUS) -> Result:
    return db.run(f"select public.relay_status('{token}');", role="anon")


def test_status_is_null_without_snapshots(db: Pg) -> None:
    r = status(db)
    assert r.ok and r.out == ""  # SQL null (psql prints nothing)
    assert db.ok(f"select public.relay_status('{STATUS}') is null;", role="anon") == "t"


def test_status_returns_the_newest_taken_at_and_health_states_only(db: Pg, tmp_path: Path) -> None:
    newer = datetime.now(UTC).replace(microsecond=0)
    health = [
        {"name": "model", "state": "ok", "since": None},
        {"name": "relay", "state": "failing", "since": None},
    ]
    assert ingest(db, remote_snapshot(tmp_path, taken_at=newer.isoformat(), health=health)).ok
    older = (newer - timedelta(minutes=3)).isoformat()
    assert ingest(db, remote_snapshot(tmp_path, taken_at=older, health=[])).ok  # arrives later
    out = json.loads(status(db).out)
    assert set(out) == {"taken_at", "health"}  # nothing else leaves
    assert datetime.fromisoformat(out["taken_at"]) == newer
    assert out["health"] == [
        {"name": "model", "state": "ok"},
        {"name": "relay", "state": "failing"},
    ]


def test_status_still_returns_health_after_a_slim_row(db: Pg, tmp_path: Path) -> None:
    """A schema-2 push is stored slim in the history, which is where the Fly checker reads:
    its health rows (name, state) must survive that."""
    _, remote = _busy_dumps(tmp_path)
    assert ingest(db, remote).ok
    assert json.loads(db.ok("select data from public.snapshots;"))["slim"] is True
    out = json.loads(status(db).out)
    assert set(out) == {"taken_at", "health"}
    assert out["health"] == [{"name": h["name"], "state": h["state"]} for h in remote["health"]]
    assert {"name": "relay", "state": "failing"} in out["health"]


def test_status_with_a_health_less_row(db: Pg) -> None:
    db.ok("insert into public.snapshots (taken_at, data) values (now(), '{}');")
    assert json.loads(status(db).out)["health"] == []


@pytest.mark.parametrize("token", ["", "short", INGEST, STATUS + "x"])
def test_status_raises_42501_on_a_wrong_token(db: Pg, token: str) -> None:
    db.ok("insert into public.snapshots (taken_at, data) values (now(), '{}');")
    r = status(db, token)
    assert not r.ok and r.sqlstate == "42501", r.err


def test_status_raises_until_configured(db: Pg) -> None:
    db.ok("update relay_private.config set status_token_sha256 = null;")
    assert status(db).sqlstate == "42501"


def test_one_token_cannot_be_both(db: Pg) -> None:
    r = db.run(
        f"update relay_private.config set status_token_sha256 = decode('{_sha(INGEST)}', 'hex');"
    )
    assert r.sqlstate == "23514"


def test_a_remote_snapshot_with_panels_is_accepted(db: Pg, tmp_path: Path) -> None:
    """PER-280: panels need no migration. Their widgets carry no ``remote_url`` key (``details``
    is flattened to rows whose keys are values), no approval codes and no secret-shaped text."""
    import sys

    sys.path.insert(0, str(ROOT / "tests"))
    from panel_helpers import panel_manifest, raw_panel, store_panel

    from resonant.store import extensions as ext_store

    now = datetime.now(UTC)
    conn = open_db(tmp_path / "panels.db")
    try:
        ext_store.save_extension(
            conn,
            ext_store.ExtensionRecord(
                name="ledger", source="repo:ledger", commit="a" * 40, tree_digest="d",
                manifest_sha="m", installed_at=now, installed_by="justin",
                manifest=panel_manifest("ledger", ("paper",)),
            ),
        )  # fmt: skip
        raw = raw_panel()
        views = raw["views"][1]["widgets"]
        views.append({"type": "details", "data": {"remote_url": "x", "list": [1, 2]}})
        store_panel(conn, "ledger", "paper", raw, now)
        snap = build_snapshot(conn, load_settings(), started_at=now, version="abcdef12")
    finally:
        conn.close()
    remote = to_remote(snap).model_dump(mode="json")
    (panel,) = remote["extensions"][0]["panels"]
    assert panel["views"] and "local_url" not in panel
    text = json.dumps(remote)
    assert '"remote_url":' not in text and "remote_url" in text  # a value, never a key
    assert ingest(db, remote).ok
    assert count_latest(db) == 1
    assert latest(db)["extensions"][0]["panels"][0]["id"] == "paper"
