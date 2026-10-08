from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from resonant.extensions.checks import approver_errors
from resonant.principals import Principals
from resonant_sdk import Effect, Level, Manifest, ToolSpec
from resonant_sdk.manifest import parse_yaml

# --- ToolSpec invariants ---------------------------------------------------------------


def test_default_levels() -> None:
    assert ToolSpec(name="r", effect=Effect.READ).effective_level is Level.L0
    assert ToolSpec(name="w", effect=Effect.WRITE).effective_level is Level.L1
    assert ToolSpec(name="p", effect=Effect.PAY).effective_level is Level.L1
    assert ToolSpec(name="w3", effect=Effect.WRITE, level=Level.L3).effective_level is Level.L3


@pytest.mark.parametrize("level", [Level.L0, Level.L2, Level.L3])
def test_pay_is_always_l1(level: Level) -> None:
    with pytest.raises(ValidationError, match="always L1"):
        ToolSpec(name="pay", effect=Effect.PAY, level=level)


def test_read_without_secrets_is_l0() -> None:
    with pytest.raises(ValidationError, match="always L0"):
        ToolSpec(name="r", effect=Effect.READ, level=Level.L1)
    # A read that touches secrets may be raised above L0.
    ToolSpec(name="r", effect=Effect.READ, level=Level.L1, secrets=("token",))


def test_commit_requires_side_effect() -> None:
    with pytest.raises(ValidationError, match="side effect"):
        ToolSpec(name="r", effect=Effect.READ, commit=True)


@pytest.mark.parametrize("name", ["Bad", "1x", "has-dash", "x" * 65])
def test_tool_name_pattern(name: str) -> None:
    with pytest.raises(ValidationError):
        ToolSpec(name=name, effect=Effect.READ)


# --- Manifest --------------------------------------------------------------------------

TRADE_JEV = """
name: trade-jev
version: 1
approvers: [justin]
jobs:
  - id: daily-session
    schedule: "15 6 * * 1-5"
    tz: America/Los_Angeles
    critical: true
    run: ./run_session.sh
tools:
  get_pnl:
    run: ./cli pnl --date {date} --json
    effect: read
    args: {date: string}
  get_status:
    run: ./cli status --json
    effect: read
  kill_switch:
    run: ./cli kill
    effect: write
    level: L3
notify: {on: [fill, error, daily_summary], channel: imessage}
health: {check: ./health.sh, every: 5m}
secrets: [tradovate_session]
"""

DORI = """
name: dori
version: 1
uses: oncall
approvers: [justin, cofounder]
oncall:
  triggers: {slack_channel: "#dori-alerts"}
  autonomy: {unlock_after: 10m, deny_paths: [payments/, auth/]}
"""


def load(text: str, **patch: Any) -> Manifest:
    data = parse_yaml(text)
    data.update(patch)
    return Manifest.model_validate(data)


def test_example_manifests_parse() -> None:
    tj = load(TRADE_JEV)
    specs = {s.name: s for s in tj.tool_specs()}
    assert specs["get_pnl"].args_schema["properties"] == {"date": {"type": "string"}}
    assert specs["kill_switch"].effective_level is Level.L3
    assert all(s.source == "ext:trade-jev" for s in specs.values())
    dori = load(DORI)
    assert dori.first_party_config == {
        "triggers": {"slack_channel": "#dori-alerts"},
        "autonomy": {"unlock_after": "10m", "deny_paths": ["payments/", "auth/"]},
    }


def test_unknown_keys_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown keys"):
        load(TRADE_JEV, schedule_all="nope")
    with pytest.raises(ValidationError, match="unknown keys"):
        load(DORI, uses="briefing")  # an `oncall:` block without uses: oncall
    with pytest.raises(ValidationError):
        load(TRADE_JEV, tools={"x": {"run": "./x", "effect": "read", "bogus": 1}})


def test_version_required() -> None:
    with pytest.raises(ValidationError):
        load(TRADE_JEV, version=2)


def test_pay_not_l1_rejected() -> None:
    with pytest.raises(ValidationError, match="always L1"):
        load(TRADE_JEV, tools={"buy": {"run": "./buy", "effect": "pay", "level": "L3"}})


def test_undeclared_placeholder_rejected() -> None:
    with pytest.raises(ValidationError, match="undeclared args"):
        load(TRADE_JEV, tools={"x": {"run": "./x {date}", "effect": "read"}})


def test_tool_secret_must_be_declared() -> None:
    tools = {"x": {"run": "./x", "effect": "write", "secrets": ["api_key"]}}
    with pytest.raises(ValidationError, match="not declared"):
        load(TRADE_JEV, tools=tools)


def test_mcp_tools_typed() -> None:
    m = load(
        TRADE_JEV,
        mcp={
            "command": "uv run gmail-mcp",
            "tools": {"search": {"effect": "read"}, "send": {"effect": "write", "commit": True}},
        },
    )
    send = next(s for s in m.tool_specs() if s.name == "send")
    assert send.commit and send.effective_level is Level.L1
    assert m.mcp is not None and m.mcp.command == ("uv", "run", "gmail-mcp")
    with pytest.raises(ValidationError):
        load(TRADE_JEV, mcp={"command": "x", "tools": {"send": {}}})  # effect required


def test_bad_cron_and_duplicate_jobs() -> None:
    with pytest.raises(ValidationError, match="cron"):
        load(TRADE_JEV, jobs=[{"id": "a", "schedule": "every day", "run": "./a"}])
    job = {"id": "a", "schedule": "0 * * * *", "run": "./a"}
    with pytest.raises(ValidationError, match="unique"):
        load(TRADE_JEV, jobs=[job, job])


def test_approvers_must_hold_grant() -> None:
    principals = Principals.model_validate(
        {
            "principals": {
                "justin": {"role": "owner"},
                "cofounder": {"grants": {"dori": ["request", "approve"]}},
            }
        }
    )
    assert approver_errors(load(DORI), principals) == []
    assert approver_errors(load(TRADE_JEV, approvers=["cofounder"]), principals) == [
        "approver 'cofounder' lacks the approve grant for 'trade-jev'"
    ]
    assert approver_errors(load(TRADE_JEV, approvers=["ghost"]), principals) == [
        "approver 'ghost' is not a known principal"
    ]


def test_yaml_on_off_stay_strings() -> None:
    assert parse_yaml("notify: {on: [fill]}\nx: yes\ny: true") == {
        "notify": {"on": ["fill"]},
        "x": "yes",
        "y": True,
    }


def test_shipped_example_extension_is_valid() -> None:
    from pathlib import Path

    from resonant_sdk import load_manifest

    repo = Path(__file__).resolve().parents[1]
    m = load_manifest(repo / "extensions/example/resonant.yaml")
    levels = {s.name: s.effective_level for s in m.tool_specs()}
    assert levels == {"get_time": Level.L0, "write_note": Level.L3}
