"""``resonant.yaml``: the extension manifest schema (version 1).

Validation is strict: unknown keys are errors. The one exception is a first-party block
named after ``uses`` (e.g. ``uses: oncall`` allows an ``oncall:`` block). The first-party
extension validates that block with its own schema.

Tool effects are declared here and nowhere else. MCP servers do not declare effects, so
every MCP tool an extension wants exposed must be listed under ``mcp.tools``. Unlisted MCP
tools are refused at runtime.
"""

from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from resonant_sdk.tool import Effect, Level, ToolSpec

SLUG = r"^[a-z][a-z0-9-]{0,47}$"
_CRON_FIELD = re.compile(r"^[A-Za-z0-9*/,\-]+$")
_DURATION = re.compile(r"^\d+[smh]$")

ArgType = Literal["string", "integer", "number", "boolean"]
_JSON_TYPES: dict[str, str] = {
    "string": "string",
    "integer": "integer",
    "number": "number",
    "boolean": "boolean",
}


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Job(_Strict):
    id: str = Field(pattern=SLUG)
    schedule: str  # 5-field cron, evaluated in `tz` (default: daemon timezone)
    tz: str | None = None
    critical: bool = False  # True: gets its own LaunchAgent and fires even if the daemon is down
    run: str

    @field_validator("schedule")
    @classmethod
    def _cron(cls, v: str) -> str:
        fields = v.split()
        if len(fields) != 5 or not all(_CRON_FIELD.match(f) for f in fields):
            raise ValueError(f"schedule {v!r} must be a 5-field cron expression")
        return v


class CliTool(_Strict):
    """A tool backed by a command. Placeholders like ``{date}`` must be declared in args."""

    run: str
    description: str = ""
    effect: Effect
    level: Level | None = None
    args: dict[str, ArgType] = Field(default_factory=dict[str, ArgType])
    secrets: tuple[str, ...] = ()
    commit: bool = False
    timeout_s: float = Field(default=30.0, gt=0)

    @model_validator(mode="after")
    def _placeholders_declared(self) -> Self:
        used = {name for _, name, _, _ in string.Formatter().parse(self.run) if name}
        if missing := used - set(self.args):
            raise ValueError(f"run uses undeclared args: {sorted(missing)}")
        return self


class McpToolDecl(_Strict):
    effect: Effect
    level: Level | None = None
    commit: bool = False
    secrets: tuple[str, ...] = ()


class McpServer(_Strict):
    command: tuple[str, ...]
    tools: dict[str, McpToolDecl]

    @field_validator("command", mode="before")
    @classmethod
    def _split(cls, v: Any) -> Any:
        return v.split() if isinstance(v, str) else v


class Notify(_Strict):
    on: tuple[str, ...]
    channel: Literal["imessage", "slack"] = "imessage"


class Health(_Strict):
    check: str
    every: str = "5m"

    @field_validator("every")
    @classmethod
    def _duration(cls, v: str) -> str:
        if not _DURATION.match(v):
            raise ValueError("every must look like 30s, 5m, or 1h")
        return v


class Manifest(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True)

    name: str = Field(pattern=SLUG)
    version: Literal[1]
    uses: str | None = Field(default=None, pattern=SLUG)
    approvers: tuple[str, ...] = ()
    jobs: tuple[Job, ...] = ()
    tools: dict[str, CliTool] = Field(default_factory=dict[str, CliTool])
    mcp: McpServer | None = None
    notify: Notify | None = None
    health: Health | None = None
    secrets: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _check(self) -> Self:
        extra = set(self.model_extra or {})
        if extra - ({self.uses} if self.uses else set()):
            raise ValueError(f"unknown keys: {sorted(extra - {self.uses})}")
        ids = [j.id for j in self.jobs]
        if len(ids) != len(set(ids)):
            raise ValueError("job ids must be unique")
        mcp_names: set[str] = set(self.mcp.tools) if self.mcp else set()
        if clash := set(self.tools) & mcp_names:
            raise ValueError(f"tool names defined twice (cli and mcp): {sorted(clash)}")
        declared = set(self.secrets)
        for spec in self.tool_specs():  # also enforces ToolSpec invariants
            if undeclared := set(spec.secrets) - declared:
                raise ValueError(f"{spec.name}: secrets not declared at top level: {undeclared}")
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
            specs += [
                ToolSpec(
                    name=name,
                    effect=d.effect,
                    level=d.level,
                    source=source,
                    extension=self.name,
                    secrets=d.secrets,
                    commit=d.commit,
                )
                for name, d in self.mcp.tools.items()
            ]
        return specs


class _StrictBoolLoader(yaml.SafeLoader):
    """SafeLoader that treats only true/false as booleans.

    YAML 1.1 also reads on/off/yes/no as booleans, which would turn ``notify: {on: [...]}``
    into ``{True: [...]}``.
    """


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
