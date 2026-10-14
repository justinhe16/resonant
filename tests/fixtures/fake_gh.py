"""A fake ``gh`` for extension tests. Never touches GitHub.

Run as ``python fake_gh.py <state.json> <gh args…>`` through a ``gh`` shim on a temp PATH.
It answers from ``state.json`` and appends each call (argv, the env keys it saw, and whether
``GH_TOKEN`` matched ``state["token"]``) to ``calls.jsonl`` beside the state file.

State keys: ``token``, ``user``, ``mine`` / ``review`` (GraphQL search nodes), ``runs``
(``"<repo>:<branch>"`` → ``gh run list`` rows), ``prs`` (``"<repo>#<n>"`` → ``gh pr view``),
``fail`` (``{"exit": n, "stderr": "..."}``) to fail every call, and ``garbage`` to print
non-JSON with exit 0.
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
    if args[:2] == ["api", "user"]:
        return 0, state.get("user", {"login": "octo"}), ""
    if args[:2] == ["api", "graphql"]:
        data = {
            alias: {"issueCount": len(state.get(alias, [])), "nodes": state.get(alias, [])}
            for alias in ("mine", "review")
        }
        return 0, {"data": data}, ""
    if args[:2] == ["run", "list"]:
        key = f"{_opt(args, '--repo')}:{_opt(args, '--branch')}"
        return 0, state.get("runs", {}).get(key, []), ""
    if args[:2] == ["pr", "view"]:
        pr = state.get("prs", {}).get(f"{_opt(args, '--repo')}#{args[2]}")
        if pr is None:
            return 1, None, "GraphQL: Could not resolve to a PullRequest"
        return 0, pr, ""
    return 2, None, f"fake gh: unhandled {args[:2]}"


def main() -> int:
    state_path = Path(sys.argv[1])
    args = sys.argv[2:]
    state = cast(dict[str, Any], json.loads(state_path.read_text()))
    call = {
        "argv": args,
        "env": sorted(os.environ),
        "token_ok": os.environ.get("GH_TOKEN") == state.get("token"),
    }
    with (state_path.parent / "calls.jsonl").open("a") as f:
        f.write(json.dumps(call) + "\n")
    if state.get("garbage"):
        sys.stdout.write("<html>not json")
        return 0
    code, out, err = answer(state, args)
    if out is not None:
        json.dump(out, sys.stdout)
    sys.stderr.write(err)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
