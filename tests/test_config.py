from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from resonant.config import BriefingConfig, ExtensionsConfig, SecretsConfig, load_settings

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


def test_imessage_self_handle(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(
        "channels:\n  imessage:\n    self_handle: resonant.agent@icloud.com\n"
    )
    assert load_settings().channels.imessage.self_handle == "resonant.agent@icloud.com"
    (resonant_home / "config.yaml").write_text(
        "channels:\n  imessage:\n    self_handle: '+15551234567'\n"
    )
    with pytest.raises(ValidationError, match="email handle"):
        load_settings()


def test_gate_and_ops_defaults(resonant_home: Path) -> None:
    s = load_settings()
    assert s.gate.approval_ttl_s == 1800
    # Running from this checkout, so the default repo is this checkout.
    assert s.ops.repo_path == REPO
    assert s.ops.uv_path is None


def test_gate_and_ops_from_yaml(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(
        "gate:\n  approval_ttl_s: 600\nops:\n  repo_path: ~/src/resonant\n  uv_path: /usr/bin/uv\n"
    )
    s = load_settings()
    assert s.gate.approval_ttl_s == 600
    assert s.ops.repo_path == Path("~/src/resonant").expanduser()
    assert s.ops.uv_path == Path("/usr/bin/uv")


@pytest.mark.parametrize("ttl", [0, 59, 86401])
def test_approval_ttl_bounds(resonant_home: Path, ttl: int) -> None:
    (resonant_home / "config.yaml").write_text(f"gate:\n  approval_ttl_s: {ttl}\n")
    with pytest.raises(ValidationError):
        load_settings()


# --- Phase 3 sections -----------------------------------------------------------------


def test_phase3_defaults(resonant_home: Path) -> None:
    s = load_settings()
    assert s.extensions.max_concurrent_jobs == 4
    assert (s.extensions.result_max_bytes, s.extensions.model_result_max_chars) == (32768, 4096)
    p = s.extensions.process
    assert p.path_env == "/opt/homebrew/bin:/usr/bin:/bin"
    assert (p.stdout_max_bytes, p.stderr_max_bytes) == (256 * 1024, 8 * 1024)
    assert (p.kill_grace_s, p.tree_sample_s, p.health_timeout_s) == (2.0, 2.0, 20.0)
    assert (s.extensions.notify.max_per_hour, s.extensions.notify.max_chars) == (20, 300)
    assert s.secrets.security_path == Path("/usr/bin/security")
    assert s.secrets.timeout_s == 5.0
    assert s.briefing.enabled is True
    assert s.briefing.local_time == time(8, 0)
    assert s.extensions_dir == resonant_home / "extensions"
    assert s.ext_data_dir == resonant_home / "ext-data"


def test_pre_phase3_config_loads_unchanged(resonant_home: Path) -> None:
    # A Phase 2 config.yaml (no Phase 3 keys) loads with every new default.
    example = (REPO / "config/resonant.example.yaml").read_text()
    (resonant_home / "config.yaml").write_text(example.split("\n# Phase 3.")[0])
    s = load_settings()
    assert s.extensions == ExtensionsConfig()
    assert s.secrets == SecretsConfig() and s.briefing == BriefingConfig()


def test_phase3_from_yaml_and_env(resonant_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (resonant_home / "config.yaml").write_text(
        "briefing:\n  enabled: false\n  at: '07:45'\n"
        "extensions:\n  max_concurrent_jobs: 2\n  process:\n    path: [/usr/bin]\n"
    )
    s = load_settings()
    assert s.briefing.enabled is False and s.briefing.local_time == time(7, 45)
    assert s.extensions.max_concurrent_jobs == 2
    assert s.extensions.process.path_env == "/usr/bin"
    monkeypatch.setenv("RESONANT_BRIEFING__AT", "21:05")
    assert load_settings().briefing.local_time == time(21, 5)


@pytest.mark.parametrize(
    "yaml_text",
    [
        "briefing:\n  at: '8:00'\n",
        "briefing:\n  at: '24:00'\n",
        "briefing:\n  at: '08:60'\n",
        "briefing:\n  at: 08:00am\n",
        "briefing:\n  cron: daily\n",
        "extensions:\n  max_concurrent_jobs: 0\n",
        "extensions:\n  max_concurrent_jobs: 17\n",
        "extensions:\n  result_max_bytes: 999999999\n",
        "extensions:\n  result_max_bytes: 2048\n  model_result_max_chars: 4096\n",
        "extensions:\n  process:\n    path: []\n",
        "extensions:\n  process:\n    path: [bin]\n",
        "extensions:\n  process:\n    stdout_max_bytes: 0\n",
        "extensions:\n  process:\n    stdout_max_bytes: 104857600\n",
        "extensions:\n  process:\n    kill_grace_s: 0\n",
        "extensions:\n  notify:\n    max_per_hour: 0\n",
        "extensions:\n  notify:\n    max_chars: 5000\n",
        "secrets:\n  security_path: security\n",
        "secrets:\n  timeout_s: 0\n",
        "secrets:\n  value: hunter2\n",
    ],
)
def test_phase3_invalid_values_rejected(resonant_home: Path, yaml_text: str) -> None:
    (resonant_home / "config.yaml").write_text(yaml_text)
    with pytest.raises(ValidationError):
        load_settings()
