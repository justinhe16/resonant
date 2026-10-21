"""Daemon configuration.

Loaded from ``$RESONANT_HOME/config.yaml`` (default ``~/.resonant``), with
``RESONANT_*`` environment variables taking precedence (``__`` for nesting).
Nothing in here is secret: credentials live in the macOS Keychain and are read
only by the daemon (the executor for extensions, the runner for ``runner/*``).
"""

from __future__ import annotations

import ipaddress
import os
import re
from datetime import time, timedelta
from pathlib import Path
from typing import Any, Literal, Self, cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from resonant_sdk.manifest import duration_s

DEFAULT_HOME = Path("~/.resonant")


def resonant_home() -> Path:
    return Path(os.environ.get("RESONANT_HOME", DEFAULT_HOME)).expanduser()


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


def is_loopback(host: str) -> bool:
    """``host`` (a name or an IP, no port or brackets) is ``localhost`` or a loopback IP.
    The API's request guard uses the same rule (``api.LoopbackOnly``)."""
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ModelConfig(_Section):
    base_url: str = "http://127.0.0.1:11434/v1"
    # Non-thinking Instruct-2507 build. Not `qwen3:30b-a3b`: that tag is the thinking-only
    # Thinking-2507 build, which ignores `think: false` and reasons before every tool call.
    name: str = "qwen3:30b-a3b-instruct-2507-q4_K_M"
    context_tokens: int = Field(default=32768, gt=0)
    timeout_s: float = Field(default=60.0, gt=0)
    # "ollama": native /api/chat (top-level `think: false`, the documented switch for qwen3).
    # "openai": the OpenAI-compatible /v1 endpoint (portable, e.g. to MLX later).
    # `resonant model probe` checks that thinking is actually off for the chosen adapter.
    api: Literal["openai", "ollama"] = "ollama"

    @field_validator("base_url")
    @classmethod
    def _local_model(cls, v: str) -> str:
        # The Ollama LaunchAgent listens on this host; it must never be reachable off-box.
        if not is_loopback(urlparse(v).hostname or ""):
            raise ValueError("model.base_url must point at a loopback host")
        return v


class ApiConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=7777, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # No public ports: remote status goes through the push-only status relay (relay:).
        if not is_loopback(v):
            raise ValueError("api.host must be a loopback address")
        return v


class HealthConfig(_Section):
    healthchecks_url: str | None = None
    ping_every_s: float = Field(default=60.0, gt=0)
    watchdog_stale_s: float = Field(default=120.0, gt=0)


_SUPABASE_KEY = re.compile(r"[A-Za-z0-9._-]{20,1024}")


class RelayConfig(_Section):
    """The status relay (docs/dashboard-plan.md D4, DB-3): the daemon pushes the ``remote``
    tier of the dashboard snapshot to Supabase every ``every_s``. Push only: nothing connects
    in. Off by default; turned on in go-live step 15."""

    enabled: bool = False
    # The Supabase project URL, https://<ref>.supabase.co. https only, no path or query.
    supabase_url: str | None = None
    # The project's anon (publishable) key. Not a secret: the deployed dashboard ships it in
    # its config.json, and on its own it can read and write nothing (the migration's grants).
    # The write credential is the Keychain item relay/supabase_key (the ingest token).
    anon_key: str | None = None
    every_s: float = Field(default=60.0, ge=15, le=3600)
    timeout_s: float = Field(default=10.0, gt=0, le=30)  # per HTTP attempt
    max_attempts: int = Field(default=3, ge=1, le=5)  # per push; the rest waits for the next

    @field_validator("supabase_url")
    @classmethod
    def _https(cls, v: str | None) -> str | None:
        if v is None:
            return None
        u = urlparse(v)
        if (
            u.scheme != "https"
            or not u.hostname
            or u.username is not None
            or u.password is not None
            or u.path not in ("", "/")
            or u.params
            or u.query
            or u.fragment
        ):
            raise ValueError(
                'relay.supabase_url must be a bare https URL, like "https://<ref>.supabase.co"'
            )
        if is_loopback(u.hostname):
            raise ValueError("relay.supabase_url must not be a loopback host")
        return v.rstrip("/")

    @field_validator("anon_key")
    @classmethod
    def _key(cls, v: str | None) -> str | None:
        if v is not None and not _SUPABASE_KEY.fullmatch(v):
            raise ValueError("relay.anon_key doesn't look like a Supabase anon or publishable key")
        return v

    @model_validator(mode="after")
    def _complete(self) -> Self:
        if self.enabled and (self.supabase_url is None or self.anon_key is None):
            raise ValueError("relay.enabled needs relay.supabase_url and relay.anon_key")
        return self


class LoopConfig(_Section):
    tick_s: float = Field(default=30.0, gt=0)
    stuck_after_s: float = Field(default=900.0, gt=0)
    max_attempts: int = Field(default=3, ge=1)


class ClaudeConfig(_Section):
    max_concurrent: int = Field(default=5, ge=1)
    reserved_oncall_slots: int = Field(default=1, ge=0)

    @model_validator(mode="after")
    def _reserve_fits(self) -> Self:
        if self.reserved_oncall_slots >= self.max_concurrent:
            raise ValueError("claude.reserved_oncall_slots must be < claude.max_concurrent")
        return self


