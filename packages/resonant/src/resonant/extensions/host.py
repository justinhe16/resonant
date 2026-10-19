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
   placeholders must be declared args; a job's only placeholder may be ``{due_at}``. Each
   manifest ``bins`` entry must be pinned (``argv0_paths["bin:<name>"]``) and still exist;
   it reaches every process of the extension as ``RESONANT_BIN_<NAME>``.
4. **Register.** Tool specs (``Manifest.tool_specs()``) and job specs (``job_specs()``,
   ``job_<id>``) go into the ``ToolRegistry``, each with an ``ExtensionHandler``. Job specs
   are kept out of chat: ``tool_specs()`` here lists tools only, and ``is_job`` tells them
   apart.

   MCP tools (``manifest.mcp``) get an ``McpToolHandler`` on one ``McpServerRuntime`` per
   server instead: a stdio server's command runs by ``argv0_paths["mcp:<id>"]`` (an in-dir
   ``.py`` under the daemon's interpreter), an http server is reached at its manifest
   ``url`` (``extensions/mcp_http.py``) with its bearer secret. Every listed tool must have
   its install-time pin in ``record.mcp_pins``, and its args schema is the pinned, stripped
   one. A version-1 pin (before GT-1) is matched by its stored input schema on the server's
   first start and re-pinned at version 2 in the row, audited ``ext.mcp_pin_migrated`` once.

Any refusal in 2-4 skips the whole extension (nothing half-registered), is audited
``ext.load_refused`` and is kept in ``refused`` for health. The status is left
alone: those are not tampering. A load never raises for one bad extension.

``handlers`` and ``mcp_handlers`` are merged into the executor's map at build.
``approvers_for`` is the gate's manifest-approvers seam. ``tracker`` is the one
``ProcessTracker`` for the kill switch.
``scheduled_jobs()`` is what the scheduler (``extensions/scheduler.py``) runs.

MCP servers (``extensions/mcp.py``) are started by :meth:`ExtensionHost.start_mcp` once the
daemon runs (and again on the next call after one ends), tracked by ``tracker`` so ``/kill``
ends them, and ended by :meth:`ExtensionHost.aclose` on shutdown. A tool whose live pin no
longer matches is disabled (hidden from chat, refused on call), audited ``ext.mcp_drift``,
and alerted once per (install, tool, live pin) through ``ALERTS_KEY`` and ``alert``.

Health (D11). The manifest's ``health.check`` is compiled at load like a tool (no
placeholders; a pinned ``gh``/``git`` uses ``argv0_paths["health"]``).
``ExtensionHealth`` turns the loaded, refused and tampered extensions into the monitor's
``ext:<name>`` checks.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
import sqlite3
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import yaml
from pydantic import ValidationError

from resonant import killswitch
from resonant.config import Settings
from resonant.executor import ContextHandler, HandlerKey
from resonant.extensions import pin
from resonant.extensions.checks import ALLOWED_COMMANDS, bin_env, bin_key, dir_errors, mcp_key
from resonant.extensions.handlers import ExtensionHandler
from resonant.extensions.mcp import (
    PIN_VERSION,
    HttpTarget,
    McpServerRuntime,
    McpToolHandler,
    parse_pins,
    server_argv,
)
from resonant.extensions.mcp_http import HTTP_MESSAGE_CAP
from resonant.extensions.process import (
    ExtAmbiguous,
    ExtCommand,
    ExtFailed,
    ExtProcess,
    ExtRequest,
    ExtRunner,
    ProcessTracker,
    TemplateError,
    ensure_data_dir,
    scratch_env,
)
from resonant.gateway.channel import ChannelError, ChannelRefused
from resonant.monitor import CheckResult
from resonant.ops import Sender
from resonant.redact import Redactor
from resonant.secrets import SecretError, SecretStore, values_of
from resonant.store import extensions as store
from resonant.store.audit import audit
from resonant.store.db import atomic, iso, kv_get, kv_set
from resonant.tools.registry import ToolRegistry
from resonant_sdk import Manifest, ToolSpec
from resonant_sdk.manifest import Job, McpHttpServer, duration_s, load_manifest

log = logging.getLogger(__name__)

