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
