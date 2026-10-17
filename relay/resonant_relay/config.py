"""Settings from the environment (Fly `[env]` for public values, `fly secrets` for the rest)."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """A bad or missing setting. The message never contains a secret's value."""


_KEY_CHARS = re.compile(r"^[A-Za-z0-9._-]+$")
_HOST = re.compile(r"^[a-z0-9.-]+$")
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DURATION = re.compile(r"^(\d+)\s*([smh]?)$")


def parse_duration(name: str, raw: str) -> float:
    """'300', '300s', '5m' or '1h' as seconds; must be positive."""
    m = _DURATION.match(raw.strip().lower())
    if not m or int(m.group(1)) <= 0:
        raise ConfigError(f"{name} must be a positive duration like 300, 300s or 5m")
    return int(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600}[m.group(2)]


def supabase_origin(raw: str) -> str:
    """The project's origin (https://<ref>.supabase.co). It goes into the CSP, so it is
    checked strictly: https, a plain host, no path, query, credentials or odd characters."""
    if any(c in raw for c in " ;,'\"\n\r\t"):
        raise ConfigError("SUPABASE_URL has characters a URL origin can't contain")
    parts = urlsplit(raw.strip())
    if parts.scheme != "https" or not parts.hostname:
        raise ConfigError("SUPABASE_URL must be an https URL like https://<ref>.supabase.co")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ConfigError("SUPABASE_URL must be just the project origin")
    if parts.path not in ("", "/"):
        raise ConfigError("SUPABASE_URL must be just the project origin (no path)")
    host = parts.hostname
    if not _HOST.match(host):
        raise ConfigError("SUPABASE_URL host has unexpected characters")
    port = f":{parts.port}" if parts.port else ""
    return f"https://{host}{port}"


def _jwt_role(key: str) -> str | None:
    """The `role` claim of a legacy JWT-style Supabase key, or None if it isn't one."""
    pieces = key.split(".")
    if len(pieces) != 3:
        return None
    payload = pieces[1] + "=" * (-len(pieces[1]) % 4)
    try:
        claims = json.loads(base64.urlsafe_b64decode(payload))
    except (binascii.Error, ValueError):
        return None
    if not isinstance(claims, dict):
        return None
    role = claims.get("role")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return role if isinstance(role, str) else None


def check_anon_key(key: str) -> str:
    """The anon key is served to every visitor in config.json, so only a key that is
    provably public passes: an `sb_publishable_` key or a JWT whose role is exactly
    `anon`. Anything else (a service_role JWT, `sb_secret_`, an access token, a JWT
    without a role) is refused."""
    key = key.strip()
    if not key:
        raise ConfigError("SUPABASE_ANON_KEY is required")
    if not _KEY_CHARS.match(key):
        raise ConfigError("SUPABASE_ANON_KEY has unexpected characters")
    if not (key.startswith("sb_publishable_") or _jwt_role(key) == "anon"):
        raise ConfigError(
            "SUPABASE_ANON_KEY is not an anon/publishable key; it would be served to "
            "every visitor, so the relay refuses to start"
        )
    return key


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    anon_key: str
    dist_dir: Path
    port: int = 8080
    # Checker
    read_mode: str = "rpc"  # "rpc" (preferred) or "table"
    status_token: str = field(default="", repr=False)
    read_key: str = field(default="", repr=False)
    check_every: float = 60.0
    stale_after: float = 300.0
    read_fail_after: float = 300.0
    # Alerts
    ntfy_url: str = "https://ntfy.sh"
    ntfy_topic: str = field(default="", repr=False)
    ntfy_token: str = field(default="", repr=False)
    webhook_url: str = field(default="", repr=False)

    def secrets(self) -> list[str]:
        """Values that must never reach a log line."""
        return [
            s for s in (self.status_token, self.read_key, self.ntfy_topic, self.ntfy_token) if s
        ] + ([self.webhook_url] if self.webhook_url else [])


def load(env: Mapping[str, str]) -> Settings:
    def get(name: str, default: str = "") -> str:
        return env.get(name, default).strip()

    url = supabase_origin(get("SUPABASE_URL") or _missing("SUPABASE_URL"))
    anon = check_anon_key(get("SUPABASE_ANON_KEY"))
    dist = Path(get("DIST_DIR", "/app/dist"))

    port_raw = get("PORT", "8080")
    if not port_raw.isdigit() or not 0 < int(port_raw) < 65536:
        raise ConfigError("PORT must be a TCP port number")

    mode = get("RELAY_READ", "rpc").lower()
    token = get("RELAY_STATUS_TOKEN")
    read_key = get("RELAY_READ_KEY")
    if mode == "rpc":
        if not token:
            raise ConfigError("RELAY_STATUS_TOKEN is required (fly secrets set ...)")
    elif mode == "table":
        if not read_key:
            raise ConfigError("RELAY_READ=table needs RELAY_READ_KEY (fly secrets set ...)")
    else:
        raise ConfigError("RELAY_READ must be rpc or table")

    stale = parse_duration("STALE_AFTER", get("STALE_AFTER", "5m"))
    settings = Settings(
        supabase_url=url,
        anon_key=anon,
        dist_dir=dist,
        port=int(port_raw),
        read_mode=mode,
        status_token=token,
        read_key=read_key,
        check_every=parse_duration("CHECK_EVERY", get("CHECK_EVERY", "60")),
        stale_after=stale,
        read_fail_after=parse_duration(
            "READ_FAIL_AFTER", get("READ_FAIL_AFTER") or str(int(stale))
        ),
        ntfy_url=get("NTFY_URL", "https://ntfy.sh").rstrip("/"),
        ntfy_topic=get("NTFY_TOPIC"),
        ntfy_token=get("NTFY_TOKEN"),
        webhook_url=get("WEBHOOK_URL"),
    )
    if not settings.ntfy_topic and not settings.webhook_url:
        raise ConfigError("set NTFY_TOPIC or WEBHOOK_URL (or both) so alerts go somewhere")
    if settings.ntfy_topic and not settings.ntfy_url.startswith("https://"):
        raise ConfigError("NTFY_URL must be https")
    if settings.webhook_url and not settings.webhook_url.startswith("https://"):
        raise ConfigError("WEBHOOK_URL must be https")
    if settings.ntfy_topic and not _TOPIC.match(settings.ntfy_topic):
        raise ConfigError("NTFY_TOPIC must be 1-64 letters, digits, '-' or '_'")
    if settings.stale_after < 2 * settings.check_every:
        raise ConfigError("STALE_AFTER must be at least twice CHECK_EVERY")
    return settings


def _missing(name: str) -> str:
    raise ConfigError(f"{name} is required")