ALERTS_KEY = "ext.alerts"  # kv: {name: {text, dedupe_key}}, sent to the owner on start
MCP_DRIFT_KEY = "ext.mcp_drift"  # kv: {"<ext>/<tool>": dedupe key of the last alert}
JOB_ARGS = frozenset({"due_at"})
PINNED_COMMANDS = ALLOWED_COMMANDS - {"python3"}


def tampered_text(name: str) -> str:
    return (
        f"{name} changed on disk since you approved it; disabled. "
        f"resonant ext update {name} to review"
    )


def drift_text(name: str, tool: str) -> str:
    return (
        f"{name}: MCP tool {tool} changed since you approved it; disabled. "
        f"resonant ext update {name} to review"
    )


def _approved_pin(record: store.ExtensionRecord, tool: str) -> str | None:
    entry = record.mcp_pins.get(tool)
    pin = cast(Mapping[str, object], entry).get("pin") if isinstance(entry, dict) else None
    return pin if isinstance(pin, str) else None


@dataclass(frozen=True)
class LoadedExtension:
    record: store.ExtensionRecord
    manifest: Manifest
    root: Path
    tools: tuple[ToolSpec, ...]
    jobs: tuple[ToolSpec, ...]
    health: ExtCommand | None = None  # the compiled manifest ``health.check``
    bins: Mapping[str, str] = field(default_factory=dict[str, str])  # RESONANT_BIN_* -> pin
    mcp: Mapping[str, McpServerRuntime] = field(default_factory=dict[str, McpServerRuntime])


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
    mcp_handlers: dict[HandlerKey, McpToolHandler] = field(
        default_factory=dict[HandlerKey, McpToolHandler]
    )
    # MCP servers read their tools' secrets at start; build_components sets the store.
    secrets: SecretStore | None = None
    # Sends queued alerts now (wiring sets it to ``send_alerts`` once iMessage is up).
    alert: Callable[[], Awaitable[object]] | None = None
    message_cap: int = 256 * 1024
    _job_keys: set[HandlerKey] = field(default_factory=set[HandlerKey])
    _side: set[asyncio.Task[object]] = field(default_factory=set[asyncio.Task[object]])

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
            message_cap=cfg.stdout_max_bytes,
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
    ) -> tuple[LoadedExtension, dict[HandlerKey, ContextHandler]]:  # both handler kinds
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
        if errors := dir_errors(root, manifest):
            raise LoadRefused(errors[0])
        bins: dict[str, str] = {}
        for name in manifest.bins:
            pinned_bin = record.argv0_paths.get(bin_key(name))
            if pinned_bin is None:
                raise LoadRefused(f"bins: {name} isn't pinned; resonant ext update {record.name}")
            if not Path(pinned_bin).is_absolute() or not Path(pinned_bin).is_file():
                raise LoadRefused(f"bins: pinned command {pinned_bin} is missing")
            bins[bin_env(name)] = pinned_bin
        data_dir = self.data_dir / record.name
        handlers: dict[HandlerKey, ContextHandler] = {}
        runtimes, schemas = self._prepare_mcp(record, manifest, root, data_dir, bins)
        tools = tuple(
            s.model_copy(update={"args_schema": schemas[s.name]}) if s.name in schemas else s
            for s in manifest.tool_specs()
        )
        jobs = tuple(manifest.job_specs())
        runs: dict[str, tuple[str, frozenset[str], bool]] = {
            name: (t.run, frozenset(t.args), False) for name, t in manifest.tools.items()
        }
        runs |= {j.spec_name: (j.run, JOB_ARGS, True) for j in manifest.jobs}
        for spec in (*tools, *jobs):
            if spec.extension != record.name or spec.source != f"ext:{record.name}":
                raise LoadRefused(f"{spec.name}: spec is not scoped to this extension")
            if spec.name in schemas:
                server = next(r for r in runtimes.values() if spec.name in r.approved)
                handlers[(record.name, spec.name)] = McpToolHandler(record.name, spec, server)
                continue
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
                bins=bins,
            )
        health: ExtCommand | None = None
        if manifest.health is not None:
            try:
                health = ExtCommand.compile(
                    manifest.health.check, root=root, argv0=record.argv0_paths.get("health")
                )
            except TemplateError as e:
                raise LoadRefused(f"health: {e}") from None
            if health.placeholders:
                raise LoadRefused("health: the check takes no placeholders")
        ext = LoadedExtension(record, manifest, root, tools, jobs, health, bins, runtimes)
        return ext, handlers

    def _prepare_mcp(
        self,
        record: store.ExtensionRecord,
        manifest: Manifest,
        root: Path,
        data_dir: Path,
        bins: Mapping[str, str],
    ) -> tuple[dict[str, McpServerRuntime], dict[str, dict[str, object]]]:
        """One runtime per MCP server, and each MCP tool's pinned (stripped) args schema.
        Every listed tool must have an install-time pin from its own server."""
        if manifest.mcp is None:
            return {}, {}
        try:
            pins = parse_pins(record.mcp_pins)
        except ValueError:
            why = f"mcp pins are malformed; resonant ext update {record.name}"
            raise LoadRefused(why) from None
        runtimes: dict[str, McpServerRuntime] = {}
        schemas: dict[str, dict[str, object]] = {}
        for sid, server in manifest.mcp.servers.items():
            argv: tuple[str, ...] = ()
            http: HttpTarget | None = None
            if isinstance(server, McpHttpServer):
                http = HttpTarget(server.url, server.secret, server.oauth)
            else:
                try:
                    argv = server_argv(
                        root, server.command, server.args, record.argv0_paths.get(mcp_key(sid))
                    )
                except ValueError as e:
                    raise LoadRefused(f"mcp server {sid}: {e}") from None
            approved: dict[str, str] = {}
            legacy: dict[str, dict[str, Any]] = {}
            for tool in server.tools:
                pin = pins.get(tool.name)
                if pin is None or pin.server != sid:
                    raise LoadRefused(
                        f"mcp tool {tool.name} isn't pinned; resonant ext update {record.name}"
                    )
                approved[tool.name] = pin.pin
                schemas[tool.name] = pin.input_schema
                if pin.version != PIN_VERSION:
                    legacy[tool.name] = pin.input_schema
            name = record.name
            runtimes[sid] = McpServerRuntime(
                ext=name,
                server_id=sid,
                argv=argv,
                root=root,
                data_dir=data_dir,
                path_env=self.path_env,
                bins=bins,
                secret_names=server.secrets,
                approved=approved,
                tracker=self.tracker,
                secrets=lambda: self.secrets,
                on_drift=lambda sid, tool, live, rec=record: self._drift(rec, sid, tool, live),
                legacy=legacy,
                on_migrate=lambda sid, tool, new, rec=record: self._migrate(rec, sid, tool, new),
                message_cap=self.message_cap if http is None else HTTP_MESSAGE_CAP,
                http=http,
            )
        return runtimes, schemas

    def _drift(self, record: store.ExtensionRecord, sid: str, tool: str, live: str | None) -> None:
        """An MCP tool no longer matches its pin: audit it and queue one owner alert per
        (install, tool, live pin), then send it if a sender is set."""
        key = f"{record.name}/{tool}"
        dedupe = f"ext-mcp-drift:{key}:{iso(record.installed_at)}:{live or 'removed'}"
        with atomic(self.conn):
            seen = kv_get(self.conn, MCP_DRIFT_KEY)
            seen_map = cast(dict[str, object], seen) if isinstance(seen, dict) else {}
            if seen_map.get(key) == dedupe:
                return
            kv_set(self.conn, MCP_DRIFT_KEY, {**seen_map, key: dedupe})
            alerts = pending_alerts(self.conn)
            alerts[key] = {"text": drift_text(record.name, tool), "dedupe_key": dedupe}
            kv_set(self.conn, ALERTS_KEY, alerts)
            audit(
                self.conn,
                "ext.mcp_drift",
                extension=record.name,
                server=sid,
                tool=tool,
                live_pin=live,
                approved_pin=_approved_pin(record, tool),
            )
        if self.alert is not None:
            task = asyncio.ensure_future(self.alert())
            self._side.add(task)
            task.add_done_callback(self._side.discard)

    def _migrate(self, record: store.ExtensionRecord, sid: str, tool: str, pin: str) -> None:
        """A version-1 MCP pin still matched by schema: save its version-2 pin and audit it
        once (the next start finds version 2 and says nothing)."""
        old = _approved_pin(record, tool)
        if old is None:
            return
        with atomic(self.conn):
            saved = store.set_mcp_pin(
                self.conn,
                record.name,
                tool,
                installed_at=record.installed_at,
                old_pin=old,
                pin=pin,
                version=PIN_VERSION,
            )
            if saved:
                audit(
                    self.conn,
                    "ext.mcp_pin_migrated",
                    extension=record.name,
                    server=sid,
                    tool=tool,
                    old_pin=old,
                    new_pin=pin,
                    version=PIN_VERSION,
                )
        if saved:
            log.info(
                "mcp pin migrated",
                extra={"extension": record.name, "tool": tool, "version": PIN_VERSION},
            )

    async def start_mcp(self) -> None:
        """Start every loaded MCP server once and check its pins (drift alerts come early).
        A server that can't start is logged; its tools try again on their next call."""
        runtimes = [r for ext in self.loaded.values() for r in ext.mcp.values()]
        results = await asyncio.gather(*(r.start() for r in runtimes), return_exceptions=True)
        for r, res in zip(runtimes, results, strict=True):
            if isinstance(res, BaseException):
                log.warning(
                    "mcp server did not start: %s",
                    type(res).__name__,
                    extra={"label": r.label, "reason": getattr(res, "reason", None)},
                )

    async def aclose(self) -> None:
        """End every MCP server (the whole process group and tree)."""
        runtimes = [r for ext in self.loaded.values() for r in ext.mcp.values()]
        await asyncio.gather(*(r.aclose() for r in runtimes), return_exceptions=True)
        for t in list(self._side):
            t.cancel()
        await asyncio.gather(*self._side, return_exceptions=True)

    def disabled_tools(self, extension: str) -> dict[str, str]:
        """MCP tools of ``extension`` disabled since load: ``{tool: "changed"|"removed"}``."""
        ext = self.loaded.get(extension)
        if ext is None:
            return {}
        return {t: why for r in ext.mcp.values() for t, why in r.disabled.items()}

    def hidden(self, spec: ToolSpec) -> bool:
        """Kept out of chat: job specs, and MCP tools disabled by a pin change."""
        if self.is_job(spec):
            return True
        return spec.extension is not None and spec.name in self.disabled_tools(spec.extension)

    def _register(self, prepared: tuple[LoadedExtension, dict[HandlerKey, ContextHandler]]) -> None:
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
        self.handlers |= {k: h for k, h in handlers.items() if isinstance(h, ExtensionHandler)}
        self.mcp_handlers |= {k: h for k, h in handlers.items() if isinstance(h, McpToolHandler)}
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
            if s.name not in self.disabled_tools(name)
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
                if spec is not None and isinstance(handler, ExtensionHandler):
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


