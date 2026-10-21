"""`resonant` command-line interface.

The CLI runs locally as the machine's user, so it acts as the owner. Remote principals
reach Resonant only through channels, where identity is resolved from principals.yaml.
"""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, cast
from urllib.parse import urlsplit

import httpx
import typer
from pydantic import ValidationError

from resonant import daemon as daemon_mod
from resonant import killswitch, launchd
from resonant.config import Settings, load_settings
from resonant.extensions.checks import EXT_PATH, approver_errors, dir_errors, name_errors
from resonant.extensions.manager import (
    AdoptRequired,
    ExtensionManager,
    ManagerError,
    Pin,
    Staged,
    confirm_lines,
    heading,
    report_grant,
    update_lines,
)
from resonant.extensions.mcp import pins_digest
from resonant.extensions.pin import TreeError
from resonant.gate import ApprovalResolution, PolicyGate
from resonant.gate.codes import clean_external
from resonant.launchd import DAEMON_LABEL
from resonant.ops import (
    OpsRunner,
    SubprocessRunner,
    clear_restart,
    kickstart_argv,
    mark_restart,
    restart_later_argv,
)
from resonant.principals import Principals, load_principals
from resonant.secrets import (
    InvalidSecretRef,
    InvalidSecretValue,
    SecretError,
    SecretMissing,
    SecretStore,
    SecretUnavailable,
    secret_ref,
    validate_ref,
    validate_value,
)
from resonant.status import collect_status
from resonant.store import approvals as ap
from resonant.store import extensions as ext_store
from resonant.store.audit import audit
from resonant.store.db import (
    StoreUnavailableError,
    atomic,
    iso,
    latest_version,
    open_db,
    open_existing,
    utcnow,
)
from resonant.tools import ToolRegistry
from resonant_sdk import load_manifest
from resonant_sdk.manifest import McpHttpServer

app = typer.Typer(no_args_is_help=True, add_completion=False)
daemon_app = typer.Typer(no_args_is_help=True, help="Run and supervise the daemon.")
app.add_typer(daemon_app, name="daemon")
ext_app = typer.Typer(no_args_is_help=True, help="Extensions (see docs/extensions.md).")
app.add_typer(ext_app, name="ext")
backup_app = typer.Typer(no_args_is_help=True, help="Store backups (see docs/go-live.md).")
app.add_typer(backup_app, name="backup")

EX_TEMPFAIL = 75
_CLI_ACTOR = "owner:cli"


def _db(settings: Settings) -> sqlite3.Connection:
    return open_db(settings.db_path)


# --- daemon ----------------------------------------------------------------------------


@daemon_app.command("run")
def daemon_run() -> None:
    """Run the daemon in the foreground (launchd uses this)."""
    raise typer.Exit(daemon_mod.run(load_settings()))


@daemon_app.command("install")
def daemon_install(
    ollama: Annotated[
        bool, typer.Option(help="Also install the Ollama LaunchAgent if ollama is on PATH.")
    ] = True,
) -> None:
    """Install and start the LaunchAgents (daemon, Ollama if available, and keep-awake)."""
    settings = load_settings()
    try:
        if ollama:
            if path := launchd.find_ollama():
                typer.echo(f"installed {launchd.install(launchd.ollama_plist(settings, path))}")
            else:
                typer.echo("ollama not found on PATH; skipping Ollama LaunchAgent")
        if settings.power.keep_awake:
            typer.echo(f"installed {launchd.install(launchd.caffeinate_plist())}")
        elif launchd.uninstall(launchd.CAFFEINATE_LABEL):
            typer.echo(f"{launchd.CAFFEINATE_LABEL}: removed (power.keep_awake is false)")
        typer.echo(f"installed {launchd.install(launchd.daemon_plist(settings))}")
    except launchd.LaunchctlError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None


@daemon_app.command("uninstall")
def daemon_uninstall(
    ollama: Annotated[bool, typer.Option(help="Also remove the Ollama LaunchAgent.")] = True,
    keep_awake: Annotated[
        bool, typer.Option(help="Also remove the keep-awake (caffeinate) LaunchAgent.")
    ] = True,
) -> None:
    """Stop and remove the LaunchAgents."""
    labels = [launchd.DAEMON_LABEL] + ([launchd.OLLAMA_LABEL] if ollama else [])
    labels += [launchd.CAFFEINATE_LABEL] if keep_awake else []
    for label in labels:
        removed = launchd.uninstall(label)
        typer.echo(f"{label}: {'removed' if removed else 'not installed'}")


@daemon_app.command("logs")
def daemon_logs(
    follow: Annotated[bool, typer.Option("--follow", "-f")] = False,
    lines: Annotated[int, typer.Option("--lines", "-n")] = 100,
) -> None:
    """Show daemon logs."""
    logs = load_settings().logs_dir
    files = [str(logs / "resonant.jsonl"), str(logs / "daemon.err.log")]
    args = ["tail", "-n", str(lines), *(["-F"] if follow else []), *files]
    os.execvp("tail", args)  # noqa: S606, S607 - replaces this process with tail


# --- status & control ------------------------------------------------------------------


_STATUS_KEYS = {"version", "uptime_s", "last_tick", "dry_run", "autonomy", "paused", "tasks"}


def _fetch_status(settings: Settings) -> dict[str, Any] | None:
    url = f"http://{settings.api.host}:{settings.api.port}/api/status"
    try:
        resp = httpx.get(url, timeout=2)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    if not isinstance(data, dict) or not set(data) >= _STATUS_KEYS:  # pyright: ignore[reportUnknownArgumentType]
        return None  # something else is listening on the port
    return cast(dict[str, Any], data)


