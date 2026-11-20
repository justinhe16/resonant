"""Principals and permissions.

A principal is a person who can make requests and approve actions. Channel identities
(``imessage:+15551234567`` or ``imessage:me@icloud.com``, ``slack:<user id>``) map to
principals. Identity grants permissions.
Message content never does: content is always untrusted input.

Roles (PER-293): ``owner | approver | member``.

- Exactly one principal has ``role: owner``. The owner can do anything, including
  admin actions (kill switch fan-out, ``/update``, ``/ext``, secrets, editing principals).
- An ``approver`` may ``request`` and ``approve`` everything: every builtin (global scope)
  and every extension. Never ``admin``, and never an ``ADMIN_TOOLS`` builtin (``/update``,
  ``/ext``, ``/pulse``'s tools, the brain shell) from any path.
- A ``member`` can only do what ``grants`` lists. A grant is per extension
  (``{dori: [request]}``: every dori tool) or per tool
  (``{dori: {request: [file_ticket], approve: []}}``: only those tools).
- Global scope (``extension=None``) is owner and approvers only, with one exception: a
  builtin whose spec names a ``grant`` (only global L0 reads can, ``ToolSpec.grant``) may be
  requested by a member whose grants list ``"builtin:<grant>": [request]``. ``may_request``
  is the check for a spec; ``can`` takes the grant (and the tool) explicitly.
- Claude (``ask_claude``, grant ``claude``, PER-293): a member reaches Claude with
  ``"builtin:claude": [request]``, or on a team config (an approver or a workspace default:
  ``team``) with any extension ``request`` grant; Claude then offers them only what
  ``may_request`` allows. Without a team config nothing changes for members.

Workspace default (PER-293): ``default: {channel: slack, grants: {...}}``. An identity on
that channel that no principal claims, and that the adapter vouches for (same workspace,
PER-295), resolves to the synthetic principal named by the identity itself
(``slack:U0123``): role member, the default's grants. It is never an approver (its grants
may not list ``approve``), and iMessage never gets a default.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from resonant_sdk import ToolSpec

Action = Literal["request", "approve", "admin"]
GrantableAction = Literal["request", "approve"]
Role = Literal["owner", "approver", "member"]

_KNOWN_CHANNELS = ("imessage", "slack")
KNOWN_CHANNELS = _KNOWN_CHANNELS  # every channel an identity may name (the hub's adapters)
DEFAULT_CHANNELS = ("slack",)  # channels a workspace default may cover: never iMessage
BUILTIN_GRANT = "builtin:"  # grants key prefix for a builtin group (``ToolSpec.grant``)
# ``ask_claude``'s grant (PER-293): a member reaches Claude with ``"builtin:claude":
# [request]``, or, on a team config (``Principals.team``), by holding ``request`` on any
# extension tool (Claude is how they use it).
CLAUDE_GRANT = "claude"
# PER-293: builtins that are admin whatever the path (brain, gate, slash command): an approver
# may neither request nor approve them. ``/update``, ``/ext`` and ``/pulse``'s tools, and the
# brain shell (it could edit principals or reach secrets).
ADMIN_TOOLS: frozenset[str] = frozenset(
    {"update_resonant", "mute_extension", "list_extensions", "pulse_preview", "shell", "shell_read"}
)
# chat.db stores handles as E.164 phone numbers or lowercase emails. Requiring the same form
# here means a handle can only match by exact string comparison.
_IMESSAGE_HANDLE = re.compile(
    r"^(\+[1-9]\d{7,14}|[a-z0-9][a-z0-9._%+-]*@[a-z0-9][a-z0-9.-]*\.[a-z]{2,})$"
)
_SLACK_ID = re.compile(r"^[A-Z0-9]{2,32}$")  # a Slack user id (U…/W…): the synthetic name


class ToolGrants(BaseModel):
    """A per-tool grant for one extension: which of its tools may be requested, and which
    approved. A tool name the extension doesn't define grants nothing (``ext validate``
    warns, ``checks.grant_warnings``)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    request: tuple[str, ...] = ()
    approve: tuple[str, ...] = ()

    def tools(self, action: GrantableAction) -> tuple[str, ...]:
        return self.request if action == "request" else self.approve


# A grants value: the whole extension (``[request, approve]``) or per tool (``ToolGrants``).
Grant = tuple[GrantableAction, ...] | ToolGrants
Grants = dict[str, Grant]


def _check_grants(v: Grants) -> Grants:
    # Builtin grants reach L0 reads only, and builtins have no member approvers.
    for key, grant in v.items():
        if key.startswith(BUILTIN_GRANT) and (
            isinstance(grant, ToolGrants) or set(grant) - {"request"}
        ):
            raise ValueError(f"grant {key!r} may only list request")
    return v


def _grants_action(grant: Grant | None, action: GrantableAction, tool: str | None) -> bool:
    """Whether ``grant`` covers ``action`` (on ``tool``, for a per-tool grant)."""
    if grant is None:
        return False
    if isinstance(grant, ToolGrants):
        return tool is not None and tool in grant.tools(action)
    return action in grant