# --- health (D11) -------------------------------------------------------------------------

FAIL_STREAK_ALERT = 3
TAMPERED_DETAIL = "disabled: changed on disk since you approved it"


@dataclass
class _HealthRun:
    at: datetime
    ok: bool
    detail: str


@dataclass
class ExtensionHealth:
    """The monitor's dynamic ``ext:<name>`` checks (``HealthMonitor(ext_checks=...)``).

    One check per extension that is loaded, refused at load, or ``disabled_tampered``.
    A loaded one is ok unless its manifest ``health.check`` failed (run every
    ``health.every`` through the process runner, with the extension's declared secrets and
    ``extensions.process.health_timeout_s``; exit 0 is ok) or a job has failed
    ``FAIL_STREAK_ALERT`` (3) times in a row (``ext_jobs.fail_streak``). Refused and
    tampered ones are always degraded. While paused no command runs (the last result
    stands). Details are code-built: stderr never reaches them.
    """

    host: ExtensionHost
    secrets: SecretStore | None = None
    timeout_s: float = 20.0
    _last: dict[str, _HealthRun] = field(default_factory=dict[str, _HealthRun])

    @classmethod
    def from_settings(
        cls, settings: Settings, host: ExtensionHost, secrets: SecretStore | None
    ) -> ExtensionHealth:
        return cls(host, secrets, settings.extensions.process.health_timeout_s)

    async def evaluate(self, now: datetime) -> dict[str, tuple[CheckResult, bool] | None]:
        """``{"ext:<name>": (result, fresh) | None}`` for every current extension. ``fresh``
        is False when only a cached health result is failing (it doesn't count toward the
        alert debounce again); None means no observation this round."""
        host = self.host
        out: dict[str, tuple[CheckResult, bool] | None] = {}
        tampered = {r.name for r in store.list_extensions(host.conn, status="disabled_tampered")}
        for name in sorted(tampered | set(host.tampered)):
            out[f"ext:{name}"] = (CheckResult(False, TAMPERED_DETAIL), True)
        for name, reason in sorted(host.refused.items()):
            out[f"ext:{name}"] = (CheckResult(False, f"not loaded: {reason}"), True)
        names = sorted(host.loaded)
        for gone in set(self._last) - set(names):
            del self._last[gone]
        ran = await asyncio.gather(*(self._run_if_due(n, now) for n in names))
        for name, fresh_run in zip(names, ran, strict=True):
            out[f"ext:{name}"] = self._result(name, fresh_run)
        return out

    def _result(self, name: str, fresh_run: bool) -> tuple[CheckResult, bool]:
        streaks = [
            f"job {j.job_id} failed {j.fail_streak} times in a row"
            for j in store.list_jobs(self.host.conn, name)
            if j.fail_streak >= FAIL_STREAK_ALERT
        ]
        last = self._last.get(name)
        drift = [
            f"mcp tool {tool} disabled ({why} since you approved it)"
            for tool, why in sorted(self.host.disabled_tools(name).items())
        ]
        problems = ([last.detail] if last is not None and not last.ok else []) + streaks + drift
        if not problems:
            return CheckResult(True, "ok"), True
        return CheckResult(False, "; ".join(problems)), fresh_run or bool(streaks or drift)

    async def _run_if_due(self, name: str, now: datetime) -> bool:
        ext = self.host.loaded[name]
        if ext.health is None or ext.manifest.health is None:
            return False
        last = self._last.get(name)
        every = timedelta(seconds=duration_s(ext.manifest.health.every))
        if last is not None and now - last.at < every:
            return False
        if self.host.tracker.paused or _paused(self.host.conn):
            return False
        try:
            ok, detail = await self._run(name, ext, ext.health)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # a health check never takes the monitor down
            log.warning("health check raised %s", type(e).__name__, extra={"extension": name})
            ok, detail = False, f"health check error ({type(e).__name__})"
        if detail == "paused":
            return False
        self._last[name] = _HealthRun(now, ok, detail)
        return True

    async def _run(self, name: str, ext: LoadedExtension, command: ExtCommand) -> tuple[bool, str]:
        env_secrets: dict[str, str] = {}
        if ext.manifest.secrets:
            try:
                if self.secrets is None:
                    raise SecretError(f"{name}/{ext.manifest.secrets[0]}", "unavailable")
                env_secrets = await self.secrets.env_for(name, ext.manifest.secrets)
            except SecretError:
                return False, "health check: a declared secret is missing or unreadable"
        data_dir = self.host.data_dir / name
        ensure_data_dir(data_dir)
        env = scratch_env(
            ext=name, data_dir=data_dir, secrets=env_secrets, path=self.host.path_env, bins=ext.bins
        )
        redactor = Redactor(values_of(env))
        req = ExtRequest(
            argv=command.render({}),
            cwd=ext.root,
            env=env,
            timeout_s=self.timeout_s,
            label=f"{name}/health",
        )
        try:
            raw = await self.host.runner.run_raw(req, redact=redactor.text)
        except ExtFailed as e:
            if e.reason == "paused":
                return False, "paused"
            return False, "health check could not start"
        except ExtAmbiguous as e:
            return False, f"health check {'timed out' if e.reason == 'timeout' else 'cut short'}"
        if raw.returncode == 0:
            return True, "ok"
        if raw.returncode < 0:
            return False, f"health check killed (signal {-raw.returncode})"
        return False, f"health check failed (exit {raw.returncode})"


def _paused(conn: sqlite3.Connection) -> bool:
    try:
        return killswitch.is_paused(conn)
    except killswitch.PausedStateUnknownError:
        return True


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
        _drop_alert(conn, name, alert["dedupe_key"])
    return sent


def _drop_alert(conn: sqlite3.Connection, name: str, dedupe_key: str) -> None:
    """Drop the alert just sent, unless a newer one replaced it meanwhile."""
    with atomic(conn):
        alerts = pending_alerts(conn)
        if alerts.get(name, {}).get("dedupe_key") == dedupe_key:
            alerts.pop(name, None)
        kv_set(conn, ALERTS_KEY, alerts)
