"""A fake ``gh`` for extension tests. Never touches GitHub.

Run as ``python fake_gh.py <state.json> <gh args…>`` through a ``gh`` shim on a temp PATH.
It answers from ``state.json`` and appends each call (argv, the env keys it saw, and whether
the ``hosts.yml`` token in ``GH_CONFIG_DIR`` matched ``state["token"]``, and the dir and file
modes) to ``calls.jsonl`` beside the state file.

State keys: ``token``, ``user``, ``mine`` / ``review`` / ``closed`` (GraphQL search nodes;
``closed`` answers the search with ``is:closed``), ``runs``
(``"<repo>:<branch>"`` → ``gh run list`` rows), ``prs`` (``"<repo>#<n>"`` → ``gh pr view``),
``list`` (``"<repo>"`` → ``gh pr list`` rows), ``fail`` (``{"exit": n, "stderr": "..."}``) to
fail every call, ``fail_on`` (``"pr merge"`` or ``"run rerun <id>"`` → the same shape) to fail
one kind of call, and ``garbage`` to print non-JSON with exit 0. ``pr merge`` and ``run rerun``
print a line of text, like gh does.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, cast


def _opt(args: list[str], flag: str) -> str:
    return args[args.index(flag) + 1] if flag in args else ""


def answer(state: dict[str, Any], args: list[str]) -> tuple[int, Any, str]:
    if fail := state.get("fail"):
        return int(fail["exit"]), None, str(fail["stderr"])
    fail_on = state.get("fail_on", {})
    for key in (" ".join(args[:2]), " ".join(args[:3])):
        if key in fail_on:
            return int(fail_on[key]["exit"]), None, str(fail_on[key]["stderr"])
    if args[:2] == ["pr", "merge"]:
        return 0, f"✓ Squashed and merged pull request #{args[2]}\n", ""
    if args[:2] == ["run", "rerun"]:
        return 0, f"✓ Requested rerun (failed jobs) of run {args[2]}\n", ""
    if args[:2] == ["api", "user"]:
        return 0, state.get("user", {"login": "octo"}), ""
    if args[:2] == ["api", "graphql"]:
        aliases = ("closed",) if any("is:closed" in a for a in args) else ("mine", "review")
        data = {
            alias: {"issueCount": len(state.get(alias, [])), "nodes": state.get(alias, [])}
            for alias in aliases
        }
        return 0, {"data": data}, ""
    if args[:2] == ["run", "list"]:
        key = f"{_opt(args, '--repo')}:{_opt(args, '--branch')}"
        return 0, state.get("runs", {}).get(key, []), ""
    if args[:2] == ["pr", "list"]:
        return 0, state.get("list", {}).get(_opt(args, "--repo"), []), ""
    if args[:2] == ["pr", "view"]:
        pr = state.get("prs", {}).get(f"{_opt(args, '--repo')}#{args[2]}")
        if pr is None:
            return 1, None, "GraphQL: Could not resolve to a PullRequest"
        return 0, pr, ""
    return 2, None, f"fake gh: unhandled {args[:2]}"


def _hosts_token() -> str | None:
    cfg = os.environ.get("GH_CONFIG_DIR")
    if not cfg:
        return None
    for line in (Path(cfg) / "hosts.yml").read_text().splitlines():
        if line.strip().startswith("oauth_token:"):
            return line.split(":", 1)[1].strip().strip('"')
    return None


def _modes() -> list[int]:
    cfg = Path(os.environ.get("GH_CONFIG_DIR", "/nonexistent"))
    return [cfg.stat().st_mode & 0o777, (cfg / "hosts.yml").stat().st_mode & 0o777]


def main() -> int:
    state_path = Path(sys.argv[1])
    args = sys.argv[2:]
    state = cast(dict[str, Any], json.loads(state_path.read_text()))
    call = {
        "argv": args,
        "env": sorted(os.environ),
        "token_ok": _hosts_token() == state.get("token"),
        "modes": _modes(),
    }
    with (state_path.parent / "calls.jsonl").open("a") as f:
        f.write(json.dumps(call) + "\n")
    if state.get("garbage"):
        sys.stdout.write("<html>not json")
        return 0
    code, out, err = answer(state, args)
    if isinstance(out, str):
        sys.stdout.write(out)
    elif out is not None:
        json.dump(out, sys.stdout)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
