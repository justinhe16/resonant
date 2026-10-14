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

import contextlib
import hashlib
import re
import secrets as pysecrets
import shlex
import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import cast

from pydantic import ValidationError

from resonant.extensions.checks import (
    approver_errors,
    commands,
    dir_errors,
    name_errors,
    resolve_argv0,
)
from resonant.extensions.pin import (
    CopyState,
    TreeEntries,
    TreeError,
    tree_digest,
    tree_entries,
    verify_copy,
)
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
from resonant.store.audit import audit
from resonant.store.db import atomic, kv_get, kv_set, utcnow
from resonant_sdk import Manifest, load_manifest
from resonant_sdk.tool import Level

MANIFEST_FILE = "resonant.yaml"
ORIGIN_KEY = "ext.origin.{name}"  # kv: {source, secrets} of the last install
NETWORK_COMMANDS = frozenset({"gh", "git"})
MAX_RUN_CHARS = 300  # commands are shown in full, verbatim
TOOL_WARN_COUNT = 12  # above this, routing accuracy suffers (plan D9)
_ASKS = (Level.L1, Level.L2)


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


def escalations(manifest: Manifest) -> list[str]:
    """What raises the stakes, one line each: secrets, approvers, asking tools and jobs,
    and commands that use the network (``gh``/``git``). Stable text, so updates can diff."""
    out: list[str] = []
    for name in manifest.secrets:
        out.append(f"secret {_s(f'{manifest.name}/{name}')} (Keychain)")
    for name in manifest.approvers:
        out.append(f"approver {_s(name)} may approve this extension's actions")
    for spec in manifest.tool_specs():
        level = spec.effective_level
        if level in _ASKS:
            extra = ", irreversible (yes <code>)" if spec.commit or level is Level.L1 else ""
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
    return out


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


def confirm_lines(staged: Staged) -> list[str]:
    """The install/update confirmation: heading, escalations first, then everything else."""
    m = staged.manifest
    lines = [heading(staged), ""]
    esc = escalations(m)
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
        lines.append(
            f"  {_s(spec.name, 40)} {spec.effect.value} {spec.effective_level.value}{flag}"
            f": {_s(spec.description or '-')}"
        )
    for name, tool in m.tools.items():
        lines.append(f"    {_s(name, 40)} runs {tool.run}")
    lines.append("Jobs:" if m.jobs else "Jobs: none")
    for job in m.jobs:
        spec = job.spec(m.name)
        lines.append(
            f"  {_s(job.id, 40)} {schedule_words(job.schedule, job.tz)}, "
            f"{spec.effect.value} {spec.effective_level.value}, catchup {_s(job.catchup, 10)}"
            f", runs {job.run}"
        )
    if m.notify:
        events = ", ".join(_s(e, 40) for e in m.notify.on)
        lines.append(f"Notify: {events} via {m.notify.channel}")
    if m.health:
        lines.append(f"Health: {m.health.check} every {_s(m.health.every, 10)}")
    if len(m.tools) > TOOL_WARN_COUNT:
        lines.append(f"warning: {len(m.tools)} tools; above {TOOL_WARN_COUNT} routing suffers")
    lines += [f"note: {n}" for n in staged.notes]
    return lines


def update_lines(old: ext_store.ExtensionRecord, old_files: TreeEntries, new: Staged) -> list[str]:
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
    if new.pin.manifest_sha == old.manifest_sha:
        lines.append("Manifest: unchanged (code only)")
        return lines
    before: set[str] = set()
    with contextlib.suppress(ValidationError):  # an old snapshot may not parse any more
        before = set(escalations(Manifest.model_validate(old.manifest)))
    after = escalations(new.manifest)
    diff = [f"  + {e}" for e in after if e not in before]
    diff += [f"  - {e}" for e in sorted(before) if e not in after]
    lines.append("Manifest: changed. Escalations:" if diff else "Manifest: changed")
    lines += diff
    return lines


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

    # --- staging ----------------------------------------------------------------------

    def stage(self, source: str | Source, *, expect_name: str | None = None) -> Staged:
        """Copy the source's approved bytes into a staging dir and validate them.

        Raises ``SourceError`` or ``ManagerError`` (with every problem); nothing is kept.
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
            pin = Pin(
                source=src.text,
                commit=files.commit,
                tree_digest=tree_digest(root),
                manifest_sha=hashlib.sha256(raw).hexdigest(),
            )
            return Staged(manifest.name, pin, manifest, root, entries, files.notes)
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
        if manifest.mcp is not None:
            problems.append("MCP servers are not supported yet (deferred, plan section 8)")
        if any(j.critical for j in manifest.jobs):
            problems.append("critical jobs are not supported yet (deferred, plan section 8)")
        if not problems:
            for where, run in commands(manifest):
                try:
                    resolve_argv0(root, run, path=self.ext_path)
                except ValueError as e:
                    problems.append(f"{where}: {e}")
        return problems

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
        staged = self.stage(approved.source, expect_name=name)
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
            )
            action = "ext.update" if previous is not None else "ext.install"
            with atomic(self.conn):
                ext_store.save_extension(self.conn, record)
                ext_store.prune_jobs(self.conn, name, [j.id for j in staged.manifest.jobs])
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
        """Pinned absolute argv[0] per spec name (``health`` for the health check)."""
        names = {f"tool {n}": n for n in manifest.tools}
        names.update({f"job {j.id}": j.spec_name for j in manifest.jobs})
        names["health"] = "health"
        return {
            names[where]: resolve_argv0(root, run, self.ext_path)
            for where, run in commands(manifest)
        }

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
