"""PER-262: probe the installed ``claude`` CLI's ``Read()`` deny rules and ``--max-turns``.

Run by hand after a Claude Code upgrade (results go in docs/brain-plan.md, PER-262)::

    uv run python scripts/probe_claude_rules.py [--model haiku] [--keep]

What it asks, each in its own ``claude -p`` (the owner's login, a scratch cwd and tree):

- Does a ``Read(...)`` deny rule bind ``Read`` itself, ``Grep`` and ``Glob``?
- Does it follow symlinks (a link inside a read root to a denied dir or file)?
- Does it ignore case (a rule written ``mixed.txt`` for a file named ``Mixed.txt``; a glob
  rule ``**/.env`` for a file named ``.ENV``)?
- Does ``--max-turns`` count per user message in a ``--input-format stream-json`` session,
  or across the whole session?

Every verdict is read from the tool results in the stream (did the token in the file come
back), never from what the model says. Safe by construction: ``--setting-sources ''`` (no
settings, hooks or MCP of the owner's), ``--strict-mcp-config`` with no MCP config (no
connectors: nothing can send), ``--no-session-persistence`` (no transcript), the scratch
tree under the system temp dir, removed at the end unless ``--keep``.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

TIMEOUT_S = 180
CLI = ["claude"]  # main() sets the full path


@dataclass(frozen=True)
class Tree:
    base: Path

    @property
    def cwd(self) -> Path:
        return self.base / "cwd"

    @property
    def root(self) -> Path:
        return self.base / "root"

    @property
    def outside(self) -> Path:
        return self.base / "outside"


TOKENS = {
    "open": "TOKEN-OPEN-1A",
    "dir": "TOKEN-DIR-7Q",
    "file": "TOKEN-FILE-3K",
    "case": "TOKEN-CASE-5M",
    "glob": "TOKEN-GLOB-9P",
    "link": "TOKEN-LINK-2W",
    "class": "TOKEN-CLASS-4R",
}


def build(base: Path) -> Tree:
    t = Tree(base)
    for d in (t.cwd, t.root / "denied", t.root / "sub", t.outside):
        d.mkdir(parents=True, exist_ok=True)
    (t.root / "open.txt").write_text(
        f"{TOKENS['open']}\nNext: read {t.root / 'open2.txt'}\n", encoding="utf-8"
    )
    (t.root / "open2.txt").write_text("TOKEN-OPEN-2B\n", encoding="utf-8")
    (t.root / "denied" / "inner.txt").write_text(TOKENS["dir"] + "\n", encoding="utf-8")
    (t.root / "secret.txt").write_text(TOKENS["file"] + "\n", encoding="utf-8")
    (t.root / "Mixed.txt").write_text(TOKENS["case"] + "\n", encoding="utf-8")
    (t.root / "sub" / ".ENV").write_text(TOKENS["glob"] + "\n", encoding="utf-8")
    (t.root / "sub" / ".EnvX").write_text(TOKENS["class"] + "\n", encoding="utf-8")
    (t.outside / "data.txt").write_text(TOKENS["link"] + "\n", encoding="utf-8")
    (t.root / "link_dir").symlink_to(t.outside, target_is_directory=True)
    (t.root / "link_file").symlink_to(t.root / "secret.txt")
    return t


def deny_rules(t: Tree) -> list[str]:
    """As ``AskScope.deny_rules`` writes them: ``//`` for an absolute path, the path and all
    under it, a glob anywhere and all under it. The case rule is written lower case."""
    rules: list[str] = []
    for p in (t.root / "denied", t.root / "secret.txt", t.root / "mixed.txt", t.outside):
        rules += [f"Read(/{p})", f"Read(/{p}/**)"]
    rules += ["Read(**/.env)", "Read(**/.env/**)"]
    rules += ["Read(**/.[eE][nN][vV][xX])"]  # a glob with per-letter classes
    return rules


def argv(
    t: Tree, model: str, *, stream_in: bool = False, max_turns: int | None = None
) -> list[str]:
    out = [
        *CLI,
        "-p",
        "--model",
        model,
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--no-session-persistence",
        "--tools",
        "Read,Grep,Glob",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "Read,Grep,Glob",
        "--settings",
        json.dumps({"permissions": {"deny": deny_rules(t)}}),
        "--add-dir",
        str(t.root),
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    if stream_in:
        out += ["--input-format", "stream-json"]
    if max_turns is not None:
        out += ["--max-turns", str(max_turns)]
    return out


def user_line(text: str) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": text}}) + "\n"


def events(raw: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if not line.startswith("{"):
            continue
        try:
            obj: Any = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(cast(dict[str, Any], obj))
    return out


def tool_results(evs: list[dict[str, Any]]) -> list[tuple[bool, str]]:
    """Every tool result in the stream: ``(is_error, text)``."""
    out: list[tuple[bool, str]] = []
    for e in evs:
        if e.get("type") != "user":
            continue
        msg: Any = e.get("message")
        content: Any = cast(dict[str, Any], msg).get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in cast(list[Any], content):
            if not isinstance(block, dict):
                continue
            b = cast(dict[str, Any], block)
            if b.get("type") != "tool_result":
                continue
            body: Any = b.get("content")
            if isinstance(body, list):
                body = " ".join(
                    str(cast(dict[str, Any], x).get("text", ""))
                    for x in cast(list[Any], body)
                    if isinstance(x, dict)
                )
            out.append((b.get("is_error") is True, str(body)))
    return out


def tool_uses(evs: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    for e in evs:
        msg: Any = e.get("message")
        if e.get("type") != "assistant" or not isinstance(msg, dict):
            continue
        content: Any = cast(dict[str, Any], msg).get("content")
        for block in cast(list[Any], content) if isinstance(content, list) else []:
            if isinstance(block, dict) and cast(dict[str, Any], block).get("type") == "tool_use":
                names.append(str(cast(dict[str, Any], block).get("name")))
    return names


def run_one(t: Tree, model: str, prompt: str) -> list[dict[str, Any]]:
    proc = subprocess.run(  # noqa: S603 - the installed CLI, fixed argv
        argv(t, model),
        input=prompt,
        cwd=t.cwd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
        check=False,
    )
    return events(proc.stdout)


@dataclass(frozen=True)
class Case:
    name: str
    question: str
    prompt: str
    token: str


def cases(t: Tree) -> list[Case]:
    r = t.root
    once = "Make exactly that one tool call, nothing else, then reply DONE."
    return [
        Case(
            "read-open",
            "control: Read of an allowed file",
            f"Call the Read tool with file_path {r / 'open.txt'}. {once}",
            TOKENS["open"],
        ),
        Case(
            "read-dir",
            "Read under a denied dir",
            f"Call the Read tool with file_path {r / 'denied' / 'inner.txt'}. {once}",
            TOKENS["dir"],
        ),
        Case(
            "grep-root",
            "Grep over a root (denied dir, file, case, link)",
            f"Call the Grep tool with pattern 'TOKEN-' path {r} output_mode content and "
            f"-n true. {once}",
            "|".join(TOKENS[k] for k in ("dir", "file", "case", "link")),
        ),
        Case(
            "grep-dir",
            "Grep inside a denied dir",
            f"Call the Grep tool with pattern 'TOKEN-' path {r / 'denied'} output_mode "
            f"content. {once}",
            TOKENS["dir"],
        ),
        Case(
            "glob-dir",
            "Glob inside a denied dir (does the name come back)",
            f"Call the Glob tool with pattern '**/*' path {r / 'denied'}. {once}",
            "inner.txt",
        ),
        Case(
            "link-dir",
            "Read via a symlinked dir whose target is denied",
            f"Call the Read tool with file_path {r / 'link_dir' / 'data.txt'}. {once}",
            TOKENS["link"],
        ),
        Case(
            "link-file",
            "Read via a symlink to a denied file",
            f"Call the Read tool with file_path {r / 'link_file'}. {once}",
            TOKENS["file"],
        ),
        Case(
            "grep-link",
            "Grep via a symlinked dir whose target is denied",
            f"Call the Grep tool with pattern 'TOKEN-' path {r / 'link_dir'} output_mode "
            f"content. {once}",
            TOKENS["link"],
        ),
        Case(
            "case-path",
            "Read of Mixed.txt under a rule written mixed.txt",
            f"Call the Read tool with file_path {r / 'Mixed.txt'}. {once}",
            TOKENS["case"],
        ),
        Case(
            "case-glob",
            "Read of .ENV under a rule written **/.env",
            f"Call the Read tool with file_path {r / 'sub' / '.ENV'}. {once}",
            TOKENS["glob"],
        ),
        Case(
            "case-class",
            "Read of .EnvX under a rule written **/.[eE][nN][vV][xX]",
            f"Call the Read tool with file_path {r / 'sub' / '.EnvX'}. {once}",
            TOKENS["class"],
        ),
        Case(
            "read-file",
            "control: Read of a denied file",
            f"Call the Read tool with file_path {r / 'secret.txt'}. {once}",
            TOKENS["file"],
        ),
    ]


def verdict(evs: list[dict[str, Any]], token: str) -> str:
    """``token``: what must not come back; ``|``-separated for several (a Grep over a root)."""
    results = tool_results(evs)
    if not results:
        return f"no tool result (tools used: {tool_uses(evs) or 'none'})"
    leaked = any(tok in text for _, text in results for tok in token.split("|"))
    errors = [text[:120].replace("\n", " ") for err, text in results if err]
    if leaked:
        return "READ (token came back)"
    if errors:
        return f"BLOCKED ({errors[0]})"
    return f"not returned ({results[0][1][:120]!r})"


def max_turns_probe(t: Tree, model: str) -> dict[str, str]:
    """``--max-turns 2`` with stream-json input: one message needing two sequential tool
    calls (3 model turns: over the cap), and a session of two messages needing one each
    (2 turns each: under a per-message cap, over a session-wide one)."""
    out: dict[str, str] = {}
    one = f"Call the Read tool with file_path {t.root / 'open2.txt'}, then reply DONE."
    two = (
        f"Call the Read tool with file_path {t.root / 'open.txt'}, then read the file it "
        "names with a second Read call, then reply DONE."
    )
    for label, msgs in (("one message, 2 tools", [two]), ("two messages, 1 tool each", [one, one])):
        proc = subprocess.Popen(  # noqa: S603 - the installed CLI, fixed argv
            argv(t, model, stream_in=True, max_turns=2),
            cwd=t.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        assert proc.stdin and proc.stdout
        seen: list[str] = []
        for m in msgs:
            proc.stdin.write(user_line(m))
            proc.stdin.flush()
            for line in proc.stdout:
                evs = events(line)
                if evs and evs[0].get("type") == "result":
                    e = evs[0]
                    seen.append(f"{e.get('subtype')} (num_turns {e.get('num_turns')})")
                    break
        proc.stdin.close()
        try:
            proc.wait(TIMEOUT_S)
        except subprocess.TimeoutExpired:
            proc.kill()
        out[label] = "; ".join(seen) or "no result"
    return out


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Probe the claude CLI's Read() deny rules and --max-turns."
    )
    ap.add_argument("--model", default="haiku")
    ap.add_argument("--keep", action="store_true", help="keep the scratch tree")
    ns = ap.parse_args()
    found = shutil.which("claude")
    if found is None:
        print("claude is not on PATH", file=sys.stderr)
        return 2
    CLI[:] = [found]
    version = subprocess.run(  # noqa: S603 - the installed CLI
        [found, "--version"], capture_output=True, text=True, check=False
    ).stdout.strip()
    base = Path(os.path.realpath(tempfile.mkdtemp(prefix="resonant-rules-probe-")))
    try:
        t = build(base)
        print(f"claude {version}, model {ns.model}, tree {base}")
        for c in cases(t):
            print(f"{c.name:10} {c.question}: {verdict(run_one(t, ns.model, c.prompt), c.token)}")
        for label, seen in max_turns_probe(t, ns.model).items():
            print(f"max-turns  {label}: {seen}")
    finally:
        if not ns.keep:
            shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
