"""`scripts/bootstrap.sh --check` and its Python helper are read-only and report per step."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
BOOTSTRAP = REPO / "scripts" / "bootstrap.sh"
HELPER = REPO / "scripts" / "go_live_check.py"
NOT_YET = "SKIP (available after Phase 1)"

LIVE_CONFIG = """\
dry_run: true
health:
  healthchecks_url: https://hc-ping.com/00000000-0000-0000-0000-000000000000
channels:
  imessage:
    self_handle: resonant.real@icloud.com
"""
LIVE_PRINCIPALS = """\
principals:
  owner:
    role: owner
    identities: ["imessage:+15557654321", "imessage:me@icloud.com"]
"""


def _snapshot(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, p.stat().st_size) for p in root.rglob("*")
    }


def _helper(home: Path) -> dict[str, tuple[str, bool, str]]:
    env = {**os.environ, "RESONANT_HOME": str(home)}
    proc = subprocess.run(
        [sys.executable, "-B", str(HELPER)], env=env, capture_output=True, text=True, check=True
    )
    rows: dict[str, tuple[str, bool, str]] = {}
    for line in proc.stdout.splitlines():
        status, required, step, detail = line.split("\t")
        rows[step] = (status, required == "1", detail)
    return rows


def test_helper_passes_a_live_config(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG)
    (resonant_home / "principals.yaml").write_text(LIVE_PRINCIPALS)
    rows = _helper(resonant_home)
    for step in (
        "config.yaml loads",
        "dry_run is true",
        "imessage self_handle set",
        "owner has imessage identity",
        "healthchecks URL set",
    ):
        assert rows[step][0] == "PASS", (step, rows[step])
        assert rows[step][1] is True
    assert rows["Full Disk Access (chat.db)"][0] in {"PASS", "FAIL"}


def test_helper_fails_the_seeded_example_config(resonant_home: Path) -> None:
    shutil.copy(REPO / "config" / "resonant.example.yaml", resonant_home / "config.yaml")
    shutil.copy(REPO / "config" / "principals.example.yaml", resonant_home / "principals.yaml")
    rows = _helper(resonant_home)
    assert rows["config.yaml loads"][0] == "PASS"
    assert rows["imessage self_handle set"][0] == "FAIL"
    assert "example" in rows["imessage self_handle set"][2]
    assert rows["owner has imessage identity"][0] == "FAIL"
    assert rows["healthchecks URL set"][0] == "FAIL"


def test_helper_fails_when_dry_run_is_off(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG.replace("true", "false"))
    assert _helper(resonant_home)["dry_run is true"][0] == "FAIL"


def test_helper_reports_invalid_config_as_fail(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text("api:\n  host: 0.0.0.0\n")
    rows = _helper(resonant_home)
    assert rows["config.yaml loads"][0] == "FAIL"


@pytest.mark.skipif(sys.platform != "darwin", reason="bootstrap.sh targets macOS")
@pytest.mark.skipif(not (REPO / ".venv" / "bin" / "resonant").exists(), reason="needs .venv")
def test_check_changes_nothing_and_fails_on_required_steps(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG)
    (resonant_home / "principals.yaml").write_text(LIVE_PRINCIPALS)
    empty = resonant_home / "empty"
    empty.mkdir()
    before = _snapshot(resonant_home)

    for home in (resonant_home, empty):
        env = {**os.environ, "RESONANT_HOME": str(home)}
        proc = subprocess.run(
            ["bash", str(BOOTSTRAP), "--check"], env=env, capture_output=True, text=True
        )
        out = proc.stdout
        for step in ("model probe", "imessage self-test", "router eval", "daemon up"):
            assert step in out
        if home is empty:
            assert proc.returncode == 1, out
            assert "config.yaml loads" in out and "FAIL" in out
        else:
            for step in ("imessage self_handle set", "owner has imessage identity"):
                line = next(ln for ln in out.splitlines() if step in ln)
                assert "PASS" in line, line
        # Commands that are not on this branch yet are reported, never run.
        for args in (["model", "probe"], ["selftest", "imessage"], ["eval", "router"]):
            present = (
                subprocess.run(
                    [str(REPO / ".venv" / "bin" / "resonant"), *args, "--help"],
                    capture_output=True,
                    env=env,
                ).returncode
                == 0
            )
            if not present:
                step = {"model": "model probe", "selftest": "imessage self-test"}.get(
                    args[0], "router eval"
                )
                line = next(ln for ln in out.splitlines() if step in ln)
                assert NOT_YET in line, line

    assert _snapshot(resonant_home) == before
    assert list(empty.iterdir()) == []
