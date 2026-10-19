"""The extension manager: stage, show, install, update and remove (plan D1, D2).

Terminal only. The flow for ``resonant ext add`` and ``ext update``:

1. :meth:`ExtensionManager.stage` copies the source's approved bytes (``sources``) into a
   staging dir, validates the manifest and the directory, and computes the :class:`Pin`
   ``{source, commit, tree_digest, manifest_sha}``.
2. :func:`confirm_lines` renders the confirmation from code: a heading
   ``<name> <source>@<sha8> digest <8>``, the escalations first, then every tool, job and
   notify event. Every manifest string goes through ``sanitize_external``.
3. The owner types the name. :meth:`ExtensionManager.install` stages the source **again**,
   refuses if any of the four bound values changed since the confirmation, moves the new
   tree into ``<extensions_dir>/<name>/`` (read-only), recomputes the pin on the copy, and
   only then writes the row and the audit row in one transaction.

Leftovers: each install records its source and declared secret names in kv
``ext.origin.<name>``. It survives ``remove`` and ``--purge`` and lives outside anything
an extension can write. If the data dir, or Keychain items an install from a *different*
source declared, still exist, ``--adopt`` is required, so a new source never silently
inherits old state or secrets.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re
import secrets as pysecrets
import shlex
import sqlite3
import sys
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from pydantic import ValidationError

from resonant.extensions.checks import (
    approver_errors,
    bin_env,
    bin_key,
    commands,
    dir_errors,
    mcp_key,
    name_errors,
    resolve_argv0,
    resolve_bin,
    resolve_mcp_command,
)
from resonant.extensions.mcp import approve, discover, pins_digest, pins_json, server_argv
from resonant.extensions.mcp_http import OAuthCredentials, discover_http
from resonant.extensions.pin import (
    CopyState,
    TreeEntries,
    TreeError,
    tree_digest,
    tree_entries,
    verify_copy,
)
from resonant.extensions.process import SCRATCH_PATH, ExtRequest, scratch_env
from resonant.extensions.sources import (
    Source,
    SourceError,
    make_readonly,
    parse_source,
    remove_tree,
    stage_path,
    stage_repo,
)
from resonant.gate.codes import sanitize_external
from resonant.principals import Principals
from resonant.secrets import InvalidSecretRef, secret_ref
from resonant.store import extensions as ext_store
from resonant.store import repos
from resonant.store.audit import audit
from resonant.store.db import atomic, kv_get, kv_set, utcnow
from resonant_sdk import Manifest, load_manifest
from resonant_sdk.manifest import McpHttpServer, McpOAuth
from resonant_sdk.tool import Level

MANIFEST_FILE = "resonant.yaml"
ORIGIN_KEY = "ext.origin.{name}"  # kv: {source, secrets} of the last install
NETWORK_COMMANDS = frozenset({"gh", "git"})
MAX_RUN_CHARS = 300  # commands are shown in full, verbatim
TOOL_WARN_COUNT = 12  # above this, routing accuracy suffers (plan D9)
_ASKS = (Level.L1, Level.L2)


def _discover_sync(req: ExtRequest) -> list[dict[str, Any]]:
    return asyncio.run(discover(req, python=sys.executable))


# (url, bearer token or OAuth credentials or None, label) -> the server's tools/list
HttpDiscoverFn = Callable[[str, "str | OAuthCredentials | None", str], list[dict[str, Any]]]


def _discover_http_sync(
    url: str, token: str | OAuthCredentials | None, label: str
) -> list[dict[str, Any]]:
    return asyncio.run(discover_http(url, token, label=label))


def _no_secret(_ref: str) -> str | None:
    return None


def _never(_sid: str, _url: str, _ref: str) -> bool:
    return False


class _NoToken(Exception):
    """An http MCP server's bearer token can't be used to list its tools."""


class ManagerError(Exception):
    """Install, update or remove refused. ``problems`` lists every reason."""

    def __init__(self, message: str, problems: Iterable[str] = ()) -> None:
        super().__init__(message)
        self.problems = list(problems)


class PinChanged(ManagerError):
    """The source or the copy no longer matches what the owner confirmed."""


class AdoptRequired(ManagerError):
    """State or secrets left by a different source exist; ``--adopt`` takes them over."""


@dataclass(frozen=True)
class Pin:
    """What the confirmation binds, and what the row stores."""

    source: str
    commit: str | None
    tree_digest: str
    manifest_sha: str
    mcp: str = ""  # pins_digest of the MCP tool pins; "" without MCP servers

    @property
    def heading_ref(self) -> str:
        return f"{self.source}@{self.commit[:8] if self.commit else 'no-commit'}"


@dataclass(frozen=True)
class Staged:
    name: str
    pin: Pin
    manifest: Manifest
    root: Path  # the staging dir; discard() or install() removes it
    files: TreeEntries
    notes: tuple[str, ...] = ()
    # What the MCP servers listed, pinned: {tool: {server, pin, input_schema}} (mcp.pins_json).
    mcp_pins: Mapping[str, Any] = field(default_factory=dict[str, Any])


@dataclass(frozen=True)
class Removed:
    record: ext_store.ExtensionRecord
    secrets_left: list[str]  # Keychain refs still present
    data_dir: Path | None  # kept (None when purged or absent)


def heading(staged: Staged) -> str:
    """``example repo:example@a1b2c3d4 digest 9f3e01aa``. Built by code, never from text."""
    return f"{staged.name} {staged.pin.heading_ref} digest {staged.pin.tree_digest[:8]}"


def _s(text: str, max_len: int = 80) -> str:
    return sanitize_external(text, max_len)


def _argv0(run: str) -> str:
    try:
        argv = shlex.split(run)
    except ValueError:
        return ""
    return argv[0] if argv else ""


def escalations(
    manifest: Manifest,
    registered: Collection[str] = (),
    mcp_pins: Mapping[str, Any] | None = None,
) -> list[str]:
    """What raises the stakes, one line each: secrets, approvers, asking tools and jobs,
    commands that use the network (``gh``/``git``), MCP servers and every MCP tool with its
    pin (``mcp_pins``, as stored), and linked repos. A repo link makes "work on <extension>"
    start runs there. The owner's GitHub login isn't known offline, so a slug not in the repo
    registry (``registered``, lowercase) is flagged. Stable text, so updates can diff."""
    out: list[str] = []
    mcp_tools = {t.name for _, t in manifest.mcp.tools()} if manifest.mcp else set[str]()
    for slug in manifest.repos:
        new = "" if slug.lower() in registered else " (not in your repo registry)"
        out.append(f"repo link {_s(slug)}: requests naming {manifest.name} run there{new}")
    for name in manifest.secrets:
        out.append(f"secret {_s(f'{manifest.name}/{name}')} (Keychain)")
    for name in manifest.approvers:
        out.append(f"approver {_s(name)} may approve this extension's actions")
    for spec in manifest.tool_specs():
        level = spec.effective_level
        if level in _ASKS and spec.name not in mcp_tools:
            extra = _final_words(spec.commit, level)
            out.append(f"tool {_s(spec.name)} {spec.effect.value} {level.value}: asks{extra}")
    for job in manifest.jobs:
        spec = job.spec(manifest.name)
        if spec.effective_level in _ASKS:
            out.append(
                f"job {_s(job.id)} {spec.effect.value} {spec.effective_level.value}: "
                f"asks an approver every run ({schedule_words(job.schedule, job.tz)})"
            )
    for where, run in commands(manifest):
        if _argv0(run) in NETWORK_COMMANDS:
            out.append(f"{_s(where)} runs {_argv0(run)} (network)")
    for name in manifest.bins:
        out.append(f"its code runs {name} (network)")
    out += _mcp_escalations(manifest, mcp_pins or {})
    if manifest.report is not None and manifest.report.max_urgency == "urgent":
        out.append(report_grant(manifest))
    return out


def _final_words(commit: bool, level: Level) -> str:
    """How a confirmation line marks an irreversible tool: only L1 needs ``yes <code>``; a
    commit tool at L2/L3 asks a plain yes (GT-1 made extension writes default L2)."""
    if level is Level.L1:
        return ", irreversible (yes <code>)"
    return ", irreversible" if commit else ""


def _mcp_escalations(manifest: Manifest, pins: Mapping[str, Any]) -> list[str]:
    if manifest.mcp is None:
        return []
    out: list[str] = []
    for sid, server in manifest.mcp.servers.items():
        if isinstance(server, McpHttpServer):
            token = f", sends secret {server.secret} as its bearer token" if server.secret else ""
            if (o := server.oauth) is not None:
                token = (
                    f", sends secrets {o.client_id}, {o.client_secret}, {o.refresh_token} to "
                    f"{_s(o.token_url, 160)} (oauth) and the access token it gets as its "
                    "bearer token"
                )
            out.append(f"mcp server {_s(sid)} calls {_s(server.url, 160)} (http, network){token}")
            continue
        line = shlex.join([server.command, *server.args])
        secrets = f", gets secrets {', '.join(server.secrets)}" if server.secrets else ""
        out.append(f"mcp server {_s(sid)} runs {_s(line, 160)} (stdio){secrets}")
    specs = {s.name: s for s in manifest.tool_specs()}
    for sid, tool in manifest.mcp.tools():
        spec = specs[tool.name]
        level = spec.effective_level
        entry: object = pins.get(tool.name)
        pin: object = cast(dict[str, Any], entry).get("pin") if isinstance(entry, dict) else None
        shown = pin[:12] if isinstance(pin, str) else "unknown"
        asks = ""
        if level in _ASKS:
            asks = ": asks" + _final_words(spec.commit, level)
        out.append(
            f"mcp tool {_s(tool.name)} ({_s(sid)}) {spec.effect.value} {level.value} "
            f"pin {shown}{asks}"
        )
    return out


_GRANT_WORDS = {
    "urgent": "may text you any time (urgent)",
    "normal": "hourly updates (normal)",
    "low": "8am briefing only (low)",
}


def report_grant(manifest: Manifest) -> str:
    """``reports: may text you any time (urgent)``; "" when the extension can't report."""
    if manifest.report is None:
        return ""
    return f"reports: {_GRANT_WORDS[manifest.report.max_urgency]}"


