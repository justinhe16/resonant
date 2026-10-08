"""Checks that relate a manifest to this installation's principals."""

from __future__ import annotations

from resonant.principals import Principals
from resonant_sdk import Manifest


def approver_errors(manifest: Manifest, principals: Principals) -> list[str]:
    """Each declared approver must exist and hold ``approve`` for this extension."""
    errors: list[str] = []
    for name in manifest.approvers:
        if name not in principals.principals:
            errors.append(f"approver {name!r} is not a known principal")
        elif not principals.can(name, "approve", manifest.name):
            errors.append(f"approver {name!r} lacks the approve grant for {manifest.name!r}")
    return errors
