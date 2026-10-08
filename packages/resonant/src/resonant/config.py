"""Daemon configuration.

Loaded from ``$RESONANT_HOME/config.yaml`` (default ``~/.resonant``), with
``RESONANT_*`` environment variables taking precedence (``__`` for nesting).
Nothing in here is secret: credentials live in the macOS Keychain and are read
only by the executor.
"""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import Any, Self, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    InitSettingsSource,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

DEFAULT_HOME = Path("~/.resonant")


def resonant_home() -> Path:
    return Path(os.environ.get("RESONANT_HOME", DEFAULT_HOME)).expanduser()


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ModelConfig(_Section):
    base_url: str = "http://127.0.0.1:11434/v1"
    name: str = "qwen3:30b-a3b"
    context_tokens: int = Field(default=32768, gt=0)
    timeout_s: float = Field(default=60.0, gt=0)


class ApiConfig(_Section):
    host: str = "127.0.0.1"
    port: int = Field(default=7777, ge=1, le=65535)

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # No public ports: the dashboard is exposed only via `tailscale serve`.
        if v != "localhost" and not ipaddress.ip_address(v).is_loopback:
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