class IMessageConfig(_Section):
    # Resonant's own Apple ID email handle (iMessage only; no phone number registered).
    # The adapter skips it on receive and uses it for the send/receive self-test.
    self_handle: str | None = None
    # Turns on the Phase 1 stack (resonant.wiring): reader, router, local runner, monitor.
    enabled: bool = False
    # Opened read-only (mode=ro); needs Full Disk Access for the daemon's Python.
    db_path: Path = Field(default=Path("~/Library/Messages/chat.db"), validate_default=True)
    poll_s: float = Field(default=2.0, gt=0)
    inbound_per_minute: int = Field(default=120, ge=1)  # per principal; excess is dropped
    max_body_chars: int = Field(default=4000, ge=1)  # longer bodies are truncated
    # Outbound (resonant.gateway.imessage.channel): per-principal token bucket.
    outbound_per_minute: int = Field(default=60, ge=1)
    # Optional principal -> handle to send to. It must be one of that principal's
    # `imessage:` identities in principals.yaml; otherwise the first identity is used.
    preferred_handles: dict[str, str] = Field(default_factory=dict)

    @field_validator("db_path")
    @classmethod
    def _expand_db_path(cls, v: Path) -> Path:
        return v.expanduser()

    @field_validator("self_handle")
    @classmethod
    def _email_handle(cls, v: str | None) -> str | None:
        if v is not None and (v != v.lower() or "@" not in v):
            raise ValueError("channels.imessage.self_handle must be a lowercase email handle")
        return v


class ChannelsConfig(_Section):
    imessage: IMessageConfig = IMessageConfig()


class GateConfig(_Section):
    # How long an approval prompt stays answerable. The waiting task wakes at expiry and
    # tells the owner it wasn't done.
    approval_ttl_s: float = Field(default=1800.0, ge=60, le=86400)
    # BA-1: at most this many changes wait on one yes. A task (a Claude brain turn, the local
    # loop, a one-shot ask) collects the changes that need approval and asks once for all of
    # them; past this, the next one is refused. Changes that run without asking (L3, a
    # standing allow) are capped apart, at toolcore.MAX_SIDE_EFFECTS (5) per task.
    batch_max: int = Field(default=10, ge=2, le=20)


class TrustConfig(_Section):
    """Worker trust checks (``resonant check``; ``trust/policy.py``)."""

    # A check costing more than this (USD) asks the owner even on a seen system and action;
    # at or under it, cost alone isn't net-new (GT-1). A cost that isn't a number always asks.
    cost_ask_over_usd: float = Field(default=20.0, ge=0, allow_inf_nan=False)


def _package_checkout() -> Path | None:
    """The git checkout containing the running package, if it runs from one."""
    for parent in Path(__file__).resolve().parents:
        if (parent / ".git").exists() and (parent / "pyproject.toml").is_file():
            return parent
    return None


class OpsConfig(_Section):
    # The repo `/update` pulls into. Default: the checkout this package runs from (None
    # when installed outside a checkout, and `/update` refuses).
    repo_path: Path | None = Field(default_factory=_package_checkout)
    # The `uv` binary `/update` syncs with. launchd's PATH is minimal, so this is an
    # absolute path; None means `/update` refuses.
    uv_path: Path | None = None

    @field_validator("repo_path", "uv_path")
    @classmethod
    def _expand(cls, v: Path | None) -> Path | None:
        return v.expanduser() if v is not None else None


class PowerConfig(_Section):
    # Install the caffeinate LaunchAgent (`resonant daemon install`), so the Mini never
    # sleeps: a sleeping Mini doesn't read chat.db or fire reminders.
    keep_awake: bool = True


def _absolute(v: Path, field: str) -> Path:
    if not v.is_absolute():
        raise ValueError(f"{field} must be an absolute path")
    return v


class ProcessConfig(_Section):
    """Extension processes (``resonant.extensions.process``). Caps are bounded both ways."""

    # The child's whole PATH. Nothing else comes from the daemon's environment. System dirs
    # only: Claude workers may write /opt/homebrew/bin (gh and git are pinned by absolute path
    # at install, so they don't need it).
    path: tuple[Path, ...] = (Path("/usr/bin"), Path("/bin"))
    # stdout (and fd-3 events) past this kills the process tree: "output too large".
    stdout_max_bytes: int = Field(default=256 * 1024, ge=1024, le=4 * 1024 * 1024)
    # stderr is kept up to this, redacted, and never shown.
    stderr_max_bytes: int = Field(default=8 * 1024, ge=256, le=64 * 1024)
    # SIGTERM to the group and tracked tree, then SIGKILL after this.
    kill_grace_s: float = Field(default=2.0, gt=0, le=30)
    # How often descendants are sampled into the ProcessTracker.
    tree_sample_s: float = Field(default=2.0, gt=0, le=30)
    # Timeout of a manifest `health.check` run.
    health_timeout_s: float = Field(default=20.0, gt=0, le=120)

    @field_validator("path")
    @classmethod
    def _absolute_dirs(cls, v: tuple[Path, ...]) -> tuple[Path, ...]:
        if not v:
            raise ValueError("extensions.process.path must not be empty")
        for p in v:
            _absolute(p, "extensions.process.path entries")
            if ":" in str(p):
                raise ValueError("extensions.process.path entries may not contain ':'")
        return v

    @property
    def path_env(self) -> str:
        return ":".join(str(p) for p in self.path)


class NotifyConfig(_Section):
    """Extension notifications (``resonant.extensions.notify``)."""

    # Per extension per hour; the rest collapse into one "+N more updates".
    max_per_hour: int = Field(default=20, ge=1, le=200)
    # Notification text is cleaned and clipped to this many characters.
    max_chars: int = Field(default=300, ge=40, le=1000)


