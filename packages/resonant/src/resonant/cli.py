"""`resonant` command-line interface."""

from __future__ import annotations

import os
from typing import Annotated

import typer

from resonant import daemon as daemon_mod
from resonant import launchd
from resonant.config import load_settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
daemon_app = typer.Typer(no_args_is_help=True, help="Run and supervise the daemon.")
app.add_typer(daemon_app, name="daemon")


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
    if ollama:
        if path := launchd.find_ollama():
            typer.echo(f"installed {launchd.install(launchd.ollama_plist(settings, path))}")
        else:
            typer.echo("ollama not found on PATH; skipping Ollama LaunchAgent")
    typer.echo(f"installed {launchd.install(launchd.daemon_plist(settings))}")


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
    files = [str(logs / "daemon.err.log"), str(logs / "daemon.out.log")]
    args = ["tail", "-n", str(lines), *(["-F"] if follow else []), *files]
    os.execvp("tail", args)  # noqa: S606, S607 - replaces this process with tail