def _holds(grant: Grant | None, action: GrantableAction) -> bool:
    """Whether ``grant`` covers ``action`` for any tool at all."""
    if grant is None:
        return False
    if isinstance(grant, ToolGrants):
        return bool(grant.tools(action))
    return action in grant


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identities: tuple[str, ...] = ()
    role: Role = "member"
    grants: Grants = Field(default_factory=dict[str, Grant])

    @field_validator("identities")
    @classmethod
    def _identity_format(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for ident in v:
            channel, sep, uid = ident.partition(":")
            if not sep or not uid or channel not in _KNOWN_CHANNELS:
                raise ValueError(
                    f"identity {ident!r} must look like '<channel>:<id>' "
                    f"with channel in {_KNOWN_CHANNELS}"
                )
            if channel == "imessage" and not _IMESSAGE_HANDLE.match(uid):
                raise ValueError(
                    f"imessage handle {uid!r} must be E.164 (+15551234567) or a lowercase email"
                )
        return v

    @field_validator("grants")
    @classmethod
    def _builtin_grants(cls, v: Grants) -> Grants:
        return _check_grants(v)


class WorkspaceDefault(BaseModel):
    """``default:``: what an unlisted, adapter-vouched identity on ``channel`` may do."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: Literal["slack"]
    grants: Grants = Field(default_factory=dict[str, Grant])

    @field_validator("grants")
    @classmethod
    def _member_grants(cls, v: Grants) -> Grants:
        _check_grants(v)
        for key, grant in v.items():
            if _holds(grant, "approve"):
                raise ValueError(f"default grant {key!r} may not list approve: never an approver")
        return v


class Principals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    principals: dict[str, Principal]
    default: WorkspaceDefault | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        owners = [n for n, p in self.principals.items() if p.role == "owner"]
        if len(owners) != 1:
            raise ValueError(f"exactly one owner required, found {owners}")
        seen: dict[str, str] = {}
        for name, p in self.principals.items():
            if ":" in name:  # ``<channel>:<id>`` names the workspace default's members
                raise ValueError(f"principal name {name!r} may not contain ':'")
            for ident in p.identities:
                if ident in seen:
                    raise ValueError(f"identity {ident!r} claimed by {seen[ident]} and {name}")
                seen[ident] = name
        return self

    @property
    def owner(self) -> str:
        return next(n for n, p in self.principals.items() if p.role == "owner")

    @property
    def team(self) -> bool:
        """A team config (PER-293, Cori): an approver or a workspace default. Without one,
        members behave exactly as before (no Claude by extension grant, plain refusals)."""
        return bool(self.approver_names) or self.default is not None

    @property
    def approver_names(self) -> tuple[str, ...]:
        """Principals with ``role: approver``, in file order (never the owner)."""
        return tuple(n for n, p in self.principals.items() if p.role == "approver")

    def _claimed(self, identity: str) -> str | None:
        for name, p in self.principals.items():
            if identity in p.identities:
                return name
        return None

    def is_default(self, name: str) -> bool:
        """``name`` is a workspace-default member (``slack:U0123``): synthetic, unlisted."""
        d = self.default
        if d is None or name in self.principals:
            return False
        channel, sep, uid = name.partition(":")
        return (
            bool(sep)
            and channel == d.channel
            and bool(_SLACK_ID.match(uid))
            and self._claimed(name) is None
        )

    def get(self, name: str) -> Principal | None:
        """The principal called ``name``: a listed one, or a workspace-default member."""
        p = self.principals.get(name)
        if p is not None:
            return p
        if self.default is not None and self.is_default(name):
            return Principal(identities=(name,), role="member", grants=self.default.grants)
        return None

    def known(self, name: str) -> bool:
        return self.get(name) is not None

    def resolve(self, identity: str, *, vouched: bool = False) -> str | None:
        """Map a channel identity to a principal name, or None if unknown. ``vouched``: the
        adapter vouches the identity is in this workspace (Slack, same ``team_id``); an
        unlisted one then resolves to the workspace default (named by the identity)."""
        name = self._claimed(identity)
        if name is not None:
            return name
        if vouched and self.is_default(identity):
            return identity
        return None

    def role(self, name: str) -> Role | None:
        p = self.get(name)
        return None if p is None else p.role

    def is_approver(self, name: str) -> bool:
        """The owner or an approver: may approve every builtin (``approve`` on global)."""
        return self.role(name) in ("owner", "approver")

    def display(self, name: str) -> str:
        """A principal's name as a reply says it (``justin`` -> ``Justin``)."""
        return name[:1].upper() + name[1:]

    def can(
        self,
        principal: str,
        action: Action,
        extension: str | None,
        *,
        grant: str | None = None,
        tool: str | None = None,
    ) -> bool:
        """``grant`` is the tool's ``ToolSpec.grant``; it matters only for a global request.
        ``tool`` is the tool's name; it matters for a per-tool grant (without it, a per-tool
        grant covers nothing) and for an approver's builtin (never an ``ADMIN_TOOLS`` one)."""
        p = self.get(principal)
        if p is None:
            return False
        if p.role == "owner":
            return True
        if action == "admin":
            return False
        if p.role == "approver":
            return extension is not None or tool not in ADMIN_TOOLS
        if extension is None:
            if action != "request" or grant is None:
                return False
            if _grants_action(p.grants.get(BUILTIN_GRANT + grant), "request", None):
                return True
            return (
                grant == CLAUDE_GRANT
                and self.team
                and any(
                    not key.startswith(BUILTIN_GRANT) and _holds(g, "request")
                    for key, g in p.grants.items()
                )
            )
        return _grants_action(p.grants.get(extension), action, tool)

    def holds(self, principal: str, action: GrantableAction, extension: str) -> bool:
        """Whether ``principal`` holds ``action`` on any of ``extension``'s tools (an approver
        and the owner always do)."""
        p = self.get(principal)
        if p is None:
            return False
        if p.role != "member":
            return True
        return _holds(p.grants.get(extension), action)

    def may_request(self, principal: str, spec: ToolSpec) -> bool:
        """Whether ``principal`` may request the tool ``spec`` (its extension or tool, or its
        grant)."""
        return self.can(principal, "request", spec.extension, grant=spec.grant, tool=spec.name)


def load_principals(path: Path) -> Principals:
    with path.open() as f:
        return Principals.model_validate(yaml.safe_load(f))
