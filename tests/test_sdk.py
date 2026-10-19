from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from resonant.extensions.checks import (
    argv0_error,
    dir_errors,
    name_errors,
    resolve_argv0,
    resolve_bin,
)
from resonant_sdk import Effect, Event, Level, Manifest, load_manifest, new_id
from resonant_sdk.manifest import cron_min_interval_s


def test_ids_unique_and_time_sortable() -> None:
    ids = [new_id() for _ in range(1000)]
    assert len(set(ids)) == 1000
    assert all(len(i) == 26 for i in ids)
    # Same-ms ids can share a prefix; the timestamp part never goes backwards.
    assert [i[:10] for i in ids] == sorted(i[:10] for i in ids)


def test_event_defaults_and_strictness() -> None:
    e = Event(source="imessage", type="message")
    assert e.id and e.trace_id and e.payload == {}
    with pytest.raises(ValidationError):
        Event(source="x", type="y", bogus=1)  # pyright: ignore[reportCallIssue]


# --- Manifest additions (Phase 3) ------------------------------------------------------

REPO = Path(__file__).resolve().parents[1]
EXAMPLE = REPO / "extensions/example"


def manifest(**patch: Any) -> Manifest:
    data: dict[str, Any] = {
        "name": "demo",
        "version": 1,
        "tools": {"get": {"run": "./cli.py get", "effect": "read"}},
    }
    data.update(patch)
    return Manifest.model_validate(data)


def job(**patch: Any) -> dict[str, Any]:
    return {"id": "sync", "schedule": "0 * * * *", "run": "./cli.py sync", **patch}


def test_job_defaults_to_write_l2_and_becomes_a_spec() -> None:
    m = manifest(jobs=[job(id="nightly-sync", schedule="0 3 * * *")])
    (spec,) = m.job_specs()
    assert spec.name == "job_nightly_sync" and spec.description == "scheduled job"
    assert spec.effect is Effect.WRITE and spec.effective_level is Level.L2
    assert spec.args_schema == {
        "type": "object",
        "properties": {"due_at": {"type": "string"}},
        "required": ["due_at"],
        "additionalProperties": False,
    }
    assert spec.source == "ext:demo" and spec.extension == "demo"
    assert [s.name for s in m.tool_specs()] == ["get"]  # jobs never join the toolset
    assert m.jobs[0].catchup_s == 0 and m.jobs[0].timeout_s == 300


@pytest.mark.parametrize(
    ("schedule", "seconds"),
    [
        ("*/5 * * * *", 300),
        ("0 * * * *", 3600),
        ("0,30 * * * *", 1800),
        ("0 8 * * 1-5", 86400),
        ("50 7 * * *", 86400),
        ("0 8,20 * * *", 12 * 3600),
        ("55 23 * * *", 86400),
        ("0-10/5 9-17 * * *", 300),
        ("30/15 * * * *", 900),
        ("59 * * * *", 3600),
    ],
)
def test_cron_min_interval(schedule: str, seconds: int) -> None:
    assert cron_min_interval_s(schedule) == seconds


@pytest.mark.parametrize(
    "schedule", ["* * * * *", "*/4 * * * *", "0,2 * * * *", "61 * * * *", "5/ * * * *"]
)
def test_schedules_faster_than_5_minutes_or_invalid_refused(schedule: str) -> None:
    with pytest.raises(ValidationError, match=r"5 minutes|range|cron"):
        manifest(jobs=[job(schedule=schedule, effect="read")])


@pytest.mark.parametrize(
    ("patch", "ok"),
    [
        ({"effect": "write", "level": "L2", "schedule": "*/30 * * * *"}, False),
        ({"schedule": "*/30 * * * *"}, False),  # default write is L1: asks
        ({"effect": "write", "level": "L2", "schedule": "0 * * * *"}, True),
        ({"effect": "write", "level": "L3", "schedule": "*/10 * * * *"}, True),
        ({"effect": "read", "schedule": "*/5 * * * *"}, True),
    ],
)
def test_asking_jobs_at_most_hourly(patch: dict[str, Any], ok: bool) -> None:
    if ok:
        manifest(jobs=[job(**patch)])
    else:
        with pytest.raises(ValidationError, match="at most hourly"):
            manifest(jobs=[job(**patch)])


