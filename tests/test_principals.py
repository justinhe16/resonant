from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from resonant.principals import Principals, load_principals

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def principals() -> Principals:
    return Principals.model_validate(
        {
            "principals": {
                "justin": {"role": "owner", "identities": ["imessage:+15550000001", "slack:U1"]},
                "cofounder": {
                    "identities": ["slack:U2"],
                    "grants": {"dori": ["request", "approve"]},
                },
            }
        }
    )


def test_resolve(principals: Principals) -> None:
    assert principals.resolve("imessage:+15550000001") == "justin"
    assert principals.resolve("slack:U2") == "cofounder"
    assert principals.resolve("slack:U999") is None
    assert principals.owner == "justin"


def test_owner_can_everything(principals: Principals) -> None:
    assert principals.can("justin", "admin", None)
    assert principals.can("justin", "approve", "trade-jev")


def test_cofounder_scoped_to_dori(principals: Principals) -> None:
    assert principals.can("cofounder", "request", "dori")
    assert principals.can("cofounder", "approve", "dori")
    assert not principals.can("cofounder", "request", "trade-jev")
    assert not principals.can("cofounder", "approve", "life")
    assert not principals.can("cofounder", "request", None)  # global scope
    assert not principals.can("cofounder", "admin", "dori")
    assert not principals.can("stranger", "request", "dori")


def test_exactly_one_owner() -> None:
    with pytest.raises(ValidationError, match="exactly one owner"):
        Principals.model_validate({"principals": {"a": {}, "b": {}}})


def test_identity_cannot_be_shared() -> None:
    with pytest.raises(ValidationError, match="claimed by"):
        Principals.model_validate(
            {
                "principals": {
                    "a": {"role": "owner", "identities": ["slack:U1"]},
                    "b": {"identities": ["slack:U1"]},
                }
            }
        )


def test_admin_not_grantable() -> None:
    with pytest.raises(ValidationError):
        Principals.model_validate(
            {
                "principals": {
                    "a": {"role": "owner"},
                    "b": {"grants": {"dori": ["admin"]}},
                }
            }
        )


@pytest.mark.parametrize(
    "ident",
    ["imessage", "telegram:1", "email:x@y.z", "slack:", "imessage:5551234567", "imessage:Me@x.com"],
)
def test_bad_identity_format(ident: str) -> None:
    with pytest.raises(ValidationError):
        Principals.model_validate({"principals": {"a": {"role": "owner", "identities": [ident]}}})


def test_example_file_is_valid() -> None:
    p = load_principals(REPO / "config/principals.example.yaml")
    assert p.owner == "owner"


def test_imessage_handles_accepted() -> None:
    p = Principals.model_validate(
        {"principals": {"a": {"role": "owner", "identities": ["imessage:me@icloud.com"]}}}
    )
    assert p.resolve("imessage:me@icloud.com") == "a"
