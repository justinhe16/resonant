"""Read-only go-live checks for ``scripts/bootstrap.sh --check``.

Loads config and principals with the daemon's own loaders and prints one line per
check: ``STATUS<TAB>REQUIRED<TAB>STEP<TAB>DETAIL`` (STATUS is PASS, FAIL or SKIP ...).
It never writes anything: no files, no database, no network. The store is opened read-only,
git runs with ``--no-optional-locks`` and ``pmset`` only with ``-g``. Keychain secrets are
checked for existence only (``security find-generic-password`` without ``-w``), never read.
See docs/go-live.md.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import stat
import subprocess
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from resonant.config import Settings
    from resonant.store.extensions import ExtensionRecord

Row = tuple[str, bool, str, str]  # (status, required, step, detail)

# The values config/*.example.yaml ship with. Seeded config still holding them is not live.
EXAMPLE_SELF_HANDLE = "resonant@example.com"
EXAMPLE_IDENTITIES = frozenset({"imessage:+15550000000", "imessage:owner@icloud.com"})
NOT_YET = "SKIP (available after Phase 1)"
DEFAULT_CHAT_DB = "~/Library/Messages/chat.db"
PHASE2_SCHEMA = 3  # migration 003: the Phase 2 gate's approvals columns
PHASE3_SCHEMA = 4  # migration 004: extensions, ext_jobs
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


# --- Phase 3: extensions, secrets, jobs, briefing (docs/go-live.md step 12) --------------


def extensions_dir_row(root: Path, records: Sequence[ExtensionRecord]) -> Row:
    """The approved copies' dir: a real directory nobody else can write, with a copy for
    every installed extension. Missing is fine before the first install."""
    step = "extensions dir"
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return ("FAIL", True, step, f"{root} is not a directory")
    if not root.exists():
        if records:
            return ("FAIL", True, step, f"{root} missing but {len(records)} installed")
        return ("PASS", True, step, "none installed yet")
    if stat.S_IMODE(root.stat().st_mode) & 0o022:
        return ("FAIL", True, step, f"{root} is writable by group or others")
    names = {r.name for r in records}
    entries = {p.name for p in root.iterdir()}
    missing = sorted(names - entries)
    if missing:
        return ("FAIL", True, step, f"no copy for {', '.join(missing)}")
    extra = sorted(entries - names)
    detail = f"{root}, {len(names)} installed"
    if extra:
        detail += f"; not installed: {', '.join(extra)}"
    return ("PASS", True, step, detail)


def pin_rows(root: Path, records: Sequence[ExtensionRecord]) -> list[Row]:
    """Each installed copy against its pin, and its pinned ``argv0`` paths. Advisory: a
    tampered copy is already disabled and alerted by the daemon (ext update restores it)."""
    from resonant.extensions.pin import verify_copy

    rows: list[Row] = []
    for r in records:
        step = f"extension {r.name} pin"
        state = verify_copy(r, root / r.name)
        gone = sorted(p for p in set(r.argv0_paths.values()) if not os.access(p, os.X_OK))
        if r.status == "disabled_tampered":
            rows.append(("FAIL", False, step, f"disabled_tampered; resonant ext update {r.name}"))
        elif state != "ok":
            rows.append(("FAIL", False, step, f"copy {state}; resonant ext update {r.name}"))
        elif gone:
            rows.append(("FAIL", False, step, f"argv0 missing: {', '.join(gone)}"))
        else:
            pin = f"{(r.commit or 'no commit')[:8]} digest {r.tree_digest[:8]}"
            rows.append(("PASS", False, step, f"{r.status}, {pin}, copy ok"))
    return rows


def secret_rows(records: Sequence[ExtensionRecord], exists: Callable[[str], bool]) -> list[Row]:
    """Each declared secret is in the Keychain. Existence only: the value is never read.
    Required for enabled extensions, advisory for disabled ones."""
    from resonant.secrets import SecretError, secret_ref
    from resonant_sdk import Manifest

    rows: list[Row] = []
    for r in records:
        try:
            names = Manifest.model_validate(r.manifest).secrets
        except ValueError:
            continue  # the pin row and the daemon report a bad snapshot
        required = r.status == "enabled"
        for name in names:
            ref = secret_ref(r.name, name)
            step = f"secret {ref}"
            try:
                present = exists(ref)
            except SecretError as e:
                rows.append(("FAIL", required, step, e.reason))
                continue
            if present:
                rows.append(("PASS", required, step, "present (value not read)"))
            else:
                rows.append(("FAIL", required, step, f"missing: resonant secret set {ref}"))
    return rows


def _next_text(at: datetime | None, now: datetime, tz: str) -> str:
    from resonant.tools.when import format_due

    return "never" if at is None else format_due(at, now=now, tz=tz)


def jobs_row(records: Sequence[ExtensionRecord], tz: str, now: datetime) -> Row:
    """Every enabled extension's jobs have a next due time, from the approved manifest's
    cron (an ``ext_jobs`` row appears only after the first due time)."""
    from resonant.extensions.cron import CronError, parse_schedule
    from resonant_sdk import Manifest

    step = "extension jobs scheduled"
    due: list[tuple[datetime, str]] = []
    bad: list[str] = []
    for r in records:
        if r.status != "enabled":
            continue
        try:
            jobs = Manifest.model_validate(r.manifest).jobs
        except ValueError:
            bad.append(f"{r.name} (manifest)")
            continue
        for job in jobs:
            try:
                at = parse_schedule(job.schedule, job.tz or tz).next_after(now)
            except (CronError, ValueError):
                at = None
            if at is None:
                bad.append(f"{r.name}/{job.id}")
            else:
                due.append((at, f"{r.name}/{job.id}"))
    if bad:
        return ("FAIL", True, step, f"no next due time: {', '.join(bad)}")
    if not due:
        return ("PASS", True, step, "no extension jobs")
    at, first = min(due)
    return ("PASS", True, step, f"{len(due)} job(s); next {first} {_next_text(at, now, tz)}")


def briefing_row(settings: Settings, now: datetime) -> Row:
    """``core/briefing``: enabled, and its next due time from ``briefing.at``."""
    from resonant.extensions import briefing
    from resonant.extensions.cron import CronError, parse_schedule

    cfg, tz = settings.briefing, settings.timezone
    if not cfg.enabled:
        return ("FAIL", False, "briefing scheduled", "briefing.enabled is false")
    try:
        at = parse_schedule(briefing.schedule(cfg), tz).next_after(now)
    except CronError as e:
        return ("FAIL", True, "briefing scheduled", str(e))
    return (
        "PASS",
        True,
        "briefing scheduled",
        f"daily {cfg.at} {tz}; next {_next_text(at, now, tz)}",
    )


def pulse_row(settings: Settings, now: datetime) -> Row:
    """``core/pulse``: enabled, its next due time (hourly) and the quiet hours."""
    from resonant.extensions import pulse_job
    from resonant.extensions.cron import CronError, parse_schedule

    cfg, tz = settings.pulse, settings.timezone
    if not cfg.enabled:
        return ("FAIL", False, "pulse scheduled", "pulse.enabled is false")
    try:
        at = parse_schedule(pulse_job.schedule(), tz).next_after(now)
    except CronError as e:
        return ("FAIL", True, "pulse scheduled", str(e))
    return (
        "PASS",
        True,
        "pulse scheduled",
        f"hourly {tz}; quiet {cfg.quiet_hours}; next {_next_text(at, now, tz)}",
    )


def _keychain_exists(ref: str) -> bool:
    import asyncio

    from resonant.secrets import SecretStore

    return asyncio.run(SecretStore().exists(ref))


def phase3_rows(
    settings: Settings,
    conn: sqlite3.Connection | None,
    now: datetime,
    exists: Callable[[str], bool] = _keychain_exists,
) -> list[Row]:
    from resonant.store.extensions import list_extensions

    records = list_extensions(conn) if conn is not None else []
    root = settings.extensions_dir
    return [
        extensions_dir_row(root, records),
        *pin_rows(root, records),
        *secret_rows(records, exists),
        jobs_row(records, settings.timezone, now),
        briefing_row(settings, now),
        pulse_row(settings, now),
    ]


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

    now = datetime.now(UTC)
    if conn is not None:
        try:
            _store_rows(conn, now)
        except sqlite3.Error as e:
            out("FAIL", True, "kill switch state", _err(e))
    else:
        out("SKIP", True, "kill switch state", "no store")
    try:
        for row in phase3_rows(settings, conn if version >= PHASE3_SCHEMA else None, now):
            out(*row)
    except sqlite3.Error as e:
        out("FAIL", True, "extensions", _err(e))
    finally:
        if conn is not None:
            conn.close()

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