def test_job_catchup_timeout_and_secrets() -> None:
    m = manifest(secrets=["token"], jobs=[job(catchup="3h", timeout_s=1800, secrets=["token"])])
    assert m.jobs[0].catchup_s == 3 * 3600 and m.job_specs()[0].secrets == ("token",)
    for bad in ({"catchup": "25h"}, {"catchup": "soon"}, {"timeout_s": 1801}):
        with pytest.raises(ValidationError):
            manifest(jobs=[job(**bad)])
    with pytest.raises(ValidationError, match="not declared"):
        manifest(jobs=[job(secrets=["token"])])
    with pytest.raises(ValidationError, match="L0"):  # a read job without secrets is L0
        manifest(jobs=[job(effect="read", level="L3")])


def test_tool_timeout_cap_and_closed_args_schema() -> None:
    m = manifest(tools={"t": {"run": "./x {a}", "effect": "read", "args": {"a": "string"}}})
    assert m.tool_specs()[0].args_schema["additionalProperties"] is False
    manifest(tools={"t": {"run": "./x", "effect": "read", "timeout_s": 300}})
    with pytest.raises(ValidationError):
        manifest(tools={"t": {"run": "./x", "effect": "read", "timeout_s": 301}})


@pytest.mark.parametrize("name", ["API_KEY", "api-key", "1key", "k" * 33, "github/token"])
def test_secret_names_are_slugs(name: str) -> None:
    with pytest.raises(ValidationError):
        manifest(secrets=[name])
    with pytest.raises(ValidationError):
        manifest(secrets=[], tools={"t": {"run": "./x", "effect": "write", "secrets": [name]}})


def test_repos_are_optional_owner_name_slugs() -> None:
    assert manifest().repos == ()
    assert manifest(repos=["me/trading-bot", "Org_1/a.b-c"]).repos == (
        "me/trading-bot",
        "Org_1/a.b-c",
    )
    assert manifest(repos=[f"me/r{i}" for i in range(20)]).repos[-1] == "me/r19"


@pytest.mark.parametrize(
    "repos",
    [
        ["trading"],
        ["a/b/c"],
        ["../x"],
        ["me/.."],
        ["./x"],
        ["me/bad name"],
        ["me/b;rm"],
        ["me/\u00e9"],
        ["me/x\n"],
        [""],
        ["me/x", "ME/X"],
        [f"me/r{i}" for i in range(21)],
    ],
)
def test_bad_repos_refused(repos: list[str]) -> None:
    with pytest.raises(ValidationError):
        manifest(repos=repos)


def test_description_examples_and_names() -> None:
    m = manifest(description="d" * 120, examples=["what time is it", "e" * 80])
    assert m.description and len(m.examples) == 2
    for bad in (
        {"description": "d" * 121},
        {"examples": ["a", "b", "c", "d"]},
        {"examples": ["e" * 81]},
        {"examples": ["two\nlines"]},
        {"examples": ["  "]},
        {"examples": ["tab\there"]},
        {"description": "two\nlines"},
        {"name": "core"},
    ):
        with pytest.raises(ValidationError):
            manifest(**bad)
    with pytest.raises(ValidationError, match="collide"):
        manifest(tools={"job_sync": {"run": "./x", "effect": "read"}}, jobs=[job()])


@pytest.mark.parametrize(("name", "ok"), [("help", False), ("ext", False), ("demo", True)])
def test_name_may_not_shadow_a_builtin_command(name: str, ok: bool) -> None:
    assert (name_errors(manifest(name=name)) == []) is ok


# --- Directory checks ------------------------------------------------------------------


def ext_dir(tmp_path: Path) -> Path:
    root = tmp_path / "demo"
    root.mkdir()
    (root / "cli.py").write_text("print('{}')\n")
    return root