@app.command()
def status(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show daemon health, task counts, and the kill-switch state. Exit 1 if the daemon is down."""
    settings = load_settings()
    live = _fetch_status(settings)
    data: dict[str, Any]
    if live is not None:
        data = live
    else:
        try:
            data = collect_status(open_existing(settings.db_path, readonly=True), settings)
        except StoreUnavailableError as e:
            data = {"error": str(e), "tasks": {}, "waiting": {}}
    data["daemon"] = "up" if live else "down"
    data["launchd_loaded"] = launchd.is_loaded(launchd.DAEMON_LABEL)
    up = live is not None
    if as_json:
        typer.echo(json.dumps(data, indent=2))
        raise typer.Exit(0 if up else 1)
    typer.echo(f"daemon      {'up' if up else 'DOWN'} (launchd: {data['launchd_loaded']})")
    if "error" in data:
        typer.echo(f"store       {data['error']}")
        raise typer.Exit(1)
    if up:
        typer.echo(f"uptime      {data['uptime_s']}s, last tick {data['last_tick']}")
    typer.echo(f"autonomy    {data['autonomy']}{'  (PAUSED)' if data['paused'] else ''}")
    typer.echo(f"dry_run     {data['dry_run']}")
    task_counts = cast(dict[str, int], data["tasks"])
    waiting = cast(dict[str, int], data.get("waiting") or {})
    tasks = ", ".join(f"{k}={v}" for k, v in sorted(task_counts.items())) or "none"
    typer.echo(f"tasks       {tasks}")
    if waiting:
        typer.echo("waiting     " + ", ".join(f"{k}={v}" for k, v in waiting.items()))
    echo_backup(data.get("backup"))
    echo_health(data.get("health"), live=up)
    if not up:
        raise typer.Exit(1)


def echo_backup(raw: object) -> None:
    """``backup      2026-10-19T10:30:00.000+00:00 (6h ago)``, or ``none yet``."""
    if not isinstance(raw, dict):
        return
    b = cast(dict[str, Any], raw)
    line = "none yet" if b.get("last_ok_at") is None else f"{b['last_ok_at']} ({b['age_h']}h ago)"
    err_at, ok_at = b.get("last_error_at"), b.get("last_ok_at")
    if isinstance(err_at, str) and (not isinstance(ok_at, str) or err_at > ok_at):
        line += f"; last try FAILED at {err_at}: {b.get('last_error')}"
    typer.echo(f"backup      {line}")


def echo_health(health: object, *, live: bool) -> None:
    """The health section: one line per check (state, since, detail)."""
    if not isinstance(health, dict) or not health:
        return
    typer.echo("health" + ("" if live else "      (last recorded; daemon is down)"))
    for name, raw in cast(dict[str, Any], health).items():
        h = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
        state = str(h.get("state") or "unknown")
        line = f"  {name:<10}{state.upper() if state == 'degraded' else state}"
        if h.get("since") and state != "unknown":
            line += f" since {h['since']}"
        if state == "degraded" and h.get("detail"):
            line += f": {h['detail']}"
        typer.echo(line)


# --- backups (BK-1) ---------------------------------------------------------------------


def _daemon_running(settings: Settings) -> str | None:
    """Why the daemon counts as running (its API answers, or launchd has it loaded, so it
    would be restarted), or None."""
    if _fetch_status(settings) is not None:
        return "the daemon is running (its API answers)"
    if launchd.is_loaded(launchd.DAEMON_LABEL):
        return f"the daemon's LaunchAgent ({launchd.DAEMON_LABEL}) is loaded"
    if pids := _daemon_pids():
        return f"a `resonant daemon run` process is running (pid {pids[0]})"
    return None


def _daemon_pids() -> list[int]:
    """Processes of this user running ``resonant daemon run`` (a foreground daemon, or one
    whose API is hung), other than this one."""
    import psutil

    found: list[int] = []
    me = os.getpid()
    for proc in psutil.process_iter(["pid", "uids", "cmdline"]):
        try:
            info = proc.info
            cmdline = cast(list[str], info.get("cmdline") or [])
            argv = [os.path.basename(a) for a in cmdline]
            uids = info.get("uids")
        except (psutil.Error, OSError):
            continue
        if info.get("pid") == me or uids is None or uids.real != os.getuid():
            continue
        if is_daemon_argv(argv):
            found.append(int(info["pid"]))
    return found


def is_daemon_argv(argv: list[str]) -> bool:
    """``…/resonant daemon run`` or ``python -m resonant daemon run`` (basenames)."""
    return any(
        arg in ("resonant", "__main__.py") and argv[i + 1 : i + 3] == ["daemon", "run"]
        for i, arg in enumerate(argv)
    )


def _size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    value = n / 1024
    for unit in ("KB", "MB"):
        if value < 1024:
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


@backup_app.command("now")
def backup_now() -> None:
    """Back the store up now (safe while the daemon runs). Replaces today's copy."""
    from resonant import backup

    settings = load_settings()
    try:
        conn = open_existing(settings.db_path)
    except StoreUnavailableError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    try:
        result = backup.run_backup(conn, settings, actor=_CLI_ACTOR)
    except backup.BackupBusy as e:
        typer.echo(f"not now: {e}; try again in a minute", err=True)
        raise typer.Exit(1) from None
    except backup.BackupError as e:
        typer.echo(f"backup FAILED: {e} (older copies kept)", err=True)
        raise typer.Exit(1) from None
    finally:
        conn.close()
    typer.echo(f"backup ok: {result.path} ({_size(result.bytes)}, integrity ok, {result.ms} ms)")
    if result.pruned:
        typer.echo(f"pruned {len(result.pruned)}: {', '.join(result.pruned)}")
    if result.prune_error is not None:
        typer.echo(f"retention FAILED (the new copy is fine): {result.prune_error}", err=True)
    if result.extra is not None:
        typer.echo(f"extra copy: {result.extra}")
    elif result.extra_error is not None:
        typer.echo(f"extra copy FAILED: {result.extra_error}", err=True)


@backup_app.command("list")
def backup_list() -> None:
    """List the daily copies (and the extra_dir and pre-restore ones) and the last result."""
    from resonant import backup

    settings = load_settings()
    cfg = settings.backup
    with contextlib.suppress(StoreUnavailableError, sqlite3.Error):
        conn = open_existing(settings.db_path, readonly=True)
        try:
            echo_backup(backup.status_of(conn))
        finally:
            conn.close()
    dirs: list[tuple[str, Path]] = [("daily", backup.daily_dir(settings))]
    if cfg.extra_dir is not None:
        dirs.append(("extra", cfg.extra_dir))
    for label, directory in dirs:
        found = backup.copies(directory)
        typer.echo(f"{label} ({directory}): {len(found) or 'none'}")
        sundays = backup.keep_days(
            (d for d, _ in found if d.weekday() == backup.SUNDAY),
            keep_daily=0,
            keep_weekly=cfg.keep_weekly,
        )
        for day, path in found:
            tag = "  weekly" if day in sundays else ""
            typer.echo(f"  {path.name}  {_size(path.stat().st_size)}{tag}")
    pre = backup.pre_restore_dir(settings)
    if pre.is_dir() and (saved := sorted(p.name for p in pre.glob("resonant-*.db"))):
        typer.echo(f"pre-restore ({pre}, never pruned): {', '.join(saved)}")


def _restore_steps(file: Path) -> list[str]:
    return [
        "1. Stop the daemon: resonant daemon uninstall --no-ollama --no-keep-awake",
        f"2. Restore: resonant backup restore {file}",
        "   (the current store is copied to backups/pre-restore/ first)",
        "3. Start it again: resonant daemon install",
        "4. Check: resonant status (and scripts/bootstrap.sh --check)",
        "5. It comes up paused: /resume (or resonant resume) when it looks right",
    ]


@backup_app.command("restore")
def backup_restore(
    file: Annotated[Path, typer.Argument(help="A backup file, e.g. from `resonant backup list`.")],
    yes: Annotated[bool, typer.Option("--yes", help="Don't ask for confirmation.")] = False,
) -> None:
    """Replace the store with a backup. Refuses while the daemon runs."""
    from resonant import backup

    settings = load_settings()
    file = file.expanduser().absolute()
    if (running := _daemon_running(settings)) is not None:
        typer.echo(f"refused: {running}. Restore with the daemon stopped:", err=True)
        for line in _restore_steps(file):
            typer.echo(line, err=True)
        raise typer.Exit(1)
    try:
        version = backup.verify_file(file)
    except backup.BackupError as e:
        typer.echo(f"refused: {e}", err=True)
        raise typer.Exit(1) from None
    typer.echo(f"{file}: integrity ok, schema v{version}")
    if not yes and not typer.confirm(f"Replace {settings.db_path} with it?"):
        raise typer.Exit(1)
    try:
        saved = backup.restore(settings, file)
    except backup.BackupError as e:
        typer.echo(f"restore FAILED: {e}", err=True)
        raise typer.Exit(1) from None
    if saved is not None:
        typer.echo(f"the old store was saved to {saved}")
    typer.echo(f"restored {settings.db_path} from {file.name}")
    if version < latest_version():
        typer.echo(f"schema v{version}: the daemon migrates it on start")
    typer.echo("the restored store is PAUSED (kill switch on): its own state may be older")
    typer.echo("next: resonant daemon install, resonant status, then /resume when it looks right")


@app.command()
def kill(
    reason: Annotated[str | None, typer.Option(help="Recorded in the audit log.")] = None,
) -> None:
    """Engage the kill switch: autonomy off, everything paused."""
    killswitch.engage(_db(load_settings()), actor=_CLI_ACTOR, reason=reason)
    typer.echo("kill switch ENGAGED: autonomy off, critical jobs and on-call paused")


@app.command()
def resume() -> None:
    """Release the kill switch."""
    killswitch.release(_db(load_settings()), actor=_CLI_ACTOR)
    typer.echo("kill switch released")


@app.command(
    "job-guard",
    context_settings={"allow_extra_args": True, "ignore_unknown_options": True},
)
def job_guard(
    ctx: typer.Context,
    job: Annotated[str, typer.Option(help="Job id, e.g. trade-jev/daily-session.")],
) -> None:
    """Run a critical job's command unless the kill switch is engaged.

    Critical-job LaunchAgents call `resonant job-guard --job <ext>/<id> -- <command...>`.
    It fails closed: if the store is missing, its schema is behind, or the paused flag can't
    be read, the job does NOT run (exit 75). It never creates or migrates the database.
    """
    command = list(ctx.args)
    if not command:
        typer.echo("job-guard: no command given", err=True)
        raise typer.Exit(2)
    settings = load_settings()
    try:
        conn = open_existing(settings.db_path)
        paused = killswitch.is_paused(conn)
    except (StoreUnavailableError, killswitch.PausedStateUnknownError, sqlite3.Error) as e:
        typer.echo(f"job-guard: cannot confirm unpaused ({e}); not running {job}", err=True)
        raise typer.Exit(EX_TEMPFAIL) from None
    if paused:
        with contextlib.suppress(sqlite3.Error), atomic(conn):
            audit(conn, "job.skipped_paused", job=job)
        typer.echo(f"job-guard: kill switch engaged; skipping {job}", err=True)
        raise typer.Exit(EX_TEMPFAIL)
    try:
        with atomic(conn):
            audit(conn, "job.exec", job=job, command=command[0])
    except sqlite3.Error as e:  # no audit trail, no run
        typer.echo(f"job-guard: cannot write audit ({e}); not running {job}", err=True)
        raise typer.Exit(EX_TEMPFAIL) from None
    try:
        os.execvp(command[0], command)  # noqa: S606 - command comes from an approved manifest
    except OSError as e:
        with atomic(conn):
            audit(conn, "job.exec_failed", job=job, error=str(e))
        typer.echo(f"job-guard: cannot exec {command[0]}: {e}", err=True)
        raise typer.Exit(127) from None


# --- approvals -------------------------------------------------------------------------

approvals_app = typer.Typer(
    no_args_is_help=True, help="Pending approvals: list, show, approve or deny."
)
app.add_typer(approvals_app, name="approvals")

ApprovalKey = Annotated[str, typer.Argument(help="Approval id, or the code of a pending one.")]

_REFUSALS = {
    "not_found": "approval not found",
    "already_resolved": "already resolved",
    "expired": "expired",
    "task_not_waiting": "its task is no longer waiting",
    "always_unsupported": "it doesn't offer yes always",
}


def _principals(settings: Settings) -> Principals:
    from resonant.components import deny_all_principals

    path = settings.principals_path
    return load_principals(path) if path.exists() else deny_all_principals()


def _open_store(settings: Settings, *, readonly: bool) -> sqlite3.Connection:
    """The existing store, never created or migrated. Exit 75 if it's missing or behind."""
    try:
        return open_existing(settings.db_path, readonly=readonly)
    except StoreUnavailableError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EX_TEMPFAIL) from None


