"""`resonant` command-line interface.

The CLI runs locally as the machine's user, so it acts as the owner. Remote principals
reach Resonant only through channels, where identity is resolved from principals.yaml.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Annotated, Any, cast

import httpx
import typer
from pydantic import ValidationError

from resonant import daemon as daemon_mod
from resonant import killswitch, launchd
from resonant.config import Settings, load_settings
from resonant.extensions.checks import approver_errors
from resonant.principals import load_principals
from resonant.status import collect_status
from resonant.store.audit import audit
from resonant.store.db import StoreUnavailableError, atomic, open_db, open_existing
from resonant_sdk import load_manifest

app = typer.Typer(no_args_is_help=True, add_completion=False)
daemon_app = typer.Typer(no_args_is_help=True, help="Run and supervise the daemon.")
app.add_typer(daemon_app, name="daemon")
ext_app = typer.Typer(no_args_is_help=True, help="Extensions (see docs/extensions.md).")
app.add_typer(ext_app, name="ext")

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
    """Install and start the LaunchAgents (daemon, and Ollama if available)."""
    settings = load_settings()
    try:
        if ollama:
            if path := launchd.find_ollama():
                typer.echo(f"installed {launchd.install(launchd.ollama_plist(settings, path))}")
            else:
                typer.echo("ollama not found on PATH; skipping Ollama LaunchAgent")
        typer.echo(f"installed {launchd.install(launchd.daemon_plist(settings))}")
    except launchd.LaunchctlError as e:
        typer.echo(f"error: {e}", err=True)
        raise typer.Exit(1) from None


@daemon_app.command("uninstall")
def daemon_uninstall(
    ollama: Annotated[bool, typer.Option(help="Also remove the Ollama LaunchAgent.")] = True,
) -> None:
    """Stop and remove the LaunchAgents."""
    labels = [launchd.DAEMON_LABEL] + ([launchd.OLLAMA_LABEL] if ollama else [])
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
    if not up:
        raise typer.Exit(1)


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
        with atomic(conn):
            audit(conn, "job.skipped_paused", job=job)
        typer.echo(f"job-guard: kill switch engaged; skipping {job}", err=True)
        raise typer.Exit(EX_TEMPFAIL)
    with atomic(conn):
        audit(conn, "job.exec", job=job, command=command[0])
    try:
        os.execvp(command[0], command)  # noqa: S606 - command comes from an approved manifest
    except OSError as e:
        with atomic(conn):
            audit(conn, "job.exec_failed", job=job, error=str(e))
        typer.echo(f"job-guard: cannot exec {command[0]}: {e}", err=True)
        raise typer.Exit(127) from None


# --- extensions ------------------------------------------------------------------------


@ext_app.command("validate")
def ext_validate(
    path: Annotated[Path, typer.Argument(help="Extension dir or resonant.yaml")],
) -> None:
    """Validate a manifest: schema, invariants, and approver grants (if principals exist)."""
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
    problems: list[str] = []
    if settings.principals_path.exists():
        problems = approver_errors(manifest, load_principals(settings.principals_path))
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
    if manifest.secrets:
        typer.echo(f"  secrets: {', '.join(manifest.secrets)}")
    for p in problems:
        typer.echo(f"error: {p}", err=True)
    if problems:
        raise typer.Exit(1)
    typer.echo("ok")