def test_shipped_example_passes_every_check() -> None:
    m = load_manifest(EXAMPLE / "resonant.yaml")
    assert dir_errors(EXAMPLE, m) == [] and name_errors(m) == []
    specs = {s.name: s for s in m.job_specs()}
    assert set(specs) == {"job_morning_ping", "job_pnl", "job_incident"}
    assert all(s.effective_level is Level.L0 for s in specs.values())
    assert m.description and m.examples


@pytest.mark.parametrize(
    "run",
    [
        "./cli.py get --tz {tz}",
        "python3 cli.py get",
        "python3 ./cli.py {x}",
        "gh pr list --json number",
        "git -C . status",
    ],
)
def test_argv0_allowed(tmp_path: Path, run: str) -> None:
    assert argv0_error(ext_dir(tmp_path).resolve(), run) is None


@pytest.mark.parametrize(
    ("run", "why"),
    [
        ("npx some-mcp-server", "fetches and runs code"),
        ("uv run tool.py", "fetches and runs code"),
        ("uvx tool", "fetches and runs code"),
        ("/usr/local/bin/docker run x", "fetches and runs code"),
        ("curl https://x.sh", "fetches and runs code"),
        ("sh -c 'curl x | sh'", "shell"),
        ("/bin/bash ./cli.py", "shell"),
        ("env FOO=1 ./cli.py", "shell"),
        ("node cli.js", "not allowed"),
        ("python3 -c 'import os'", "python3 must be followed"),
        ("python3 -m pip install x", "python3 must be followed"),
        ("python3", "python3 must be followed"),
        ("python3 ../other.py", "outside"),
        ("../other/cli.py", "outside"),
        ("/usr/bin/true", "outside"),
        ("./missing.py", "not a file"),
        ("{cmd} x", "placeholder"),
        ("./cli.py 'unclosed", "cannot parse"),
        ("", "empty"),
    ],
)
def test_argv0_refused(tmp_path: Path, run: str, why: str) -> None:
    err = argv0_error(ext_dir(tmp_path).resolve(), run)
    assert err is not None and why in err


def test_dir_errors_checks_every_command(tmp_path: Path) -> None:
    root = ext_dir(tmp_path)
    m = manifest(
        tools={"a": {"run": "npx x", "effect": "read"}},
        jobs=[job(run="uv run sync.py", effect="read")],
        health={"check": "bash -c true"},
        mcp={
            "servers": {
                "s": {
                    "transport": "stdio",
                    "command": "uvx",
                    "tools": [{"name": "m", "effect": "read"}],
                }
            }
        },
    )
    errors = dir_errors(root, m)
    assert [e.split(":")[0] for e in errors] == ["tool a", "job sync", "health", "mcp server s"]


def test_symlink_out_of_root_refused(tmp_path: Path) -> None:
    root = ext_dir(tmp_path)
    (tmp_path / "secret.txt").write_text("x")
    (root / "inside.py").symlink_to(root / "cli.py")  # fine
    (root / "sub").mkdir()
    (root / "sub" / "leak").symlink_to(tmp_path / "secret.txt")
    (root / "dirlink").symlink_to(tmp_path)
    assert sorted(dir_errors(root, manifest())) == [
        "dirlink: symlink resolves outside the extension dir",
        "sub/leak: symlink resolves outside the extension dir",
    ]
    # A run through an in-tree symlink that points outside is refused too.
    (root / "run.py").symlink_to(tmp_path / "secret.txt")
    assert "outside" in (argv0_error(root.resolve(), "./run.py") or "")


