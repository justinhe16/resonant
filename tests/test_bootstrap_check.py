"""`scripts/bootstrap.sh --check` and its Python helper are read-only and report per step."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import pytest

from resonant import killswitch
from resonant.config import load_settings
from resonant.e2e.extensions import install_fixture
from resonant.loop import NewTask
from resonant.secrets import FakeKeychain, SecretStore
from resonant.store import approvals as ap
from resonant.store import extensions as ext_store
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
    assert rows["dead-man switch"][:2] == ("PASS", False)  # advisory: go-live step 15
    assert rows["Full Disk Access (chat.db)"][0] in {"PASS", "FAIL"}


def test_helper_fails_the_seeded_example_config(resonant_home: Path) -> None:
    shutil.copy(REPO / "config" / "resonant.example.yaml", resonant_home / "config.yaml")
    shutil.copy(REPO / "config" / "principals.example.yaml", resonant_home / "principals.yaml")
    rows = _helper(resonant_home)
    assert rows["config.yaml loads"][0] == "PASS"
    assert rows["imessage self_handle set"][0] == "FAIL"
    assert "example" in rows["imessage self_handle set"][2]
    assert rows["owner has imessage identity"][0] == "FAIL"
    assert rows["dead-man switch"][:2] == ("FAIL", False)


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
    assert rows["store schema 14"][:2] == ("FAIL", True)


def test_dry_run_off_fails_on_a_pre_phase2_store(resonant_home: Path) -> None:
    conn = _store(resonant_home)
    conn.execute("PRAGMA user_version = 2")
    conn.close()
    (resonant_home / "config.yaml").write_text(
        LIVE_CONFIG.replace("dry_run: true", "dry_run: false")
    )
    rows = _helper(resonant_home)
    assert rows["store schema 14"][:2] == ("FAIL", True)
    assert "v2 < v14" in rows["store schema 14"][2]
    assert rows["dry_run"][:2] == ("FAIL", True)


def test_dry_run_off_passes_on_phase2_and_needs_uv(resonant_home: Path, tmp_path: Path) -> None:
    _store(resonant_home).close()
    config = LIVE_CONFIG.replace("dry_run: true", "dry_run: false")
    (resonant_home / "config.yaml").write_text(config)
    rows = _helper(resonant_home)
    assert rows["store schema 14"][:2] == ("PASS", True)
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


# --- Phase 3 rows (go-live step 12) -------------------------------------------------------

SECRET_EXT = """\
name: demo
version: 1
secrets: [token, other]
tools:
  status:
    run: ./cli.py status
    effect: read
    secrets: [token]
jobs:
  - id: nightly
    schedule: "0 3 * * *"
    run: ./cli.py nightly
    effect: read
    secrets: [other]
"""
PLAIN_EXT = """\
name: plain
version: 1
tools:
  status:
    run: ./cli.py status
    effect: read
jobs:
  - id: watch
    schedule: "*/10 * * * *"
    run: ./cli.py watch
    effect: read