_NUM = re.compile(r"^\d{1,2}$")


def schedule_words(cron: str, tz: str | None = None) -> str:
    """A few common cron shapes in words; anything else as the quoted expression."""
    fields = cron.split()
    words = f"cron {_s(cron, 40)}"
    if len(fields) == 5:
        minute, hour, dom, month, dow = fields
        if minute.startswith("*/") and minute[2:].isdigit() and fields[1:] == ["*"] * 4:
            words = f"every {minute[2:]} minutes"
        elif _NUM.match(minute) and hour == "*" and (dom, month, dow) == ("*", "*", "*"):
            words = f"hourly at :{int(minute):02d}"
        elif _NUM.match(minute) and _NUM.match(hour) and (dom, month) == ("*", "*"):
            at = f"{int(hour):02d}:{int(minute):02d}"
            days = {"*": "daily", "1-5": "weekdays", "0,6": "weekends", "6,0": "weekends"}
            if dow in days:
                words = f"{days[dow]} at {at}"
    return f"{words} {_s(tz, 40)}" if tz else words


def confirm_lines(staged: Staged, registered: Collection[str] = ()) -> list[str]:
    """The install/update confirmation: heading, escalations first, then everything else."""
    m = staged.manifest
    lines = [heading(staged), ""]
    esc = escalations(m, registered, staged.mcp_pins)
    lines.append("Escalations:" if esc else "Escalations: none")
    lines += [f"  ! {e}" for e in esc]
    if m.description or m.examples:
        lines.append("Intent:")
        if m.description:
            lines.append(f"  {_s(m.description, 120)}")
        lines += [f"  e.g. {_s(e)}" for e in m.examples]
    lines.append("Tools:" if m.tools or m.mcp else "Tools: none")
    for spec in m.tool_specs():
        flag = " commit" if spec.commit else ""
        flag += " never-yes-always" if not spec.standing else ""
        lines.append(
            f"  {_s(spec.name, 40)} {spec.effect.value} {spec.effective_level.value}{flag}"
            f": {_s(spec.description or '-')}"
        )
    for name, tool in m.tools.items():
        lines.append(f"    {_s(name, 40)} runs {tool.run}")
    if m.mcp:
        for sid, tool in m.mcp.tools():
            names = ", ".join(_s(n, 40) for n in _arg_names(staged.mcp_pins.get(tool.name)))
            lines.append(
                f"    {_s(tool.name, 40)} via mcp server {_s(sid, 40)}; args: {names or 'none'}"
            )
    lines.append("Jobs:" if m.jobs else "Jobs: none")
    for job in m.jobs:
        spec = job.spec(m.name)
        lines.append(
            f"  {_s(job.id, 40)} {schedule_words(job.schedule, job.tz)}, "
            f"{spec.effect.value} {spec.effective_level.value}, catchup {_s(job.catchup, 10)}"
            f", runs {job.run}"
        )
    if m.repos:
        lines.append(f"Repos: {', '.join(_s(r, 80) for r in m.repos)}")
    if m.report is not None and m.report.max_urgency != "urgent":
        lines.append(report_grant(m).replace("reports:", "Reports:", 1))
    if m.notify:
        events = ", ".join(_s(e, 40) for e in m.notify.on)
        lines.append(f"Notify: {events} via {m.notify.channel}")
    if m.health:
        lines.append(f"Health: {m.health.check} every {_s(m.health.every, 10)}")
    if len(m.tools) > TOOL_WARN_COUNT:
        lines.append(f"warning: {len(m.tools)} tools; above {TOOL_WARN_COUNT} routing suffers")
    lines += [f"note: {n}" for n in staged.notes]
    return lines


