from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from resonant.config import load_settings

REPO = Path(__file__).resolve().parents[1]


def test_defaults_are_safe(resonant_home: Path) -> None:
    s = load_settings()
    assert s.home == resonant_home
    assert s.dry_run is True
    assert s.api.host == "127.0.0.1"
    assert s.model.context_tokens == 32768
    assert s.db_path == resonant_home / "resonant.db"


def test_yaml_then_env_precedence(resonant_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (resonant_home / "config.yaml").write_text("dry_run: false\napi:\n  port: 9000\n")
    s = load_settings()
    assert s.dry_run is False
    assert s.api.port == 9000

    monkeypatch.setenv("RESONANT_API__PORT", "9100")
    assert load_settings().api.port == 9100


def test_example_config_is_valid(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text((REPO / "config/resonant.example.yaml").read_text())
    load_settings()


def test_public_bind_rejected(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text("api:\n  host: 0.0.0.0\n")
    with pytest.raises(ValidationError, match="loopback"):
        load_settings()


def test_unknown_keys_rejected(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text("model:\n  nmae: typo\n")
    with pytest.raises(ValidationError):
        load_settings()


def test_explicit_home_reads_its_config(tmp_path: Path) -> None:
    other = tmp_path / "other"
    other.mkdir()
    (other / "config.yaml").write_text("dry_run: false\n")
    s = load_settings(home=other)
    assert s.home == other
    assert s.dry_run is False


@pytest.mark.parametrize(
    "yaml_text",
    [
        "timezone: Mars/Olympus\n",
        "claude:\n  max_concurrent: 2\n  reserved_oncall_slots: 2\n",
        "loop:\n  tick_s: 0\n",
    ],
)
def test_invalid_values_rejected(resonant_home: Path, yaml_text: str) -> None:
    (resonant_home / "config.yaml").write_text(yaml_text)
    with pytest.raises(ValidationError):
        load_settings()
