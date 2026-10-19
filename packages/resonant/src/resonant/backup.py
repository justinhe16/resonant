"""Daily store backup (BK-1): ``core/backup``, the ``backup`` health check, and the CLI's
``resonant backup now|list|restore``.

How a copy is made (:func:`make_copy`):

- SQLite's online backup API (``sqlite3.Connection.backup``) from a separate connection to
  ``resonant.db``, in one step: a consistent snapshot under a WAL read transaction, so the
  daemon keeps writing meanwhile (a plain ``cp`` of a WAL database can tear).
- Into ``<home>/backups/daily/.resonant-YYYY-MM-DD.db.tmp`` (created 0600; the directory is
  0700), switched to ``journal_mode=DELETE`` so the copy is one self-contained file, then
  ``PRAGMA integrity_check``. Only an ``ok`` copy is fsynced and atomically renamed to
  ``resonant-YYYY-MM-DD.db`` (the owner's local date). A failed check deletes the temp file
  and leaves every older copy alone; retention runs only after a good copy.
- One backup at a time: ``backups/daily/.lock`` (``flock``) covers the job and
  ``resonant backup now``.

Retention (:func:`keep_days`): the newest ``backup.keep_daily`` (7) copies plus the newest
``backup.keep_weekly`` (4) Sunday copies. Only files named exactly
``resonant-YYYY-MM-DD.db`` in ``backups/daily/`` (and ``backup.extra_dir``) are ever
deleted. Anything else, such as a pre-migration copy taken by hand at deploy time or the
``backups/pre-restore/`` copies ``restore`` makes, is never touched.

``backup.extra_dir`` (off by default): the verified copy is also copied there (temp name,
integrity check, rename, 0600) with the same retention. The directory must already exist,
so an unmounted disk is a reported failure rather than a folder on the boot disk. Resonant
never uploads anything; an iCloud Drive folder is synced by macOS, the owner's choice.

State in kv: ``backup.last_ok`` ``{at, file, bytes, ms}``, ``backup.last_error``
``{at, error}``, ``backup.extra_last_ok`` / ``backup.extra_last_error`` and
``backup.armed_at`` (when backups were first enabled, so a new install doesn't alert before
its first 03:30). Audit rows ``backup.done``, ``backup.failed``, ``backup.extra_failed`` and
``backup.restored``.

Health: :func:`health_check` is the ``HealthMonitor``'s ``backup`` check. With no successful
backup for ``backup.alert_after_hours`` (36h), counted from the last success or from
``armed_at``, it degrades and the monitor texts the owner once ("hey, some issues here:
backup: no successful backup in 37h"), then "resolved" after the next good copy. A
configured ``extra_dir`` that hasn't had a good copy in as long degrades it too. Success is
silent.

Kill switch: the job runs while paused (``CoreJob.runs_while_paused``). It only reads the
store and writes under ``<home>/backups`` (and ``extra_dir``), sends nothing and spawns no
process, and a long pause shouldn't leave the owner without a recent copy (or end in a
stale-backup alert caused by the pause itself). It also runs with ``dry_run`` on.
"""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import logging
import os
import re
import shutil
import sqlite3
import time
from collections.abc import Callable, Generator, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from zoneinfo import ZoneInfo

from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set, latest_version, schema_version, utcnow

if TYPE_CHECKING:
    from resonant.config import BackupConfig, Settings
    from resonant.executor import CallContext
    from resonant.extensions.scheduler import CoreDeps, CoreJob
    from resonant.monitor import CheckResult

log = logging.getLogger(__name__)

JOB_ID = "backup"
CATCHUP = timedelta(hours=20)  # the daemon was down at 03:30: catch up later that day
TIMEOUT_S = 900.0
DAILY = "daily"
PRE_RESTORE = "pre-restore"
NAME_RE = re.compile(r"^resonant-(\d{4}-\d{2}-\d{2})\.db$")
LOCK_NAME = ".lock"
SUNDAY = 6
ERROR_MAX = 160

KV_LAST_OK = "backup.last_ok"
KV_LAST_ERROR = "backup.last_error"
KV_EXTRA_OK = "backup.extra_last_ok"
KV_EXTRA_ERROR = "backup.extra_last_error"
KV_ARMED = "backup.armed_at"

