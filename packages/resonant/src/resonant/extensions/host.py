"""ExtensionHost: loads installed extensions at daemon start and registers what they run.

``load()`` runs once, from ``build_components``, before the gate and executor are built.
For each ``enabled`` row (``store.extensions``), in name order:

1. **Pin.** ``pin.verify_copy(record, <extensions_dir>/<name>)``. Anything but ``ok`` (an
   edited, added or removed file, a missing copy) sets ``status=disabled_tampered`` and
   queues one owner alert in the same transaction (``set_status`` is True only on the
   flip, so it alerts once), audited ``ext.tampered``. Nothing of it is registered.
2. **Manifest.** The approved snapshot from the row, validated again; its name must be the
   row's. ``checks.dir_errors`` must pass on the copy.
3. **Commands.** Every tool's and job's ``run`` is compiled once (``ExtCommand.compile``,
   ``argv0`` from ``record.argv0_paths[<spec name>]``, which must still exist). Tool
   placeholders must be declared args; a job's only placeholder may be ``{due_at}``.
4. **Register.** Tool specs (``Manifest.tool_specs()``) and job specs (``job_specs()``,
   ``job_<id>``) go into the ``ToolRegistry``, each with an ``ExtensionHandler``. Job specs
   are kept out of chat: ``tool_specs()`` here lists tools only, and ``is_job`` tells them
   apart.

Any refusal in 2-4 skips the whole extension (nothing half-registered), is audited
``ext.load_refused`` and is kept in ``refused`` for health (P3-12). The status is left
alone: those are not tampering. A load never raises for one bad extension.

``handlers`` is merged into the executor's map at build. ``approvers_for`` is the gate's
manifest-approvers seam. ``tracker`` is the one ``ProcessTracker`` for the kill switch.
``scheduled_jobs()`` is what the scheduler (``extensions/scheduler.py``) runs.
"""

from __future__ import annotations

import logging
import shlex
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import yaml
from pydantic import ValidationError

from resonant.config import Settings
from resonant.executor import HandlerKey
from resonant.extensions import pin
from resonant.extensions.checks import ALLOWED_COMMANDS, dir_errors
from resonant.extensions.handlers import ExtensionHandler
from resonant.extensions.process import (
    ExtCommand,
    ExtProcess,
    ExtRunner,
    ProcessTracker,
    TemplateError,
)
from resonant.gateway.channel import ChannelError, ChannelRefused
from resonant.ops import Sender
from resonant.store import extensions as store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set
from resonant.tools.registry import ToolRegistry
from resonant_sdk import Manifest, ToolSpec
from resonant_sdk.manifest import Job, load_manifest

log = logging.getLogger(__name__)

ALERTS_KEY = "ext.alerts"  # kv: {name: {text, dedupe_key}}, sent to the owner on start
JOB_ARGS = frozenset({"due_at"})
PINNED_COMMANDS = ALLOWED_COMMANDS - {"python3"}


def tampered_text(name: str) -> str:
    return (
        f"{name} changed on disk since you approved it; disabled. "
        f"resonant ext update {name} to review"
    )


@dataclass(frozen=True)
class LoadedExtension:
    record: store.ExtensionRecord
    manifest: Manifest
    root: Path
    tools: tuple[ToolSpec, ...]
    jobs: tuple[ToolSpec, ...]


@dataclass(frozen=True)
class HostJob:
    ext: str
    job: Job  # the manifest entry: schedule, tz, catchup
    spec: ToolSpec  # the registered job_<id> spec
    handler: ExtensionHandler


class LoadRefused(Exception):
    pass


