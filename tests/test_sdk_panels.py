"""The manifest's ``panels:`` block (PER-280) and the stdlib helpers that build a panel's
output."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from resonant.extensions.checks import argv0_error, commands, dir_errors
from resonant_sdk import Effect, Manifest, panel
from resonant_sdk.manifest import PANELS_MAX, cron_values


def panel_entry(**patch: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "id": "paper",
        "title": "Paper trading",
        "command": ["./cli.py", "panel", "paper"],
        "refresh": {"idle": "*/15 * * * *"},
    }
    entry.update(patch)
    return entry


def manifest(*panels: dict[str, Any], **patch: Any) -> Manifest:
    data: dict[str, Any] = {
        "name": "demo",
        "version": 1,
        "tools": {"get": {"run": "./cli.py get", "effect": "read"}},
        "panels": list(panels),
    }
    data.update(patch)
    return Manifest.model_validate(data)


def test_a_panel_parses_with_its_defaults_and_becomes_an_l0_read_spec() -> None:
    m = manifest(panel_entry())
    (p,) = m.panels
    assert p.description == "" and p.tz is None and p.secrets == ()
    assert p.timeout_s == 30 and p.max_bytes == 16384 and p.refresh.active is None
    assert p.run == "./cli.py panel paper" and p.spec_name == "panel_paper"
    (spec,) = m.panel_specs()
    assert spec.name == "panel_paper" and spec.effect is Effect.READ
    assert spec.effective_level.value == "L0" and spec.extension == "demo"
    assert spec.source == "ext:demo" and spec.timeout_s == 30
    assert spec.args_schema["required"] == ["due_at"]


def test_the_whole_block_and_the_hyphenated_spec_name() -> None:
    m = manifest(
        panel_entry(
            id="paper-pnl",
            description="PnL and positions",
            refresh={"idle": "*/15 * * * *", "active": "*/1 9-16 * * 1-5"},
            tz="America/New_York",
            secrets=["alpaca_key"],
            timeout_s=120,
            max_bytes=32768,
        ),
        secrets=["alpaca_key"],
    )
    (p,) = m.panels
    assert p.spec_name == "panel_paper_pnl" and p.refresh.active == "*/1 9-16 * * 1-5"
    (spec,) = m.panel_specs()
    assert spec.secrets == ("alpaca_key",) and spec.timeout_s == 120


def test_an_old_manifest_without_panels_still_validates_and_round_trips() -> None:
    m = Manifest.model_validate({"name": "old", "version": 1})
    assert m.panels == () and m.panel_specs() == []
    again = Manifest.model_validate(m.model_dump(mode="json"))
    assert again == m
    withp = manifest(panel_entry())
    assert Manifest.model_validate(withp.model_dump(mode="json")) == withp


def test_the_command_becomes_a_run_string_the_checks_see() -> None:
    m = manifest(panel_entry(command=["./cli.py", "panel", "two words"]))
    assert m.panels[0].run == "./cli.py panel 'two words'"
    assert ("panel paper", m.panels[0].run) in commands(m)


@pytest.mark.parametrize(
    "patch",
    [
        {"id": "Paper"},
        {"id": "1paper"},
        {"id": "p" * 49},
        {"title": ""},
        {"title": "t" * 61},
        {"title": "two\nlines"},
        {"description": "d" * 161},
        {"description": "bad\x07"},
        {"command": []},
        {"command": ["x"] * 17},
        {"command": ["./cli.py", ""]},
        {"command": ["./cli.py", "{date}"]},
        {"command": ["./cli.py", "a}"]},
        {"command": ["./cli.py", "café"]},
        {"command": ["./cli.py", "x" * 201]},
        {"command": ["./cli.py", "tab\there"]},
        {"timeout_s": 0},
        {"timeout_s": 121},
        {"max_bytes": 1023},
        {"max_bytes": 32769},
        {"tz": "Mars/Base"},
        {"bogus": 1},
        {"refresh": {}},
        {"refresh": {"idle": "*/15 * * * *", "extra": "x"}},
    ],
)
def test_panel_validation_rejects(patch: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        manifest(panel_entry(**patch))


@pytest.mark.parametrize(
    "cron",
    [
        "@hourly",
        "*/15 * * *",
        "*/15 * * * * *",
        "0 9 * * mon",  # names are not accepted
        "0 9 * jan *",
        "60 * * * *",
        "0 24 * * *",
        "0 0 0 * *",
        "0 0 32 * *",
        "0 0 * 13 *",
        "0 0 * * 8",
        "*/0 * * * *",
        "",
    ],
)
def test_crons_are_five_numeric_in_range_fields(cron: str) -> None:
    with pytest.raises(ValidationError):
        manifest(panel_entry(refresh={"idle": cron}))
    with pytest.raises(ValidationError):
        manifest(panel_entry(refresh={"idle": "0 * * * *", "active": cron}))


def test_a_panel_may_refresh_every_minute_and_idle_is_required() -> None:
    m = manifest(panel_entry(refresh={"idle": "*/1 * * * *", "active": "* 9-16 * * 1-5"}))
    assert m.panels[0].refresh.idle == "*/1 * * * *"
    with pytest.raises(ValidationError):
        manifest(panel_entry(refresh={"active": "*/1 * * * *"}))
    assert cron_values("1-5", 0, 7) == {1, 2, 3, 4, 5}


def test_ids_are_unique_and_there_are_at_most_eight() -> None:
    with pytest.raises(ValidationError, match="unique"):
        manifest(panel_entry(), panel_entry(title="again"))
    ok = [panel_entry(id=f"p{i}") for i in range(PANELS_MAX)]
    assert len(manifest(*ok).panels) == PANELS_MAX
    with pytest.raises(ValidationError):
        manifest(*ok, panel_entry(id="one-more"))


def test_undeclared_secrets_are_refused() -> None:
    with pytest.raises(ValidationError, match="not declared"):
        manifest(panel_entry(secrets=["alpaca_key"]))


def test_a_panel_spec_may_not_shadow_a_tool_or_a_job() -> None:
    with pytest.raises(ValidationError, match="collide"):
        manifest(
            panel_entry(),
            tools={"panel_paper": {"run": "./cli.py x", "effect": "read"}},
        )
    # a job spec is job_<id>: no clash is possible by name, but a tool named like a panel is
    with pytest.raises(ValidationError, match="collide"):
        manifest(panel_entry(id="x"), tools={"panel_x": {"run": "./cli.py x", "effect": "read"}})


def test_dir_errors_applies_the_argv0_rules_to_a_panel(tmp_path: Path) -> None:
    (tmp_path / "cli.py").write_text("print('{}')\n")
    good = manifest(panel_entry())
    assert dir_errors(tmp_path, good) == []
    bad = manifest(panel_entry(command=["sh", "-c", "true"]))
    errors = dir_errors(tmp_path, bad)
    assert any(e.startswith("panel paper:") for e in errors), errors
    assert argv0_error(tmp_path, bad.panels[0].run) is not None


def test_the_helpers_build_the_documented_shapes() -> None:
    at = datetime(2026, 10, 16, 9, 0, tzinfo=UTC)
    out = panel.panel(
        [
            panel.view(
                "main",
                "Main",
                [
                    panel.stat("Equity", None, format="money"),
                    panel.series("line", [at], {"A": [1.5]}, unit="USD"),
                    panel.table(
                        [panel.column("a", "A", "pill")],
                        [{"a": panel.pill("x", "ok")}],
                    ),
                    panel.status([{"name": "db", "state": "up"}]),
                    panel.kv({"k": 1}),
                    panel.text("hi"),
                    panel.details({"a": [1]}, collapsed=False),
                ],
            )
        ],
        updated_at=at,
        health="ok",
        local_url="http://127.0.0.1:1/",
    )
    assert out["schema"] == 1 and out["updated_at"] == "2026-10-16T09:00:00+00:00"
    stat, series = out["views"][0]["widgets"][:2]
    assert stat == {"type": "stat", "label": "Equity", "value": None, "format": "money"}
    assert series["x"] == {"type": "time", "values": ["2026-10-16T09:00:00+00:00"]}
    json.dumps(out)
    with pytest.raises(ValueError, match="timezone"):
        panel.panel([], updated_at=datetime(2026, 1, 1))