DIR_MODE = 0o700
FILE_MODE = 0o600


class BackupError(RuntimeError):
    """A backup (or restore) step failed. The message is short and code-built."""


IntegrityCheck = Callable[[sqlite3.Connection], str]


def integrity_check(conn: sqlite3.Connection) -> str:
    """``"ok"``, or the first problems ``PRAGMA integrity_check`` reports."""
    rows = [str(r[0]) for r in conn.execute("PRAGMA integrity_check").fetchall()]
    return "ok" if rows == ["ok"] else "; ".join(rows[:3]) or "no result"


# --- names, dates, retention -----------------------------------------------------------


def backup_name(day: date) -> str:
    return f"resonant-{day.isoformat()}.db"


def local_day(now: datetime, tz: str) -> date:
    return now.astimezone(ZoneInfo(tz)).date()


def daily_dir(settings: Settings) -> Path:
    return settings.backups_dir / DAILY


def pre_restore_dir(settings: Settings) -> Path:
    return settings.backups_dir / PRE_RESTORE


def copies(directory: Path) -> list[tuple[date, Path]]:
    """``(day, path)`` of every regular ``resonant-YYYY-MM-DD.db`` file, newest first.
    Symlinks and anything else are ignored (and so never pruned)."""
    found: list[tuple[date, Path]] = []
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return []
    for p in entries:
        m = NAME_RE.fullmatch(p.name)
        if m is None or p.is_symlink() or not p.is_file():
            continue
        try:
            found.append((date.fromisoformat(m.group(1)), p))
        except ValueError:
            continue
    found.sort(reverse=True)
    return found


def keep_days(days: Iterable[date], *, keep_daily: int, keep_weekly: int) -> set[date]:
    """The newest ``keep_daily`` days plus the newest ``keep_weekly`` Sundays."""
    ordered = sorted(set(days), reverse=True)
    sundays = [d for d in ordered if d.weekday() == SUNDAY]
    return set(ordered[: max(keep_daily, 0)]) | set(sundays[: max(keep_weekly, 0)])


def prune(directory: Path, *, keep_daily: int, keep_weekly: int) -> list[Path]:
    """Delete the copies :func:`keep_days` doesn't keep; returns what was deleted."""
    found = copies(directory)
    keep = keep_days((d for d, _ in found), keep_daily=keep_daily, keep_weekly=keep_weekly)
    removed: list[Path] = []
    for day, path in found:
        if day in keep:
            continue
        try:
            path.unlink()
        except FileNotFoundError:
            continue
        removed.append(path)
    return removed


# --- making a copy ---------------------------------------------------------------------


class BackupBusy(BackupError):
    """Another backup holds the lock (``resonant backup now`` at 03:30, or a run the
    scheduler timed out whose thread is still copying). Not a failure: nothing is recorded."""


def ensure_dir(path: Path) -> None:
    """Create ``path`` (and missing parents) 0700, and tighten it to 0700 if it exists."""
    path.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
    os.chmod(path, DIR_MODE)


