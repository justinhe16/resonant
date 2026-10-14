"""`scripts/bootstrap.sh --check` and its Python helper are read-only and report per step."""

from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType

import pytest

from resonant import killswitch
from resonant.loop import NewTask
from resonant.store import approvals as ap
from resonant.store import tasks as repo
from resonant.store.db import kv_set, open_db, utcnow
from resonant_sdk import Effect, Level

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


# `pmset -g assertions` with caffeinate running (abridged).
CAFFEINATED = """\
Assertion status system-wide:
   PreventSystemSleep             1
   PreventUserIdleSystemSleep     1
Listed by owning process:
   pid 412(caffeinate): [0x0000a1b2] 12:00:01 PreventUserIdleSystemSleep named: "caffeinate"
   pid 412(caffeinate): [0x0000a1b3] 12:00:01 PreventSystemSleep named: "caffeinate"
"""
NOT_CAFFEINATED = """\
Assertion status system-wide:
   PreventUserIdleSystemSleep     0
Listed by owning process:
   pid 99(coreaudiod): [0x0000a1b4] 00:00:10 PreventUserIdleSystemSleep named: "audio"
"""
SLEEPS = " sleep                1 (sleep prevented by sharingd)\n displaysleep         10\n"
NEVER_SLEEPS = " sleep                0\n displaysleep         10\n"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("go_live_check", HELPER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_pmset(bin_dir: Path, assertions: str = CAFFEINATED, settings: str = SLEEPS) -> Path:
    """A ``pmset`` first on PATH, so tests never run the real one."""
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "assertions.txt").write_text(assertions)
    (bin_dir / "settings.txt").write_text(settings)
    pmset = bin_dir / "pmset"
    pmset.write_text(
        "#!/bin/sh\n"
        '[ "$1" = "-g" ] || exit 2\n'
        f'if [ "$2" = assertions ]; then cat "{bin_dir}/assertions.txt"; '
        f'else cat "{bin_dir}/settings.txt"; fi\n'
    )
    pmset.chmod(0o755)
    return bin_dir


def _helper(home: Path, fake_bin: Path | None = None) -> dict[str, tuple[str, bool, str]]:
    fake_bin = fake_bin or _fake_pmset(home.parent / "fakebin")
    env = {
        **os.environ,
        "RESONANT_HOME": str(home),
        "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
    }
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
        "imessage self_handle set",
        "owner has imessage identity",
    ):
        assert rows[step][0] == "PASS", (step, rows[step])
        assert rows[step][1] is True
    assert rows["healthchecks URL set"][:2] == ("PASS", False)  # advisory: docs/followups.md
    assert rows["Full Disk Access (chat.db)"][0] in {"PASS", "FAIL"}


def test_helper_fails_the_seeded_example_config(resonant_home: Path) -> None:
    shutil.copy(REPO / "config" / "resonant.example.yaml", resonant_home / "config.yaml")
    shutil.copy(REPO / "config" / "principals.example.yaml", resonant_home / "principals.yaml")
    rows = _helper(resonant_home)
    assert rows["config.yaml loads"][0] == "PASS"
    assert rows["imessage self_handle set"][0] == "FAIL"
    assert "example" in rows["imessage self_handle set"][2]
    assert rows["owner has imessage identity"][0] == "FAIL"
    assert rows["healthchecks URL set"][:2] == ("FAIL", False)


def test_helper_passes_a_real_icloud_self_handle(resonant_home: Path) -> None:
    # The example is a reserved-domain address, so a real @icloud.com handle is never "example".
    config = LIVE_CONFIG.replace("resonant.real@icloud.com", "resonant.agent@icloud.com")
    (resonant_home / "config.yaml").write_text(config)
    assert _helper(resonant_home)["imessage self_handle set"][0] == "PASS"


def _store(home: Path) -> sqlite3.Connection:
    return open_db(home / "resonant.db")


def _git_repo(path: Path) -> Path:
    path.mkdir()
    for args in (
        ["init", "-q", "-b", "main"],
        [
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-q",
            "--allow-empty",
            "-m",
            "x",
        ],
    ):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    return path


def _uv(tmp_path: Path) -> Path:
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\n")
    uv.chmod(0o755)
    return uv