def _find_approval(conn: sqlite3.Connection, key: str, owner: str, now: datetime) -> ap.Approval:
    """By id first. Codes are reused over time, so a code matches only pending approvals.
    Exit 1 if nothing (or more than one approval) matches."""
    if (by_id := ap.get_approval(conn, key)) is not None:
        return by_id
    code = key.strip().upper()
    matches = [a for a in ap.pending_for(conn, owner, now) if a.code == code]
    if len(matches) == 1:
        return matches[0]
    if matches:
        ids = ", ".join(a.id for a in matches)
        typer.echo(f"error: code {code} is ambiguous ({ids}); use the id", err=True)
    else:
        typer.echo(f"error: no approval with id {key} or pending code {code}", err=True)
    raise typer.Exit(1)


def _expires_in(expires_at: datetime, now: datetime) -> str:
    s = max(0, int((expires_at - now).total_seconds()))
    if s < 60:
        return f"{s}s"
    m = s // 60
    return f"{m}m" if m < 60 else f"{m // 60}h{m % 60:02d}m"


def _tool_name(a: ap.Approval) -> str:
    return f"{a.extension}:{a.tool}" if a.extension else a.tool


def _approval_dict(a: ap.Approval, now: datetime) -> dict[str, Any]:
    return {
        "id": a.id,
        "code": a.code,
        "status": a.status,
        "task_id": a.task_id,
        "tool": a.tool,
        "extension": a.extension,
        "effect": a.effect.value,
        "level": a.level.value,
        "require_code": a.require_code,
        "summary": a.summary,
        "requested_by": a.requested_by,
        "created_at": iso(a.created_at),
        "expires_at": iso(a.expires_at),
        "expires_in_s": max(0, int((a.expires_at - now).total_seconds())),
        "decided_by": a.decided_by,
        "decided_at": iso(a.decided_at) if a.decided_at else None,
    }


@approvals_app.command("list")
def approvals_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """List pending approvals, oldest first: code, tool, expires in, summary, id."""
    settings = load_settings()
    owner = _principals(settings).owner
    with contextlib.closing(_open_store(settings, readonly=True)) as conn:
        now = utcnow()
        pending = ap.pending_for(conn, owner, now)
        batches = ap.batches_of(conn, [a.id for a in pending])  # BA-1
    if as_json:
        typer.echo(json.dumps([_approval_dict(a, now) for a in pending], indent=2))
        return
    if not pending:
        typer.echo("no pending approvals")
        return
    for a in pending:
        batch = batches.get(a.id)
        # a batch is one decision: approving or denying any row of it decides all of them
        note = f"  [batch of {len(batch.approval_ids)}: decides all]" if batch else ""
        typer.echo(
            f"{a.code or '--':<4} {_tool_name(a):<24} {_expires_in(a.expires_at, now):>6}  "
            f"{a.summary}  ({a.id}){note}"
        )


@approvals_app.command("show")
def approvals_show(
    key: ApprovalKey, as_json: Annotated[bool, typer.Option("--json")] = False
) -> None:
    """Show one approval with its full args (the summary masks them)."""
    settings = load_settings()
    owner = _principals(settings).owner
    with contextlib.closing(_open_store(settings, readonly=True)) as conn:
        now = utcnow()
        a = _find_approval(conn, key, owner, now)
        args = ap.intent_args(conn, a)
    if as_json:
        typer.echo(json.dumps({**_approval_dict(a, now), "args": args}, indent=2))
        return
    state = a.status
    if a.status == "pending":
        state += f", expires in {_expires_in(a.expires_at, now)}"
    elif a.decided_by:
        state += f" by {a.decided_by}"
    typer.echo(f"id          {a.id}")
    typer.echo(f"code        {a.code or '-'}")
    typer.echo(f"status      {state}")
    typer.echo(f"tool        {_tool_name(a)} ({a.effect.value} {a.level.value})")
    typer.echo(f"task        {a.task_id or '-'}")
    typer.echo(f"requested   by {a.requested_by or '-'} at {iso(a.created_at)}")
    typer.echo(f"summary     {a.summary}")
    shown = "(not recorded)" if args is None else json.dumps(args, indent=2, ensure_ascii=False)
    typer.echo(f"args        {shown}")