@pytest.mark.parametrize(
    ("name", "text"),
    [
        ("pyproject.toml", '[project]\nname = "x"\ndependencies = ["requests"]\n'),
        ("pyproject.toml", '[project.optional-dependencies]\ndev = ["pytest"]\n'),
        ("pyproject.toml", '[dependency-groups]\ndev = ["pytest"]\n'),
        ("pyproject.toml", "[tool.poetry.dependencies]\npython = '^3.12'\n"),
        ("pyproject.toml", "not = [toml"),
        ("requirements.txt", "requests\n"),
        ("requirements-dev.txt", "pytest\n"),
        ("uv.lock", ""),
        ("poetry.lock", ""),
        ("pyproject.toml", '[project]\nname = "x"\ndynamic = ["dependencies"]\n'),
        ("package.json", "{}"),
        ("setup.py", ""),
    ],
)
def test_dependency_files_refused(tmp_path: Path, name: str, text: str) -> None:
    root = ext_dir(tmp_path)
    (root / "nested").mkdir()
    (root / "nested" / name).write_text(text)
    (err,) = dir_errors(root, manifest())
    assert err.startswith(f"nested/{name}: extensions are stdlib only")


def test_pyproject_without_dependencies_is_fine(tmp_path: Path) -> None:
    root = ext_dir(tmp_path)
    (root / "pyproject.toml").write_text('[project]\nname = "x"\ndependencies = []\n')
    assert dir_errors(root, manifest()) == []


def test_resolve_argv0_pins_absolute_paths(tmp_path: Path, brew_gh: Path) -> None:
    root = ext_dir(tmp_path)
    assert resolve_argv0(root, "./cli.py get") == str((root / "cli.py").resolve())
    assert resolve_argv0(root, "python3 cli.py") == sys.executable
    keg_gh = os.path.realpath(brew_gh / "gh")  # the keg's file, not the link
    assert resolve_argv0(root, "gh pr list", path=str(brew_gh)) == keg_gh
    with pytest.raises(ValueError, match="not found"):
        resolve_argv0(root, "git status", path=str(brew_gh))
    with pytest.raises(ValueError, match="fetches"):
        resolve_argv0(root, "npx x", path=str(brew_gh))


