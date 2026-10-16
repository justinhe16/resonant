"""Daemon configuration.

Loaded from ``$RESONANT_HOME/config.yaml`` (default ``~/.resonant``), with
``RESONANT_*`` environment variables taking precedence (``__`` for nesting).
Nothing in here is secret: credentials live in the macOS Keychain and are read
only by the executor.
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


def _is_loopback(host: str) -> bool:
    if host == "localhost":
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
        if not _is_loopback(urlparse(v).hostname or ""):
            raise ValueError("model.base_url must point at a loopback host")
        return v


class ApiConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=7777, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # No public ports: remote status is planned via the push-only status relay.
        if not _is_loopback(v):
            raise ValueError("api.host must be a loopback address")
        return v


class HealthConfig(_Section):
    healthchecks_url: str | None = None
    ping_every_s: float = Field(default=60.0, gt=0)
    watchdog_stale_s: float = Field(default=120.0, gt=0)


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


_RUNNER_REPO_NAME = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_RUNNER_REMOTE = re.compile(r"github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_RUNNER_BASE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]{0,99}")


class RunnerRepo(_Section):
    """One repo from the ``runner.repos`` seed (docs/phase5a-plan.md D3)."""

    # "github.com/owner/repo": no scheme, no credentials; the daemon adds https://.
    remote: str
    base: str = "main"
    # `.github/**` changes are refused unless this is set.
    allow_github_changes: bool = False
    # Quote the owner's ask in the PR body (false for public repos that shouldn't show it).
    quote_ask: bool = True

    @field_validator("remote")
    @classmethod
    def _remote(cls, v: str) -> str:
        v = v.removesuffix(".git")
        if not _RUNNER_REMOTE.fullmatch(v) or {".", ".."} & set(v.split("/")):
            raise ValueError('runner repo remote must look like "github.com/owner/repo"')
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
    """The Claude runner (docs/phase5a-plan.md). Nothing here is secret."""

    # The per-run token source. Without it the runner is not set up.
    github_app: GitHubAppConfig | None = None

    # name -> repo. Only a seed: at startup these are imported into the repo registry (the
    # `repos` table) as source=owner, and the registry is what the runner reads. Repos not
    # listed here are registered the first time they are used (docs/repos-plan.md D1).
    repos: dict[str, RunnerRepo] = Field(default_factory=dict[str, RunnerRepo])
    # Run dirs. Not under ~/.resonant, which the sandbox denies.
    work_dir: Path = Field(default=Path("~/resonant-runs"), validate_default=True)
    # Bare mirrors, daemon-only.
    mirror_dir: Path = Field(default=Path("~/.resonant/runner/mirrors"), validate_default=True)
    # Per-call token files for git and gh (0700, daemon-only, never sandbox-readable).
    secrets_dir: Path = Field(default=Path("~/.resonant/run-secrets"), validate_default=True)
    # Package caches shared between runs and used by nothing outside the sandbox.
    cache_dir: Path = Field(default=Path("~/resonant-runs/.cache"), validate_default=True)
    # Extra paths a session may neither read nor write, on top of the thin guard's own
    # (run-secrets, Messages, the Keychain; resonant/sandbox/runner.sb).
    deny_read: list[Path] = Field(default_factory=list[Path])
    max_run_dir_mb: int = Field(default=5000, ge=100)
    max_cache_gb: int = Field(default=20, ge=1)
    max_minutes: int = Field(default=45, ge=1, le=600)
    max_budget_usd: float | None = Field(default=None, gt=0)
    max_bundle_mb: int = Field(default=50, ge=1, le=500)
    # Days a finished run's dir is kept (a daily job deletes it after).
    keep_days: int = Field(default=7, ge=0, le=365)
    git_path: Path = Path("/usr/bin/git")
    gh_path: Path = Path("/opt/homebrew/bin/gh")

    @field_validator("repos")
    @classmethod
    def _names(cls, v: dict[str, RunnerRepo]) -> dict[str, RunnerRepo]:
        for name in v:
            if not _RUNNER_REPO_NAME.fullmatch(name):
                raise ValueError(f"runner repo name {name!r} must match [a-z][a-z0-9_-]{{0,31}}")
        return v

    @field_validator("work_dir", "mirror_dir", "cache_dir", "secrets_dir")
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
            for other, label in ((self.work_dir, "work_dir"), (self.cache_dir, "cache_dir")):
                if private.is_relative_to(other) or other.is_relative_to(private):
                    raise ValueError(f"runner.{plabel} must not overlap runner.{label}")
        for denied in self.deny_read:
            for other, label in ((self.work_dir, "work_dir"), (self.cache_dir, "cache_dir")):
                if denied.is_relative_to(other) or other.is_relative_to(denied):
                    raise ValueError(f"runner.deny_read must not overlap runner.{label}")
        return self

    @field_validator("git_path", "gh_path")
    @classmethod
    def _absolute_binary(cls, v: Path) -> Path:
        return _absolute(v, "runner binary paths")


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
    loop: LoopConfig = LoopConfig()
    claude: ClaudeConfig = ClaudeConfig()
    channels: ChannelsConfig = ChannelsConfig()
    gate: GateConfig = GateConfig()
    ops: OpsConfig = OpsConfig()
    power: PowerConfig = PowerConfig()
    extensions: ExtensionsConfig = ExtensionsConfig()
    secrets: SecretsConfig = SecretsConfig()
    briefing: BriefingConfig = BriefingConfig()
    pulse: PulseConfig = PulseConfig()
    runner: RunnerConfig = RunnerConfig()

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
