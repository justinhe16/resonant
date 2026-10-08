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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import (
    BaseSettings,
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
    context_tokens: int = 32768
    timeout_s: float = 60.0


class ApiConfig(_Section):
    host: str = "127.0.0.1"
    port: int = 7777

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, v: str) -> str:
        # No public ports: the dashboard is exposed only via `tailscale serve`.
        if v != "localhost" and not ipaddress.ip_address(v).is_loopback:
            raise ValueError("api.host must be a loopback address")
        return v


class HealthConfig(_Section):
    healthchecks_url: str | None = None
    ping_every_s: float = 60.0
    watchdog_stale_s: float = 120.0


class LoopConfig(_Section):
    tick_s: float = 30.0
    stuck_after_s: float = 900.0
    max_attempts: int = 3


class ClaudeConfig(_Section):
    max_concurrent: int = Field(default=5, ge=1)
    reserved_oncall_slots: int = Field(default=1, ge=0)


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
        yaml_source = YamlConfigSettingsSource(
            settings_cls, yaml_file=resonant_home() / "config.yaml"
        )
        return (init_settings, env_settings, yaml_source)


def load_settings(**overrides: Any) -> Settings:
    settings = Settings(**overrides)
    settings.home = settings.home.expanduser()
    return settings