def _loose(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n")
    path.chmod(0o755)
    return path


def test_resolve_bin_refuses_a_gh_workers_could_replace(
    tmp_path: Path, brew_gh: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only a guarded Homebrew keg or a root-owned file is pinned: a loose file in an install
    dir, a link into a keg elsewhere, or a gh outside the install dirs is refused."""
    assert resolve_bin("gh", str(brew_gh)) == os.path.realpath(brew_gh / "gh")
    elsewhere = _loose(tmp_path / "evil" / "Cellar" / "gh" / "9.9" / "bin" / "gh")
    (brew_gh / "gh").unlink()
    (brew_gh / "gh").symlink_to(elsewhere)  # a keg, but not this prefix's
    with pytest.raises(ValueError, match="workers can write"):
        resolve_bin("gh", str(brew_gh))
    (brew_gh / "gh").unlink()
    _loose(brew_gh / "gh")  # a loose file next to others a worker may write
    with pytest.raises(ValueError, match="workers can write"):
        resolve_bin("gh", str(brew_gh))
    other = _loose(tmp_path / "other" / "gh")  # not an install dir
    with pytest.raises(ValueError, match="workers can write"):
        resolve_bin("gh", str(other.parent))
    with pytest.raises(ValueError, match="can't be pinned"):
        resolve_bin("python3", str(brew_gh))
    # A root-owned file (no writable dir above it) is fine wherever it is.
    from resonant.extensions import checks

    def root_owned(path: Path) -> bool:
        return path == other.resolve()

    monkeypatch.setattr(checks, "_unwritable", root_owned)
    assert resolve_bin("gh", str(other.parent)) == str(other.resolve())


def test_resolve_bin_accepts_the_system_git() -> None:
    if not os.path.isfile("/usr/bin/git") or os.access("/usr/bin", os.W_OK):
        pytest.skip("no root-owned /usr/bin/git here")
    assert resolve_bin("git", "/usr/bin") == os.path.realpath("/usr/bin/git")


def test_bins_declare_only_pinnable_host_binaries() -> None:
    base = {"name": "x", "version": 1}
    assert Manifest.model_validate({**base, "bins": ["gh"]}).bins == ("gh",)
    for bad in (["curl"], ["gh", "gh"], ["python3"]):
        with pytest.raises(ValidationError):
            Manifest.model_validate({**base, "bins": bad})


def test_emit_writes_one_event_line_to_the_events_fd(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from resonant_sdk.emit import emit

    monkeypatch.delenv("RESONANT_EVENTS_FD", raising=False)
    assert emit("ping", "hi", "k1") is False  # not under Resonant
    r, w = os.pipe()
    try:
        monkeypatch.setenv("RESONANT_EVENTS_FD", str(w))
        assert emit("ci_failed", "red", "pr#1:run:2", {"n": 1}) is True
        line = os.read(r, 4096).decode()
    finally:
        os.close(r)
        os.close(w)
    assert line == '{"type":"ci_failed","text":"red","dedupe_key":"pr#1:run:2","data":{"n":1}}\n'
    with pytest.raises(ValueError, match="dedupe_key"):
        emit("x", "y", "")


# --- Pulse reports (Phase 3.5) ---------------------------------------------------------


def test_report_grant_is_optional_and_strict() -> None:
    assert manifest().report is None  # existing manifests are unchanged
    assert manifest(report={"max_urgency": "urgent"}).report is not None
    for bad in ({}, {"max_urgency": "loud"}, {"max_urgency": "normal", "extra": 1}):
        with pytest.raises(ValidationError):
            manifest(report=bad)


def test_item_is_a_reserved_event_in_notify_on() -> None:
    assert manifest(notify={"on": ["ci_failed"]}).notify is not None
    with pytest.raises(ValidationError, match="reserved"):
        manifest(notify={"on": ["ci_failed", "item"]})
    with pytest.raises(ValidationError, match="reserved"):
        manifest(notify={"on": ["item"]}, report={"max_urgency": "normal"})


def _captured_events(monkeypatch: pytest.MonkeyPatch, calls: Any) -> list[dict[str, Any]]:
    import json
    import os

    r, w = os.pipe()
    try:
        monkeypatch.setenv("RESONANT_EVENTS_FD", str(w))
        calls()
        os.close(w)
        w = -1
        raw = os.read(r, 65536).decode()
    finally:
        os.close(r)
        if w >= 0:
            os.close(w)
    return [json.loads(line) for line in raw.splitlines()]


def test_report_writes_an_item_event_with_a_unique_dedupe_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from resonant_sdk.emit import MAX_DEDUPE_KEY, report

    def go() -> None:
        assert report("dori/incident/inc-412", "Dori failing", urgency="urgent", state={"s": 1})
        assert report("dori/incident/inc-412", "Dori failing", urgency="urgent", state={"s": 1})
        assert report("k" * 120, "t", clear=True, ttl="30m")
        assert report("github/summary", "0 open PRs", urgency="low", quiet=True)

    first, second, third, fourth = _captured_events(monkeypatch, go)
    assert fourth["data"]["quiet"] is True and fourth["data"]["clear"] is False
    assert first["type"] == "item" and first["text"] == "Dori failing"
    assert first["data"] == {
        "key": "dori/incident/inc-412",
        "urgency": "urgent",
        "state": {"s": 1},
        "clear": False,
        "ttl": "24h",
        "quiet": False,
    }
    assert first["dedupe_key"].startswith("item:dori/incident/inc-412:")
    assert first["dedupe_key"] != second["dedupe_key"]  # a repeat is never dropped at ingest
    assert len(third["dedupe_key"]) <= MAX_DEDUPE_KEY
    assert third["data"]["clear"] is True and third["data"]["state"] is None
    assert third["data"]["urgency"] == "normal" and third["data"]["ttl"] == "30m"


def test_report_without_the_events_fd_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    from resonant_sdk.emit import report

    monkeypatch.delenv("RESONANT_EVENTS_FD", raising=False)
    assert report("a", "b") is False


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"key": ""}, "key"),
        ({"key": "Upper"}, "key"),
        ({"key": "x" * 121}, "key"),
        ({"key": "has space"}, "key"),
        ({"urgency": "loud"}, "urgency"),
        ({"key": "a\n"}, "key"),
        ({"ttl": "24h\n"}, "ttl"),
        ({"ttl": "30s"}, "ttl"),
        ({"ttl": "7d"}, "ttl"),
        ({"state": {"x": object()}}, "state"),
        ({"state": {"x": "y" * 3000}}, "state"),
        ({"ttl": "169h"}, "ttl"),
        ({"ttl": "0h"}, "ttl"),
        ({"ttl": "soon"}, "ttl"),
    ],
)
def test_report_validates_locally(
    monkeypatch: pytest.MonkeyPatch, kwargs: dict[str, Any], match: str
) -> None:
    from resonant_sdk.emit import report

    monkeypatch.delenv("RESONANT_EVENTS_FD", raising=False)
    args: dict[str, Any] = {"key": "ok", "title": "t", **kwargs}
    with pytest.raises(ValueError, match=match):
        report(**args)


def test_emit_module_imports_only_the_stdlib() -> None:
    import ast

    import resonant_sdk.emit as mod

    tree = ast.parse(Path(mod.__file__ or "").read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names)


def test_manifest_patterns_reject_a_trailing_newline() -> None:
    from resonant_sdk.manifest import duration_s

    with pytest.raises(ValueError):
        duration_s("5m\n")
    with pytest.raises(ValidationError):
        manifest(notify={"on": ["ci_failed\n"]})


# --- secret() (P5A-7) ---------------------------------------------------------------------


def _run_secret_snippet(code: str, payload: bytes | None) -> subprocess.CompletedProcess[str]:
    """Run ``code`` as ``python -I -S`` with secret.py importable and payload on a pipe."""
    import resonant_sdk.secret as mod

    env = {"PATH": "/usr/bin:/bin"}
    pass_fds: tuple[int, ...] = ()
    r = -1
    if payload is not None:
        r, w = os.pipe()
        os.write(w, payload)
        os.close(w)
        env["RESONANT_SECRETS_FD"] = str(r)
        pass_fds = (r,)
    try:
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-S",
                "-c",
                f"import sys; sys.path.insert(0, {str(Path(mod.__file__ or '').parent)!r})\n{code}",
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
            pass_fds=pass_fds,
        )
    finally:
        if r >= 0:
            os.close(r)


def test_secret_reads_the_pipe_once_and_caches() -> None:
    proc = _run_secret_snippet(
        "from secret import secret\n"
        "import os\n"
        "print(secret('token'), secret('token'), secret('api_key'))\n"
        "print('RESONANT_SECRETS_FD' in os.environ)",
        b'{"token": "tok-1", "api_key": "key-2"}',
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.split("\n")[:2] == ["tok-1 tok-1 key-2", "False"]


def test_secret_missing_name_errors_cleanly() -> None:
    proc = _run_secret_snippet(
        "from secret import secret, SecretError\n"
        "try:\n    secret('nope')\nexcept SecretError as e:\n    print('ERR', e)",
        b'{"token": "tok-1"}',
    )
    assert "ERR secret 'nope' is missing" in proc.stdout and "tok-1" not in proc.stdout


@pytest.mark.parametrize("payload", [None, b"", b"not json", b'["a"]', b'{"a": 1}'])
def test_secret_errors_when_nothing_usable_was_delivered(payload: bytes | None) -> None:
    proc = _run_secret_snippet(
        "from secret import secret, SecretError\n"
        "try:\n    secret('token')\nexcept SecretError as e:\n    print('ERR', e)",
        payload,
    )
    assert proc.returncode == 0 and proc.stdout.startswith("ERR ")


def test_secret_module_imports_only_the_stdlib() -> None:
    import ast

    import resonant_sdk.secret as mod

    tree = ast.parse(Path(mod.__file__ or "").read_text())
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            roots.add(node.module.split(".")[0])
    assert roots <= set(sys.stdlib_module_names)


def test_second_secret_call_after_a_failed_read_repeats_the_reason() -> None:
    proc = _run_secret_snippet(
        "from secret import secret, SecretError\n"
        "for _ in range(2):\n"
        "    try:\n        secret('t')\n    except SecretError as e:\n        print('ERR', e)",
        b"not json",
    )
    assert proc.stdout.splitlines() == ["ERR secrets could not be read"] * 2
