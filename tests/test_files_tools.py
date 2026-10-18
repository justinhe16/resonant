"""BR-2: the read tools (``tools/files.py``): scope (escapes, symlinks, deny list), caps,
binary refusal, redaction, search (Python walk and a fake ripgrep), tail_log,
read_transcript, the spec flags and member grants."""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from resonant.api import DaemonState
from resonant.components import build_components
from resonant.config import DEFAULT_READ_DENY, load_settings
from resonant.executor import ToolFailed
from resonant.gate import Allow, Deny, Intent, PolicyGate
from resonant.loop import Loop, NewTask, SlotPool
from resonant.principals import Principals
from resonant.store import tasks as task_repo
from resonant.store.db import kv_set, utcnow
from resonant.tools import ToolRegistry, files
from resonant.tools.files import ReadScope, redact_secrets
from resonant_sdk import Effect, Level, ToolSpec

TOKEN = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
BODY = "b3BlbnNzaC1y" + "ZXktdjEAAAAA"  # split for the secret scanner
PEM = f"-----BEGIN OPENSSH PRIVATE KEY-----\n{BODY}\n-----END OPENSSH PRIVATE KEY-----"


def write(path: Path, text: str | bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    return path


@pytest.fixture
def root(tmp_path: Path) -> Path:
    r = tmp_path / "code"
    write(r / "app" / "main.py", "".join(f"line {i}\n" for i in range(1, 31)))
    write(r / "app" / "README.md", "hello mycelium\n")
    write(r / ".env", f"GITHUB_TOKEN={TOKEN}\n")
    return r.resolve()


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    return write(tmp_path / "outside" / "secret.txt", "mycelium outside\n").resolve()


def scope_for(*roots: Path, deny: tuple[str, ...] = (), always: tuple[Path, ...] = ()) -> ReadScope:
    return ReadScope.build(list(roots), [*DEFAULT_READ_DENY, *deny], always)


def read(scope: ReadScope, path: Path | str, **args: Any) -> dict[str, Any]:
    out = files.read_file(scope, {"path": str(path), **args})
    json.dumps(out, allow_nan=False)
    return out


# --- scope ---------------------------------------------------------------------------------


def test_read_file_lines_offset_limit(root: Path) -> None:
    scope = scope_for(root)
    out = read(scope, root / "app" / "main.py", offset=3, limit=2)
    assert out["content"] == "3: line 3\n4: line 4"
    assert (out["start"], out["end"], out["more"], out["untrusted"]) == (3, 4, True, True)
    whole = read(scope, root / "app" / "main.py")
    assert whole["end"] == 30 and whole["more"] is False
    past = read(scope, root / "app" / "main.py", offset=99)
    assert past["content"] == "" and past["note"] == "the file has 30 lines"


def test_home_relative_paths(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(root.parent))
    assert read(scope_for(root), "~/code/app/README.md")["content"] == "1: hello mycelium"


@pytest.mark.parametrize(
    ("path", "why"),
    [
        ("app/main.py", "absolute"),
        ("", "1 to"),
        ("/etc/hosts", "outside the read roots"),
    ],
)
def test_bad_or_outside_paths(root: Path, path: str, why: str) -> None:
    with pytest.raises(ToolFailed, match=why):
        read(scope_for(root), path)


def test_dotdot_refused_even_when_it_lands_inside(root: Path) -> None:
    with pytest.raises(ToolFailed, match=r"'\.\.'"):
        read(scope_for(root), f"{root}/app/../app/main.py")


def test_missing_file_and_directory(root: Path) -> None:
    with pytest.raises(ToolFailed, match="doesn't exist"):
        read(scope_for(root), root / "nope.txt")
    with pytest.raises(ToolFailed, match="is a directory"):
        read(scope_for(root), root / "app")


def test_fifo_refused_without_blocking(root: Path) -> None:
    fifo = root / "pipe"
    os.mkfifo(fifo)
    with pytest.raises(ToolFailed, match="regular file"):
        read(scope_for(root), fifo)


def test_symlink_out_of_root_refused(root: Path, outside: Path) -> None:
    (root / "link.txt").symlink_to(outside)
    (root / "linkdir").symlink_to(outside.parent)
    scope = scope_for(root)
    with pytest.raises(ToolFailed, match="symlink out of the read roots"):
        read(scope, root / "link.txt")
    with pytest.raises(ToolFailed, match="symlink out of the read roots"):
        read(scope, root / "linkdir" / "secret.txt")


def test_symlink_inside_root_followed_but_not_to_denied(root: Path) -> None:
    (root / "alias.md").symlink_to(root / "app" / "README.md")
    (root / "notes.txt").symlink_to(root / ".env")
    scope = scope_for(root)
    assert read(scope, root / "alias.md")["path"] == str(root / "app" / "README.md")
    with pytest.raises(ToolFailed, match="leads to a path on the deny list"):
        read(scope, root / "notes.txt")


@pytest.mark.parametrize(
    "name",
    [
        ".env",
        ".env.local",
        "prod.env",
        ".ENV",
        "server.pem",
        "Server.PEM",
        "tls.key",
        "id_ed25519",
        "credentials",
        "credentials.json",
        "app_credentials.json",
        ".netrc",
        ".git/config",
        "deep/.git/config",
        "vendored/.ssh/id_x.txt",
        "infra/prod.tfstate",
        ".zsh_history",
    ],
)
def test_deny_list_names(root: Path, name: str) -> None:
    write(root / name, "x\n")
    with pytest.raises(ToolFailed, match="deny list"):
        read(scope_for(root), root / name)


def test_deny_paths_configured_and_always(root: Path) -> None:
    write(root / "private" / "a.txt", "x\n")
    write(root / "resonant.db", "x\n")
    scope = scope_for(root, deny=(str(root / "private"),), always=(root / "resonant.db",))
    for p in (root / "private" / "a.txt", root / "PRIVATE" / "a.txt", root / "resonant.db"):
        with pytest.raises(ToolFailed, match="deny list"):
            read(scope, p)


def test_default_scope_from_settings(tmp_path: Path) -> None:
    settings = load_settings(
        runner={
            "code_dir": str(tmp_path / "c"),
            "work_dir": str(tmp_path / "runs"),
            "mirror_dir": str(tmp_path / "state" / "runner" / "mirrors"),
            "secrets_dir": str(tmp_path / "state" / "run-secrets"),
            "cache_dir": str(tmp_path / "runs" / ".cache"),
        }
    )
    scope = ReadScope.from_settings(settings)
    home = Path.home()
    for denied in (
        home / ".ssh" / "id_rsa",
        home / "Library" / "Keychains" / "login.keychain-db",
        home / "Library" / "Messages" / "chat.db",
        home / "Documents" / "taxes.txt",
        home / ".aws" / "credentials",
        settings.db_path,
        Path(str(settings.db_path) + "-wal"),
        settings.ext_data_dir / "github" / "token",
        settings.runner.secrets_dir / "R1",
        settings.runner.work_dir / "R1" / "creds" / "token",
        settings.principals_path,
    ):
        assert scope.denied(denied), denied
    assert scope.inside(Path(os.path.realpath(tmp_path / "c")) / "dori" / "app.py")
    assert scope.inside(Path(os.path.realpath(settings.logs_dir)) / "resonant.jsonl")
    assert scope.inside(Path(os.path.realpath(tmp_path / "state" / "runner" / "transcripts")))
    assert not scope.inside(Path(os.path.realpath(settings.home)) / "config.yaml")


def test_configured_roots_replace_defaults(tmp_path: Path, root: Path) -> None:
    settings = load_settings(brain={"read_roots": [str(root)], "read_deny": ["*.secret"]})
    scope = ReadScope.from_settings(settings)
    assert scope.roots == (root,)
    write(root / "x.secret", "x\n")
    with pytest.raises(ToolFailed, match="deny list"):
        read(scope, root / "x.secret")
    with pytest.raises(ToolFailed, match="deny list"):
        read(scope, root / ".env")  # the defaults still apply


@pytest.mark.parametrize(
    "brain",
    [{"read_roots": ["code"]}, {"read_deny": [""]}, {"read_deny": [" x"]}, {"rg_path": "rg"}],
)
def test_brain_config_validation(brain: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        load_settings(brain=brain)


# --- content -------------------------------------------------------------------------------


def test_binary_refused(root: Path) -> None:
    write(root / "img.png", b"\x89PNG\r\n\x1a\n\0\0\0")
    write(root / "latin1.txt", "caf\xe9 au lait\n".encode("latin-1") * 10)
    scope = scope_for(root)
    for name in ("img.png", "latin1.txt"):
        with pytest.raises(ToolFailed, match="binary"):
            read(scope, root / name)


def test_utf8_cut_at_the_sniff_boundary_is_text(root: Path) -> None:
    text = "\n" * (files.SNIFF_BYTES - 1) + "é\n"  # the 2-byte é straddles the boundary
    write(root / "u.txt", text)
    out = read(scope_for(root), root / "u.txt", offset=files.SNIFF_BYTES)
    assert out["content"] == f"{files.SNIFF_BYTES}: é"


def test_caps(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    write(root / "long.txt", "x" * 5000 + "\nshort\n")
    scope = scope_for(root)
    first = read(scope, root / "long.txt")["content"].split("\n")[0]
    assert first == "1: " + "x" * (files.LINE_MAX - 1) + "…"
    assert read(scope, root / "long.txt", offset=2)["content"] == "2: short"
    write(root / "many.txt", "".join(f"{i}\n" for i in range(2000)))
    capped = read(scope, root / "many.txt", limit=10_000)
    assert capped["end"] == files.READ_LINES_MAX and capped["more"] is True
    monkeypatch.setattr(files, "CONTENT_MAX", 40)
    small = read(scope, root / "many.txt")
    assert len(small["content"]) <= 40 and small["more"] is True
    assert "read on with offset" in small["note"]


@pytest.mark.parametrize(
    "args",
    [{"limit": 0}, {"offset": 0}, {"limit": "5"}, {"offset": True}, {"x": 1}],
)
def test_read_file_bad_args(root: Path, args: dict[str, Any]) -> None:
    with pytest.raises(ToolFailed):
        read(scope_for(root), root / "app" / "main.py", **args)


def test_redaction_in_file_contents(root: Path) -> None:
    body = "\n".join(
        [
            f"token: {TOKEN}",
            PEM,
            # Split so the secret scanner in CI doesn't flag the fixtures themselves.
            "PASSWORD = " + '"hunter2hunter2"',
            "api_key" + "=abc123def456ghi",
            "url = https://bob:" + "s3cret@example.com/x",
            "Authorization: Bearer " + "abcdefghijklmnop1234",
            "jwt "
            + ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0", "dozjgNryP4J3jV"]),
            "token = get_token()",
            "max_tokens = 1000000",
            "tokenizer = 'bert-base-uncased'",
        ]
    )
    write(root / "conf.py", body)
    content = read(scope_for(root), root / "conf.py")["content"]
    for leaked in (TOKEN, BODY, "hunter2", "abc123def456", "s3cret"):
        assert leaked not in content
    assert "abcdefghijklmnop1234" not in content and "dozjgNryP4J3" not in content
    for kept in ("token = get_token()", "max_tokens = 1000000", "tokenizer = 'bert-base-uncased'"):
        assert kept in content
    assert "[redacted]" in content
    assert "4: [redacted]" in content and "5: PASSWORD" in content  # line numbers kept


@pytest.mark.parametrize(
    "line",
    [
        "password" + "=hunter2",
        "password: " + "correcthorsebattery",
        "{token: " + "abc123def456}",
        "API_KEY=" + "abcdef123456",
    ],
)
def test_redaction_bare_values(line: str) -> None:
    assert redact_secrets(line).count("[redacted]") == 1


def test_secret_split_by_the_line_clip_is_still_redacted(root: Path) -> None:
    write(root / "c.txt", "x" * (files.LINE_MAX - 10) + " " + TOKEN + "\n")
    content = read(scope_for(root), root / "c.txt")["content"]
    assert TOKEN[:12] not in content and content.endswith("…")


async def test_long_lines_stay_fast(root: Path, tmp_path: Path) -> None:
    import time

    evil = "a." * 100_000 + " mycelium"
    write(root / "evil.txt", evil + "\n")
    write(tmp_path / "logs" / "x.log", evil + "\n")
    write(tmp_path / "logs" / "resonant.jsonl", json.dumps({"event": evil}) + "\n")
    events = [{"type": "assistant", "message": {"content": [{"type": "text", "text": evil}]}}]
    write(tmp_path / "t" / "R1.jsonl", json.dumps(events[0]) + "\n")
    start = time.monotonic()
    read(scope_for(root), root / "evil.txt")
    await files.search(scope_for(root), None, {"pattern": "a.a."})
    files.tail_log(tmp_path / "logs", {"name": "x.log"})
    files.tail_log(tmp_path / "logs", {})
    files.read_transcript(tmp_path / "t", {"code": "R1"})
    assert redact_secrets("a." * 5000) == "a." * 5000
    assert time.monotonic() - start < 2.0


async def test_search_hit_inside_a_key_block(root: Path) -> None:
    body = "x" * 40
    write(
        root / "notes.md",
        f"-----BEGIN RSA PRIVATE KEY-----\n{body}\n-----END RSA PRIVATE KEY-----\n",
    )
    out = await files.search(scope_for(root), None, {"pattern": "xxxx"})
    assert out["hits"] == [{"path": str(root / "notes.md"), "line": 2, "text": "[redacted]"}]


def test_redact_secrets_is_idempotent() -> None:
    once = redact_secrets(f"x {TOKEN} password" + "='abcdefgh1'")
    assert redact_secrets(once) == once


# --- list_dir ------------------------------------------------------------------------------


def test_list_dir(root: Path, outside: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (root / "app" / "out").symlink_to(outside.parent)
    write(root / "app" / "server.pem", "x")
    scope = scope_for(root)
    out = files.list_dir(scope, {"path": str(root / "app")})
    assert out["entries"] == [
        {"name": "README.md", "type": "file", "size": 15},
        {"name": "main.py", "type": "file", "size": out["entries"][1]["size"]},
        {"name": "out", "type": "link"},
    ]
    assert out["count"] == 3 and out["truncated"] is False and out["untrusted"] is True
    top = files.list_dir(scope, {"path": str(root)})
    assert [e["name"] for e in top["entries"]] == ["app"]  # .env left out
    monkeypatch.setattr(files, "LIST_MAX", 1)
    cut = files.list_dir(scope, {"path": str(root / "app")})
    assert len(cut["entries"]) == 1 and cut["count"] == 3 and cut["truncated"] is True
    with pytest.raises(ToolFailed, match="isn't a directory"):
        files.list_dir(scope, {"path": str(root / "app" / "main.py")})
    with pytest.raises(ToolFailed, match="symlink out"):
        files.list_dir(scope, {"path": str(root / "app" / "out")})


# --- search --------------------------------------------------------------------------------


async def test_search_python_walk(root: Path, outside: Path) -> None:
    write(root / "app" / "notes.txt", f"mycelium (a.b) {TOKEN}\n")
    write(root / "app" / "server.pem", "mycelium\n")
    write(root / "app" / "blob.bin", b"mycelium\0\0")
    write(root / ".hidden" / "x.txt", "mycelium\n")
    write(root / "node_modules" / "x.txt", "mycelium\n")
    (root / "app" / "out.txt").symlink_to(outside)
    scope = scope_for(root)
    out = await files.search(scope, None, {"pattern": "mycelium"})
    assert out["engine"] == "python" and out["untrusted"] is True
    assert {(Path(h["path"]).name, h["line"]) for h in out["hits"]} == {
        ("README.md", 1),
        ("notes.txt", 1),
    }
    hit = next(h for h in out["hits"] if h["path"].endswith("notes.txt"))
    assert TOKEN not in hit["text"] and "[redacted]" in hit["text"]
    # literal by default; regex on request; smart case
    assert (await files.search(scope, None, {"pattern": "(a.b)"}))["count"] == 1
    assert (await files.search(scope, None, {"pattern": "Mycelium"}))["count"] == 0
    # glob, path to a file, limit
    assert (await files.search(scope, None, {"pattern": "mycelium", "glob": "*.md"}))["count"] == 1
    one = await files.search(
        scope, None, {"pattern": "line", "path": str(root / "app" / "main.py")}
    )
    assert one["count"] == 20 and one["more"] is True
    with pytest.raises(ToolFailed, match="regex needs ripgrep"):
        await files.search(scope, None, {"pattern": "(a+)+$", "regex": True})
    with pytest.raises(ToolFailed, match="deny list"):
        await files.search(scope, None, {"pattern": "x", "path": str(root / ".env")})


async def test_search_nested_roots_searched_once(root: Path) -> None:
    scope = scope_for(root, root / "app")
    out = await files.search(scope, None, {"pattern": "mycelium"})
    assert out["count"] == 1 and out["roots"] == [str(root)]


async def test_search_walk_budget(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(files, "WALK_MAX_FILES", 0)
    out = await files.search(scope_for(root), None, {"pattern": "mycelium"})
    assert out["partial"] == "the search budget ran out"


@pytest.mark.parametrize(
    "args",
    [
        {},
        {"pattern": ""},
        {"pattern": "x" * 201},
        {"pattern": "x", "regex": "yes"},
        {"pattern": "x", "y": 1},
    ],
)
async def test_search_bad_args(root: Path, args: dict[str, Any]) -> None:
    with pytest.raises(ToolFailed):
        await files.search(scope_for(root), None, args)


async def test_search_with_ripgrep(root: Path, outside: Path, tmp_path: Path) -> None:
    argv_file = tmp_path / "argv.txt"
    lines = [
        f"{root / 'app' / 'README.md'}\\0001:hello mycelium {TOKEN}",
        f"{root / '.env'}\\0001:mycelium",
        f"{outside}\\0001:mycelium outside",
        "garbage line",
    ]
    rg = write(
        tmp_path / "bin" / "rg",
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$@\" > '{argv_file}'\n"
        + "".join(f"printf '{line}\\n'\n" for line in lines),
    )
    rg.chmod(rg.stat().st_mode | stat.S_IXUSR)
    out = await files.search(scope_for(root), rg, {"pattern": "-mycelium", "glob": "*.md"})
    assert out["engine"] == "rg"
    assert [(h["path"], h["line"]) for h in out["hits"]] == [(str(root / "app" / "README.md"), 1)]
    assert TOKEN not in out["hits"][0]["text"]
    argv = argv_file.read_text().splitlines()
    assert {"--no-config", "--fixed-strings", "--null", "--glob=*.md", "--glob=!.env*"} <= set(argv)
    assert argv.index("--glob=*.md") < argv.index("--glob=!.env*")  # the deny globs win
    assert "--regexp=-mycelium" in argv and argv[-2:] == ["--", str(root)]


async def test_search_missing_ripgrep_falls_back(root: Path, tmp_path: Path) -> None:
    out = await files.search(scope_for(root), tmp_path / "no-rg", {"pattern": "mycelium"})
    assert out["engine"] == "python" and out["count"] == 1


# --- tail_log ------------------------------------------------------------------------------


def test_tail_log(tmp_path: Path) -> None:
    logs = tmp_path / "logs"
    records = [
        {"timestamp": f"2026-10-18T10:00:{i:02d}Z", "level": "info", "event": f"e{i}"}
        for i in range(30)
    ]
    records.append(
        {
            "timestamp": "2026-10-18T10:01:00Z",
            "level": "warning",
            "event": "fetch",
            "url": "https://x.example/a",
            "header": f"Bearer {TOKEN}",
        }
    )
    write(logs / "resonant.jsonl", "".join(json.dumps(r) + "\n" for r in records))
    write(logs / "daemon.err.log", "Traceback\nboom password" + "=abcd1234xyz to +15551234567\n")
    out = files.tail_log(logs, {"n": 3})
    assert out["lines"][:2] == ["10:00:28 info e28", "10:00:29 info e29"]
    assert out["lines"][2].startswith("10:01:00 warning fetch url=<url>")
    assert TOKEN not in out["lines"][2] and out["untrusted"] is True
    assert files.tail_log(logs, {"contains": "E1"})["lines"][0] == "10:00:01 info e1"
    assert files.tail_log(logs, {"n": 500})["n"] == files.LOGS_MAX
    err = files.tail_log(logs, {"name": "daemon.err.log"})
    assert err["lines"] == ["Traceback", "boom password=[redacted] to <handle>"]
    missing = files.tail_log(logs, {"name": "nope.log"})
    assert missing["found"] is False and missing["logs"] == ["daemon.err.log", "resonant.jsonl"]


def test_tail_log_refuses_paths_and_symlinks(tmp_path: Path, outside: Path) -> None:
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "evil.log").symlink_to(outside)
    for bad in ({"name": "../x"}, {"name": "a/b"}, {"n": 0}, {"x": 1}):
        with pytest.raises(ToolFailed):
            files.tail_log(logs, bad)
    with pytest.raises(ToolFailed, match=r"can't open evil\.log"):
        files.tail_log(logs, {"name": "evil.log"})


# --- read_transcript -------------------------------------------------------------------------


def _line(obj: dict[str, Any]) -> str:
    return json.dumps(obj) + "\n"


def test_read_transcript(tmp_path: Path) -> None:
    tdir = tmp_path / "transcripts"
    events = [
        {"type": "system", "subtype": "init"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Looking [K4] at it"}]},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "t1",
                        "name": "Bash",
                        "input": {
                            "command": f"curl -H 'Authorization: token {TOKEN}' https://api.x"
                        },
                    }
                ]
            },
        },
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "t1",
                        "content": [{"type": "text", "text": "ok " * 200}],
                    }
                ]
            },
        },
        {"type": "result", "result": "Done: PR #3"},
        {"type": "resonant.exit"},
    ]
    write(tdir / "R4.jsonl", "".join(_line(e) for e in events))
    out = files.read_transcript(tdir, {"code": "r4"})
    assert out["found"] is True and out["code"] == "R4" and out["untrusted"] is True
    kinds = [e["kind"] for e in out["entries"]]
    assert kinds == ["said", "call", "result", "turn ended", "session ended"]
    said, call, result = (e["text"] for e in out["entries"][:3])
    assert said == "Looking ? at it"  # cleaned like run_activity: codes masked
    assert call.startswith("$ curl") and TOKEN not in call
    assert len(result) <= files.RESULT_MAX and result.endswith("...")
    assert out["entries"][3]["text"] == "Done: PR #3"
    last = files.read_transcript(tdir, {"code": "R4", "n": 2})
    assert [e["kind"] for e in last["entries"]] == ["turn ended", "session ended"]
    assert last["more"] is True
    assert files.read_transcript(tdir, {"code": "R9"}) == {
        "found": False,
        "code": "R9",
        "text": "No transcript for R9.",
        "untrusted": True,
    }