def _resolve(key: str, *, approved: bool, always: bool = False) -> None:
    verb = "approved" if approved else "denied"
    settings = load_settings()
    principals = _principals(settings)
    with contextlib.closing(_open_store(settings, readonly=False)) as conn:
        a = _find_approval(conn, key, principals.owner, utcnow())
        typer.echo(f"[{a.code or '-'}] {a.summary}")
        gate = PolicyGate(
            conn,
            principals,
            ToolRegistry(),
            dry_run=settings.dry_run,
            approval_ttl_s=settings.gate.approval_ttl_s,
        )
        result = gate.resolve(
            ApprovalResolution(
                approval_id=a.id,
                approved=approved,
                actor=principals.owner,
                actor_identity="cli",
                path="cli",
                always=always,
            )
        )
        if not result.ok:
            reason = _REFUSALS.get(result.reason or "", result.reason or "refused")
            if result.reason == "already_resolved" and (cur := ap.get_approval(conn, a.id)):
                reason = f"already {cur.status}"
            typer.echo(f"error: not {verb}: {reason}", err=True)
            raise typer.Exit(1)
        rule = _rule_saved(conn, a.id) if always else None
    if always:
        verb += " (yes always)"
    typer.echo(f"{verb}; task woken (the daemon picks it up on its next tick)")
    if rule:
        typer.echo(rule)


def _rule_saved(conn: sqlite3.Connection, approval_id: str) -> str | None:
    """``rule added: <text> (rule 7)`` for the standing allow a ``yes always`` saved."""
    from resonant.store import trust as trust_store

    row = ap.get_approval(conn, approval_id)
    if row is None or not row.always_scope or not row.always_action:
        return None  # a worker's trust check: its run reads the answer
    rules = trust_store.rules_for(conn, row.always_scope, row.always_action)
    rule = next((r for r in rules if r.effect == "allow"), None)
    return f"rule added: {rule.text} (rule {rule.id})" if rule is not None else None


@approvals_app.command("approve")
def approvals_approve(
    key: ApprovalKey,
    always: Annotated[
        bool,
        typer.Option(
            "--always",
            help="Approve and save the yes always rule its prompt offered (as a 'yes always' "
            "reply). Refused for one that offered none.",
        ),
    ] = False,
) -> None:
    """Approve a pending approval and wake its task. Exit 1 if refused."""
    _resolve(key, approved=True, always=always)


@approvals_app.command("deny")
def approvals_deny(key: ApprovalKey) -> None:
    """Deny a pending approval and wake its task. Exit 1 if refused."""
    _resolve(key, approved=False)


# --- extensions ------------------------------------------------------------------------


@ext_app.command("validate")
def ext_validate(
    path: Annotated[Path, typer.Argument(help="Extension dir or resonant.yaml")],
) -> None:
    """Validate a manifest: schema, invariants, names, the directory (stdlib only, argv[0]
    allowlist, symlinks) and approver grants (if principals exist)."""
    manifest_path = path / "resonant.yaml" if path.is_dir() else path
    try:
        manifest = load_manifest(manifest_path)
    except FileNotFoundError:
        typer.echo(f"error: {manifest_path} not found", err=True)
        raise typer.Exit(1) from None
    except ValidationError as e:
        typer.echo(f"invalid manifest {manifest_path}:\n{e}", err=True)
        raise typer.Exit(1) from None
    settings = load_settings()
    problems = name_errors(manifest) + dir_errors(manifest_path.parent, manifest)
    if settings.principals_path.exists():
        problems += approver_errors(manifest, load_principals(settings.principals_path))
    else:
        typer.echo(f"note: {settings.principals_path} missing; approver grants not checked")
    typer.echo(f"{manifest.name} v{manifest.version}")
    for spec in manifest.tool_specs():
        flag = " commit" if spec.commit else ""
        typer.echo(
            f"  tool {spec.name:<24} {spec.effect.value:<5} {spec.effective_level.value}{flag}"
        )
    for job in manifest.jobs:
        typer.echo(f"  job  {job.id:<24} {job.schedule}{'  critical' if job.critical else ''}")
    if manifest.mcp:
        for sid, server in manifest.mcp.servers.items():
            names = ", ".join(t.name for t in server.tools)
            where = (
                f"{server.url} (http)"
                if isinstance(server, McpHttpServer)
                else f"{server.command} (stdio)"
            )
            typer.echo(f"  mcp  {sid:<24} {where}: {names}")
        typer.echo("  note: ext add contacts each MCP server once to read and pin its tools")
    if grant := report_grant(manifest):
        typer.echo(f"  {grant}")
    if manifest.secrets:
        typer.echo(f"  secrets: {', '.join(manifest.secrets)}")
    for p in problems:
        typer.echo(f"error: {p}", err=True)
    if problems:
        raise typer.Exit(1)
    typer.echo("ok")


def _daemon_up(settings: Settings) -> bool:
    return _fetch_status(settings) is not None


def _restart_runner() -> OpsRunner:
    """Spawns the detached restart helper. Tests swap in a ``FakeOpsRunner``."""
    return SubprocessRunner()


def _restart_daemon(settings: Settings, conn: sqlite3.Connection, owner: str, note: str) -> None:
    """Restart through the reply-first path (restart marker + detached helper), if the
    daemon is up. Extensions are materialized once at start; there is no hot reload."""
    if not _daemon_up(settings):
        typer.echo("The daemon isn't running; it loads the change when it starts.")
        return
    mark_restart(conn, by=owner, commit=None, now=utcnow(), note=note)
    try:
        _restart_runner().spawn_detached(restart_later_argv(DAEMON_LABEL))
    except OSError as e:
        clear_restart(conn)
        typer.echo(f"warning: couldn't start the restart helper ({type(e).__name__})", err=True)
        typer.echo(f"Restart it yourself: {' '.join(kickstart_argv(DAEMON_LABEL))}", err=True)
        return
    typer.echo("Restarting the daemon; it texts you when it's back.")


def _ext_manager(settings: Settings, conn: sqlite3.Connection) -> ExtensionManager:
    import asyncio

    store = _secret_store()
    principals = _principals(settings)

    def secret_exists(ref: str) -> bool:
        return asyncio.run(store.exists(ref))

    def secret_value(ref: str) -> str | None:
        try:
            return asyncio.run(store.get(ref))
        except SecretError:
            return None

    def allow_token(server_id: str, url: str, ref: str) -> bool:
        # Asked before the review screen: listing an http server's tools needs its token,
        # and it goes only to this URL (the manifest's; redirects are refused).
        # The manifest validated url as printable ASCII with no spaces.
        typer.echo(f"MCP server {server_id} at {url} needs {ref} to list its tools.")
        return typer.confirm(f"Send {ref} to {urlsplit(url).hostname} now?", default=False)

    return ExtensionManager(
        conn,
        extensions_dir=settings.extensions_dir,
        data_dir=settings.ext_data_dir,
        repo_path=settings.ops.repo_path,
        principals=principals,
        secret_exists=secret_exists,
        secret_value=secret_value,
        allow_token=allow_token,
        # where `gh`/`git` argv0 is found and pinned at install (the owner's terminal); the
        # child's own PATH is extensions.process.path (system dirs only)
        ext_path=EXT_PATH,
        actor=principals.owner,
    )