@dataclass
class ExtensionHost:
    conn: sqlite3.Connection
    registry: ToolRegistry
    extensions_dir: Path
    data_dir: Path
    runner: ExtRunner
    path_env: str
    tracker: ProcessTracker = field(default_factory=ProcessTracker)
    loaded: dict[str, LoadedExtension] = field(default_factory=dict[str, LoadedExtension])
    refused: dict[str, str] = field(default_factory=dict[str, str])  # name -> reason
    tampered: list[str] = field(default_factory=list[str])
    handlers: dict[HandlerKey, ExtensionHandler] = field(
        default_factory=dict[HandlerKey, ExtensionHandler]
    )
    _job_keys: set[HandlerKey] = field(default_factory=set[HandlerKey])

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        conn: sqlite3.Connection,
        registry: ToolRegistry,
        *,
        runner: ExtRunner | None = None,
    ) -> ExtensionHost:
        """A host over ``settings.extensions_dir`` / ``ext_data_dir`` with the configured
        process caps. ``runner`` replaces the real ``ExtProcess`` (tests)."""
        cfg = settings.extensions.process
        tracker = ProcessTracker(sample_s=cfg.tree_sample_s, grace_s=cfg.kill_grace_s)
        if runner is None:
            runner = ExtProcess(
                tracker,
                stdout_cap=cfg.stdout_max_bytes,
                stderr_cap=cfg.stderr_max_bytes,
                events_cap=cfg.stdout_max_bytes,
            )
        return cls(
            conn=conn,
            registry=registry,
            extensions_dir=settings.extensions_dir,
            data_dir=settings.ext_data_dir,
            runner=runner,
            path_env=cfg.path_env,
            tracker=tracker,
        )

    # --- load -----------------------------------------------------------------------------

    def load(self) -> None:
        """Load every enabled row once (see the module docstring). Never raises for one
        bad extension."""
        for record in store.list_extensions(self.conn, status="enabled"):
            root = self.extensions_dir / record.name
            state = pin.verify_copy(record, root)
            if state != "ok":
                self._tamper(record, f"copy {state}")
                continue
            try:
                self._register(self._prepare(record, root))
            except LoadRefused as e:
                self._refuse(record.name, str(e))
            except Exception as e:  # never let one extension stop the daemon
                log.exception("extension load failed", extra={"extension": record.name})
                self._refuse(record.name, f"load failed ({type(e).__name__})")
        log.info(
            "extensions loaded",
            extra={
                "loaded": sorted(self.loaded),
                "refused": sorted(self.refused),
                "tampered": self.tampered,
            },
        )

    def _prepare(
        self, record: store.ExtensionRecord, root: Path
    ) -> tuple[LoadedExtension, dict[HandlerKey, ExtensionHandler]]:
        try:
            manifest = Manifest.model_validate(record.manifest)
        except ValidationError as e:
            n = e.error_count()
            raise LoadRefused(f"approved manifest no longer validates ({n} errors)") from None
        if manifest.name != record.name:
            raise LoadRefused("manifest name does not match the installed name")
        try:  # the snapshot must be the pinned resonant.yaml
            on_disk = load_manifest(root / "resonant.yaml")
        except (OSError, ValueError, yaml.YAMLError):
            raise LoadRefused("resonant.yaml in the copy can't be read") from None
        if on_disk != manifest:
            raise LoadRefused("approved manifest snapshot does not match resonant.yaml")
        if manifest.mcp is not None:
            raise LoadRefused("mcp servers are not supported yet")
        if errors := dir_errors(root, manifest):
            raise LoadRefused(errors[0])
        tools = tuple(manifest.tool_specs())
        jobs = tuple(manifest.job_specs())
        data_dir = self.data_dir / record.name
        handlers: dict[HandlerKey, ExtensionHandler] = {}
        runs: dict[str, tuple[str, frozenset[str], bool]] = {
            name: (t.run, frozenset(t.args), False) for name, t in manifest.tools.items()
        }
        runs |= {j.spec_name: (j.run, JOB_ARGS, True) for j in manifest.jobs}
        for spec in (*tools, *jobs):
            if spec.extension != record.name or spec.source != f"ext:{record.name}":
                raise LoadRefused(f"{spec.name}: spec is not scoped to this extension")
            run, allowed, job = runs[spec.name]
            argv0 = record.argv0_paths.get(spec.name)
            # Only gh/git run by the pinned path; scripts and python3 run with the daemon's
            # interpreter, so a moved interpreter path doesn't matter.
            pinned = shlex.split(run)[0] in PINNED_COMMANDS
            if pinned and argv0 is not None and not Path(argv0).is_file():
                raise LoadRefused(f"{spec.name}: pinned command {argv0} is missing")
            try:
                command = ExtCommand.compile(run, root=root, argv0=argv0)
            except TemplateError as e:
                raise LoadRefused(f"{spec.name}: {e}") from None
            if extra := command.placeholders - allowed:
                raise LoadRefused(f"{spec.name}: undeclared placeholders {sorted(extra)}")
            handlers[(record.name, spec.name)] = ExtensionHandler(
                ext=record.name,
                spec=spec,
                command=command,
                root=root,
                data_dir=data_dir,
                runner=self.runner,
                path_env=self.path_env,
                job=job,
            )
        ext = LoadedExtension(record, manifest, root, tools, jobs)
        return ext, handlers

    def _register(
        self, prepared: tuple[LoadedExtension, dict[HandlerKey, ExtensionHandler]]
    ) -> None:
        ext, handlers = prepared
        name = ext.record.name
        if any(s.extension == name for s in self.registry.specs()):
            raise LoadRefused("already registered")
        try:
            for spec in (*ext.tools, *ext.jobs):
                self.registry.register(spec)
        except ValueError as e:
            self.registry.unregister_extension(name)
            raise LoadRefused(str(e)) from None
        self.handlers |= handlers
        self._job_keys |= {(name, s.name) for s in ext.jobs}
        self.loaded[name] = ext

    def _tamper(self, record: store.ExtensionRecord, reason: str) -> None:
        self.tampered.append(record.name)
        log.warning("extension pin mismatch", extra={"extension": record.name, "reason": reason})
        with atomic(self.conn):
            if store.set_status(self.conn, record.name, "disabled_tampered"):
                alerts = pending_alerts(self.conn)
                alerts[record.name] = {
                    "text": tampered_text(record.name),
                    "dedupe_key": f"ext-tampered:{record.name}:{iso(record.installed_at)}",
                }
                kv_set(self.conn, ALERTS_KEY, alerts)
                audit(
                    self.conn,
                    "ext.tampered",
                    extension=record.name,
                    reason=reason,
                    tree_digest=record.tree_digest,
                    commit=record.commit,
                )

    def _refuse(self, name: str, reason: str) -> None:
        self.refused[name] = reason
        log.warning("extension not loaded", extra={"extension": name, "reason": reason})
        with atomic(self.conn):
            audit(self.conn, "ext.load_refused", extension=name, reason=reason)

    # --- queries --------------------------------------------------------------------------

    def is_job(self, spec: ToolSpec) -> bool:
        return (spec.extension, spec.name) in self._job_keys

    def tool_specs(self, extension: str | None = None) -> list[ToolSpec]:
        """Loaded extensions' chat tools (never job specs), by extension then name."""
        return [
            s
            for name in sorted(self.loaded)
            if extension is None or name == extension
            for s in sorted(self.loaded[name].tools, key=lambda s: s.name)
        ]

    def job_specs(self, extension: str | None = None) -> list[ToolSpec]:
        return [
            s
            for name in sorted(self.loaded)
            if extension is None or name == extension
            for s in sorted(self.loaded[name].jobs, key=lambda s: s.name)
        ]

    def scheduled_jobs(self) -> list[HostJob]:
        """Every loaded job with its manifest entry, registered spec and handler, by
        extension then job id."""
        out: list[HostJob] = []
        for name in sorted(self.loaded):
            ext = self.loaded[name]
            specs = {s.name: s for s in ext.jobs}
            for job in sorted(ext.manifest.jobs, key=lambda j: j.id):
                spec = specs.get(job.spec_name)
                handler = self.handlers.get((name, job.spec_name))
                if spec is not None and handler is not None:
                    out.append(HostJob(name, job, spec, handler))
        return out

    def manifest(self, name: str) -> Manifest | None:
        ext = self.loaded.get(name)
        return None if ext is None else ext.manifest

    def approvers_for(self, extension: str) -> tuple[str, ...]:
        """The gate's seam: the loaded manifest's ``approvers`` (untrusted; the gate keeps
        only names holding the grant). Unknown extension or any error: ``()``, owner only."""
        try:
            m = self.manifest(extension)
            return () if m is None else tuple(str(a) for a in m.approvers)
        except Exception:
            log.exception("approvers_for failed; owner only")
            return ()


