"""scripts/check_names.py: no known real names in tracked files, and the guard itself works."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_names.py"
SALT = "test-salt"


def _run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    full = {k: v for k, v in os.environ.items() if k != "NAME_DENYLIST"} | (env or {})
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args], capture_output=True, text=True, env=full
    )


def _repo(tmp_path: Path, files: dict[str, str], denied: list[str]) -> tuple[Path, Path]:
    repo = tmp_path / "repo"
    repo.mkdir()
    for rel, text in files.items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    hashes = [hashlib.sha256(f"{SALT}:{n}".encode()).hexdigest() for n in denied]
    deny = tmp_path / "deny.json"
    deny.write_text(json.dumps({"salt": SALT, "sha256": hashes}))
    return repo, deny


def test_repo_has_no_denied_names() -> None:
    r = _run()
    assert r.returncode == 0, r.stdout + r.stderr
    assert "names: ok" in r.stdout


def test_planted_name_fails_and_is_masked(tmp_path: Path) -> None:
    repo, deny = _repo(
        tmp_path,
        {
            "tests/test_x.py": 'OWNER = "alice"\nAPPROVER = "Zorblax"  # planted\n',
            "ok.md": "nothing here\n",
        },
        ["zorblax"],
    )
    r = _run("--root", str(repo), "--denylist", str(deny))
    assert r.returncode == 1
    assert "tests/test_x.py:2: denied name z******" in r.stdout
    assert "zorblax" not in (r.stdout + r.stderr).lower()  # never printed in full


def test_matches_whole_tokens_and_camel_case_parts(tmp_path: Path) -> None:
    repo, deny = _repo(
        tmp_path,
        {
            "a.py": "zorblaxes = zorblax2x = 1\n",  # longer runs: not the name
            "b.py": "ZORBLAX_ID = zorblaxId = 1\n",  # constant and camelCase parts
        },
        ["zorblax"],
    )
    r = _run("--root", str(repo), "--denylist", str(deny))
    assert r.returncode == 1
    assert [ln for ln in r.stdout.splitlines() if "denied" in ln] == [
        "a.py:1: denied name z******",  # zorblax2x splits at the digit
        "b.py:1: denied name z******",
    ]


def test_env_names_and_skipped_files(tmp_path: Path) -> None:
    repo, deny = _repo(
        tmp_path,
        {"a.txt": "hello quux\n", "uv.lock": "quux\n", "bin.dat": "quux\0\n"},
        [],
    )
    clean = _run("--root", str(repo), "--denylist", str(deny))
    assert clean.returncode == 0
    assert "NAME_DENYLIST is not set" in clean.stdout
    r = _run("--root", str(repo), "--denylist", str(deny), env={"NAME_DENYLIST": "foo, Quux"})
    assert r.returncode == 1
    assert [ln for ln in r.stdout.splitlines() if "denied" in ln] == ["a.txt:1: denied name q***"]


def test_hash_prints_the_line_to_add(tmp_path: Path) -> None:
    _, deny = _repo(tmp_path, {"a.txt": "x\n"}, [])
    r = _run("--denylist", str(deny), "--hash", "Zorblax")
    assert r.stdout.strip() == hashlib.sha256(f"{SALT}:zorblax".encode()).hexdigest()