class ExtensionsConfig(_Section):
    # L0 jobs run inline as bounded background coroutines, at most this many at once.
    max_concurrent_jobs: int = Field(default=4, ge=1, le=16)
    # A tool or job result is stored (redacted) up to this many bytes...
    result_max_bytes: int = Field(default=32 * 1024, ge=1024, le=256 * 1024)
    # ...and the model sees at most this many characters of it, as untrusted data.
    model_result_max_chars: int = Field(default=4096, ge=256, le=32 * 1024)
    process: ProcessConfig = ProcessConfig()
    notify: NotifyConfig = NotifyConfig()

    @model_validator(mode="after")
    def _model_view_fits(self) -> Self:
        if self.model_result_max_chars > self.result_max_bytes:
            raise ValueError(
                "extensions.model_result_max_chars must be <= extensions.result_max_bytes"
            )
        return self


class SecretsConfig(_Section):
    """Keychain access (``resonant.secrets``). Values are never in config."""

    # Items created by this binary trust it, so the GUI-session daemon reads without a prompt.
    security_path: Path = Path("/usr/bin/security")
    timeout_s: float = Field(default=5.0, gt=0, le=30)

    @field_validator("security_path")
    @classmethod
    def _absolute_binary(cls, v: Path) -> Path:
        return _absolute(v, "secrets.security_path")


_HHMM = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class BriefingConfig(_Section):
    """The core daily briefing (``core/briefing``), sent to the owner once a day."""

    enabled: bool = True
    # Local time in `settings.timezone`, 24-hour "HH:MM".
    at: str = "08:00"

    @field_validator("at")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.fullmatch(v):
            raise ValueError('briefing.at must be 24-hour "HH:MM", e.g. "08:00"')
        return v

    @property
    def local_time(self) -> time:
        hour, minute = self.at.split(":")
        return time(int(hour), int(minute))


class BackupConfig(_Section):
    """The daily store backup (``core/backup``, BK-1): an online SQLite backup to
    ``<home>/backups/daily/resonant-YYYY-MM-DD.db``, checked, then renamed into place."""

    enabled: bool = True
    # Local time in `settings.timezone`, 24-hour "HH:MM". A quiet hour.
    daily_at: str = "03:30"
    # Retention: the newest `keep_daily` copies plus the newest `keep_weekly` Sunday copies.
    keep_daily: int = Field(default=7, ge=1, le=366)
    keep_weekly: int = Field(default=4, ge=0, le=104)
    # Optional second copy (another disk, an iCloud Drive folder). The directory must
    # already exist: Resonant never creates it (an unmounted disk would otherwise end up
    # as a folder on the boot disk). Same names and retention. Never uploaded by Resonant.
    extra_dir: Path | None = None
    # No successful backup for this long: the `backup` health check degrades and alerts.
    alert_after_hours: int = Field(default=36, ge=25, le=24 * 14)

    @field_validator("daily_at")
    @classmethod
    def _hhmm(cls, v: str) -> str:
        if not _HHMM.fullmatch(v):
            raise ValueError('backup.daily_at must be 24-hour "HH:MM", e.g. "03:30"')
        return v

    @field_validator("extra_dir")
    @classmethod
    def _extra(cls, v: Path | None) -> Path | None:
        return None if v is None else _absolute(v.expanduser(), "backup.extra_dir")

    @property
    def local_time(self) -> time:
        return _hhmm_time(self.daily_at)


class RetentionConfig(_Section):
    """The daily store prune (``core/prune``, PER-259, ``resonant.store_upkeep``): how long
    rows are kept before they are deleted. Runs half an hour after ``backup.daily_at`` and only
    when a good backup is under 24 hours old. A row something still points at is never deleted
    (an open task's events, an approval's resolving event, the brain summary's cursor event, a
    task with approvals, runs or texts), however old it is."""

    enabled: bool = True
    # Consumed events older than this. At least 7: the github rollup reads the last 7 days of
    # events, and the brain's recent history is read from them too.
    events_days: int = Field(default=90, ge=7, le=3650)
    # Finished (done, failed, cancelled) tasks not touched for this long. At least 7: the
    # `system_texts` a task's end text is kept for 7 days, and `/projects` lists recent work.
    tasks_days: int = Field(default=90, ge=7, le=3650)
    # Audit rows. Longer by default and at least 30 days: it is the record of what Resonant did.
    audit_days: int = Field(default=365, ge=30, le=3650)


_QUIET_HOURS = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)-([01]\d|2[0-3]):([0-5]\d)$")