def _arg_names(entry: object) -> list[str]:
    """The property names of a pinned MCP tool's stripped schema (shown at install)."""
    schema: object = (
        cast(dict[str, Any], entry).get("input_schema") if isinstance(entry, dict) else None
    )
    props: object = (
        cast(dict[str, Any], schema).get("properties") if isinstance(schema, dict) else None
    )
    return list(cast(dict[str, Any], props)) if isinstance(props, dict) else []


def update_lines(
    old: ext_store.ExtensionRecord,
    old_files: TreeEntries,
    new: Staged,
    registered: Collection[str] = (),
) -> list[str]:
    """What an update changes: pins, files, and escalations added or removed."""
    lines = [
        f"{new.name}: {old.source}@{(old.commit or 'no-commit')[:8]} digest "
        f"{old.tree_digest[:8]} -> {new.pin.heading_ref} digest {new.pin.tree_digest[:8]}"
    ]
    if new.pin.source != old.source:
        lines.append(f"  source changes from {old.source} to {new.pin.source}")
    changed = sorted(
        [f"+ {p}" for p in new.files if p not in old_files]
        + [f"- {p}" for p in old_files if p not in new.files]
        + [f"~ {p}" for p in new.files if p in old_files and old_files[p] != new.files[p]]
    )
    lines.append("Files:" if changed else "Files: unchanged")
    lines += [f"  {_s(c, 100)}" for c in changed]
    if new.pin.manifest_sha == old.manifest_sha and pins_digest(old.mcp_pins) == new.pin.mcp:
        lines.append("Manifest: unchanged (code only)")
        return lines
    before: set[str] = set()
    with contextlib.suppress(ValidationError):  # an old snapshot may not parse any more
        before = set(escalations(Manifest.model_validate(old.manifest), registered, old.mcp_pins))
    after = escalations(new.manifest, registered, new.mcp_pins)
    diff = [f"  + {e}" for e in after if e not in before]
    diff += [f"  - {e}" for e in sorted(before) if e not in after]
    old_grant = _old_grant(old)
    new_grant = report_grant(new.manifest)
    if old_grant != new_grant and not new_grant.startswith("reports: may"):
        # An urgent grant already shows as an added escalation above.
        was = old_grant.removeprefix("reports: ") or "none"
        diff = [d for d in diff if d != f"  - {old_grant}"]  # the ~ line says it
        diff.append(f"  ~ {new_grant or 'reports: none (refused)'} (was: {was})")
    what = "Manifest" if new.pin.manifest_sha != old.manifest_sha else "MCP tools"
    lines.append(f"{what}: changed. Escalations:" if diff else f"{what}: changed")
    lines += diff
    return lines