@contextlib.contextmanager
def _ext_errors() -> Generator[None]:
    """Manager and source refusals as ``error:`` lines: exit 1, or 75 for the Keychain."""
    try:
        yield
    except SecretError as e:
        typer.echo(f"error: the Keychain is unavailable ({e.reason}); nothing changed", err=True)
        raise typer.Exit(EX_TEMPFAIL) from None
    except TreeError as e:  # SourceError included
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None
    except ManagerError as e:
        typer.echo(f"error: {e}", err=True)
        for p in e.problems:
            typer.echo(f"  {p}", err=True)
        if isinstance(e, AdoptRequired):
            typer.echo("Re-run with --adopt to take them over.", err=True)
        raise typer.Exit(1) from None


def _typed_confirm(name: str, verb: str) -> None:
    typed = typer.prompt(f"Type {name} to {verb}", default="", show_default=False)
    if typed.strip() != name:
        typer.echo("Not confirmed; nothing changed.", err=True)
        raise typer.Exit(1)


def _router_check(settings: Settings, conn: sqlite3.Connection, staged: Staged) -> None:
    """Advisory only (docs/phase3-plan.md D2): the router eval on the repo's example set
    with and without ``extension:<name>``, printing builtin accuracy before and after. A
    drop is a warning, never a refusal; no model or no set skips it with one line."""
    from resonant.evals import router_eval

    m = staged.manifest
    if not m.description or not (m.tools or m.mcp):
        typer.echo(
            f"Router: no description or tools, so chat won't route to {staged.name}; "
            f"/{staged.name} works."
        )
        return
    typer.echo(f"Router: new intent extension:{staged.name}")
    try:
        before = router_eval.installed_intents(conn, exclude=staged.name)
        lines = router_eval.advisory(settings, m, before=before)
    except Exception as e:  # advisory: never blocks an install or update
        lines = [f"Router: advisory eval skipped ({type(e).__name__})"]
    for line in lines:
        typer.echo(line)


@ext_app.command("add")
def ext_add(
    source: Annotated[str, typer.Argument(help="repo:<name> or path:/abs/dir (a git work tree)")],
    adopt: Annotated[
        bool, typer.Option("--adopt", help="Take over data and secrets from another source.")
    ] = False,
) -> None:
    """Install an extension after you review it and type its name. Terminal only."""
    settings = load_settings()
    with contextlib.closing(_open_store(settings, readonly=False)) as conn, _ext_errors():
        mgr = _ext_manager(settings, conn)
        staged = mgr.stage(source)
        try:
            if ext_store.get_extension(conn, staged.name) is not None:
                typer.echo(
                    f"error: {staged.name} is installed; use: resonant ext update {staged.name}",
                    err=True,
                )
                raise typer.Exit(1)
            if (reasons := mgr.leftovers(staged)) and not adopt:
                raise AdoptRequired(f"{staged.name}: leftovers from another source", reasons)
            for line in confirm_lines(staged, mgr.registered_slugs()):
                typer.echo(line)
            _router_check(settings, conn, staged)
            _typed_confirm(staged.name, "install")
        finally:
            mgr.discard(staged)
        mgr.install(staged.pin, name=staged.name, adopt=adopt)
        typer.echo(f"Installed {heading(staged)}")
        _restart_daemon(settings, conn, mgr.actor, f"Installed {staged.name}")


@ext_app.command("update")
def ext_update(name: Annotated[str, typer.Argument(help="An installed extension")]) -> None:
    """Re-stage an extension from its source, show what changed, and re-approve it."""
    settings = load_settings()
    with contextlib.closing(_open_store(settings, readonly=False)) as conn, _ext_errors():
        mgr = _ext_manager(settings, conn)
        record = ext_store.get_extension(conn, name)
        if record is None:
            raise ManagerError(f"{name} is not installed")
        staged = mgr.stage(record.source, expect_name=name)
        try:
            copy = mgr.verify(record)
            current = Pin(
                record.source,
                record.commit,
                record.tree_digest,
                record.manifest_sha,
                pins_digest(record.mcp_pins),
            )
            if staged.pin == current and copy == "ok" and record.status != "disabled_tampered":
                typer.echo(f"{name} is up to date ({heading(staged)})")
                return
            for line in update_lines(
                record, mgr.installed_files(name), staged, mgr.registered_slugs()
            ):
                typer.echo(line)
            if copy != "ok":
                typer.echo(f"note: the installed copy is {copy} since you approved it")
            typer.echo("")
            for line in confirm_lines(staged, mgr.registered_slugs()):
                typer.echo(line)
            _router_check(settings, conn, staged)
            _typed_confirm(name, "update")
        finally:
            mgr.discard(staged)
        mgr.install(staged.pin, name=name)
        typer.echo(f"Updated {heading(staged)}")
        _restart_daemon(settings, conn, mgr.actor, f"Updated {name}")


@ext_app.command("remove")
def ext_remove(
    name: Annotated[str, typer.Argument(help="An installed extension")],
    purge: Annotated[bool, typer.Option("--purge", help="Also delete its data dir.")] = False,
) -> None:
    """Remove an extension: its row, jobs and copy. Keeps its data and Keychain items."""
    settings = load_settings()
    with contextlib.closing(_open_store(settings, readonly=False)) as conn, _ext_errors():
        mgr = _ext_manager(settings, conn)
        if ext_store.get_extension(conn, name) is None:
            raise ManagerError(f"{name} is not installed")
        _typed_confirm(name, "remove" + (" it and delete its data" if purge else ""))
        removed = mgr.remove(name, purge=purge)
        typer.echo(f"Removed {name}")
        if removed.data_dir is not None:
            typer.echo(f"Kept its data: {removed.data_dir} (remove --purge deletes it)")
        if removed.secrets_left:
            typer.echo("Keychain items left (delete with resonant secret rm):")
            for ref in removed.secrets_left:
                typer.echo(f"  resonant secret rm {ref}")
        _restart_daemon(settings, conn, mgr.actor, f"Removed {name}")


def _clean_repos(manifest: dict[str, Any]) -> list[str]:
    repos_: list[Any] = manifest.get("repos") or []
    return [clean_external(str(x))[:80] for x in repos_]