def test_dry_run_off_fails_without_a_phase2_store(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(
        LIVE_CONFIG.replace("dry_run: true", "dry_run: false")
    )
    rows = _helper(resonant_home)
    assert rows["dry_run"] == ("FAIL", True, "dry_run off on a pre-Phase-2 install")
    assert rows["store schema 4"][:2] == ("FAIL", True)


def test_dry_run_off_fails_on_a_pre_phase2_store(resonant_home: Path) -> None:
    conn = _store(resonant_home)
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    (resonant_home / "config.yaml").write_text(
        LIVE_CONFIG.replace("dry_run: true", "dry_run: false")
    )
    rows = _helper(resonant_home)
    assert rows["store schema 4"][:2] == ("FAIL", True)
    assert "v2 < v4" in rows["store schema 4"][2]
    assert rows["dry_run"][:2] == ("FAIL", True)


def test_dry_run_off_passes_on_phase2_and_needs_uv(resonant_home: Path, tmp_path: Path) -> None:
    _store(resonant_home).close()
    config = LIVE_CONFIG.replace("dry_run: true", "dry_run: false")
    (resonant_home / "config.yaml").write_text(config)
    rows = _helper(resonant_home)
    assert rows["store schema 4"][:2] == ("PASS", True)
    assert rows["dry_run"] == ("PASS", True, "false (Phase 2 gate installed)")
    assert rows["ops.uv_path set"][:2] == ("FAIL", True)  # required once ops run for real

    uv = _uv(tmp_path)
    (resonant_home / "config.yaml").write_text(config + f"ops:\n  uv_path: {uv}\n")
    assert _helper(resonant_home)["ops.uv_path set"] == ("PASS", True, str(uv))


def test_uv_path_is_advisory_under_dry_run(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG + "ops:\n  uv_path: /nonexistent/uv\n")
    rows = _helper(resonant_home)
    assert rows["dry_run"] == ("PASS", True, "true (stage A of step 11)")
    assert rows["ops.uv_path set"] == ("FAIL", False, "/nonexistent/uv is not an executable file")


def test_kill_switch_rows(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG)
    conn = _store(resonant_home)
    rows = _helper(resonant_home)
    assert rows["kill switch state"] == ("PASS", True, "autonomy on, paused false")
    assert rows["autonomy on"][:2] == ("PASS", False)

    killswitch.engage(conn, actor="owner")
    rows = _helper(resonant_home)
    assert rows["kill switch state"] == ("PASS", True, "autonomy off, paused true")
    assert rows["autonomy on"][:2] == ("FAIL", False)  # advisory: the owner may mean it

    kv_set(conn, "autonomy", "maybe")
    assert _helper(resonant_home)["kill switch state"][:2] == ("FAIL", True)
    conn.close()


def test_stale_approvals_are_advisory(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG)
    conn = _store(resonant_home)
    task_id = repo.insert_task(
        conn, NewTask(kind="chat", runner="local", priority=1, principal="owner")
    )
    now = utcnow()
    for code, expires in (("A2", now - timedelta(hours=2)), ("A3", now + timedelta(minutes=5))):
        ap.insert_approval(
            conn,
            ap.NewApproval(
                task_id=task_id,
                intent_hash=f"h{code}",
                tool="restart_resonant",
                effect=Effect.WRITE,
                level=Level.L2,
                summary="restart Resonant",
                approvers=("owner",),
                code=code,
                expires_at=expires,
            ),
            now - timedelta(hours=3),
        )
    conn.close()
    assert _helper(resonant_home)["approvals not stale"] == (
        "FAIL",
        False,
        "1 pending past expiry + 1h",
    )


def test_repo_on_main_and_clean(resonant_home: Path, tmp_path: Path) -> None:
    repo_path = _git_repo(tmp_path / "checkout")
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG + f"ops:\n  repo_path: {repo_path}\n")
    assert _helper(resonant_home)["repo on main and clean"] == (
        "PASS",
        False,
        f"{repo_path} on main, clean",
    )

    (repo_path / "README.md").write_text("x")
    status, required, detail = _helper(resonant_home)["repo on main and clean"]
    assert (status, required) == ("FAIL", False) and "1 uncommitted change" in detail

    (repo_path / "README.md").unlink()
    subprocess.run(["git", "-C", str(repo_path), "checkout", "-q", "--detach"], check=True)
    status, _, detail = _helper(resonant_home)["repo on main and clean"]
    assert status == "FAIL" and "detached HEAD, not main" in detail


def test_keep_awake_row_uses_pmset(resonant_home: Path, tmp_path: Path) -> None:
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG)
    rows = _helper(resonant_home, _fake_pmset(tmp_path / "a", CAFFEINATED, SLEEPS))
    assert rows["keep-awake"] == (
        "PASS",
        False,
        "caffeinate holds PreventSystemSleep, PreventUserIdleSystemSleep",
    )
    rows = _helper(resonant_home, _fake_pmset(tmp_path / "b", NOT_CAFFEINATED, NEVER_SLEEPS))
    assert rows["keep-awake"] == ("PASS", False, "sleep 0")
    rows = _helper(resonant_home, _fake_pmset(tmp_path / "c", NOT_CAFFEINATED, SLEEPS))
    assert rows["keep-awake"][:2] == ("FAIL", False)
    assert "sleep 1" in rows["keep-awake"][2]


def test_keep_awake_parser() -> None:
    keep_awake = _load_helper().keep_awake
    assert keep_awake(CAFFEINATED, None)[0] is True
    assert keep_awake(NOT_CAFFEINATED, NEVER_SLEEPS) == (True, "sleep 0")
    # displaysleep 0 is not system sleep 0
    assert keep_awake(NOT_CAFFEINATED, " sleep 5\n displaysleep 0\n")[0] is False
    assert keep_awake(None, None) == (False, "pmset -g unavailable")


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

    fake_bin = _fake_pmset(resonant_home.parent / "fakebin")
    for home in (resonant_home, empty):
        env = {
            **os.environ,
            "RESONANT_HOME": str(home),
            "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
        }
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
