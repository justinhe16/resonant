"""Read-only go-live checks for ``scripts/bootstrap.sh --check``.

Loads config and principals with the daemon's own loaders and prints one line per
check: ``STATUS<TAB>REQUIRED<TAB>STEP<TAB>DETAIL`` (STATUS is PASS, FAIL or SKIP ...).
It never writes anything: no files, no database, no network. The store is opened read-only,
git runs with ``--no-optional-locks`` and ``pmset`` only with ``-g``. See docs/go-live.md.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# The values config/*.example.yaml ship with. Seeded config still holding them is not live.
EXAMPLE_SELF_HANDLE = "resonant@example.com"
EXAMPLE_IDENTITIES = frozenset({"imessage:+15550000000", "imessage:owner@icloud.com"})
NOT_YET = "SKIP (available after Phase 1)"
DEFAULT_CHAT_DB = "~/Library/Messages/chat.db"
PHASE2_SCHEMA = 3  # migration 003: the Phase 2 gate's approvals columns
STALE_AFTER = timedelta(hours=1)  # pending approvals this far past expires_at are stale
CMD_TIMEOUT_S = 10.0
_CAFFEINATE_RE = re.compile(
    r"^\s*pid \d+\(caffeinate\):.*\b(PreventSystemSleep|PreventUserIdleSystemSleep)\b",
    re.MULTILINE,
)
_SLEEP_RE = re.compile(r"^\s*sleep\s+(\d+)", re.MULTILINE)


def out(status: str, required: bool, step: str, detail: str = "") -> None:
    print(f"{status}\t{int(required)}\t{step}\t{detail}", flush=True)


def _err(e: Exception) -> str:
    first = (str(e).splitlines() or [""])[0]
    return f"{type(e).__name__}: {first}"


def _run(argv: list[str]) -> str | None:
    """stdout of a read-only command, or None if it is missing or fails."""
    exe = shutil.which(argv[0])
    if exe is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [exe, *argv[1:]], capture_output=True, text=True, timeout=CMD_TIMEOUT_S, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout if proc.returncode == 0 else None


def keep_awake(assertions: str | None, settings: str | None) -> tuple[bool, str]:
    """From ``pmset -g assertions`` and ``pmset -g``: caffeinate holds a system-sleep
    assertion, or system sleep is off (``sleep 0``)."""
    held = sorted({m[1] for m in _CAFFEINATE_RE.finditer(assertions or "")})
    if held:
        return True, f"caffeinate holds {', '.join(held)}"
    sleep = _SLEEP_RE.search(settings or "")
    if sleep is not None and sleep[1] == "0":
        return True, "sleep 0"
    if assertions is None and settings is None:
        return False, "pmset -g unavailable"
    minutes = f"sleep {sleep[1]}" if sleep is not None else "sleep unknown"
    return False, f"no caffeinate assertion and {minutes} (resonant daemon install, step 2)"


def repo_state(repo: Path | None) -> tuple[bool, str]:
    """The ``/update`` checkout: on ``main`` with a clean tree."""
    if repo is None:
        return False, "ops.repo_path unset and not running from a checkout; /update refuses"
    git = ["git", "-C", str(repo), "--no-optional-locks"]
    branch = _run([*git, "rev-parse", "--abbrev-ref", "HEAD"])
    if branch is None:
        return False, f"{repo} is not a git checkout"
    branch = branch.strip()
    if branch != "main":
        where = "a detached HEAD" if branch == "HEAD" else f"branch {branch}"
        return False, f"{repo} is on {where}, not main; /update refuses (git checkout main)"
    status = _run([*git, "status", "--porcelain"])
    if status is None:
        return False, f"git status failed in {repo}"
    if status.strip():
        n = len(status.splitlines())
        return False, f"{repo} has {n} uncommitted change(s); /update refuses"
    return True, f"{repo} on main, clean"


def _store_rows(conn: sqlite3.Connection, now: datetime) -> None:
    from resonant.store.db import iso, kv_get

    autonomy: Any = kv_get(conn, "autonomy")
    paused: Any = kv_get(conn, "paused")
    if autonomy in ("on", "off") and isinstance(paused, bool):
        out("PASS", True, "kill switch state", f"autonomy {autonomy}, paused {str(paused).lower()}")
    else:
        out("FAIL", True, "kill switch state", f"autonomy {autonomy!r}, paused {paused!r}")
    if autonomy == "on":
        out("PASS", False, "autonomy on", "kill switch off")
    else:
        out("FAIL", False, "autonomy on", "kill switch engaged: everything above L0 asks (/resume)")

    cutoff = iso(now - STALE_AFTER)
    (stale,) = conn.execute(
        "SELECT COUNT(*) FROM approvals WHERE status = 'pending' AND expires_at < ?", (cutoff,)
    ).fetchone()
    if stale:
        out("FAIL", False, "approvals not stale", f"{stale} pending past expiry + 1h")
    else:
        out("PASS", False, "approvals not stale", "none")


def main() -> None:
    from resonant.config import load_settings
    from resonant.principals import load_principals

    try:
        settings = load_settings()
    except Exception as e:  # any load or validation error is a FAIL, not a crash
        out("FAIL", True, "config.yaml loads", _err(e))
        return
    cfg = settings.home / "config.yaml"
    if cfg.is_file():
        out("PASS", True, "config.yaml loads", str(cfg))
    else:
        out("FAIL", True, "config.yaml loads", f"{cfg} missing; run scripts/bootstrap.sh")

    im = settings.channels.imessage
    handle = im.self_handle
    if not handle:
        out("FAIL", True, "imessage self_handle set", "channels.imessage.self_handle is empty")
    elif handle == EXAMPLE_SELF_HANDLE:
        out("FAIL", True, "imessage self_handle set", f"{handle} is the example value")
    else:
        out("PASS", True, "imessage self_handle set", handle)

    # `enabled` / `db_path` arrive with the iMessage channel; detect them at runtime.
    enabled: object = getattr(im, "enabled", None)
    if enabled is None:
        out(NOT_YET, False, "imessage channel enabled")
    else:
        out("PASS", False, "imessage channel enabled", f"enabled: {enabled} (turn on in step 6)")

    try:
        principals = load_principals(settings.principals_path)
        owner = principals.principals[principals.owner]
        handles = [i for i in owner.identities if i.startswith("imessage:")]
        real = [i for i in handles if i not in EXAMPLE_IDENTITIES]
        if real:
            out("PASS", True, "owner has imessage identity", f"{len(real)} handle(s)")
        elif handles:
            out("FAIL", True, "owner has imessage identity", "only the example handles listed")
        else:
            out("FAIL", True, "owner has imessage identity", "no imessage: identity on owner")
    except Exception as e:
        out("FAIL", True, "owner has imessage identity", _err(e))

    db_path: object = getattr(im, "db_path", None)
    chat_db = Path(str(db_path or DEFAULT_CHAT_DB)).expanduser()
    try:
        with chat_db.open("rb") as f:
            f.read(1)
        out("PASS", True, "Full Disk Access (chat.db)", f"{chat_db} readable")
    except OSError as e:
        out("FAIL", True, "Full Disk Access (chat.db)", f"{chat_db}: {e.strerror or e}")

    # Advisory until the status relay is built (docs/followups.md).
    if settings.health.healthchecks_url:
        out("PASS", False, "healthchecks URL set", "health.healthchecks_url is set")
    else:
        out("FAIL", False, "healthchecks URL set", "health.healthchecks_url is empty")

    # Phase 2: the gate, ops and power (docs/go-live.md steps 2 and 11).
    from resonant.store.db import (
        StoreUnavailableError,
        latest_version,
        open_existing,
        schema_version,
    )

    conn: sqlite3.Connection | None = None
    try:
        conn = open_existing(settings.db_path, readonly=True)
        version = schema_version(conn)
    except (StoreUnavailableError, sqlite3.Error) as e:
        version = 0
        out("FAIL", True, f"store schema {latest_version()}", f"{e} (start the daemon: step 6)")
    else:
        out("PASS", True, f"store schema {latest_version()}", f"{settings.db_path} v{version}")

    if settings.dry_run:
        out("PASS", True, "dry_run", "true (stage A of step 11)")
    elif version >= PHASE2_SCHEMA:
        out("PASS", True, "dry_run", "false (Phase 2 gate installed)")
    else:
        out("FAIL", True, "dry_run", "dry_run off on a pre-Phase-2 install")

    if conn is not None:
        try:
            _store_rows(conn, datetime.now(UTC))
        except sqlite3.Error as e:
            out("FAIL", True, "kill switch state", _err(e))
        finally:
            conn.close()
    else:
        out("SKIP", True, "kill switch state", "no store")

    out("PASS", True, "timezone", settings.timezone)  # validated when config loads

    uv = settings.ops.uv_path
    live = not settings.dry_run
    if uv is not None and uv.is_file() and os.access(uv, os.X_OK):
        out("PASS", live, "ops.uv_path set", str(uv))
    elif uv is None:
        out("FAIL", live, "ops.uv_path set", "unset; /update refuses (set it in config.yaml)")
    else:
        out("FAIL", live, "ops.uv_path set", f"{uv} is not an executable file")

    ok, detail = repo_state(settings.ops.repo_path)
    out("PASS" if ok else "FAIL", False, "repo on main and clean", detail)

    ok, detail = keep_awake(_run(["pmset", "-g", "assertions"]), _run(["pmset", "-g"]))
    if not settings.power.keep_awake:
        detail += " (power.keep_awake is false)"
    out("PASS" if ok else "FAIL", False, "keep-awake", detail)


if __name__ == "__main__":
    main()
