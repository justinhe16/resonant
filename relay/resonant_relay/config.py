"""Settings from the environment (Fly `[env]` for public values, `fly secrets` for the rest)."""

from __future__ import annotations

import base64
import binascii
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from urllib.parse import urlsplit


class ConfigError(ValueError):
    """A bad or missing setting. The message never contains a secret's value."""


_KEY_CHARS = re.compile(r"^[A-Za-z0-9._-]+$")
_HOST = re.compile(r"^[a-z0-9.-]+$")
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_DURATION = re.compile(r"^(\d+)\s*([smh]?)$")
# The same rule as relay_private.ingest_tokens.instance and the daemon's instance.name.
_INSTANCE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
DEFAULT_INSTANCE = "resonant"
CHANNELS = ("ntfy", "webhook", "slack")
SLACK_HOST = "hooks.slack.com"


def instance_env(prefix: str, instance: str) -> str:
    """`RELAY_NOTIFY_CORI` for ("RELAY_NOTIFY_", "cori"); a '-' becomes '_'."""
    return prefix + instance.upper().replace("-", "_")


def parse_instances(raw: str) -> tuple[str, ...]:
    """RELAY_INSTANCES: a comma list of instance names, `resonant` when unset."""
    names: list[str] = []
    for part in raw.split(","):
        name = part.strip().lower()
        if not name:
            continue
        if not _INSTANCE.match(name):
            raise ConfigError(
                "RELAY_INSTANCES must be instance names (a lowercase letter, then up to 31 "
                "lowercase letters, digits or '-'), comma-separated"
            )
        if name not in names:
            names.append(name)
    return tuple(names) or (DEFAULT_INSTANCE,)


def check_slack_webhook(name: str, raw: str) -> str:
    """A Slack Incoming Webhook URL: https on hooks.slack.com, nothing else. The value is a
    secret, so no message here contains it."""
    url = raw.strip()
    if any(c in url for c in " \n\r\t"):
        raise ConfigError(f"{name} has characters a URL can't contain")
    parts = urlsplit(url)
    if parts.scheme != "https":
        raise ConfigError(f"{name} must be https")
    if parts.hostname != SLACK_HOST or parts.port or parts.username or parts.password:
        raise ConfigError(f"{name} must be a Slack Incoming Webhook on {SLACK_HOST}")
    return url


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
    # Instances (PER-305): which to watch and where each one's alerts go.
    instances: tuple[str, ...] = (DEFAULT_INSTANCE,)
    # Instance -> channel names ("ntfy", "webhook", "slack"); every watched instance has one.
    notify: Mapping[str, tuple[str, ...]] = field(default_factory=lambda: {})
    # Instance -> its own Slack Incoming Webhook URL (SLACK_WEBHOOK_<INSTANCE>), a secret.
    slack_webhooks: Mapping[str, str] = field(default_factory=lambda: {}, repr=False)

    def global_channels(self) -> tuple[str, ...]:
        """The channels set up for everything: ntfy and/or the generic webhook."""
        return tuple(
            c for c, on in (("ntfy", self.ntfy_topic), ("webhook", self.webhook_url)) if on
        )

    def channels_for(self, instance: str) -> tuple[str, ...]:
        return self.notify.get(instance) or self.global_channels()

    def secrets(self) -> list[str]:
        """Values that must never reach a log line."""
        return (
            [s for s in (self.status_token, self.read_key, self.ntfy_topic, self.ntfy_token) if s]
            + ([self.webhook_url] if self.webhook_url else [])
            + [u for u in self.slack_webhooks.values() if u]
        )


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
            raise ConfigError("RELAY_STATUS_TOKEN is required (fly secrets import ...)")
    elif mode == "table":
        if not read_key:
            raise ConfigError("RELAY_READ=table needs RELAY_READ_KEY (fly secrets import ...)")
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
    return _with_instances(settings, get)


def _with_instances(settings: Settings, get: Callable[[str], str]) -> Settings:
    """RELAY_INSTANCES, each instance's SLACK_WEBHOOK_<INSTANCE> and RELAY_NOTIFY_<INSTANCE>.

    An instance's channels default to the global ones (today's behaviour) plus `slack` when
    its own Slack webhook is set, so a missing Slack secret only means no Slack posts. An
    explicit RELAY_NOTIFY_<INSTANCE> must name only channels that are set up."""
    instances = parse_instances(get("RELAY_INSTANCES"))
    slack: dict[str, str] = {}
    for inst in instances:
        name = instance_env("SLACK_WEBHOOK_", inst)
        if raw := get(name):
            slack[inst] = check_slack_webhook(name, raw)
    glob = settings.global_channels()
    notify: dict[str, tuple[str, ...]] = {}
    for inst in instances:
        name = instance_env("RELAY_NOTIFY_", inst)
        raw = get(name)
        if not raw:
            notify[inst] = glob + (("slack",) if inst in slack else ())
            continue
        chosen: list[str] = []
        for part in raw.split(","):
            ch = part.strip().lower()
            if not ch:
                continue
            if ch not in CHANNELS:
                raise ConfigError(f"{name} lists an unknown channel (use ntfy, webhook, slack)")
            if ch == "slack" and inst not in slack:
                raise ConfigError(
                    f"{name} lists slack but {instance_env('SLACK_WEBHOOK_', inst)} is not set"
                )
            if ch in ("ntfy", "webhook") and ch not in glob:
                need = "NTFY_TOPIC" if ch == "ntfy" else "WEBHOOK_URL"
                raise ConfigError(f"{name} lists {ch} but {need} is not set")
            if ch not in chosen:
                chosen.append(ch)
        if not chosen:
            raise ConfigError(f"{name} must list at least one channel")
        notify[inst] = tuple(chosen)
    return replace(settings, instances=instances, notify=notify, slack_webhooks=slack)


def _missing(name: str) -> str:
    raise ConfigError(f"{name} is required")