def _hhmm_time(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


class PulseConfig(_Section):
    """The pulse (docs/pulse-plan.md): hourly "btw" messages and urgent interrupts."""

    enabled: bool = True
    # Local time in `settings.timezone`, "HH:MM-HH:MM". Only urgent items go out inside it.
    # A window whose end is before its start wraps midnight ("22:00-06:00").
    quiet_hours: str = "00:00-06:00"
    # How long an approval waits before the next hourly pulse mentions it once more (a
    # duration: 10m, 1h). Capped at half of `gate.approval_ttl_s` (30m by default), so the
    # approval hasn't expired by then.
    remention_after: str = "10m"
    # GH-2: a run or open PR with no activity this long is flagged once in the progress
    # rollup ("looks like R3 got dropped, ~5h quiet"). A duration, 1h to 168h.
    stall_after: str = "4h"

    @field_validator("quiet_hours")
    @classmethod
    def _quiet_hours(cls, v: str) -> str:
        m = _QUIET_HOURS.fullmatch(v)
        if not m:
            raise ValueError('pulse.quiet_hours must look like "00:00-06:00"')
        if v[:5] == v[6:]:
            raise ValueError("pulse.quiet_hours must start and end at different times")
        return v

    @field_validator("remention_after")
    @classmethod
    def _remention(cls, v: str) -> str:
        try:
            seconds = duration_s(v)
        except ValueError as e:
            raise ValueError("pulse.remention_after must look like 30m or 4h") from e
        if not 5 * 60 <= seconds <= 24 * 3600:
            raise ValueError("pulse.remention_after must be between 5m and 24h")
        return v

    @field_validator("stall_after")
    @classmethod
    def _stall_after(cls, v: str) -> str:
        try:
            seconds = duration_s(v)
        except ValueError as e:
            raise ValueError("pulse.stall_after must look like 4h or 90m") from e
        if not 3600 <= seconds <= 7 * 24 * 3600:
            raise ValueError("pulse.stall_after must be between 1h and 168h")
        return v

    @property
    def quiet_start(self) -> time:
        return _hhmm_time(self.quiet_hours[:5])

    @property
    def quiet_end(self) -> time:
        return _hhmm_time(self.quiet_hours[6:])

    def in_quiet(self, local_time: time) -> bool:
        """Whether a local wall-clock time is inside the quiet window (start inclusive)."""
        start, end = self.quiet_start, self.quiet_end
        if start < end:
            return start <= local_time < end
        return local_time >= start or local_time < end  # wraps midnight

    @property
    def remention(self) -> timedelta:
        return timedelta(seconds=duration_s(self.remention_after))

    @property
    def stall(self) -> timedelta:
        return timedelta(seconds=duration_s(self.stall_after))


_RUNNER_REPO_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_RUNNER_REMOTE = re.compile(r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_RUNNER_BASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,99}")


class RunnerRepo(_Section):
    """One repo from the optional ``runner.repos`` seed (docs/repos-plan.md D1)."""

    # "github.com/owner/repo": no scheme, no credentials; the daemon adds https://.
    remote: str
    base: str = "main"
    # base, allow_github_changes and quote_ask are written to the registry only when the entry
    # sets them (model_fields_set); unset, the registry keeps its value, or for a new repo uses
    # its defaults (base "main", allow_github_changes on, quote_ask on unless a fork repo).
    # `.github/**` changes are refused when this is false.
    allow_github_changes: bool = True
    # Quote the owner's ask in the PR body (false for public repos that shouldn't show it).
    quote_ask: bool = True
    # Where the owner's checkout lives, when not at <runner.code_dir>/<name> (docs/employee-plan.md
    # D1). It is fetched and given worktrees, never checked out or reset.
    path: Path | None = None

    @field_validator("remote")
    @classmethod
    def _remote(cls, v: str) -> str:
        v = v.removesuffix(".git")
        if not _RUNNER_REMOTE.fullmatch(v) or {".", ".."} & set(v.split("/")):
            raise ValueError('runner repo remote must look like "github.com/owner/repo"')
        return v

    @field_validator("path")
    @classmethod
    def _path(cls, v: Path | None) -> Path | None:
        if v is None:
            return None
        v = v.expanduser()
        if not v.is_absolute():
            raise ValueError("runner repo path must be an absolute path")
        return v

    @field_validator("base")
    @classmethod
    def _base(cls, v: str) -> str:
        if not _RUNNER_BASE.fullmatch(v) or ".." in v or v.endswith(("/", ".lock")):
            raise ValueError("runner repo base must be a plain branch name")
        return v

    @property
    def slug(self) -> str:
        """``owner/repo``, as ``gh --repo`` wants it."""
        return self.remote.removeprefix("github.com/")


class GitHubAppConfig(_Section):
    """The GitHub App that gives each run its own one-repo token (docs/repos-plan.md D10).
    Both numbers are public identifiers; the private key is the Keychain item
    ``runner/github_app_key``."""

    app_id: int = Field(gt=0)
    installation_id: int = Field(gt=0)


class RunnerConfig(_Section):
    """The Claude runner (docs/employee-plan.md D1). Nothing here is secret."""

    # The wall around workers (docs/employee-plan.md, GD-1). ``off`` (the default): a worker
    # runs like the owner at the keyboard, with the real HOME (the owner's ~/.claude skills,
    # CLAUDE.md, settings and MCP servers) and no sandbox-exec; trust rules and approvals are
    # followed by the worker as instructed, not enforced by the OS. ``thin``: EM-1's
    # allow-default Seatbelt profile around every session and the daemon's git in checkouts, a
    # run-local HOME and project settings only (``resonant sandbox check`` proves it).
    guard: Literal["off", "thin"] = "off"
    # Remote Control per worker (RC-1): ``on`` asks each session for a claude.ai session the
    # owner can watch and steer from the Claude app (an undocumented control request; a reply
    # that fails or never comes leaves the run going without it). ``off`` doesn't ask.
    remote_control: Literal["on", "off"] = "on"
    # The per-run token source. Without it workers can't push.
    github_app: GitHubAppConfig | None = None

    # name -> repo. Only a seed: at startup these are imported into the repo registry (the
    # `repos` table) as source=owner, and the registry is what the runner reads. Repos not
    # listed here are registered the first time they are used (docs/repos-plan.md D1).
    repos: dict[str, RunnerRepo] = Field(default_factory=dict[str, RunnerRepo])
    # Run dirs (the session's home, tmp, credentials). Not under ~/.resonant, which the sandbox
    # denies. The work itself happens in a worktree under code_dir.
    work_dir: Path = Field(default=Path("~/resonant-runs"), validate_default=True)
    # The owner's checkouts, <code_dir>/<name> (cloned on first use, a repo's `path` overrides
    # it), each run's worktree at <code_dir>/.worktrees/<name>/R<n> and the scratch dirs of
    # runs with no repo at <code_dir>/.scratch/R<n> (docs/employee-plan.md D1).
    code_dir: Path = Field(default=Path("~/code"), validate_default=True)
    # Bare mirrors, daemon-only: what the pre-merge checks read (a checkout is worker-writable).
    mirror_dir: Path = Field(default=Path("~/.resonant/runner/mirrors"), validate_default=True)
    # Per-call token files for git and gh (0700, daemon-only, never sandbox-readable).
    secrets_dir: Path = Field(default=Path("~/.resonant/run-secrets"), validate_default=True)
    # Package caches shared between runs and used by nothing outside the sandbox.
    cache_dir: Path = Field(default=Path("~/resonant-runs/.cache"), validate_default=True)
    # Extra paths a session may neither read nor write, on top of the thin guard's own
    # (run-secrets, Messages, the Keychain; resonant/sandbox/runner.sb).
    deny_read: list[Path] = Field(default_factory=list[Path])
    # The disk watchdog (docs/employee-plan.md D1): each run's folder and worktree (MiB) and
    # the shared cache (GiB) are capped, and a growing run is stopped when its volume has
    # under min_free_disk_mb (MiB) free. What can't be read is skipped, never over a cap.
    max_run_dir_mb: int = Field(default=5000, ge=100)
    min_free_disk_mb: int = Field(default=10240, ge=0)
    max_cache_gb: int = Field(default=20, ge=1)
    max_minutes: int = Field(default=45, ge=1, le=600)
    max_budget_usd: float | None = Field(default=None, gt=0)
    # How long a worker's `resonant ask` (or a `resonant check` that asks the owner) stays
    # open. After it the session resumes with "no answer from the owner" (EM-3, D3).
    ask_timeout_minutes: int = Field(default=240, ge=1, le=7 * 24 * 60)
    # DEPRECATED, ignored: the bundle handback it limited is gone (EM-2). Accepted only so
    # old config files still load (extra keys are refused); drop it from your config.
    max_bundle_mb: int = Field(default=50, ge=1, le=500)
    # Days a finished run's dir is kept (a daily job deletes it after).
    keep_days: int = Field(default=7, ge=0, le=365)
    git_path: Path = Path("/usr/bin/git")
    gh_path: Path = Path("/opt/homebrew/bin/gh")

    @field_validator("guard", "remote_control", mode="before")
    @classmethod
    def _on_off(cls, v: object) -> object:
        # YAML 1.1 reads a bare on/off as a boolean: `guard: off` means "off", not False.
        if isinstance(v, bool):
            return "on" if v else "off"
        return v

    @field_validator("repos")
    @classmethod
    def _names(cls, v: dict[str, RunnerRepo]) -> dict[str, RunnerRepo]:
        for name in v:
            if not _RUNNER_REPO_NAME.fullmatch(name):
                raise ValueError(f"runner repo name {name!r} must match [a-z][a-z0-9_-]{{0,31}}")
        return v

    @field_validator("work_dir", "code_dir", "mirror_dir", "cache_dir", "secrets_dir")
    @classmethod
    def _dirs(cls, v: Path) -> Path:
        v = v.expanduser()
        if not v.is_absolute():
            raise ValueError("runner directories must be absolute paths")
        # Symlinks resolved now, so overlap checks and the sandbox see the same real path.
        return v.resolve()

    @field_validator("deny_read")
    @classmethod
    def _deny_read(cls, v: list[Path]) -> list[Path]:
        return [cls._dirs(p) for p in v]

    @model_validator(mode="after")
    def _mirror_apart(self) -> Self:
        # The mirror is daemon-only; the sandbox can write the work and cache dirs.
        for private, plabel in ((self.mirror_dir, "mirror_dir"), (self.secrets_dir, "secrets_dir")):
            for other, label in (
                (self.work_dir, "work_dir"),
                (self.cache_dir, "cache_dir"),
                (self.code_dir, "code_dir"),
            ):
                if private.is_relative_to(other) or other.is_relative_to(private):
                    raise ValueError(f"runner.{plabel} must not overlap runner.{label}")
        # Run dirs and checkouts are cleaned up by different rules; they never share a tree.
        if self.code_dir.is_relative_to(self.work_dir) or self.work_dir.is_relative_to(
            self.code_dir
        ):
            raise ValueError("runner.code_dir must not overlap runner.work_dir")
        for denied in self.deny_read:
            for other, label in ((self.work_dir, "work_dir"), (self.cache_dir, "cache_dir")):
                if denied.is_relative_to(other) or other.is_relative_to(denied):
                    raise ValueError(f"runner.deny_read must not overlap runner.{label}")
        return self

    @field_validator("git_path", "gh_path")
    @classmethod
    def _absolute_binary(cls, v: Path) -> Path:
        return _absolute(v, "runner binary paths")


_CALENDAR = "mcp__claude_ai_Google_Calendar__"
_GMAIL = "mcp__claude_ai_Gmail__"


def _connector_reads() -> dict[Literal["email", "calendar"], list[str]]:
    return {
        # The Calendar names are the connector's real ones; the Gmail names are a guess until
        # the owner finishes connecting Gmail in claude.ai (docs/brain-plan.md D8).
        "calendar": [f"{_CALENDAR}{t}" for t in ("list_calendars", "list_events", "get_event",
                                                   "search_events", "suggest_time")],
        "email": [f"{_GMAIL}search_threads", f"{_GMAIL}get_thread"],
    }  # fmt: skip


def _draft_tools() -> list[str]:
    return [f"{_GMAIL}create_draft"]


def _attachment_roots() -> list[Path]:
    return [Path("~/Library/Messages/Attachments")]


class AskClaudeConfig(_Section):
    """``ask_claude`` (docs/brain-plan.md D8, ``runners/ask.py``): a read-only ``claude -p``
    session per question, its own task, apart from the worker slots."""

    enabled: bool = True
    concurrency: int = Field(default=1, ge=1, le=4)  # sessions at once
    # Messages to Claude a day (settings.timezone): Claude brain turns, one-shot asks, media
    # and watch checks alike. 0 (the default): no daily cap; ``claude_session.max_per_hour``
    # is the runaway guard. Hit: the owner hears it once that day and qwen answers.
    daily_cap: int = Field(default=0, ge=0, le=5000)
    timeout_s: float = Field(default=120.0, gt=0, le=900)
    max_turns: int = Field(default=15, ge=1, le=50)
    reply_max_chars: int = Field(default=1500, ge=200, le=4000)
    # API billing only (``runner/anthropic_key``): ``--max-budget-usd`` per ask.
    max_budget_usd: float = Field(default=1.0, gt=0, le=20)
    # Connector read tools a kind needs, exact claude.ai tool names. Every one listed must
    # appear in the session's init event, or the ask is refused; a name that reads as a
    # write (send, create, update, delete, respond, ...) is never allowed whatever this says.
    connector_tools: dict[Literal["email", "calendar"], list[str]] = Field(
        default_factory=_connector_reads
    )
    # Draft tools, allowed for ``email`` asks only when the connector has them (never
    # required). A name that reads as a send or a delete is never allowed.
    draft_tools: list[str] = Field(default_factory=_draft_tools)
    # Where attachments (screenshots, photos) may come from besides ``brain.read_roots``.
    attachment_roots: list[Path] = Field(default_factory=_attachment_roots, validate_default=True)
    # BR-10: an ask the brain hands over (triage, its own ask_claude call, an escalation)
    # also gets Resonant's tools through a per-task MCP server, every call through the same
    # gate and executor as the local model's. Off: those asks are read-only, as in BR-5.
    resonant_tools: bool = True

    @field_validator("attachment_roots")
    @classmethod
    def _expand_roots(cls, v: list[Path]) -> list[Path]:
        return [_absolute(p.expanduser(), "brain.ask_claude.attachment_roots") for p in v]

    @field_validator("connector_tools")
    @classmethod
    def _connector_names(
        cls, v: dict[Literal["email", "calendar"], list[str]]
    ) -> dict[Literal["email", "calendar"], list[str]]:
        for tools in v.values():
            _mcp_names(tools)
        return v

    @field_validator("draft_tools")
    @classmethod
    def _draft_names(cls, v: list[str]) -> list[str]:
        return _mcp_names(v)


# A connector tool name as Claude Code lists it: mcp__<server>__<snake_case tool>. Anything
# else (camelCase, dashes) is refused at load rather than guessed at by the write filter.
_MCP_TOOL = re.compile(r"mcp__[A-Za-z0-9_]+__[a-z][a-z0-9_]*")


def _mcp_names(tools: list[str]) -> list[str]:
    for t in tools:
        if not _MCP_TOOL.fullmatch(t):
            raise ValueError(f"{t!r} isn't a connector tool name (mcp__<server>__<snake_case>)")
    return tools


# Refused by the read tools even inside a read root, always (docs/brain-plan.md D5);
# ``brain.read_deny`` adds to these. An entry starting with ``/`` or ``~`` is a path (it and
# everything under it); anything else is a glob matched against consecutive path components
# (``*.pem`` against every name, ``.git/config`` against every pair).
DEFAULT_READ_DENY: tuple[str, ...] = (
    "~/Documents",
    "~/Library",  # Mail, Messages, the Keychains, app data
    "~/.ssh",
    "~/.gnupg",
    "~/.aws",
    "~/.config/gh",
    "~/.docker",
    "~/.kube",
    ".env*",
    "*.env",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "*.keychain*",
    "id_*",
    "credentials",
    "credentials.*",
    ".credentials*",
    "*_credentials.json",
    ".git-credentials",
    ".netrc",
    ".npmrc",
    ".pypirc",
    ".git/config",
    # The same credential dirs anywhere (a copy in a repo, a mounted home).
    ".ssh",
    ".gnupg",
    ".aws",
    ".kube",
    ".docker",
    ".pgpass",
    ".htpasswd",
    "*.p8",
    "*.jks",
    "*.keystore",
    "*.tfstate",
    "*.tfstate.*",
    "service-account*.json",
    ".*_history",
)


class ClaudeSessionConfig(_Section):
    """BR-11: the Claude brain, one long-lived ``claude`` session (``--input-format
    stream-json``, stdin kept open) for the owner's hard texts (``runners/claude_session.py``).
    Each hand-off is one user message into it; one turn at a time, the rest queue in order.
    Off: every hand-off is its own one-shot ``claude -p`` (BR-10)."""

    enabled: bool = True
    # Ask the session for Remote Control when it starts (RC-1's control request); the URL
    # shows on /status and the dashboard. Off by default.
    remote_control: bool = False
    # A backstop to Claude Code's own auto-compaction: a fresh session (primed from
    # Resonant's history) after this many turns or hours, whichever comes first.
    rotate_turns: int = Field(default=50, ge=1, le=1000)
    rotate_hours: float = Field(default=12.0, gt=0, le=168)
    # One turn's budget; past it the session is killed (a hang) and restarts on the next.
    turn_timeout_s: float = Field(default=240.0, gt=0, le=900)
    # Hard texts waiting behind the one in flight; past it the owner is told it's busy.
    queue_max: int = Field(default=3, ge=0, le=20)
    # Runaway guard: messages to Claude per clock hour (the brain, one-shot asks, media and
    # watch checks); past it the owner hears it once and qwen answers until the next hour.
    # 0: off.
    max_per_hour: int = Field(default=60, ge=0, le=1000)
    # PER-258: a turn whose own shell changes or edits ran ends its reply with one
    # code-built "Did: ..." line (no line for reads).
    progress_texts: bool = True
    # PER-262: how often an idle session checks whether another process (the CLI's
    # `resonant pause`) paused Resonant; a pause stops it within this many seconds.
    pause_check_s: float = Field(default=5.0, gt=0, le=300)
    # PER-262: transcripts of the brain's earlier sessions (rotated, restarted) older than
    # this many days are removed from its own ~/.claude/projects/<cwd> dir. 0: kept.
    transcript_days: int = Field(default=14, ge=0, le=3650)
    # PER-262: Claude's own turns (tool rounds) in one owner message; `--max-turns` counts
    # per user message in a stream-json session (probed on 2.1.305), so it bounds a turn
    # that loops on tools well before `turn_timeout_s`.
    max_turns: int = Field(default=40, ge=1, le=200)


class TriageConfig(_Section):
    """BR-10: the local model's first call on a chat turn decides who answers it.

    Latency and usage: a local answer is ~1-5 s and free; a Claude answer takes ~5-20 s
    (longer with tools) and draws on the owner's Claude subscription (``ask_claude``'s
    daily cap and usage limits apply). The ack goes out first, within ~1 s on a warm model.
    """

    # ``normal``: the local model keeps the easy ones (a status, a note or reminder, one
    # lookup with an obvious tool, small talk, a follow-up already answered) and hands the
    # rest over, choosing Claude when in doubt. ``claude``: only a status, a note or reminder
    # and small talk stay local.
    bias: Literal["normal", "claude"] = "normal"
    # The triage call's budget; past it (a cold prompt on a busy model) the turn goes to
    # Claude with a canned ack.
    timeout_s: float = Field(default=8.0, gt=0, le=60)


class WatchConfig(_Section):
    """Watches (BR-7, docs/brain-plan.md D10): ``create_watch`` registers a periodic check,
    run by ``core/watches`` and ``runners/watch.py``."""

    enabled: bool = True
    max_active: int = Field(default=10, ge=1, le=50)  # active watches at once, per owner
    # A tool check runs at most this often; an ask check (a Claude session, counted against
    # brain.ask_claude.daily_cap) at most every ``ask_min_interval_minutes``.
    min_interval_minutes: int = Field(default=15, ge=15, le=24 * 60)
    ask_min_interval_minutes: int = Field(default=60, ge=15, le=24 * 60)
    default_days: int = Field(default=7, ge=1, le=30)  # a watch ends after this, unfired
    max_days: int = Field(default=30, ge=1, le=90)
    max_fails: int = Field(default=3, ge=1, le=20)  # consecutive failed checks end a watch

    @model_validator(mode="after")
    def _days(self) -> Self:
        if self.default_days > self.max_days:
            raise ValueError("brain.watches.default_days must be <= brain.watches.max_days")
        return self


class BrainConfig(_Section):
    """The brain (docs/brain-plan.md). BR-2: where the read tools (``tools/files.py``) look."""

    # BR-1 (D1), default since BR-8: ``loop`` sends every non-fast-path chat message to the
    # brain loop (``runners/brain.py``). ``intents`` is the opt-in fallback: the labeler and
    # one intent's fixed toolset (the brain also uses it for a turn when the model fails).
    mode: Literal["intents", "loop"] = "loop"
    # BR-10, loop mode only. ``triage``: the local model's first call routes each turn to
    # itself or to Claude (with an ack). ``local``: the loop as before (Claude only when it
    # asks or gets stuck). ``claude``: every turn goes to Claude, after a local ack.
    route: Literal["triage", "local", "claude"] = "triage"
    triage: TriageConfig = Field(default_factory=TriageConfig)
    # BR-10b, PER-245: after outside text (web, files, email, logs, extension output, a
    # Claude answer), this many later turns stay tainted (their model-chosen changes ask,
    # except notes, reminders and facts). The turn that read it is tainted as well.
    taint_hops: int = Field(default=3, ge=0, le=20)
    # Where read_file, list_dir and search may look. Unset: runner.code_dir, ~/resonant (and
    # ops.repo_path), and Resonant's logs, docs and run transcripts. Set: exactly these.
    read_roots: list[Path] | None = None
    # Added to DEFAULT_READ_DENY (which always applies), checked before the roots. Resonant's
    # own secrets (the DB, run-secrets, ext-data, run dirs) are refused whatever this says.
    read_deny: list[str] = Field(default_factory=list[str])
    # ripgrep for `search`, by absolute path. Missing: a bounded Python search instead.
    rg_path: Path | None = Path("/opt/homebrew/bin/rg")
    # ask_claude (BR-5, docs/brain-plan.md D8): reads the same roots and deny list.
    ask_claude: AskClaudeConfig = Field(default_factory=AskClaudeConfig)
    # BR-11: the Claude brain (one persistent session for the owner's hard texts).
    claude_session: ClaudeSessionConfig = Field(default_factory=ClaudeSessionConfig)
    # BR-7 (D10). Voice notes: a local whisper binary (whisper.cpp's ``whisper-cli``, by
    # absolute path). Unset: a voice note gets "voice needs whisper" (Claude takes no audio).
    whisper_path: Path | None = None
    # Its model file, passed as ``-m <model>`` before ``whisper_args`` when set.
    whisper_model: Path | None = None
    # ``{input}`` is the audio file (a 16 kHz mono WAV when afconvert is there). The
    # transcript is read from stdout.
    whisper_args: list[str] = Field(default_factory=lambda: ["-nt", "-np", "-f", "{input}"])
    whisper_timeout_s: float = Field(default=120.0, gt=0, le=900)
    # Brain-authored texts (a watch firing): at most this many a day (settings.timezone), on
    # top of extensions.notify.max_per_hour. Over it, a notice waits for the briefing.
    proactive_per_day: int = Field(default=3, ge=0, le=50)
    watches: WatchConfig = Field(default_factory=WatchConfig)

    @field_validator("whisper_path", "whisper_model")
    @classmethod
    def _whisper(cls, v: Path | None) -> Path | None:
        return _absolute(v.expanduser(), "brain.whisper_path/whisper_model") if v else None

    @field_validator("whisper_args")
    @classmethod
    def _whisper_args(cls, v: list[str]) -> list[str]:
        if sum(a.count("{input}") for a in v) != 1:
            raise ValueError("brain.whisper_args must hold {input} exactly once")
        return v

    @field_validator("read_roots")
    @classmethod
    def _roots(cls, v: list[Path] | None) -> list[Path] | None:
        if v is None:
            return None
        return [_absolute(p.expanduser(), "brain.read_roots entries") for p in v]

    @field_validator("read_deny")
    @classmethod
    def _deny(cls, v: list[str]) -> list[str]:
        for entry in v:
            if not entry.strip() or "\0" in entry or entry.strip() != entry:
                raise ValueError(
                    "brain.read_deny entries must be non-empty, without NUL or edge spaces"
                )
        return v

    @field_validator("rg_path")
    @classmethod
    def _rg(cls, v: Path | None) -> Path | None:
        return _absolute(v, "brain.rg_path") if v is not None else None


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RESONANT_",
        env_nested_delimiter="__",
        extra="forbid",
    )

    home: Path = Field(default_factory=resonant_home)
    timezone: str = "America/Los_Angeles"
    # Safe by default: the gate logs side effects instead of executing them.
    dry_run: bool = True

    model: ModelConfig = ModelConfig()
    api: ApiConfig = ApiConfig()
    health: HealthConfig = HealthConfig()
    relay: RelayConfig = RelayConfig()
    loop: LoopConfig = LoopConfig()
    claude: ClaudeConfig = ClaudeConfig()
    channels: ChannelsConfig = ChannelsConfig()
    gate: GateConfig = GateConfig()
    ops: OpsConfig = OpsConfig()
    power: PowerConfig = PowerConfig()
    extensions: ExtensionsConfig = ExtensionsConfig()
    secrets: SecretsConfig = SecretsConfig()
    briefing: BriefingConfig = BriefingConfig()
    backup: BackupConfig = BackupConfig()
    retention: RetentionConfig = RetentionConfig()
    pulse: PulseConfig = PulseConfig()
    runner: RunnerConfig = RunnerConfig()
    trust: TrustConfig = TrustConfig()
    brain: BrainConfig = Field(default_factory=BrainConfig)

    @field_validator("home")
    @classmethod
    def _expand_home(cls, v: Path) -> Path:
        return v.expanduser()

    @field_validator("timezone")
    @classmethod
    def _valid_tz(cls, v: str) -> str:
        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError) as e:
            raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @property
    def db_path(self) -> Path:
        return self.home / "resonant.db"

    @property
    def backups_dir(self) -> Path:
        """Store backups (BK-1): ``daily/`` and ``pre-restore/``."""
        return self.home / "backups"

    @property
    def logs_dir(self) -> Path:
        return self.home / "logs"

    @property
    def principals_path(self) -> Path:
        return self.home / "principals.yaml"

    @property
    def extensions_dir(self) -> Path:
        """Approved, read-only extension copies: ``<extensions_dir>/<name>/``."""
        return self.home / "extensions"

    @property
    def ext_data_dir(self) -> Path:
        """Writable extension state (the child's HOME): ``<ext_data_dir>/<name>/``."""
        return self.home / "ext-data"

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # config.yaml lives in the effective home: an explicit `home=` wins over
        # RESONANT_HOME, which wins over the default.
        home = resonant_home()
        if isinstance(init_settings, InitSettingsSource):
            init_kwargs = cast(dict[str, object], init_settings.init_kwargs)  # pyright: ignore[reportUnknownMemberType]
            explicit = init_kwargs.get("home")
            if isinstance(explicit, str | Path):
                home = Path(explicit).expanduser()
        yaml_source = YamlConfigSettingsSource(settings_cls, yaml_file=home / "config.yaml")
        return (init_settings, env_settings, yaml_source)


def load_settings(**overrides: Any) -> Settings:
    return Settings(**overrides)