def pending_alerts(conn: sqlite3.Connection) -> dict[str, dict[str, str]]:
    value: object = kv_get(conn, ALERTS_KEY)
    if not isinstance(value, dict):
        return {}
    out: dict[str, dict[str, str]] = {}
    for k, v in cast(Mapping[object, object], value).items():
        if isinstance(k, str) and isinstance(v, dict):
            entry = cast(Mapping[object, object], v)
            text, key = entry.get("text"), entry.get("dedupe_key")
            if isinstance(text, str) and isinstance(key, str):
                out[k] = {"text": text, "dedupe_key": key}
    return out


async def send_alerts(conn: sqlite3.Connection, channel: Sender, owner: str | None) -> int:
    """Send queued extension alerts to the owner; returns how many were sent.

    A sent or refused alert is dropped; any other channel error, or no owner yet, keeps it
    for the next start (the dedupe key prevents a double send).
    """
    if owner is None:
        return 0
    sent = 0
    for name, alert in sorted(pending_alerts(conn).items()):
        try:
            await channel.send(owner, alert["text"], dedupe_key=alert["dedupe_key"])
            sent += 1
        except ChannelRefused as e:
            log.warning("extension alert refused: %s", e)
        except ChannelError as e:
            log.warning("extension alert failed (will retry on next start): %s", e)
            continue
        _drop_alert(conn, name)
    return sent


def _drop_alert(conn: sqlite3.Connection, name: str) -> None:
    with atomic(conn):
        alerts = pending_alerts(conn)
        alerts.pop(name, None)
        kv_set(conn, ALERTS_KEY, alerts)
