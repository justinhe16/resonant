"""``resonant.yaml``: the extension manifest schema (version 1).

Validation is strict: unknown keys are errors. The one exception is a first-party block
named after ``uses`` (e.g. ``uses: oncall`` allows an ``oncall:`` block). The first-party
extension validates that block with its own schema.

Tool and job effects are declared here and nowhere else. MCP servers do not declare effects,
so every MCP tool an extension wants exposed must be listed under ``mcp.servers.<id>.tools``,
with its effect, level and secrets. Unlisted MCP tools are never exposed.

Checks that need the extension's directory or this installation (stdlib only, the argv[0]
allowlist, symlinks, builtin command names, approver grants) live in
``resonant.extensions.checks``.
"""

from __future__ import annotations

import re
import string
from itertools import pairwise
from pathlib import Path
from typing import Annotated, Any, Literal, Self
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from resonant_sdk.tool import TOOL_NAME_PATTERN, Effect, Level, ToolSpec

SLUG = r"^[a-z][a-z0-9-]{0,47}$"
SECRET_NAME = r"^[a-z][a-z0-9_]{0,31}$"  # noqa: S105 - a pattern, not a secret
RESERVED_NAMES = frozenset({"core"})  # core jobs use ext="core"
TOOL_TIMEOUT_MAX_S = 300.0
JOB_TIMEOUT_MAX_S = 1800.0
CATCHUP_MAX_S = 24 * 3600
JOB_MIN_INTERVAL_S = 5 * 60
ASKING_JOB_MIN_INTERVAL_S = 3600  # an L1/L2 job prompts a person each run
DESCRIPTION_MAX = 120
EXAMPLES_MAX = 3
EXAMPLE_MAX = 80
REPOS_MAX = 20

_CRON_FIELD = re.compile(r"^[A-Za-z0-9*/,\-]+$")
_DURATION = re.compile(r"^\d+[smh]$")
_REPO_SLUG = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_IDENT = re.compile(r"^[a-z_][a-z0-9_]*$")
_EVENT = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
RESERVED_EVENT = "item"  # the pulse's event type (``report()``); never a ``notify.on`` entry
_UNIT_S = {"s": 1, "m": 60, "h": 3600}

SecretName = Annotated[str, Field(pattern=SECRET_NAME)]

ArgType = Literal["string", "integer", "number", "boolean"]
_JSON_TYPES: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def duration_s(v: str) -> int:
    """Seconds in a ``30s`` / ``5m`` / ``1h`` duration."""
    if not _DURATION.fullmatch(v):
        raise ValueError(f"{v!r} must look like 30s, 5m, or 1h")
    return int(v[:-1]) * _UNIT_S[v[-1]]


def _cron_values(field: str, lo: int, hi: int) -> set[int]:
    """Expand one numeric cron field (``*``, ``a``, ``a-b``, any with ``/n``, comma lists)."""
    values: set[int] = set()
    for item in field.split(","):
        base, _, step_s = item.partition("/")
        if "/" in item and not step_s.isdigit():
            raise ValueError(f"bad cron step in {field!r}")
        step = int(step_s) if step_s else 1
        if base == "*":
            start, end = lo, hi
        else:
            a, _, b = base.partition("-")
            if not a.isdigit() or (b and not b.isdigit()):
                raise ValueError(f"bad cron field {field!r}")
            start = int(a)
            end = int(b) if b else (hi if step_s else start)
        if step < 1 or not lo <= start <= end <= hi:
            raise ValueError(f"cron field {field!r} is out of range {lo}-{hi}")
        values.update(range(start, end + 1, step))
    return values


def cron_min_interval_s(schedule: str) -> int:
    """The shortest gap between two firings of a 5-field cron, from its minute and hour fields.

    Conservative: it assumes consecutive days both fire, so a day-of-week or day-of-month
    restriction never makes a schedule look sparser than it is.
    """
    minute, hour = schedule.split()[:2]
    times = sorted(
        h * 60 + m for h in _cron_values(hour, 0, 23) for m in _cron_values(minute, 0, 59)
    )
    gaps = [b - a for a, b in pairwise(times)]
    gaps.append(times[0] + 24 * 60 - times[-1])  # across midnight
    return min(gaps) * 60