def test_read_transcript_refusals(tmp_path: Path, outside: Path) -> None:
    tdir = tmp_path / "transcripts"
    tdir.mkdir()
    (tdir / "R5.jsonl").symlink_to(outside)
    assert files.read_transcript(tdir, {"code": "R5"})["found"] is False  # never followed
    for bad in ({"code": "R0"}, {"code": "../R1"}, {"code": 3}, {}, {"code": "R1", "n": 0}):
        with pytest.raises(ToolFailed):
            files.read_transcript(tdir, bad)


# --- registration, specs, grants -----------------------------------------------------------


def test_specs_are_untrusted_granted_l0_reads() -> None:
    assert {s.name for s in files.SPECS} == {
        "read_file",
        "list_dir",
        "search",
        "tail_log",
        "read_transcript",
    }
    for s in files.SPECS:
        assert s.effect is Effect.READ and s.effective_level is Level.L0
        assert s.untrusted is True and s.grant == files.GRANT and s.extension is None


def test_grant_only_on_builtin_l0_reads() -> None:
    with pytest.raises(ValidationError, match="member grant"):
        ToolSpec(name="w", effect=Effect.WRITE, grant="files")
    with pytest.raises(ValidationError, match="member grant"):
        ToolSpec(name="r", effect=Effect.READ, extension="dori", source="ext:dori", grant="files")
    with pytest.raises(ValidationError, match="member grant"):
        ToolSpec(name="r", effect=Effect.READ, level=Level.L1, secrets=("t",), grant="files")
    with pytest.raises(ValidationError):
        ToolSpec(name="r", effect=Effect.READ, grant="Bad Name")
    assert ToolSpec(name="r", effect=Effect.READ).untrusted is False


