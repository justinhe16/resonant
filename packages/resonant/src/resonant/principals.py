"""Principals and permissions.

A principal is a person who can make requests and approve actions. Channel identities
(``imessage:+15551234567`` or ``imessage:me@icloud.com``, ``slack:<user id>``) map to
principals. Identity grants permissions.
Message content never does: content is always untrusted input.

Rules:
- Exactly one principal has ``role: owner``. The owner can do anything, including
  admin actions (kill switch, global settings).
- Every other principal can only do what ``grants`` lists, per extension.
- Global scope (``extension=None``) and ``admin`` are owner-only.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Action = Literal["request", "approve", "admin"]
GrantableAction = Literal["request", "approve"]

_KNOWN_CHANNELS = ("imessage", "slack")
# chat.db stores handles as E.164 phone numbers or lowercase emails. Requiring the same form
# here means a handle can only match by exact string comparison.
_IMESSAGE_HANDLE = re.compile(r"^(\+[1-9]\d{7,14}|[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,})$")


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    identities: tuple[str, ...] = ()
    role: Literal["owner", "member"] = "member"
    grants: dict[str, tuple[GrantableAction, ...]] = Field(default_factory=dict)

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


class Principals(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    principals: dict[str, Principal]

    @model_validator(mode="after")
    def _check(self) -> Self:
        owners = [n for n, p in self.principals.items() if p.role == "owner"]
        if len(owners) != 1:
            raise ValueError(f"exactly one owner required, found {owners}")
        seen: dict[str, str] = {}
        for name, p in self.principals.items():
            for ident in p.identities:
                if ident in seen:
                    raise ValueError(f"identity {ident!r} claimed by {seen[ident]} and {name}")
                seen[ident] = name
        return self

    @property
    def owner(self) -> str:
        return next(n for n, p in self.principals.items() if p.role == "owner")

    def resolve(self, identity: str) -> str | None:
        """Map a channel identity to a principal name, or None if unknown."""
        for name, p in self.principals.items():
            if identity in p.identities:
                return name
        return None

    def can(self, principal: str, action: Action, extension: str | None) -> bool:
        p = self.principals.get(principal)
        if p is None:
            return False
        if p.role == "owner":
            return True
        if action == "admin" or extension is None:
            return False
        return action in p.grants.get(extension, ())


def load_principals(path: Path) -> Principals:
    with path.open() as f:
        return Principals.model_validate(yaml.safe_load(f))