@ext_app.command("list")
def ext_list(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Installed extensions: status, pins, and whether the copy still matches its pin."""
    settings = load_settings()
    with contextlib.closing(_open_store(settings, readonly=True)) as conn:
        mgr = _ext_manager(settings, conn)
        rows = [(r, mgr.verify(r)) for r in ext_store.list_extensions(conn)]
    out = [
        {
            "name": r.name,
            "status": r.status,
            "source": r.source,
            "commit": r.commit,
            "tree_digest": r.tree_digest,
            "copy": copy,
            "tools": len(r.manifest.get("tools") or {}),
            "jobs": len(r.manifest.get("jobs") or []),
            "repos": _clean_repos(r.manifest),
            "installed_at": iso(r.installed_at),
        }
        for r, copy in rows
    ]
    if as_json:
        typer.echo(json.dumps(out, indent=2))
        return
    if not out:
        typer.echo("no extensions installed")
        return
    for r, copy in rows:
        sha = (r.commit or "no-commit")[:8]
        typer.echo(
            f"{r.name:<16} {r.status:<18} {r.source}@{sha} digest {r.tree_digest[:8]}"
            f"  copy {copy}  tools {len(r.manifest.get('tools') or {})}"
            f" jobs {len(r.manifest.get('jobs') or [])}  {iso(r.installed_at)}"
            + (f"  repos {', '.join(_clean_repos(r.manifest))}" if r.manifest.get("repos") else "")
        )


# --- secrets ---------------------------------------------------------------------------

secret_app = typer.Typer(
    no_args_is_help=True,
    help=(
        "Extension and runner secrets in the login Keychain (service resonant, account "
        "<ext>/<name>; the runner's are runner/<name>)."
    ),
)
app.add_typer(secret_app, name="secret")
SecretRef = Annotated[
    str, typer.Argument(help="<ext>/<name>, like github/token or runner/claude_token")
]


def _secret_store() -> SecretStore:
    """The store the ``secret`` commands use. Tests swap in one over a ``FakeKeychain``."""
    return SecretStore()


def _stdin_is_tty() -> bool:
    import sys

    return sys.stdin.isatty()


def _checked_ref(ref: str) -> str:
    try:
        return validate_ref(ref)
    except InvalidSecretRef as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(2) from None


def _read_secret_value(ref: str, from_file: str | None = None) -> str:
    """From ``--from-file`` (a path, or ``-`` for all of stdin; read here, so a multi-line
    value like a PEM never touches a command line), else a hidden prompt (twice) on a
    terminal, else one line of stdin. Never echoed."""
    import getpass
    import sys

    if from_file is not None:
        try:
            text = (
                sys.stdin.read()
                if from_file == "-"
                else Path(from_file).expanduser().read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError) as e:
            typer.echo(f"error: can't read {from_file}: {type(e).__name__}", err=True)
            raise typer.Exit(2) from None
        return text.replace("\r\n", "\n").strip("\n")
    if _stdin_is_tty():
        value = getpass.getpass(f"Value for {ref} (hidden): ")
        if getpass.getpass("Again: ") != value:
            typer.echo("error: the two entries didn't match; nothing stored", err=True)
            raise typer.Exit(1)
        return value
    return sys.stdin.readline().removesuffix("\n").removesuffix("\r")


@secret_app.command("set")
def secret_set(
    ref: SecretRef,
    from_file: Annotated[
        str | None,
        typer.Option(
            "--from-file",
            help="Read the value from this file (or - for stdin); it may be several lines, "
            "like a PEM, up to about 2.5 KB. Delete the file afterwards.",
        ),
    ] = None,
) -> None:
    """Store a secret. The value comes from a hidden prompt, stdin or --from-file, never the
    command line."""
    import asyncio
    import hmac

    ref = _checked_ref(ref)
    value = _read_secret_value(ref, from_file)
    try:
        validate_value(value)
    except InvalidSecretValue as e:
        typer.echo(f"error: value {e}; nothing stored", err=True)
        raise typer.Exit(2) from None
    store = _secret_store()

    async def go() -> bool:
        await store.set(ref, value)
        try:
            readback = await store.get(ref)
        except SecretMissing:
            readback = ""
        if hmac.compare_digest(readback.encode(), value.encode()):
            return True
        await store.delete(ref)
        return False

    try:
        verified = asyncio.run(go())
    except SecretError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EX_TEMPFAIL) from None
    if not verified:
        typer.echo(f"error: {ref} didn't read back as entered; removed it", err=True)
        raise typer.Exit(1)
    typer.echo(f"stored {ref}")


def _declared_refs(paths: list[Path]) -> list[tuple[str, str | None]]:
    """``(ref, problem)`` for every secret the given extensions declare, sorted by ref."""
    refs: dict[str, str | None] = {}
    for path in paths:
        manifest_path = path / "resonant.yaml" if path.is_dir() else path
        try:
            manifest = load_manifest(manifest_path)
        except (OSError, ValueError):  # pydantic's ValidationError is a ValueError
            typer.echo(f"note: skipped {manifest_path}: not a valid manifest", err=True)
            continue
        for name in manifest.secrets:
            try:
                refs[secret_ref(manifest.name, name)] = None
            except InvalidSecretRef:
                refs[f"{manifest.name}/{name}"] = "invalid name"
    return sorted(refs.items())


@secret_app.command("list")
def secret_list(
    dirs: Annotated[
        list[Path] | None,
        typer.Argument(help="Extension dirs to include besides the installed ones"),
    ] = None,
) -> None:
    """Declared secrets and whether each is present. Names only: no value is read."""
    import asyncio

    installed = load_settings().home / "extensions"
    found = sorted(installed.glob("*/resonant.yaml")) if installed.is_dir() else []
    refs = _declared_refs([*found, *(dirs or [])])
    if not refs:
        typer.echo("no declared secrets (installed extensions, or the dirs given)")
        return
    store = _secret_store()
    unavailable = False
    for ref, problem in refs:
        state = problem
        if state is None:
            try:
                state = "present" if asyncio.run(store.exists(ref)) else "missing"
            except SecretError as e:
                state, unavailable = e.reason, True
        typer.echo(f"  {ref:<40} {state}")
    if unavailable:
        raise typer.Exit(EX_TEMPFAIL)


@secret_app.command("check")
def secret_check(ref: SecretRef) -> None:
    """Read the secret the way the daemon does (no prompt, 5 s timeout). Shows only the outcome."""
    import asyncio

    ref = _checked_ref(ref)
    try:
        asyncio.run(_secret_store().get(ref))
    except SecretMissing:
        typer.echo(f"{ref}: missing. Set it with: resonant secret set {ref}", err=True)
        raise typer.Exit(1) from None
    except SecretUnavailable as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EX_TEMPFAIL) from None
    typer.echo(f"{ref}: ok (no prompt)")


@secret_app.command("rm")
def secret_rm(ref: SecretRef) -> None:
    """Delete a secret from the Keychain."""
    import asyncio

    ref = _checked_ref(ref)
    try:
        removed = asyncio.run(_secret_store().delete(ref))
    except SecretError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(EX_TEMPFAIL) from None
    if not removed:
        typer.echo(f"{ref}: not found", err=True)
        raise typer.Exit(1)
    typer.echo(f"removed {ref}")


# --- model -----------------------------------------------------------------------------

model_app = typer.Typer(no_args_is_help=True, help="Local LLM (Ollama): probe and benchmark.")
app.add_typer(model_app, name="model")

MODEL_PROBE_KV = "model.probe"


def _run_probe(settings: Settings) -> dict[str, Any]:
    # Imported lazily: the openai SDK is slow to import and only these commands need it.
    import asyncio

    from resonant.models import OllamaClient

    async def go() -> dict[str, Any]:
        async with OllamaClient(settings.model) as client:
            result = await client.probe()
        return result.as_dict()

    data = asyncio.run(go())
    data.update(model=settings.model.name, api=settings.model.api)
    _record_probe(settings, data)
    return data


def _record_probe(settings: Settings, data: dict[str, Any]) -> None:
    """Best-effort: keep the last probe in kv for status/tools. Never creates the store."""
    from resonant.store.db import kv_set, now_iso

    with contextlib.suppress(StoreUnavailableError, sqlite3.Error):
        conn = open_existing(settings.db_path)
        try:
            with atomic(conn):
                kv_set(conn, MODEL_PROBE_KV, {**data, "checked_at": now_iso()})
        finally:
            conn.close()


def _echo_probe(data: dict[str, Any]) -> None:
    def yn(v: object, good: str, bad: str) -> str:
        return good if v else bad

    typer.echo(f"model       {data['model']} ({data['api']} adapter)")
    typer.echo(f"present     {yn(data['model_present'], 'yes', 'NO')}")
    if data["model_present"]:
        typer.echo(f"context     {yn(data['ctx_ok'], 'ok', 'TOO SMALL / unknown')}")
        typer.echo(f"thinking    {yn(data['thinking_off_ok'], 'off', 'NOT OFF')}")
        typer.echo(f"tool call   {yn(data['tool_call_ok'], 'ok', 'FAILED (see detail)')}")
    if data["latency_ms"] is not None:
        typer.echo(f"latency     {data['latency_ms']} ms (1-token JSON call)")
    if data["detail"]:
        typer.echo(f"detail      {data['detail']}")


@model_app.command("probe")
def model_probe(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check the model is pulled, its context is big enough, thinking is off, and a tool call
    comes back with no reasoning. Exit 1 if not."""
    data = _run_probe(load_settings())
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        _echo_probe(data)
    if not data["ok"]:
        raise typer.Exit(1)


@model_app.command("bench")
def model_bench(
    n: Annotated[int, typer.Option("--n", min=1, help="Timed calls per prompt kind.")] = 20,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Probe, warm up, then time label-style JSON and single-tool calls (p50/p95)."""
    import asyncio

    from resonant.models import OllamaClient
    from resonant.models.bench import run_bench

    settings = load_settings()
    probe = _run_probe(settings)
    if not probe["ok"]:
        if as_json:
            typer.echo(json.dumps({"probe": probe}, indent=2))
        else:
            _echo_probe(probe)
        raise typer.Exit(1)

    async def go() -> list[dict[str, Any]]:
        async with OllamaClient(settings.model) as client:
            return [s.summary() for s in await run_bench(client, n)]

    results = asyncio.run(go())
    if as_json:
        typer.echo(json.dumps({"probe": probe, "results": results}, indent=2))
    else:
        typer.echo(f"model {settings.model.name} ({settings.model.api} adapter), n={n}, warmed up")
        for r in results:
            if "p50_ms" in r:
                typer.echo(
                    f"  {r['kind']:<6} p50 {r['p50_ms']:>6} ms   p95 {r['p95_ms']:>6} ms"
                    f"   failures {r['failures']}/{n}"
                )
            else:
                typer.echo(f"  {r['kind']:<6} all {n} calls failed")
    if any("p50_ms" not in r for r in results):
        raise typer.Exit(1)


# --- self-tests ------------------------------------------------------------------------

selftest_app = typer.Typer(no_args_is_help=True, help="End-to-end checks of real channels.")
app.add_typer(selftest_app, name="selftest")


@selftest_app.command("imessage")
def selftest_imessage(
    timeout: Annotated[
        float, typer.Option(min=1.0, help="Seconds to wait for the message in chat.db.")
    ] = 10.0,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Send a nonce to self_handle and read it back from chat.db. Exit 0 if ok, else 1.

    Safe next to a running daemon: it uses its own reader with its own cursor, and both
    readers drop self-test messages, so nothing is routed or answered. The result is
    stored in kv `imessage.selftest`.
    """
    import asyncio

    from resonant.components import deny_all_principals
    from resonant.gateway.imessage.channel import IMessageChannel

    settings = load_settings()
    path = settings.principals_path
    principals = load_principals(path) if path.exists() else deny_all_principals()
    conn = _db(settings)
    try:
        channel = IMessageChannel(settings.channels.imessage, principals, conn)
        result = asyncio.run(channel.self_test(timeout_s=timeout))
    finally:
        conn.close()
    if as_json:
        typer.echo(json.dumps({"ok": result.ok, "detail": result.detail}, indent=2))
    else:
        typer.echo(f"imessage self-test {'ok' if result.ok else 'FAILED'}: {result.detail}")
    if not result.ok:
        raise typer.Exit(1)


# --- evals -----------------------------------------------------------------------------

eval_app = typer.Typer(no_args_is_help=True, help="Evals on your own sets (see evals/README.md).")
app.add_typer(eval_app, name="eval")


@eval_app.command("router")
def eval_router(
    set_path: Annotated[
        Path | None,
        typer.Option("--set", help="JSONL set. Default: ~/.resonant/evals/router.jsonl."),
    ] = None,
    repeat: Annotated[int, typer.Option(min=1, help="Run the whole set N times.")] = 1,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    min_accuracy: Annotated[
        float, typer.Option(min=0.0, max=1.0, help="Exit 1 if accuracy is below this.")
    ] = 0.9,
) -> None:
    """Route each text through the production router; print accuracy, confusion and latency.

    Texts appear only in this command's output. The summary saved to
    ~/.resonant/evals/results/<ts>.json refers to examples by line number. Exit 1 if
    accuracy < --min-accuracy, 2 if the set is missing or malformed.
    """
    from resonant.evals import router_eval

    code = router_eval.run(
        load_settings(),
        set_path=set_path,
        repeat=repeat,
        as_json=as_json,
        min_accuracy=min_accuracy,
        echo=typer.echo,
        echo_err=lambda s: typer.echo(s, err=True),
    )
    raise typer.Exit(code)


@eval_app.command("task")
def eval_task(
    set_path: Annotated[
        Path | None,
        typer.Option(
            "--set",
            help="JSONL set, or 'owner' (~/.resonant/evals/task.jsonl). Default: the owner's"
            " set, else evals/task.example.jsonl.",
        ),
    ] = None,
    model: Annotated[
        str,
        typer.Option(help="live: the configured local model. scripted: each case's script (CI)."),
    ] = "live",
    mode: Annotated[
        str, typer.Option(help="loop, intents, or both (runs the set once per mode).")
    ] = "loop",
    repeat: Annotated[int, typer.Option(min=1, help="Run the whole set N times.")] = 1,
    only: Annotated[
        str | None, typer.Option(help="Only the case with this id, or this category.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
    min_accuracy: Annotated[
        float, typer.Option(min=0.0, max=1.0, help="Exit 1 if a mode's accuracy is below this.")
    ] = 0.85,
    out: Annotated[
        Path | None,
        typer.Option(help="Where the summary JSON goes. Default: ~/.resonant/evals/results/."),
    ] = None,
    route: Annotated[
        str,
        typer.Option(help="brain.route for the run: triage (default), local or claude (BR-10)."),
    ] = "triage",
    live_claude: Annotated[
        bool,
        typer.Option(
            "--live-claude",
            help="With --route claude: hand the hard rows to a real Claude brain session (your"
            " claude login) and grade its answers. Manual only; never in CI.",
        ),
    ] = False,
    limit: Annotated[
        int | None,
        typer.Option(
            min=1, help="Only the first N cases (with --live-claude: N hard rows, default 5)."
        ),
    ] = None,
) -> None:
    """Run each text through the brain in a fresh, hermetic home; grade tools, escalation,
    asks, facts and (BR-10) who answered (docs/brain-plan.md §4).

    Each case gets a temp home with its seeded world: never your state, ~/code or the
    Keychain. Writes other than notes, reminders and facts are recorded, not run, and
    ask_claude starts no Claude session, except with --live-claude (PER-250): the hard rows go
    to a real Claude brain session, with Resonant's tools backed by the case's world. Texts
    and replies appear only in this command's output; the saved summary refers to cases by
    id. Exit 1 if a mode's accuracy is below --min-accuracy, a write ran without an approval,
    or a case timed out; 2 if the set is missing or malformed or an option is bad.
    """
    from resonant.evals import task_eval

    if model not in ("live", "scripted"):
        typer.echo("--model must be 'live' or 'scripted'", err=True)
        raise typer.Exit(2)
    modes = {"loop": ["loop"], "intents": ["intents"], "both": ["loop", "intents"]}.get(mode)
    if modes is None:
        typer.echo("--mode must be 'loop', 'intents' or 'both'", err=True)
        raise typer.Exit(2)
    if route not in task_eval.ROUTES:
        typer.echo("--route must be 'triage', 'local' or 'claude'", err=True)
        raise typer.Exit(2)
    code = task_eval.run(
        load_settings(),
        set_path=set_path,
        model_kind=model,
        modes=cast("list[task_eval.Mode]", modes),
        repeat=repeat,
        only=only,
        as_json=as_json,
        min_accuracy=min_accuracy,
        out_dir=out,
        echo=typer.echo,
        echo_err=lambda s: typer.echo(s, err=True),
        route=route,
        live_claude=live_claude,
        limit=limit,
    )
    raise typer.Exit(code)


@eval_app.command("draft")
def eval_draft(
    from_history: Annotated[
        bool, typer.Option("--from-history", help="Draft from your past texts in the store.")
    ] = False,
    since: Annotated[
        str | None,
        typer.Option(help="Only texts since: a date (2026-09-01), an ISO time, or 30d/12h/2w."),
    ] = None,
    limit: Annotated[int, typer.Option(min=1, help="Draft at most N new rows (newest).")] = 200,
    out: Annotated[
        Path | None,
        typer.Option(help="The set to write. Default: ~/.resonant/evals/task.jsonl."),
    ] = None,
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Draft task-eval rows from your past texts, each marked review: true (PER-248).

    Reads the store read-only: your chat texts, the tools each turn called (the audit log),
    the approvals you answered and the reply. Writes suggested expectations and a world to
    replay against; skips approval replies, texts holding a credential and near-duplicates;
    strips approval codes and other people's handles. Rows already in the file are kept as
    they are. Nothing leaves the Mini. Prints counts only, never a text. Exit 2 if there is
    no store or the existing set is malformed.
    """
    from resonant.evals import history

    if not from_history:
        typer.echo("only --from-history is supported: resonant eval draft --from-history", err=True)
        raise typer.Exit(2)
    code = history.run(
        load_settings(),
        since=since,
        limit=limit,
        out=out,
        as_json=as_json,
        echo=typer.echo,
        echo_err=lambda s: typer.echo(s, err=True),
    )
    raise typer.Exit(code)


# --- benchmarks ------------------------------------------------------------------------

bench_app = typer.Typer(no_args_is_help=True, help="End-to-end latency benchmarks.")
app.add_typer(bench_app, name="bench")


@bench_app.command("e2e")
def bench_e2e(
    n: Annotated[int, typer.Option("--n", min=1, help="Timed prompts per path.")] = 10,
    model: Annotated[
        str,
        typer.Option(help="fake: scripted model (CI). real: the configured Ollama model."),
    ] = "fake",
    as_json: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    """Time chat.db row -> send call through the real router, runner and channel (p50/p95).

    Runs in a throwaway home with a fake chat.db and a recording osascript runner, so it
    never texts anyone and is safe next to a live daemon. Exit 1 if a prompt got no reply
    or a p50 misses its target (fast <= 1s, llm <= 3s, loop <= 5s with brain.mode loop);
    2 for a bad --model.
    """
    import asyncio

    from resonant.e2e.bench import report, run_e2e_bench

    if model not in ("fake", "real"):
        typer.echo("--model must be 'fake' or 'real'", err=True)
        raise typer.Exit(2)

    async def go() -> dict[str, Any]:
        if model == "fake":  # needs no local config
            return report(await run_e2e_bench(n), model_name="fake (scripted)")
        from resonant.models import OllamaClient

        settings = load_settings()

        async with OllamaClient(settings.model) as client:
            results = await run_e2e_bench(n, model=client, base=settings)
        return report(results, model_name=f"{settings.model.name} ({settings.model.api})")

    data = asyncio.run(go())
    if as_json:
        typer.echo(json.dumps(data, indent=2))
    else:
        typer.echo(f"e2e bench, model {data['model']}, n={n} per path, warmed up")
        for r in data["results"]:
            verdict = "ok" if r["pass"] else "MISS"
            if "p50_ms" in r:
                typer.echo(
                    f"  {r['path']:<5} p50 {r['p50_ms']:>6} ms   p95 {r['p95_ms']:>6} ms"
                    f"   target p50 <= {r['target_p50_ms']} ms   failures {r['failures']}/{n}"
                    f"   {verdict}"
                )
            else:
                typer.echo(f"  {r['path']:<5} no replies ({r['failures']}/{n} timed out)   MISS")
    if not data["pass"]:
        raise typer.Exit(1)


# --- sandbox ---------------------------------------------------------------------------

sandbox_app = typer.Typer(
    no_args_is_help=True,
    help="The workers' thin guard (docs/employee-plan.md D2), used when runner.guard is thin.",
)
app.add_typer(sandbox_app, name="sandbox")


@sandbox_app.command("check")
def sandbox_check(
    scope: Annotated[
        str, typer.Option(help="all, ci (host-independent probes) or local (needs the Mini).")
    ] = "all",
    as_json: Annotated[bool, typer.Option("--json")] = False,
    thin: Annotated[
        bool, typer.Option("--thin", help="Probe the thin guard even while runner.guard is off.")
    ] = False,
) -> None:
    """Run every probe against the live runner profile and print a table. Exit 1 on any
    failure. Re-run after every macOS update. Set CLAUDE_CODE_OAUTH_TOKEN to also run an
    authenticated `claude -p` inside the sandbox. With runner.guard: off (the default)
    workers run without the sandbox, so every probe is skipped (and said so) unless --thin."""
    from resonant.sandbox import SandboxError, default_deny_exec
    from resonant.sandbox.probes import probe_context, render_table, run_probes, skipped_probes

    if scope not in ("all", "ci", "local"):
        typer.echo("--scope must be all, ci or local", err=True)
        raise typer.Exit(2)
    scopes: tuple[Any, ...] = ("ci", "local") if scope == "all" else (scope,)
    runner = load_settings().runner  # the same dirs a real run uses
    if runner.guard == "off" and not thin:
        why = "runner.guard is off: workers run without sandbox-exec"
        results = skipped_probes(scopes, why)
        if as_json:
            typer.echo(json.dumps([r.as_dict() for r in results], indent=2))
        else:
            typer.echo(
                f"guard: off. {why} (like the owner at the keyboard), so the thin guard's "
                "probes are skipped. Set runner.guard: thin to use it, or pass --thin to "
                "probe it anyway."
            )
            typer.echo(render_table(results))
        return
    if not as_json:
        typer.echo(f"guard: {runner.guard}" + (" (probing thin: --thin)" if thin and
                   runner.guard == "off" else ""))  # fmt: skip
    try:
        with probe_context(
            work_dir=runner.work_dir,
            cache_dir=runner.cache_dir,
            secrets_dir=runner.secrets_dir,
            deny_exec=default_deny_exec(),
            deny_read=runner.deny_read,
        ) as ctx:
            results = run_probes(ctx, scopes)
    except SandboxError as e:
        typer.echo(f"sandbox unavailable: {e}", err=True)
        raise typer.Exit(1) from e
    if as_json:
        typer.echo(json.dumps([r.as_dict() for r in results], indent=2))
    else:
        typer.echo(render_table(results))
    if any(r.status == "fail" for r in results):
        raise typer.Exit(1)