@pytest.fixture
def principals() -> Principals:
    return Principals.model_validate(
        {
            "principals": {
                "justin": {"role": "owner"},
                "reader": {"grants": {"builtin:files": ["request"]}},
                "cofounder": {"grants": {"dori": ["request", "approve"]}},
            }
        }
    )


def test_principals_builtin_grant(principals: Principals) -> None:
    list_tasks = ToolSpec(name="list_tasks", effect=Effect.READ)
    assert principals.may_request("justin", files.READ_FILE)
    assert principals.may_request("reader", files.READ_FILE)
    assert not principals.may_request("cofounder", files.READ_FILE)
    assert not principals.may_request("reader", list_tasks)
    assert not principals.can("reader", "approve", None, grant="files")
    assert not principals.can("reader", "admin", None, grant="files")
    assert not principals.can("reader", "request", None)


def test_builtin_grant_may_only_request() -> None:
    with pytest.raises(ValidationError, match="may only list request"):
        Principals.model_validate(
            {
                "principals": {
                    "justin": {"role": "owner"},
                    "x": {"grants": {"builtin:files": ["request", "approve"]}},
                }
            }
        )


def test_gate_lets_a_granted_member_read(db: sqlite3.Connection, principals: Principals) -> None:
    registry = ToolRegistry()
    list_tasks = ToolSpec(name="list_tasks", effect=Effect.READ)
    for spec in (files.READ_FILE, list_tasks):
        registry.register(spec)
    kv_set(db, "autonomy", "on")
    gate = PolicyGate(db, principals, registry, dry_run=False, approval_ttl_s=1800.0)
    task_id = task_repo.insert_task(db, NewTask(kind="chat", runner="local", priority=1))
    task_repo.set_status(db, task_id, "running")

    def decide(spec: ToolSpec, who: str) -> object:
        args = {"path": "/x"} if spec is files.READ_FILE else {}
        return gate.evaluate(Intent.for_tool(spec, task_id=task_id, principal=who, args=args))

    assert isinstance(decide(files.READ_FILE, "reader"), Allow)
    assert isinstance(decide(files.READ_FILE, "cofounder"), Deny)
    assert isinstance(decide(list_tasks, "reader"), Deny)
    assert isinstance(decide(files.READ_FILE, "justin"), Allow)


async def test_registered_handlers(db: sqlite3.Connection, root: Path, tmp_path: Path) -> None:
    settings = load_settings(
        brain={"read_roots": [str(root)], "rg_path": str(tmp_path / "no-rg")},
        runner={"mirror_dir": str(tmp_path / "state" / "runner" / "mirrors")},
    )
    write(settings.logs_dir / "resonant.jsonl", json.dumps({"event": "hi"}) + "\n")
    state = DaemonState(settings, db, Loop(db, {}, settings.loop, SlotPool(2, 1)), utcnow())
    components = build_components(settings, db, state)
    h = components.handlers
    assert (await h[(None, "read_file")]({"path": str(root / "app" / "README.md")}))["content"]
    assert (await h[(None, "list_dir")]({"path": str(root)}))["count"] == 1
    assert (await h[(None, "search")]({"pattern": "mycelium"}))["count"] == 1
    assert (await h[(None, "tail_log")]({}))["lines"] == ["hi"]
    assert (await h[(None, "read_transcript")]({"code": "R1"}))["found"] is False
    for spec in files.SPECS:
        assert components.registry.get(None, spec.name) == spec