"""
# 2026-10-14 10:30 in Los Angeles.
NOW = datetime(2026, 10, 14, 17, 30, tzinfo=UTC)


def test_phase3_rows_with_no_extensions(resonant_home: Path) -> None:
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG)
    _store(resonant_home).close()
    rows = _helper(resonant_home)
    assert rows["extensions dir"] == ("PASS", True, "none installed yet")
    assert rows["extension jobs scheduled"] == ("PASS", True, "no extension jobs")
    status, required, detail = rows["briefing scheduled"]
    assert (status, required) == ("PASS", True)
    assert detail.startswith("daily 08:00 America/Los_Angeles; next ")
    assert [step for step in rows if step.startswith(("secret ", "extension "))] == [
        "extension jobs scheduled"
    ]

    (resonant_home / "config.yaml").write_text(LIVE_CONFIG + "briefing:\n  enabled: false\n")
    rows = _helper(resonant_home)
    assert rows["briefing scheduled"] == ("FAIL", False, "briefing.enabled is false")


def test_phase3_rows_with_an_installed_extension(resonant_home: Path) -> None:
    # No secrets declared, so the subprocess never asks the (real) Keychain anything.
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG)
    settings = load_settings()
    conn = _store(resonant_home)
    record = install_fixture(settings, conn, PLAIN_EXT, {"cli.py": "print('{}')\n"})
    conn.close()
    before = _snapshot(resonant_home)
    rows = _helper(resonant_home)
    # Read-only. (SQLite itself may add the WAL index files beside a WAL-mode database.)
    after = {k: v for k, v in _snapshot(resonant_home).items() if not k.endswith(("-shm", "-wal"))}
    assert after == before
    assert rows["extensions dir"][:2] == ("PASS", True)
    assert rows["extension plain pin"] == (
        "PASS",
        False,
        f"enabled, a1b2c3d4 digest {record.tree_digest[:8]}, copy ok",
    )
    status, required, detail = rows["extension jobs scheduled"]
    assert (status, required) == ("PASS", True) and detail.startswith("1 job(s); next plain/watch ")
    assert not [step for step in rows if step.startswith("secret ")]


def _records(resonant_home: Path, *manifests: str) -> list[ext_store.ExtensionRecord]:
    settings = load_settings()
    conn = _store(resonant_home)
    for manifest in manifests:
        install_fixture(settings, conn, manifest, {"cli.py": "print('{}')\n"})
    records = ext_store.list_extensions(conn)
    conn.close()
    return records


def test_extensions_dir_row(resonant_home: Path) -> None:
    check = _load_helper()
    root = resonant_home / "extensions"
    assert check.extensions_dir_row(root, []) == (
        "PASS",
        True,
        "extensions dir",
        "none installed yet",
    )
    records = _records(resonant_home, PLAIN_EXT)
    assert check.extensions_dir_row(root, records)[:2] == ("PASS", True)
    (root / ".staging-abc").mkdir()
    assert check.extensions_dir_row(root, records)[3].endswith("not installed: .staging-abc")
    root.chmod(0o777)
    try:
        assert check.extensions_dir_row(root, records)[:3] == ("FAIL", True, "extensions dir")
    finally:
        root.chmod(0o755)
    shutil.rmtree(root / "plain")
    assert check.extensions_dir_row(root, records) == (
        "FAIL",
        True,
        "extensions dir",
        "no copy for plain",
    )


def test_pin_rows_are_advisory(resonant_home: Path) -> None:
    check = _load_helper()
    root = resonant_home / "extensions"
    (record,) = _records(resonant_home, PLAIN_EXT)
    ((status, required, step, _),) = check.pin_rows(root, [record])
    assert (status, required, step) == ("PASS", False, "extension plain pin")

    (root / "plain" / "cli.py").write_text("print('edited')\n")
    assert check.pin_rows(root, [record]) == [
        ("FAIL", False, "extension plain pin", "copy changed; resonant ext update plain")
    ]
    tampered = replace(record, status="disabled_tampered")
    assert check.pin_rows(root, [tampered])[0][3] == (
        "disabled_tampered; resonant ext update plain"
    )
    (root / "plain" / "cli.py").write_text("print('{}')\n")
    unpinned = replace(record, manifest={**record.manifest, "bins": ["gh"]})
    assert check.pin_rows(root, [unpinned])[0][3] == "argv0 missing: gh (not pinned)"
    gone = replace(record, argv0_paths={"x": "/nonexistent/gh"})
    assert check.pin_rows(root, [gone])[0] == (
        "FAIL",
        False,
        "extension plain pin",
        "argv0 missing: /nonexistent/gh",
    )


def test_secret_rows_check_existence_only(resonant_home: Path) -> None:
    check = _load_helper()
    (record,) = _records(resonant_home, SECRET_EXT)
    keychain = FakeKeychain(items={"demo/token": "a-secret-value-1"})
    store = SecretStore(keychain)

    def exists(ref: str) -> bool:
        return asyncio.run(store.exists(ref))

    assert check.secret_rows([record], exists) == [
        ("PASS", True, "secret demo/token", "present (value not read)"),
        ("FAIL", True, "secret demo/other", "missing: resonant secret set demo/other"),
    ]
    assert keychain.calls and all("-w" not in c.argv for c in keychain.calls)
    disabled = replace(record, status="disabled")
    assert [r[1] for r in check.secret_rows([disabled], exists)] == [False, False]
    keychain.fail_with = 36  # locked: reported, never a crash
    assert check.secret_rows([record], exists)[0] == (
        "FAIL",
        True,
        "secret demo/token",
        "unavailable: keychain locked or no user interaction allowed (exit 36)",
    )


def test_runner_rows_report_names_only(resonant_home: Path) -> None:
    check = _load_helper()
    settings = load_settings(
        runner={
            "github_app": {"app_id": 123, "installation_id": 456},
            "repos": {
                "demo": {"remote": "github.com/o/demo"},
                "old": {"remote": "github.com/o/old"},
            },
        }
    )
    keychain = FakeKeychain(items={"runner/github_token": "ghp_" + "a" * 36})
    store = SecretStore(keychain)

    def exists(ref: str) -> bool:
        return asyncio.run(store.exists(ref))

    cis = {"demo": True, "old": False}

    def ci_of(_settings: object, name: str) -> bool | None:
        return cis[name]

    def no_mirror(_settings: object, _name: str) -> bool | None:
        return None

    rows = {r[2]: r for r in check.runner_rows(settings, 14, exists, ci_of)}
    assert rows["runs table"][:2] == ("PASS", False)
    assert rows["runner repos"][3] == "2: demo, old"
    assert rows["runner secret runner/github_token"][0] == "PASS"
    for owner_step in (
        "runner gh pr create with an installation token",
        "runner gh pr checks --watch with an installation token",
        "runner base branch protection",
    ):
        assert rows[owner_step][:2] == ("SKIP", False) and "owner step" in rows[owner_step][3]
    assert rows["runner github app"][:2] == ("PASS", False)
    assert rows["runner github app"][3] == "app 123, installation 456"
    key = rows["runner secret runner/github_app_key"]
    assert key[0] == "FAIL" and "secret set runner/github_app_key --from-file" in key[3]
    claude = rows["runner secret runner/claude_token (or runner/anthropic_key)"]
    assert claude[0] == "FAIL" and "claude setup-token" in claude[3]
    assert rows["runner repo demo has CI"][0] == "PASS"
    assert rows["runner repo old has CI"][0] == "FAIL"
    assert all(not r[1] for r in rows.values())  # advisory
    assert keychain.calls and all("-w" not in c.argv for c in keychain.calls)
    # old schema, no repos, no mirror yet
    old = check.runner_rows(load_settings(), 5, exists)
    assert old[:2] == [
        ("FAIL", False, "runs table", "schema v5, need 14"),
        (
            "SKIP",
            False,
            "runner repos",
            "none seeded in runner.repos (optional: repos register on first use)",
        ),
    ]
    assert old[2][:3] == ("FAIL", False, "runner github app")  # no runner.github_app
    none = check.runner_rows(settings, 14, exists, no_mirror)
    assert [r[0] for r in none if r[2].endswith("has CI")] == ["SKIP", "SKIP"]


def test_runner_ci_row_reads_the_mirror_only(resonant_home: Path, tmp_path: Path) -> None:
    check = _load_helper()
    settings = load_settings(
        runner={
            "repos": {"demo": {"remote": "github.com/o/demo"}},
            "mirror_dir": tmp_path / "mirrors",
            "work_dir": tmp_path / "runs",
            "cache_dir": tmp_path / "runs" / ".cache",
            "secrets_dir": tmp_path / "secrets",
        }
    )
    assert check._repo_has_ci(settings, "demo") is None  # no mirror: nothing fetched
    assert not (tmp_path / "mirrors").exists()


def test_jobs_and_briefing_rows_come_from_config(resonant_home: Path) -> None:
    check = _load_helper()
    records = _records(resonant_home, SECRET_EXT, PLAIN_EXT)
    tz = "America/Los_Angeles"
    assert check.jobs_row(records, tz, NOW) == (
        "PASS",
        True,
        "extension jobs scheduled",
        "2 job(s); next plain/watch 10:40am",
    )
    disabled = [replace(r, status="disabled") for r in records]
    assert check.jobs_row(disabled, tz, NOW)[3] == "no extension jobs"
    settings = load_settings(briefing={"at": "07:15"})
    assert check.briefing_row(settings, NOW) == (
        "PASS",
        True,
        "briefing scheduled",
        "daily 07:15 America/Los_Angeles; next tomorrow 7:15am",
    )
    assert check.pulse_row(settings, NOW) == (
        "PASS",
        True,
        "pulse scheduled",
        "hourly America/Los_Angeles; quiet 00:00-06:00; next 11:00am",
    )
    off = load_settings(pulse={"enabled": False})
    assert check.pulse_row(off, NOW) == ("FAIL", False, "pulse scheduled", "pulse.enabled is false")
    keychain = FakeKeychain(items={"demo/token": "a-secret-value-1", "demo/other": "b-value-22"})
    store = SecretStore(keychain)

    def exists(ref: str) -> bool:
        return asyncio.run(store.exists(ref))

    conn = _store(resonant_home)
    rows = check.phase3_rows(load_settings(), conn, NOW, exists)
    conn.close()
    assert [r[2] for r in rows] == [
        "extensions dir",
        "extension demo pin",
        "extension plain pin",
        "secret demo/token",
        "secret demo/other",
        "extension jobs scheduled",
        "briefing scheduled",
        "pulse scheduled",
    ]
    assert all(r[0] == "PASS" for r in rows), rows


# --- the status relay (DB-3) ---------------------------------------------------------------

RELAY_ON = {
    "enabled": True,
    "supabase_url": "https://abcdefghijklmnop.supabase.co",
    "anon_key": "fake-fake-fake-fake-fake",
}


def _relay_rows(
    conn: sqlite3.Connection | None, exists: object = None, **relay: object
) -> dict[str, tuple[str, bool, str]]:
    helper = _load_helper()
    calls: list[str] = []

    def fake_exists(ref: str) -> bool:
        calls.append(ref)
        if isinstance(exists, BaseException):
            raise exists
        return bool(exists)

    rows = helper.relay_rows(load_settings(relay=relay), conn, fake_exists)
    out = {step: (status, required, detail) for status, required, step, detail in rows}
    out["_calls"] = ("", False, ",".join(calls))
    return out


def test_dead_man_row_passes_with_the_relay_or_a_healthchecks_url(resonant_home: Path) -> None:
    helper = _load_helper()
    hc = {"healthchecks_url": "https://hc-ping.com/00000000-0000-0000-0000-000000000000"}
    status, required, step, detail = helper.deadman_row(load_settings(relay=RELAY_ON))
    assert (status, required, step) == ("PASS", False, "dead-man switch")
    assert "relay on" in detail and "fly status" in detail
    assert helper.deadman_row(load_settings(health=hc))[:2] == ("PASS", False)
    both = helper.deadman_row(load_settings(relay=RELAY_ON, health=hc))
    assert both[0] == "PASS" and "healthchecks_url" in both[3]
    none = helper.deadman_row(load_settings())
    assert none[:2] == ("FAIL", False) and "go-live step 15" in none[3]


def test_relay_rows_are_advisory_while_off(resonant_home: Path) -> None:
    rows = _relay_rows(None)
    assert rows["status relay enabled"][:2] == ("FAIL", False)
    assert rows["relay supabase_url set"][:2] == ("FAIL", False)
    assert rows["relay anon_key set"][:2] == ("FAIL", False)
    assert rows["relay secret relay/supabase_key"] == (
        "SKIP",
        False,
        "relay off; Keychain not checked",
    )
    assert "relay pushing" not in rows
    assert rows["_calls"][2] == ""  # the Keychain isn't asked


def test_relay_rows_when_on(resonant_home: Path, tmp_path: Path) -> None:
    from resonant.dashboard.snapshot import RELAY_HEALTH_KV
    from resonant.secrets import SecretUnavailable

    conn = open_db(tmp_path / "r.db")
    rows = _relay_rows(conn, True, **RELAY_ON)
    assert rows["status relay enabled"] == ("PASS", False, "every 60s")
    assert rows["relay supabase_url set"][:2] == ("PASS", True)
    assert rows["relay anon_key set"] == ("PASS", True, "set (public key)")
    assert rows["relay secret relay/supabase_key"] == ("PASS", True, "present (value not read)")
    assert rows["_calls"][2] == "relay/supabase_key"
    assert rows["relay pushing"][:2] == ("FAIL", False)

    kv_set(conn, RELAY_HEALTH_KV, {"state": "ok", "since": "2026-10-16T12:00:00.000+00:00"})
    assert _relay_rows(conn, True, **RELAY_ON)["relay pushing"][:2] == ("PASS", False)
    kv_set(conn, RELAY_HEALTH_KV, {"state": "failing", "since": None, "detail": "HTTP 401"})
    assert _relay_rows(conn, True, **RELAY_ON)["relay pushing"] == (
        "FAIL",
        False,
        "failing: HTTP 401",
    )

    missing = _relay_rows(conn, False, **RELAY_ON)["relay secret relay/supabase_key"]
    assert missing == ("FAIL", True, "missing: resonant secret set relay/supabase_key")
    locked = SecretUnavailable("relay/supabase_key", "unavailable: keychain locked")
    assert _relay_rows(conn, locked, **RELAY_ON)["relay secret relay/supabase_key"] == (
        "FAIL",
        True,
        "unavailable: keychain locked",
    )
    conn.close()


def test_helper_shows_relay_rows_without_the_keychain(resonant_home: Path) -> None:
    # The relay is off by default, so the subprocess never asks the (real) Keychain.
    (resonant_home / "config.yaml").write_text(LIVE_CONFIG)
    rows = _helper(resonant_home)
    assert rows["status relay enabled"][:2] == ("FAIL", False)
    assert rows["relay secret relay/supabase_key"][0] == "SKIP"


def test_runner_rows_by_guard_mode(resonant_home: Path, tmp_path: Path) -> None:
    """GD-1: with the guard off the sandbox row skips, and with no Claude secret the owner's
    own login passes the Claude row; under ``thin`` the login doesn't count."""
    check = _load_helper()
    own = tmp_path / "resonant"
    (own / ".git").mkdir(parents=True)

    def nothing(_ref: str) -> bool:
        return False

    asked: list[str] = []

    def logged_in(settings: object) -> bool:
        asked.append("login")
        return True

    off = load_settings(ops={"repo_path": own})
    assert off.runner.guard == "off"
    sandbox = check.sandbox_row(off)
    assert sandbox[:3] == ("SKIP", False, "runner sandbox") and "guard is off" in sandbox[3]
    rows = {r[2]: r for r in check.runner_rows(off, 14, nothing, login=logged_in)}
    claude = rows["runner secret runner/claude_token (or runner/anthropic_key)"]
    assert claude[0] == "PASS" and "your own claude login works" in claude[3]
    assert rows["runner guard"][0] == "PASS" and rows["runner guard"][3].startswith("off:")
    assert (
        rows["runner repo resonant"][:2] == ("PASS", False)
        and str(own) in rows["runner repo resonant"][3]
    )

    def logged_out(settings: object) -> bool:
        return False

    rows = {r[2]: r for r in check.runner_rows(off, 14, nothing, login=logged_out)}
    claude = rows["runner secret runner/claude_token (or runner/anthropic_key)"]
    assert claude[0] == "FAIL" and "log in with `claude`" in claude[3]

    asked.clear()
    thin = load_settings(runner={"guard": "thin"}, ops={"repo_path": None})
    rows = {r[2]: r for r in check.runner_rows(thin, 14, nothing, login=logged_in)}
    claude = rows["runner secret runner/claude_token (or runner/anthropic_key)"]
    assert claude[0] == "FAIL" and "log in" not in claude[3] and asked == []
    assert rows["runner guard"][3].startswith("thin:")
    assert rows["runner repo resonant"][0] == "SKIP"


def test_runner_remote_control_row(resonant_home: Path) -> None:
    """RC-1: the setting, and the last session's answer the runner kept in kv."""
    check = _load_helper()
    on = load_settings()
    assert on.runner.remote_control == "on"  # the default
    row = check.remote_control_row(on)
    assert row[:3] == ("PASS", False, "runner remote control") and row[3].startswith("on:")
    ok = check.remote_control_row(on, {"state": "ok", "run": "R4", "at": "t", "detail": ""})
    assert ok[0] == "PASS" and "last answer ok (R4" in ok[3]
    bad = {"state": "unavailable", "run": "R5", "at": "t", "detail": "no answer in 15s"}
    row = check.remote_control_row(on, bad)
    assert row[0] == "FAIL" and "R5: no answer in 15s" in row[3]
    off = load_settings(runner={"remote_control": False})  # YAML's bare `off`
    assert off.runner.remote_control == "off"
    assert check.remote_control_row(off, bad)[0] == "SKIP"

    def nothing(_ref: str) -> bool:
        return False

    rows = {r[2]: r for r in check.runner_rows(off, 14, nothing)}
    assert rows["runner remote control"][0] == "SKIP"