@contextlib.contextmanager
def _locked(directory: Path) -> Generator[None]:
    """One backup or restore at a time: ``flock`` on ``<directory>/.lock`` (the daily dir;
    never in ``extra_dir``, which may be a synced folder)."""
    ensure_dir(directory)
    fd = os.open(directory / LOCK_NAME, os.O_CREAT | os.O_RDWR, FILE_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupBusy("another backup is running") from None
        yield
    finally:
        os.close(fd)


def _fsync(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _new_temp(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        path.unlink()  # left by a crash: we hold the lock, so it's nobody's
    os.close(os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, FILE_MODE))


def _cleanup(path: Path) -> None:
    for p in (path, path.with_name(path.name + "-journal")):
        with contextlib.suppress(FileNotFoundError):
            p.unlink()


def _source(db_path: Path) -> sqlite3.Connection:
    if not db_path.is_file():
        raise BackupError(f"no store at {db_path}")
    src = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    src.execute("PRAGMA busy_timeout=5000")
    return src


def make_copy(
    db_path: Path,
    directory: Path,
    name: str,
    *,
    check: IntegrityCheck = integrity_check,
) -> Path:
    """Back ``db_path`` up to ``directory/name`` (see the module doc). Blocking: run it in a
    thread, under :func:`_locked`. Raises :class:`BackupError` and leaves ``directory`` as it
    was on failure."""
    final = directory / name
    tmp = directory / f".{name}.tmp"
    try:
        ensure_dir(directory)
        _new_temp(tmp)
        src = _source(db_path)
        try:
            dst = sqlite3.connect(tmp, isolation_level=None)
            try:
                src.backup(dst)  # pages=-1: the whole database in one consistent step
                dst.execute("PRAGMA journal_mode=DELETE")
                result = check(dst)
            finally:
                dst.close()
        finally:
            src.close()
        if result != "ok":
            raise BackupError(f"integrity check failed: {result}")
        os.chmod(tmp, FILE_MODE)
        _fsync(tmp)
        os.replace(tmp, final)
        _fsync(directory)
    except BackupError:
        _cleanup(tmp)
        raise
    except (sqlite3.Error, OSError) as e:
        _cleanup(tmp)
        raise BackupError(f"{type(e).__name__}: {e}") from e
    return final


def copy_verified(path: Path, directory: Path, *, check: IntegrityCheck = integrity_check) -> Path:
    """Copy a finished backup file into an existing ``directory`` (``backup.extra_dir``):
    temp name, integrity check of the copy, rename, 0600. Called under the daily dir's lock,
    so the finished file can't be replaced while it is read."""
    if not directory.is_dir():
        raise BackupError("extra_dir does not exist (disk not mounted?)")
    final = directory / path.name
    tmp = directory / f".{path.name}.tmp"
    try:
        _new_temp(tmp)
        shutil.copyfile(path, tmp)
        os.chmod(tmp, FILE_MODE)
        conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        try:
            result = check(conn)
        finally:
            conn.close()
        if result != "ok":
            raise BackupError(f"integrity check of the extra copy failed: {result}")
        _fsync(tmp)
        os.replace(tmp, final)
        _fsync(directory)
    except BackupError:
        _cleanup(tmp)
        raise
    except (sqlite3.Error, OSError) as e:
        _cleanup(tmp)
        raise BackupError(f"{type(e).__name__}: {e}") from e
    return final


# --- one backup run --------------------------------------------------------------------


@dataclass(frozen=True)
class BackupResult:
    path: Path
    bytes: int
    ms: int
    pruned: tuple[str, ...]
    extra: Path | None = None
    extra_error: str | None = None
    prune_error: str | None = None  # retention failed; the new copy is still good

    def as_dict(self) -> dict[str, Any]:
        return {
            "file": self.path.name,
            "bytes": self.bytes,
            "ms": self.ms,
            "pruned": list(self.pruned),
            "extra": self.extra is not None,
            "extra_error": self.extra_error,
            "prune_error": self.prune_error,
        }


def _clip(text: str) -> str:
    return text if len(text) <= ERROR_MAX else text[: ERROR_MAX - 3] + "..."


def _describe(e: BaseException) -> str:
    """A short, code-built error: a BackupError's own text, else ``<Type>: <message>``."""
    return _clip(str(e) if isinstance(e, BackupError) else f"{type(e).__name__}: {e}")


def _prune(directory: Path, keep: dict[str, int]) -> tuple[list[str], str | None]:
    try:
        return [p.name for p in prune(directory, **keep)], None
    except OSError as e:
        return [], _describe(e)


def create_backup(
    settings: Settings, *, now: datetime, check: IntegrityCheck = integrity_check
) -> BackupResult:
    """The file work of one run (blocking): today's copy, retention, the extra copy, all
    under the daily dir's lock. Writes nothing to the store; :func:`record` does. Raises
    :class:`BackupBusy` when another run holds the lock, :class:`BackupError` otherwise;
    only the main copy failing fails the run."""
    cfg = settings.backup
    start = time.monotonic()
    daily = daily_dir(settings)
    try:
        name = backup_name(local_day(now, settings.timezone))
        ensure_dir(settings.backups_dir)
        ensure_dir(daily)
    except (OSError, ValueError) as e:
        raise BackupError(_describe(e)) from e
    with _locked(daily):
        path = make_copy(settings.db_path, daily, name, check=check)
        keep = {"keep_daily": cfg.keep_daily, "keep_weekly": cfg.keep_weekly}
        pruned, prune_error = _prune(daily, keep)
        extra: Path | None = None
        extra_error: str | None = None
        if cfg.extra_dir is not None:
            try:
                extra = copy_verified(path, cfg.extra_dir, check=check)
            except BackupError as e:
                extra_error = _describe(e)
            else:
                _, extra_prune_error = _prune(cfg.extra_dir, keep)
                prune_error = prune_error or extra_prune_error
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
    ms = int((time.monotonic() - start) * 1000)
    return BackupResult(path, size, ms, tuple(pruned), extra, extra_error, prune_error)


def record(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    result: BackupResult | None = None,
    error: str | None = None,
    actor: str = "core/backup",
) -> None:
    """kv and the audit row for one run: ``result`` on success, ``error`` on failure."""
    with atomic(conn):
        if result is None:
            kv_set(conn, KV_LAST_ERROR, {"at": iso(now), "error": _clip(error or "failed")})
            audit(conn, "backup.failed", requested_by=actor, error=_clip(error or "failed"))
            return
        kv_set(
            conn,
            KV_LAST_OK,
            {"at": iso(now), "file": result.path.name, "bytes": result.bytes, "ms": result.ms},
        )
        conn.execute("DELETE FROM kv WHERE key = ?", (KV_LAST_ERROR,))
        audit(
            conn,
            "backup.done",
            requested_by=actor,
            file=result.path.name,
            bytes=result.bytes,
            ms=result.ms,
            pruned=len(result.pruned),
            prune_error=result.prune_error,
        )
        if result.extra is not None:
            kv_set(conn, KV_EXTRA_OK, {"at": iso(now), "file": result.extra.name})
            conn.execute("DELETE FROM kv WHERE key = ?", (KV_EXTRA_ERROR,))
        elif result.extra_error is not None:
            kv_set(conn, KV_EXTRA_ERROR, {"at": iso(now), "error": result.extra_error})
            audit(conn, "backup.extra_failed", requested_by=actor, error=result.extra_error)


def run_backup(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    now: datetime | None = None,
    check: IntegrityCheck = integrity_check,
    actor: str = "core/backup",
) -> BackupResult:
    """One blocking backup with its kv and audit rows (``resonant backup now``). A failure
    of any kind is recorded and raised as :class:`BackupError`; :class:`BackupBusy` is
    raised without recording anything."""
    at = now or utcnow()
    try:
        result = create_backup(settings, now=at, check=check)
    except BackupBusy:
        raise
    except Exception as e:
        record(conn, now=at, error=_describe(e), actor=actor)
        if isinstance(e, BackupError):
            raise
        raise BackupError(_describe(e)) from e
    record(conn, now=at, result=result, actor=actor)
    return result


def arm(conn: sqlite3.Connection, cfg: BackupConfig, now: datetime) -> None:
    """Note when backups were enabled (the 36h clock before the first copy). Cleared while
    disabled, so turning them back on doesn't alert at once for the time they were off.
    Read at daemon start: changing ``backup.*`` needs a restart, like other config."""
    with atomic(conn):
        if not cfg.enabled:
            conn.execute("DELETE FROM kv WHERE key = ?", (KV_ARMED,))
        elif kv_get(conn, KV_ARMED) is None:
            kv_set(conn, KV_ARMED, iso(now))


def schedule(cfg: BackupConfig) -> str:
    at = cfg.local_time
    return f"{at.minute} {at.hour} * * *"


def backup_job(
    deps: CoreDeps,
    *,
    clock: Callable[[], datetime] = utcnow,
    check: IntegrityCheck = integrity_check,
) -> CoreJob | None:
    """The ``CORE_JOBS`` factory. None when ``backup.enabled`` is false."""
    from resonant.extensions.scheduler import CoreJob

    settings, conn = deps.settings, deps.conn
    arm(conn, settings.backup, clock())
    if not settings.backup.enabled:
        return None

    async def run(args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        now = clock()
        try:
            result = await asyncio.to_thread(create_backup, settings, now=now, check=check)
        except BackupBusy:
            return {"skipped": "another backup is running"}
        except Exception as e:
            record(conn, now=now, error=_describe(e))
            raise BackupError(_describe(e)) from e
        record(conn, now=now, result=result)
        return result.as_dict()

    return CoreJob(
        id=JOB_ID,
        schedule=schedule(settings.backup),
        run=run,
        catchup=CATCHUP,
        timeout_s=TIMEOUT_S,
        runs_while_paused=True,
    )


# --- read side: status and health ------------------------------------------------------


def _at(raw: object) -> datetime | None:
    value = cast(dict[str, Any], raw).get("at") if isinstance(raw, dict) else raw
    if not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.utcoffset() is not None else None


def _field(raw: object, key: str) -> Any:
    return cast(dict[str, Any], raw).get(key) if isinstance(raw, dict) else None


def status_of(conn: sqlite3.Connection, now: datetime | None = None) -> dict[str, Any]:
    """``{last_ok_at, file, age_h, last_error_at, last_error, extra_last_ok_at}`` for
    ``/status``, ``resonant status`` and ``resonant backup list``. ``age_h`` is whole hours
    since the last good copy (None before the first)."""
    at = now or utcnow()
    ok, err = kv_get(conn, KV_LAST_OK), kv_get(conn, KV_LAST_ERROR)
    last_ok, last_err = _at(ok), _at(err)
    return {
        "last_ok_at": None if last_ok is None else iso(last_ok),
        "file": _field(ok, "file"),
        "age_h": None if last_ok is None else max(0, int((at - last_ok).total_seconds() // 3600)),
        "last_error_at": None if last_err is None else iso(last_err),
        "last_error": _field(err, "error"),
        "extra_last_ok_at": (None if (x := _at(kv_get(conn, KV_EXTRA_OK))) is None else iso(x)),
    }


def _local(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d %H:%M %Z")


def health_check(conn: sqlite3.Connection, settings: Settings, now: datetime) -> CheckResult | None:
    """The monitor's ``backup`` check (see the module doc); None while disabled."""
    from resonant.monitor import CheckResult

    cfg = settings.backup
    if not cfg.enabled:
        return None
    limit = timedelta(hours=cfg.alert_after_hours)
    last_ok = _at(kv_get(conn, KV_LAST_OK))
    armed = _at(kv_get(conn, KV_ARMED))
    refs = [t for t in (last_ok, armed) if t is not None]
    if not refs:
        return CheckResult(True, "no backup yet")
    age = now - max(refs)
    if age > limit:
        hours = int(age.total_seconds() // 3600)
        detail = f"no successful backup in {hours}h"
        if (err := _field(kv_get(conn, KV_LAST_ERROR), "error")) is not None:
            detail += f" (last error: {err})"
        return CheckResult(False, detail)
    if cfg.extra_dir is not None:
        extra_ok = _at(kv_get(conn, KV_EXTRA_OK))
        extra_refs = [t for t in (extra_ok, armed) if t is not None]
        if extra_refs and now - max(extra_refs) > limit:
            err = _field(kv_get(conn, KV_EXTRA_ERROR), "error")
            return CheckResult(False, f"extra copy failing: {err or 'none made'}")
    if last_ok is None:
        return CheckResult(True, "no backup yet")
    return CheckResult(True, f"last backup {_local(last_ok, settings.timezone)}")


# --- restore ---------------------------------------------------------------------------


def verify_file(path: Path, *, check: IntegrityCheck = integrity_check) -> int:
    """Check a backup file before a restore; returns its schema version."""
    if path.is_symlink() or not path.is_file():
        raise BackupError(f"{path} is not a file")
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            result = check(conn)
            version = schema_version(conn)
            tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
        finally:
            conn.close()
    except sqlite3.Error as e:
        raise BackupError(f"not a readable SQLite database ({type(e).__name__})") from e
    if result != "ok":
        raise BackupError(f"integrity check failed: {result}")
    if "kv" not in tables or version < 1:
        raise BackupError("not a Resonant store")
    if version > latest_version():
        raise BackupError(f"schema v{version} is newer than this code (v{latest_version()})")
    return version


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _raw_save(db_path: Path, directory: Path) -> Path:
    """The pre-restore save when the live store can't be backed up (it is corrupt, which
    is when a restore is needed): the file and its ``-wal``/``-shm`` copied as they are."""
    ensure_dir(directory)
    for suffix in ("", "-wal", "-shm"):
        src = db_path.with_name(db_path.name + suffix)
        if src.is_file():
            dst = directory / src.name
            shutil.copyfile(src, dst)
            os.chmod(dst, FILE_MODE)
    return directory


def restore(
    settings: Settings,
    path: Path,
    *,
    now: datetime | None = None,
    check: IntegrityCheck = integrity_check,
) -> Path | None:
    """Replace the store's contents with ``path`` (the daemon must be stopped; the CLI
    checks). The current store is first saved to ``backups/pre-restore/`` (returned; None
    when there was no store): a checked backup, or, when the live store is too damaged for
    one, its raw files in ``pre-restore/resonant-<stamp>/``. The copy goes through SQLite's
    backup API into the live file, so its WAL and shared-memory files stay consistent.
    The restored store comes up **paused** (the copy's kill-switch state may predate the
    owner's last ``/kill``) and with a fresh ``backup.armed_at``."""
    at = now or utcnow()
    db = settings.db_path
    live = [db.with_name(db.name + s) for s in ("", "-wal", "-shm")]
    if any(_same_file(path, p) for p in live):
        raise BackupError("that is the live store itself")
    verify_file(path, check=check)
    saved: Path | None = None
    with _locked(daily_dir(settings)):
        if db.is_file():
            stamp = at.astimezone(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d-%H%M%S")
            pre = pre_restore_dir(settings)
            raw = pre / f"resonant-{stamp}"
            # Raw files first, before SQLite opens a possibly damaged store (closing it can
            # delete a WAL SQLite can't use); kept only if a checked copy can't be made.
            try:
                _raw_save(db, raw)
            except OSError as e:
                raise BackupError(f"could not save the current store: {_describe(e)}") from e
            try:
                saved = make_copy(db, pre, f"resonant-{stamp}.db", check=check)
            except BackupError:
                saved = raw
            else:
                shutil.rmtree(raw, ignore_errors=True)
        # Build the restored store in a fresh file next to the live one, so a damaged live
        # file or WAL is never opened or replayed, then swap it in (the daemon is stopped).
        tmp = db.with_name(f".{db.name}.restore.tmp")
        ensure_dir(db.parent)
        _cleanup(tmp)
        for suffix in ("-wal", "-shm"):
            _cleanup(tmp.with_name(tmp.name + suffix))
        try:
            _new_temp(tmp)
            src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            try:
                dst = sqlite3.connect(tmp, isolation_level=None)
                dst.row_factory = sqlite3.Row
                try:
                    src.backup(dst)
                    dst.execute("PRAGMA journal_mode=DELETE")
                    result = check(dst)
                    if result != "ok":
                        raise BackupError(f"integrity check after restore failed: {result}")
                    # One transaction: the restored store is never visible unpaused.
                    with atomic(dst):
                        before = {
                            "autonomy": kv_get(dst, "autonomy"),
                            "paused": kv_get(dst, "paused"),
                        }
                        kv_set(dst, "autonomy", "off")
                        kv_set(dst, "paused", True)
                        kv_set(dst, KV_ARMED, iso(at))
                        audit(dst, "backup.restored", requested_by="owner:cli", file=path.name)
                        audit(
                            dst,
                            "killswitch.engage",
                            requested_by="owner:cli",
                            reason="backup restored",
                            before=before,
                        )
                finally:
                    dst.close()
            finally:
                src.close()
            os.chmod(tmp, FILE_MODE)
            _fsync(tmp)
            for suffix in ("-wal", "-shm"):  # saved above; never replayed over the restore
                with contextlib.suppress(FileNotFoundError):
                    db.with_name(db.name + suffix).unlink()
            os.replace(tmp, db)
            _fsync(db.parent)
        except BackupError:
            _cleanup(tmp)
            raise
        except (sqlite3.Error, OSError) as e:
            _cleanup(tmp)
            raise BackupError(f"restore failed ({type(e).__name__}: {e})") from e
    return saved


__all__ = [
    "CATCHUP",
    "JOB_ID",
    "BackupBusy",
    "BackupError",
    "BackupResult",
    "backup_job",
    "backup_name",
    "copies",
    "create_backup",
    "daily_dir",
    "health_check",
    "keep_days",
    "make_copy",
    "prune",
    "restore",
    "run_backup",
    "status_of",
    "verify_file",
]