class Job(_Strict):
    """A scheduled command. ``effect`` defaults to ``write`` (L2 unless ``level`` says
    otherwise: it asks)."""

    id: str = Field(pattern=SLUG)
    schedule: str  # 5-field cron, evaluated in `tz` (default: daemon timezone)
    tz: str | None = None
    critical: bool = False  # True: gets its own LaunchAgent and fires even if the daemon is down
    run: str
    effect: Effect = Effect.WRITE
    level: Level | None = None
    secrets: tuple[SecretName, ...] = ()
    timeout_s: float = Field(default=300.0, gt=0, le=JOB_TIMEOUT_MAX_S)
    catchup: str = "none"  # "none" or a duration <= 24h

    @field_validator("tz")
    @classmethod
    def _tz(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                ZoneInfo(v)
            except (ZoneInfoNotFoundError, ValueError) as e:
                raise ValueError(f"unknown timezone {v!r}") from e
        return v

    @field_validator("schedule")
    @classmethod
    def _cron(cls, v: str) -> str:
        fields = v.split()
        if len(fields) != 5 or not all(_CRON_FIELD.fullmatch(f) for f in fields):
            raise ValueError(f"schedule {v!r} must be a 5-field cron expression")
        if cron_min_interval_s(v) < JOB_MIN_INTERVAL_S:
            raise ValueError(f"schedule {v!r} fires more often than every 5 minutes")
        return v

    @field_validator("catchup")
    @classmethod
    def _catchup(cls, v: str) -> str:
        if v != "none" and duration_s(v) > CATCHUP_MAX_S:
            raise ValueError("catchup must be none or a duration of at most 24h")
        return v

    @property
    def catchup_s(self) -> int:
        """The catch-up window in seconds; 0 for ``none``."""
        return 0 if self.catchup == "none" else duration_s(self.catchup)

    @property
    def spec_name(self) -> str:
        return "job_" + self.id.replace("-", "_")

    def spec(self, extension: str) -> ToolSpec:
        """The job as a ToolSpec. It never enters a model toolset or a member's commands."""
        return ToolSpec(
            name=self.spec_name,
            description="scheduled job",
            args_schema={
                "type": "object",
                "properties": {"due_at": {"type": "string"}},
                "required": ["due_at"],
                "additionalProperties": False,
            },
            effect=self.effect,
            level=self.level,
            source=f"ext:{extension}",
            extension=extension,
            timeout_s=self.timeout_s,
            secrets=self.secrets,
        )


class CliTool(_Strict):
    """A tool backed by a command. Placeholders like ``{date}`` must be declared in args."""

    run: str
    description: str = ""
    effect: Effect
    level: Level | None = None
    args: dict[str, ArgType] = Field(default_factory=dict[str, ArgType])
    secrets: tuple[SecretName, ...] = ()
    commit: bool = False
    timeout_s: float = Field(default=30.0, gt=0, le=TOOL_TIMEOUT_MAX_S)

    @model_validator(mode="after")
    def _placeholders_declared(self) -> Self:
        used: set[str] = set()
        for _, name, spec, conversion in string.Formatter().parse(self.run):
            if name is None:
                continue
            if not _IDENT.fullmatch(name) or spec or conversion:
                raise ValueError(f"placeholder {{{name}}} must be a plain arg name like {{date}}")
            used.add(name)
        if missing := used - set(self.args):
            raise ValueError(f"run uses undeclared args: {sorted(missing)}")
        if bad := [a for a in self.args if not _IDENT.fullmatch(a)]:
            raise ValueError(f"arg names must be identifiers: {bad}")
        return self


MCP_SERVER_ID = r"^[a-z][a-z0-9_-]{0,31}$"
MCP_ARGS_MAX = 32
MCP_ARG_MAX = 300
MCP_TOOLS_MAX = 64


class McpTool(_Strict):
    """One MCP tool the extension exposes. The server's own description and schema text never
    reach the model: ``description`` here is what it sees, and the effect, level, secrets and
    timeout come only from this entry."""

    name: str = Field(pattern=TOOL_NAME_PATTERN)
    description: str = Field(default="", max_length=300)
    effect: Effect
    level: Level | None = None
    commit: bool = False
    secrets: tuple[SecretName, ...] = ()
    timeout_s: float = Field(default=30.0, gt=0, le=TOOL_TIMEOUT_MAX_S)

    @field_validator("description")
    @classmethod
    def _one_line(cls, v: str) -> str:
        if not v.isprintable():
            raise ValueError("description must be one line with no control characters")
        return v


class McpStdioServer(_Strict):
    """A local MCP server over stdio. ``command`` is one word: a ``.py`` file in the extension
    dir (``./server.py``) or a server binary, pinned by its real absolute path at install
    (``resonant.extensions.checks.resolve_mcp_command``). ``args`` are literal."""

    transport: Literal["stdio"]
    command: str = Field(min_length=1, max_length=MCP_ARG_MAX)
    args: tuple[str, ...] = Field(default=(), max_length=MCP_ARGS_MAX)
    tools: tuple[McpTool, ...] = Field(min_length=1, max_length=MCP_TOOLS_MAX)

    @field_validator("command")
    @classmethod
    def _command(cls, v: str) -> str:
        if not (v.isascii() and v.isprintable()) or any(c.isspace() for c in v):
            raise ValueError("command must be one printable word; put arguments in args")
        return v

    @field_validator("args")
    @classmethod
    def _args(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        bad = [a for a in v if not (a.isascii() and a.isprintable()) or len(a) > MCP_ARG_MAX]
        if bad:
            raise ValueError(f"args must be printable ASCII of at most {MCP_ARG_MAX} chars")
        return v

    @field_validator("tools")
    @classmethod
    def _unique(cls, v: tuple[McpTool, ...]) -> tuple[McpTool, ...]:
        return _unique_tools(v)

    @property
    def secrets(self) -> tuple[str, ...]:
        """The union of its tools' declared secrets: all the server process receives."""
        return tuple(sorted({s for t in self.tools for s in t.secrets}))


_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def mcp_url_problem(url: str) -> str | None:
    """Why ``url`` can't be an MCP server's endpoint, or None. https with a host, no user
    info or fragment; plain http only for a loopback host (the daemon refuses it outside
    tests: ``resonant.extensions.checks.mcp_url_error``)."""
    if not (url.isascii() and url.isprintable()) or any(c.isspace() for c in url):
        return "url must be printable ASCII with no spaces"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return "url is malformed"
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        return "url must not carry user info"
    if parts.fragment or url.endswith("#"):
        return "url must not have a fragment"
    host = (parts.hostname or "").lower()
    if not host:
        return "url needs a host"
    if parts.scheme == "https":
        return None
    if parts.scheme == "http" and host in _LOOPBACK_HOSTS and port is not None:
        return None
    return "url must be https"


class McpOAuth(_Strict):
    """``auth: oauth``: an OAuth 2.0 refresh-token grant (RFC 6749 §6) at one fixed
    ``token_url``. The three names are declared secrets: the daemon posts them to
    ``token_url`` (and nowhere else) for a short-lived access token, which it sends to the
    server's ``url`` as ``Authorization: Bearer <token>``. Access tokens live only in the
    daemon's memory; nothing is written to disk."""

    token_url: str = Field(min_length=1, max_length=MCP_ARG_MAX)
    client_id: SecretName
    client_secret: SecretName
    refresh_token: SecretName

    @field_validator("token_url")
    @classmethod
    def _url(cls, v: str) -> str:
        if problem := mcp_url_problem(v):
            raise ValueError(f"token_url: {problem}")
        return v

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        if len({self.client_id, self.client_secret, self.refresh_token}) != 3:
            raise ValueError("oauth client_id, client_secret and refresh_token must differ")
        return self

    @property
    def secrets(self) -> tuple[str, ...]:
        return (self.client_id, self.client_secret, self.refresh_token)


class McpHttpServer(_Strict):
    """A remote MCP server over streamable HTTP at one fixed ``url``. With ``auth: bearer``,
    ``secret`` (a declared secret) is sent as ``Authorization: Bearer <value>`` to that URL
    and nowhere else. With ``auth: oauth`` (``oauth``: ``McpOAuth``), a refresh token is
    exchanged at ``oauth.token_url`` for the access token sent to ``url``. Its tools declare
    no secrets of their own."""

    transport: Literal["http"]
    url: str = Field(min_length=1, max_length=MCP_ARG_MAX)
    auth: Literal["bearer", "oauth"] | None = None
    secret: SecretName | None = None
    oauth: McpOAuth | None = None
    tools: tuple[McpTool, ...] = Field(min_length=1, max_length=MCP_TOOLS_MAX)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str) -> str:
        if problem := mcp_url_problem(v):
            raise ValueError(problem)
        return v

    @field_validator("tools")
    @classmethod
    def _unique(cls, v: tuple[McpTool, ...]) -> tuple[McpTool, ...]:
        if any(t.secrets for t in v):
            raise ValueError(
                "tools of an http server declare no secrets; the server's bearer secret is "
                "the only one sent"
            )
        return _unique_tools(v)

    @model_validator(mode="after")
    def _auth(self) -> Self:
        if (self.auth == "bearer") != (self.secret is not None):
            raise ValueError("auth: bearer needs secret: <name>, and secret needs auth: bearer")
        if (self.auth == "oauth") != (self.oauth is not None):
            raise ValueError("auth: oauth needs an oauth: block, and oauth needs auth: oauth")
        return self

    @property
    def secrets(self) -> tuple[str, ...]:
        """The bearer secret, or the OAuth client and refresh token: all this server's
        session ever uses (the OAuth ones go only to ``oauth.token_url``)."""
        if self.oauth is not None:
            return self.oauth.secrets
        return (self.secret,) if self.secret else ()


def _unique_tools(v: tuple[McpTool, ...]) -> tuple[McpTool, ...]:
    names = [t.name for t in v]
    if len(names) != len(set(names)):
        raise ValueError("mcp tool names must be unique")
    return v


McpServer = Annotated[McpStdioServer | McpHttpServer, Field(discriminator="transport")]


class Mcp(_Strict):
    """``mcp: {servers: {<id>: McpServer}}``. Only the listed tools are ever exposed."""

    servers: dict[Annotated[str, Field(pattern=MCP_SERVER_ID)], McpServer] = Field(min_length=1)

    def tools(self) -> list[tuple[str, McpTool]]:
        """Every listed tool as ``(server id, tool)``, by server then manifest order."""
        return [(sid, t) for sid, server in self.servers.items() for t in server.tools]


class Notify(_Strict):
    on: tuple[str, ...] = Field(min_length=1)
    channel: Literal["imessage", "slack"] = "imessage"

    @field_validator("on")
    @classmethod
    def _events(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if RESERVED_EVENT in v:
            raise ValueError(
                f"{RESERVED_EVENT!r} is a reserved event type (pulse reports, set by "
                "`report:`); it can't be in notify.on"
            )
        if bad := [e for e in v if not _EVENT.fullmatch(e)]:
            raise ValueError(f"event names must be lowercase slugs: {bad}")
        return v


class Report(_Strict):
    """The pulse grant: the highest urgency this extension's ``report()`` items may use.

    Items asking for more are clamped to it. Without this section, reports are refused.
    """

    max_urgency: Literal["urgent", "normal", "low"]


class Health(_Strict):
    check: str
    every: str = "5m"

    @field_validator("every")
    @classmethod
    def _duration(cls, v: str) -> str:
        duration_s(v)
        return v


class Manifest(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    name: str = Field(pattern=SLUG)
    version: Literal[1]
    description: str = Field(default="", max_length=DESCRIPTION_MAX)  # the router intent
    examples: tuple[str, ...] = Field(default=(), max_length=EXAMPLES_MAX)  # router few-shots
    uses: str | None = Field(default=None, pattern=SLUG)
    approvers: tuple[str, ...] = ()
    jobs: tuple[Job, ...] = ()
    tools: dict[str, CliTool] = Field(default_factory=dict[str, CliTool])
    mcp: Mcp | None = None
    notify: Notify | None = None
    report: Report | None = None
    health: Health | None = None
    secrets: tuple[SecretName, ...] = ()
    repos: tuple[str, ...] = Field(default=(), max_length=REPOS_MAX)  # GitHub "owner/name" links
    # Host binaries the extension's own code runs (the github extension's gh). Each is pinned
    # by absolute path at install and given as RESONANT_BIN_<NAME>; PATH never has to hold it.
    bins: tuple[Literal["gh", "git"], ...] = ()

    @field_validator("name")
    @classmethod
    def _not_reserved(cls, v: str) -> str:
        if v in RESERVED_NAMES:
            raise ValueError(f"extension name {v!r} is reserved")
        return v

    @field_validator("description")
    @classmethod
    def _description(cls, v: str) -> str:
        if not v.isprintable():
            raise ValueError("description must be one line with no control characters")
        return v

    @field_validator("examples")
    @classmethod
    def _examples(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if bad := [e for e in v if not e.strip() or len(e) > EXAMPLE_MAX or not e.isprintable()]:
            raise ValueError(f"examples must be single lines of 1-{EXAMPLE_MAX} chars: {bad}")
        return v

    @field_validator("repos")
    @classmethod
    def _repos(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if bad := [r for r in v if not _REPO_SLUG.fullmatch(r) or {".", ".."} & set(r.split("/"))]:
            raise ValueError(f"repos must be GitHub owner/name: {bad}")
        if len({r.lower() for r in v}) != len(v):
            raise ValueError("repos must be unique")
        return v

    @field_validator("bins")
    @classmethod
    def _bins(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(v)) != len(v):
            raise ValueError("bins must be unique")
        return v

    @model_validator(mode="after")
    def _check(self) -> Self:
        extra = set(self.model_extra or {})
        if extra - ({self.uses} if self.uses else set()):
            raise ValueError(f"unknown keys: {sorted(extra - {self.uses})}")
        ids = [j.id for j in self.jobs]
        if len(ids) != len(set(ids)):
            raise ValueError("job ids must be unique")
        mcp_names = [t.name for _, t in self.mcp.tools()] if self.mcp else []
        if len(mcp_names) != len(set(mcp_names)):
            raise ValueError("mcp tool names must be unique across servers")
        if clash := set(self.tools) & set(mcp_names):
            raise ValueError(f"tool names defined twice (cli and mcp): {sorted(clash)}")
        job_specs = self.job_specs()  # also enforces ToolSpec invariants
        tool_specs = self.tool_specs()
        if clash := {s.name for s in tool_specs} & {s.name for s in job_specs}:
            raise ValueError(f"tool names collide with job spec names: {sorted(clash)}")
        declared = set(self.secrets)
        for sid, server in (self.mcp.servers if self.mcp else {}).items():
            if undeclared := set(server.secrets) - declared:
                raise ValueError(
                    f"mcp server {sid}: secrets not declared at top level: {undeclared}"
                )
        for spec in tool_specs + job_specs:
            if undeclared := set(spec.secrets) - declared:
                raise ValueError(f"{spec.name}: secrets not declared at top level: {undeclared}")
        for job in self.jobs:
            asks = job.spec(self.name).effective_level in (Level.L1, Level.L2)
            if asks and cron_min_interval_s(job.schedule) < ASKING_JOB_MIN_INTERVAL_S:
                raise ValueError(
                    f"job {job.id}: it asks for approval (L1/L2), so it may run at most hourly"
                )
        return self

    @property
    def first_party_config(self) -> dict[str, Any] | None:
        return (self.model_extra or {}).get(self.uses) if self.uses else None

    def tool_specs(self) -> list[ToolSpec]:
        source = f"ext:{self.name}"
        specs = [
            ToolSpec(
                name=name,
                description=t.description,
                args_schema={
                    "type": "object",
                    "properties": {a: {"type": _JSON_TYPES[ty]} for a, ty in t.args.items()},
                    "required": sorted(t.args),
                    "additionalProperties": False,
                },
                effect=t.effect,
                level=t.level,
                source=source,
                extension=self.name,
                timeout_s=t.timeout_s,
                secrets=t.secrets,
                commit=t.commit,
            )
            for name, t in self.tools.items()
        ]
        if self.mcp:
            # The args schema is the server's, stripped and pinned at install; the host fills
            # it in from the install record. Here it is a placeholder.
            specs += [
                ToolSpec(
                    name=t.name,
                    description=t.description,
                    effect=t.effect,
                    level=t.level,
                    source=source,
                    extension=self.name,
                    timeout_s=t.timeout_s,
                    secrets=t.secrets,
                    commit=t.commit,
                )
                for _, t in self.mcp.tools()
            ]
        return specs

    def job_specs(self) -> list[ToolSpec]:
        """One ``job_<id>`` spec per job. Never part of a model toolset."""
        return [j.spec(self.name) for j in self.jobs]


class _StrictBoolLoader(yaml.SafeLoader):
    """SafeLoader that treats only true/false as booleans and rejects duplicate keys.

    YAML 1.1 also reads on/off/yes/no as booleans, which would turn ``notify: {on: [...]}``
    into ``{True: [...]}``. Duplicate keys normally keep the last value silently.
    """

    def construct_mapping(self, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
        seen: set[Any] = set()
        for key_node, _ in node.value:
            key = self.construct_object(key_node, deep=deep)  # pyright: ignore[reportUnknownMemberType]
            if key in seen:
                raise yaml.constructor.ConstructorError(
                    None, None, f"duplicate key {key!r}", key_node.start_mark
                )
            seen.add(key)
        return super().construct_mapping(node, deep=deep)


_StrictBoolLoader.yaml_implicit_resolvers = {
    ch: [(tag, rx) for tag, rx in resolvers if tag != "tag:yaml.org,2002:bool"]
    for ch, resolvers in yaml.SafeLoader.yaml_implicit_resolvers.items()
}
_StrictBoolLoader.add_implicit_resolver(  # pyright: ignore[reportUnknownMemberType]
    "tag:yaml.org,2002:bool", re.compile(r"^(?:true|True|TRUE|false|False|FALSE)$"), list("tTfF")
)


def parse_yaml(text: str) -> Any:
    return yaml.load(text, Loader=_StrictBoolLoader)  # noqa: S506 - SafeLoader subclass


def load_manifest(path: Path) -> Manifest:
    return Manifest.model_validate(parse_yaml(path.read_text()))
