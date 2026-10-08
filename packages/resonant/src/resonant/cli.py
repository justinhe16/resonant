"""`resonant` command-line interface.

The CLI runs locally as the machine's user, so it acts as the owner. Remote principals
reach Resonant only through channels, where identity is resolved from principals.yaml.
"""

from __future__ import annotations

import contextlib
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
    echo_health(data.get("health"), live=up)
    if not up:
        raise typer.Exit(1)


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
    if data["latency_ms"] is not None:
        typer.echo(f"latency     {data['latency_ms']} ms (1-token JSON call)")
    if data["detail"]:
        typer.echo(f"detail      {data['detail']}")


@model_app.command("probe")
def model_probe(as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Check the model is pulled, its context is big enough, and thinking is off. Exit 1 if not."""
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