def _old_grant(old: ext_store.ExtensionRecord) -> str:
    try:
        return report_grant(Manifest.model_validate(old.manifest))
    except ValidationError:
        return ""


class ExtensionManager:
    """Owns ``<extensions_dir>/<name>/``, ``<data_dir>/<name>/`` and the ``extensions`` rows."""

    def __init__(
        self,
        conn: sqlite3.Connection,
        *,
        extensions_dir: Path,
        data_dir: Path,
        repo_path: Path | None,
        principals: Principals,
        secret_exists: Callable[[str], bool],
        ext_path: str,
        actor: str,
        clock: Callable[[], datetime] = utcnow,
        mcp_discover: Callable[[ExtRequest], list[dict[str, Any]]] | None = None,
        mcp_discover_http: HttpDiscoverFn | None = None,
        secret_value: Callable[[str], str | None] | None = None,
        allow_token: Callable[[str, str, str], bool] | None = None,
    ) -> None:
        self.conn = conn
        self.extensions_dir = extensions_dir
        self.data_dir = data_dir
        self.repo_path = repo_path
        self.principals = principals
        self.secret_exists = secret_exists
        self.ext_path = ext_path
        self.actor = actor
        self.clock = clock
        self.mcp_discover = mcp_discover or _discover_sync
        self.mcp_discover_http = mcp_discover_http or _discover_http_sync
        # An http MCP server that needs its bearer token to list its tools gets it only when
        # the owner allows it at staging (allow_token(server id, url, secret ref)), or at
        # install when the manifest is exactly the one they confirmed. Default: never.
        self.secret_value: Callable[[str], str | None] = (
            _no_secret if secret_value is None else secret_value
        )
        self.allow_token: Callable[[str, str, str], bool] = (
            _never if allow_token is None else allow_token
        )

    # --- staging ----------------------------------------------------------------------

    def stage(
        self,
        source: str | Source,
        *,
        expect_name: str | None = None,
        approved_manifest_sha: str | None = None,
    ) -> Staged:
        """Copy the source's approved bytes into a staging dir and validate them.

        ``approved_manifest_sha`` (install): the manifest the owner confirmed. A different one
        raises ``PinChanged`` before any MCP server is contacted; the same one lets an http
        server's bearer token go to its (confirmed) URL without asking again.

        Raises ``SourceError``, ``ManagerError`` (with every problem) or ``PinChanged``;
        nothing is kept.
        """
        src = parse_source(source) if isinstance(source, str) else source
        self.extensions_dir.mkdir(parents=True, exist_ok=True)
        root = self.extensions_dir / f".staging-{pysecrets.token_hex(6)}"
        root.mkdir(mode=0o700)
        try:
            if src.kind == "repo":
                if self.repo_path is None:
                    raise SourceError("ops.repo_path is not set, so repo: sources can't be read")
                files = stage_repo(self.repo_path, src.arg, root)
            else:
                files = stage_path(Path(src.arg), root)
            manifest_file = root / MANIFEST_FILE
            if manifest_file.is_symlink() or not manifest_file.is_file():
                raise ManagerError(f"{src.text}: no {MANIFEST_FILE} at the top of the tree")
            raw = manifest_file.read_bytes()
            try:
                manifest = load_manifest(manifest_file)
            except (ValidationError, ValueError) as e:
                raise ManagerError(f"{src.text}: invalid manifest", [str(e)]) from None
            problems = self._problems(src, manifest, root)
            if expect_name is not None and manifest.name != expect_name:
                problems.append(f"the manifest's name is {manifest.name!r}, not {expect_name!r}")
            if problems:
                raise ManagerError(f"{src.text}: refused", problems)
            entries = tree_entries(root)
            digest = tree_digest(root)
            manifest_sha = hashlib.sha256(raw).hexdigest()
            confirmed = approved_manifest_sha is not None
            if confirmed and manifest_sha != approved_manifest_sha:
                raise PinChanged(
                    f"{manifest.name}: the source changed since you confirmed; not installed"
                )
            mcp_pins, mcp_notes = self._discover_mcp(manifest, root, confirmed=confirmed)
            if tree_digest(root) != digest:
                raise ManagerError(f"{src.text}: refused", ["an MCP server changed its own files"])
            pin = Pin(
                source=src.text,
                commit=files.commit,
                tree_digest=digest,
                manifest_sha=manifest_sha,
                mcp=pins_digest(mcp_pins),
            )
            notes = (*files.notes, *mcp_notes)
            return Staged(manifest.name, pin, manifest, root, entries, notes, mcp_pins)
        except BaseException:
            remove_tree(root)
            raise

    def _problems(self, src: Source, manifest: Manifest, root: Path) -> list[str]:
        problems = name_errors(manifest) + dir_errors(root, manifest)
        for where, run in commands(manifest):
            # Shown verbatim in the confirmation, so it must be plain printable ASCII.
            if not (run.isascii() and run.isprintable()) or len(run) > MAX_RUN_CHARS:
                problems.append(
                    f"{where}: run must be printable ASCII, at most {MAX_RUN_CHARS} chars"
                )
        problems += approver_errors(manifest, self.principals)
        if src.kind == "repo" and manifest.name != src.arg:
            problems.append(f"repo:{src.arg} declares the name {manifest.name!r}")
        if any(j.critical for j in manifest.jobs):
            problems.append("critical jobs are not supported yet (deferred, plan section 8)")
        if not problems:
            for where, run in commands(manifest):
                try:
                    resolve_argv0(root, run, path=self.ext_path)
                except ValueError as e:
                    problems.append(f"{where}: {e}")
            for name in manifest.bins:
                try:
                    resolve_bin(name, self.ext_path)
                except ValueError as e:
                    problems.append(f"bins: {e}")
            for sid, server in (manifest.mcp.servers if manifest.mcp else {}).items():
                if isinstance(server, McpHttpServer):
                    continue
                try:
                    resolve_mcp_command(root, server.command, self.ext_path)
                except ValueError as e:
                    problems.append(f"mcp server {sid}: {e}")
        return problems

    def _discover_mcp(
        self, manifest: Manifest, root: Path, *, confirmed: bool = False
    ) -> tuple[dict[str, Any], tuple[str, ...]]:
        """Start each stdio MCP server once, from the staged copy, with no secrets and a
        scratch home, list each http server's tools (with its bearer token only as
        ``_http_token`` allows), and pin the tools the manifest lists. Raises ManagerError
        with every problem."""
        if manifest.mcp is None:
            return {}, ()
        scratch = self.extensions_dir / f".discover-{pysecrets.token_hex(6)}"
        scratch.mkdir(mode=0o700)
        pins: dict[str, Any] = {}
        notes: list[str] = []
        problems: list[str] = []
        try:
            (scratch / "tmp").mkdir(mode=0o700)
            bins = {bin_env(b): resolve_bin(b, self.ext_path) for b in manifest.bins}
            env = scratch_env(ext=manifest.name, data_dir=scratch, path=SCRATCH_PATH, bins=bins)
            for sid, server in manifest.mcp.servers.items():
                label = f"{manifest.name}/mcp:{sid}"
                try:
                    if isinstance(server, McpHttpServer):
                        token = self._http_token(manifest.name, sid, server, confirmed)
                        tools = self.mcp_discover_http(server.url, token, label)
                    else:
                        pinned = resolve_mcp_command(root, server.command, self.ext_path)
                        req = ExtRequest(
                            argv=server_argv(root, server.command, server.args, pinned),
                            cwd=root,
                            env=env,
                            timeout_s=20.0,
                            label=label,
                        )
                        tools = self.mcp_discover(req)
                except _NoToken as e:
                    problems.append(f"mcp server {sid}: {e}")
                    continue
                except Exception as e:
                    reason = getattr(e, "reason", None) or type(e).__name__
                    problems.append(f"mcp server {sid} could not list its tools ({reason})")
                    continue
                found = approve(sid, tuple(t.name for t in server.tools), tools)
                problems += found.problems
                pins |= pins_json(found.pins)
                if found.ignored:
                    extra = len(found.ignored) - 10
                    shown = ", ".join(_s(n, 40) for n in found.ignored[:10])
                    shown += f" and {extra} more" if extra > 0 else ""
                    notes.append(f"mcp server {sid}: not in the manifest, never exposed: {shown}")
        finally:
            remove_tree(scratch)
        if problems:
            raise ManagerError(f"{manifest.name}: refused", problems)
        return dict(sorted(pins.items())), tuple(notes)

    def _http_token(
        self, ext: str, sid: str, server: McpHttpServer, confirmed: bool
    ) -> str | OAuthCredentials | None:
        """The bearer token (or, for ``auth: oauth``, the credentials to get one) to list an
        http server's tools with: None without auth. The owner allows it per staging
        (``allow_token``; for OAuth, asked for the token URL, where the refresh token goes),
        or confirmed this exact manifest (its URLs included). Raises ``_NoToken``."""
        if (o := server.oauth) is not None:
            return self._oauth_creds(ext, sid, o, confirmed)
        if server.secret is None:
            return None
        ref = secret_ref(ext, server.secret)
        if not confirmed and not self.allow_token(sid, server.url, ref):
            raise _NoToken(f"needs {ref} to list its tools, and you didn't allow sending it")
        value = self.secret_value(ref)
        if value is None:
            raise _NoToken(
                f"needs {ref} to list its tools; store it first: resonant secret set {ref}"
            )
        return value

    def _oauth_creds(self, ext: str, sid: str, o: McpOAuth, confirmed: bool) -> OAuthCredentials:
        refs = [secret_ref(ext, n) for n in o.secrets]
        ref = secret_ref(ext, o.refresh_token)
        if not confirmed and not self.allow_token(sid, o.token_url, ref):
            raise _NoToken(f"needs {ref} to list its tools, and you didn't allow sending it")
        values: list[str] = []
        for r in refs:
            value = self.secret_value(r)
            if value is None:
                raise _NoToken(
                    f"needs {r} to list its tools; store it first: resonant secret set {r}"
                )
            values.append(value)
        client_id, client_secret, refresh_token = values
        return OAuthCredentials(o.token_url, client_id, client_secret, refresh_token)

    def registered_slugs(self) -> frozenset[str]:
        """Lowercase slugs in the repo registry, for flagging unfamiliar repo links."""
        return frozenset(r.slug.lower() for r in repos.list_all(self.conn))

    def discard(self, staged: Staged) -> None:
        remove_tree(staged.root)

    # --- leftovers --------------------------------------------------------------------

    def data_dir_for(self, name: str) -> Path:
        return self.data_dir / name

    def _origin(self, name: str) -> dict[str, object]:
        value: object = kv_get(self.conn, ORIGIN_KEY.format(name=name))
        return cast(dict[str, object], value) if isinstance(value, dict) else {}

    def recorded_source(self, name: str) -> str | None:
        """The source of the last install of ``name`` (kept after remove), or None."""
        value = self._origin(name).get("source")
        return value if isinstance(value, str) else None

    def recorded_secrets(self, name: str) -> list[str]:
        """Secret names the last install of ``name`` declared."""
        value = self._origin(name).get("secrets")
        items = cast(list[object], value) if isinstance(value, list) else []
        return [v for v in items if isinstance(v, str)]

    def present_secrets(self, name: str, names: Iterable[str]) -> list[str]:
        refs: list[str] = []
        for n in sorted(set(names)):
            try:
                ref = secret_ref(name, n)
            except InvalidSecretRef:
                continue
            if self.secret_exists(ref):
                refs.append(ref)
        return refs

    def leftovers(self, staged: Staged) -> list[str]:
        """Why ``--adopt`` is needed: state or secrets from a different (or unknown) source."""
        name, source = staged.name, staged.pin.source
        row = ext_store.get_extension(self.conn, name)
        prior = row.source if row is not None else self.recorded_source(name)
        data = self.data_dir_for(name)
        old_secrets = list(row.manifest.get("secrets") or ()) if row is not None else []
        old_secrets += self.recorded_secrets(name)
        reasons: list[str] = []
        if data.exists() and prior is None:
            reasons.append(f"{data} exists but no install recorded which source created it")
        if prior is not None and prior != source:
            if data.exists():
                reasons.append(f"{data} was created by {prior}")
            secrets = self.present_secrets(name, [*staged.manifest.secrets, *old_secrets])
            if secrets:
                reasons.append(f"Keychain items {', '.join(secrets)} were stored for {prior}")
        return reasons

    # --- install ----------------------------------------------------------------------

    def install(
        self, approved: Pin, *, name: str, adopt: bool = False
    ) -> ext_store.ExtensionRecord:
        """Install or update to exactly ``approved`` (what the owner confirmed).

        Re-stages the source; refuses (``PinChanged``) if any bound value differs, before
        and after the copy. Refuses (``AdoptRequired``) leftovers without ``adopt``. The
        row and the ``ext.install``/``ext.update`` audit row are written last, together.
        """
        staged = self.stage(
            approved.source, expect_name=name, approved_manifest_sha=approved.manifest_sha
        )
        old_tmp: Path | None = None
        final = self.extensions_dir / name
        swapped = False
        try:
            if staged.pin != approved:
                raise PinChanged(f"{name}: the source changed since you confirmed; not installed")
            reasons = self.leftovers(staged)
            if reasons and not adopt:
                raise AdoptRequired(f"{name}: leftovers from another source", reasons)
            previous = ext_store.get_extension(self.conn, name)
            if final.exists() or final.is_symlink():
                old_tmp = self.extensions_dir / f".old-{name}-{pysecrets.token_hex(6)}"
                final.rename(old_tmp)
            staged.root.rename(final)
            swapped = True
            make_readonly(final)
            after = Pin(
                source=approved.source,
                commit=approved.commit,
                tree_digest=tree_digest(final),
                manifest_sha=hashlib.sha256((final / MANIFEST_FILE).read_bytes()).hexdigest(),
                mcp=pins_digest(staged.mcp_pins),
            )
            if after != approved:
                raise PinChanged(f"{name}: the copy doesn't match what you confirmed")
            record = ext_store.ExtensionRecord(
                name=name,
                source=approved.source,
                commit=approved.commit,
                tree_digest=approved.tree_digest,
                manifest_sha=approved.manifest_sha,
                manifest=staged.manifest.model_dump(mode="json"),
                installed_at=self.clock(),
                installed_by=self.actor,
                argv0_paths=self._argv0_paths(final, staged.manifest),
                status="disabled" if previous and previous.status == "disabled" else "enabled",
                mcp_pins=staged.mcp_pins,
            )
            action = "ext.update" if previous is not None else "ext.install"
            with atomic(self.conn):
                ext_store.save_extension(self.conn, record)
                ext_store.prune_jobs(self.conn, name, [j.id for j in staged.manifest.jobs])
                repos.set_links(self.conn, name, staged.manifest.repos)
                origin = {"source": approved.source, "secrets": sorted(staged.manifest.secrets)}
                kv_set(self.conn, ORIGIN_KEY.format(name=name), origin)
                audit(
                    self.conn,
                    action,
                    requested_by=self.actor,
                    extension=name,
                    source=approved.source,
                    commit=approved.commit,
                    tree_digest=approved.tree_digest,
                    manifest_sha=approved.manifest_sha,
                    adopted=bool(reasons),
                    path="cli",
                )
        except BaseException:
            try:
                remove_tree(final if swapped else staged.root)
            finally:
                if old_tmp is not None:
                    old_tmp.rename(final)
            raise
        if old_tmp is not None:
            remove_tree(old_tmp)
        self._ensure_data_dir(name)
        return record

    def _argv0_paths(self, root: Path, manifest: Manifest) -> dict[str, str]:
        """Pinned absolute argv[0] per spec name (``health`` for the health check), and each
        manifest ``bins`` entry under ``bin:<name>``."""
        names = {f"tool {n}": n for n in manifest.tools}
        names.update({f"job {j.id}": j.spec_name for j in manifest.jobs})
        names["health"] = "health"
        pins = {
            names[where]: resolve_argv0(root, run, self.ext_path)
            for where, run in commands(manifest)
        }
        pins.update({bin_key(b): resolve_bin(b, self.ext_path) for b in manifest.bins})
        for sid, server in (manifest.mcp.servers if manifest.mcp else {}).items():
            if isinstance(server, McpHttpServer):
                continue  # its url is in the manifest, bound by manifest_sha
            pins[mcp_key(sid)] = resolve_mcp_command(root, server.command, self.ext_path)
        return pins

    def _ensure_data_dir(self, name: str) -> None:
        data = self.data_dir_for(name)
        data.mkdir(mode=0o700, parents=True, exist_ok=True)
        (data / "tmp").mkdir(mode=0o700, exist_ok=True)

    # --- remove -----------------------------------------------------------------------

    def remove(self, name: str, *, purge: bool = False) -> Removed:
        """Delete the row (and its job state), then the copy. Keeps the data dir unless
        ``purge``. Keychain items are never deleted here; they are listed instead."""
        record = ext_store.get_extension(self.conn, name)
        if record is None:
            raise ManagerError(f"{name} is not installed")
        with atomic(self.conn):
            ext_store.delete_extension(self.conn, name)
            repos.drop_links(self.conn, name)
            audit(
                self.conn,
                "ext.remove",
                requested_by=self.actor,
                extension=name,
                source=record.source,
                tree_digest=record.tree_digest,
                purge=purge,
                path="cli",
            )
        remove_tree(self.extensions_dir / name)
        data = self.data_dir_for(name)
        if purge:
            remove_tree(data)
        secrets = self.present_secrets(name, record.manifest.get("secrets") or ())
        return Removed(record, secrets, data if data.exists() else None)

    # --- status -----------------------------------------------------------------------

    def verify(self, record: ext_store.ExtensionRecord) -> CopyState:
        """``ok``, ``missing`` or ``changed``: the installed copy against the pinned digest."""
        return verify_copy(record, self.extensions_dir / record.name)

    def installed_files(self, name: str) -> TreeEntries:
        root = self.extensions_dir / name
        try:
            return tree_entries(root) if root.is_dir() else {}
        except (OSError, TreeError):
            return {}
