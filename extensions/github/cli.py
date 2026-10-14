#!/usr/bin/env python3
"""github extension CLI: open PRs, one PR, a stateless CI/review watch, a daily digest.

Stdlib only and a single file: Resonant runs it as ``python -I -S cli.py``, so nothing next to
it is importable. Every GitHub call goes through the ``gh`` CLI with the Keychain token
(``RESONANT_SECRET_TOKEN``) passed as ``GH_TOKEN`` in gh's own environment only.

Tools print one JSON object on stdout and exit 0. Any failure prints one line to stderr and
exits 1; this extension only reads, so a failed run changed nothing.

``watch`` keeps no state. It derives events from GitHub's current state, each with a
deterministic ``dedupe_key``, and Resonant's events table (UNIQUE on the key) is the memory.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

DEFAULT_PATH = "/opt/homebrew/bin:/usr/bin:/bin"
GH_TIMEOUT_S = 30
SEARCH_LIMIT = 50  # PRs per search
RUN_LIMIT = 20  # workflow runs per PR branch
MAX_EVENTS = 100  # the process runner keeps at most 100 events per run
TEXT_MAX = 300  # notify clips at 300 chars
TITLE_MAX = 80
CHECKS_MAX = 30
DIGEST_FAILING_MAX = 3

FAILED = frozenset({"failure", "timed_out", "startup_failure"})
SUCCEEDED = frozenset({"success"})
# Check-run conclusions and status-context states that count as red, in GraphQL's casing.
_RED = frozenset({"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"})
_PENDING_STATES = frozenset({"PENDING", "EXPECTED"})

_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_AUTHOR = re.compile(r"^(@me|[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))$")

_ROLLUP = """statusCheckRollup { state contexts(first: 50) { nodes { __typename
  ... on CheckRun { name status conclusion }
  ... on StatusContext { context state } } } }"""
_PR_FIELDS = f"""... on PullRequest {{ number title url isDraft createdAt updatedAt
  headRefName headRefOid reviewDecision repository {{ nameWithOwner }}
  commits(last: 1) {{ nodes {{ commit {{ {_ROLLUP} }} }} }} }}"""
SEARCH_QUERY = f"""query($mine: String!, $review: String!) {{
  mine: search(query: $mine, type: ISSUE, first: {SEARCH_LIMIT}) {{
    issueCount nodes {{ {_PR_FIELDS} }} }}
  review: search(query: $review, type: ISSUE, first: {SEARCH_LIMIT}) {{
    issueCount nodes {{ {_PR_FIELDS} }} }}
}}"""
PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,author,headRefName,headRefOid,baseRefName,"
    "reviewDecision,mergeable,statusCheckRollup,additions,deletions,changedFiles,"
    "createdAt,updatedAt"
)
RUN_FIELDS = "databaseId,workflowName,status,conclusion,headSha,createdAt"


class Failed(Exception):
    """A clean, definite failure: one line to stderr, exit 1."""


# --- config and gh ---------------------------------------------------------------------


def data_dir() -> Path:
    return Path(os.environ.get("RESONANT_EXT_DATA") or Path.home())


def load_config() -> tuple[str, tuple[str, ...]]:
    """``(author, repos)`` from ``$RESONANT_EXT_DATA/config.json``; defaults ``@me``, all."""
    path = data_dir() / "config.json"
    if not path.exists():
        return "@me", ()
    try:
        raw = json.loads(path.read_text())
    except (OSError, ValueError) as e:
        raise Failed(f"config.json is unreadable: {type(e).__name__}") from None
    cfg = _d(raw)
    if not isinstance(raw, dict) or set(cfg) - {"author", "repos"}:
        raise Failed('config.json must be {"author": "...", "repos": ["owner/name", ...]}')
    author = cfg.get("author", "@me")
    repos = _l(cfg.get("repos", []))
    if not isinstance(author, str) or not _AUTHOR.match(author):
        raise Failed("config.json: author must be @me or a GitHub login")
    if not isinstance(cfg.get("repos", []), list) or not all(
        isinstance(r, str) and _REPO.match(r) for r in repos
    ):
        raise Failed("config.json: repos must be a list of owner/name strings")
    return author, tuple(sorted(str(r) for r in repos))


class Gh:
    """Runs ``gh`` by absolute path with a minimal env; the token goes only to that child."""

    def __init__(self) -> None:
        token = os.environ.get("RESONANT_SECRET_TOKEN", "")
        if not token:
            raise Failed(
                "secret github/token is missing; on the Mini: resonant secret set github/token"
            )
        search_path = os.environ.get("PATH") or DEFAULT_PATH
        found = shutil.which("gh", path=search_path)
        if found is None:
            raise Failed(f"gh not found on {search_path}")
        self.path = str(Path(found).resolve())
        self.token = token
        self.env = {
            "PATH": search_path,
            "HOME": str(data_dir()),
            "LANG": "en_US.UTF-8",
            "GH_TOKEN": token,
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_SPINNER_DISABLED": "1",
            "NO_COLOR": "1",
        }
        if tmp := os.environ.get("TMPDIR"):
            self.env["TMPDIR"] = tmp

    def json(self, *args: str) -> Any:
        try:
            proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
                [self.path, *args],
                env=self.env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                timeout=GH_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise Failed(f"gh {args[0]} timed out after {GH_TIMEOUT_S}s") from None
        except OSError as e:
            raise Failed(f"gh could not run: {type(e).__name__}") from None
        if proc.returncode != 0:
            tail = self.redact(" ".join(proc.stderr.split()))[-200:]
            raise Failed(f"gh {args[0]} failed (exit {proc.returncode}): {tail}")
        try:
            return json.loads(proc.stdout)
        except ValueError:
            raise Failed(f"gh {args[0]} returned invalid JSON") from None

    def redact(self, text: str) -> str:
        return text.replace(self.token, "[redacted]")


# --- shaping ---------------------------------------------------------------------------


def _d(v: object) -> dict[str, Any]:
    return cast(dict[str, Any], v) if isinstance(v, dict) else {}


def _l(v: object) -> list[Any]:
    return cast(list[Any], v) if isinstance(v, list) else []


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


def clip(text: str, n: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[: n - 1] + "…"


def checks_of(contexts: list[Any]) -> tuple[str, list[str], list[dict[str, str]]]:
    """``(ci, failing names, checks)`` from rollup contexts (GraphQL or ``gh pr view``)."""
    checks: list[dict[str, str]] = []
    failing: list[str] = []
    pending = False
    for c in map(_d, contexts):
        if _s(c.get("__typename")) == "StatusContext" or "context" in c:
            name, state = _s(c.get("context")), _s(c.get("state")).upper()
            red, waiting = state in _RED, state in _PENDING_STATES
        else:
            name = _s(c.get("name"))
            status, conclusion = _s(c.get("status")).upper(), _s(c.get("conclusion")).upper()
            waiting = status != "COMPLETED"
            state = conclusion or status
            red = not waiting and conclusion in _RED
        checks.append({"name": clip(name, 60), "state": state.lower()})
        pending = pending or waiting
        if red:
            failing.append(clip(name, 60))
    ci = "failing" if failing else "pending" if pending else "passing" if checks else "none"
    return ci, sorted(set(failing)), checks


def shape_pr(node: dict[str, Any]) -> dict[str, Any]:
    """A search node as the compact PR dict ``prs``, ``watch`` and ``digest`` share."""
    commits = _l(_d(node.get("commits")).get("nodes"))
    rollup = _d(_d(_d(commits[-1] if commits else {}).get("commit")).get("statusCheckRollup"))
    ci, failing, _ = checks_of(_l(_d(rollup.get("contexts")).get("nodes")))
    return {
        "repo": _s(_d(node.get("repository")).get("nameWithOwner")),
        "number": node.get("number") if isinstance(node.get("number"), int) else 0,
        "title": clip(_s(node.get("title")), TITLE_MAX),
        "url": _s(node.get("url")),
        "draft": node.get("isDraft") is True,
        "branch": _s(node.get("headRefName")),
        "head_sha": _s(node.get("headRefOid")),
        "ci": ci,
        "failing": failing,
        "review": _s(node.get("reviewDecision")).lower() or None,
        "created_at": _s(node.get("createdAt")),
        "updated_at": _s(node.get("updatedAt")),
    }


def search(gh: Gh) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """``(my open PRs, PRs awaiting my review, truncated)``, sorted by repo and number."""
    author, repos = load_config()
    scope = " ".join(["is:pr", "is:open", "archived:false", *(f"repo:{r}" for r in repos)])
    data = _d(
        gh.json(
            "api",
            "graphql",
            "-f",
            f"query={SEARCH_QUERY}",
            "-f",
            f"mine={scope} author:{author}",
            "-f",
            f"review={scope} review-requested:{author}",
        )
    )
    if not isinstance(data.get("data"), dict):
        raise Failed("gh api graphql returned no data")
    out: list[list[dict[str, Any]]] = []
    truncated = False
    for alias in ("mine", "review"):
        result = _d(_d(data["data"]).get(alias))
        nodes = [shape_pr(_d(n)) for n in _l(result.get("nodes")) if _d(n).get("number")]
        truncated = truncated or (_int(result.get("issueCount")) > len(nodes))
        out.append(sorted(nodes, key=lambda p: (p["repo"], p["number"])))
    return out[0], out[1], truncated


def _int(v: Any) -> int:
    return v if isinstance(v, int) else 0


def ref(pr: dict[str, Any]) -> str:
    """``resonant#40``: the short form people read."""
    return f"{str(pr['repo']).rsplit('/', 1)[-1]}#{pr['number']}"


def event(kind: str, text: str, key: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"type": kind, "text": clip(text, TEXT_MAX), "dedupe_key": key[:200], "data": data}


def emit(result: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    """Events go to fd 3 when Resonant opened it (``RESONANT_EVENTS_FD``), else in the result."""
    events = events[:MAX_EVENTS]
    fd = os.environ.get("RESONANT_EVENTS_FD", "")
    if fd.isdigit():
        try:
            with os.fdopen(int(fd), "w", closefd=False) as f:
                f.writelines(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
            return result
        except OSError:
            pass  # not open (run by hand): fall back to the result
    return {**result, "events": events} if events else result


# --- commands --------------------------------------------------------------------------


def cmd_prs(gh: Gh) -> dict[str, Any]:
    mine, review, truncated = search(gh)
    failing = sum(1 for p in mine if p["ci"] == "failing")
    summary = f"{len(mine)} open PRs, {failing} failing CI, {len(review)} awaiting your review"
    return {"summary": summary, "prs": mine, "review_requested": review, "truncated": truncated}


def cmd_pr(gh: Gh, repo: str, number: int) -> dict[str, Any]:
    if not _REPO.match(repo):
        raise Failed("repo must look like owner/name")
    if number <= 0:
        raise Failed("number must be a positive integer")
    v = _d(gh.json("pr", "view", str(number), "--repo", repo, "--json", PR_VIEW_FIELDS))
    ci, failing, checks = checks_of(_l(v.get("statusCheckRollup")))
    return {
        "repo": repo,
        "number": number,
        "title": clip(_s(v.get("title")), 200),
        "url": _s(v.get("url")),
        "state": _s(v.get("state")).lower(),
        "draft": v.get("isDraft") is True,
        "author": _s(_d(v.get("author")).get("login")),
        "branch": _s(v.get("headRefName")),
        "base": _s(v.get("baseRefName")),
        "head_sha": _s(v.get("headRefOid")),
        "review": _s(v.get("reviewDecision")).lower() or None,
        "mergeable": _s(v.get("mergeable")).lower() or None,
        "ci": ci,
        "failing": failing,
        "checks": checks[:CHECKS_MAX],
        "additions": _int(v.get("additions")),
        "deletions": _int(v.get("deletions")),
        "changed_files": _int(v.get("changedFiles")),
        "created_at": _s(v.get("createdAt")),
        "updated_at": _s(v.get("updatedAt")),
    }


def run_events(pr: dict[str, Any], runs: list[Any]) -> list[dict[str, Any]]:
    """CI events for one PR from its branch's workflow runs, per workflow.

    The latest finished run on the head failed: ``ci_failed``. It succeeded and the previous
    finished run of that workflow on the branch failed: ``ci_fixed``. Cancelled, skipped and
    in-progress runs are ignored. Keys use the run id, so they're stable across watch runs.
    """
    finished = [
        _d(r)
        for r in runs
        if _s(_d(r).get("status")) == "completed"
        and _s(_d(r).get("conclusion")) in FAILED | SUCCEEDED
        and isinstance(_d(r).get("databaseId"), int)
    ]
    finished.sort(key=lambda r: (_s(r.get("createdAt")), r["databaseId"]), reverse=True)
    by_workflow: dict[str, list[dict[str, Any]]] = {}
    for r in finished:
        by_workflow.setdefault(_s(r.get("workflowName")), []).append(r)
    base = f"{pr['repo']}#{pr['number']}"
    out: list[dict[str, Any]] = []
    for workflow in sorted(by_workflow):
        latest, *older = by_workflow[workflow]
        if _s(latest.get("headSha")) != pr["head_sha"]:
            continue  # the head's run for this workflow hasn't finished yet
        run_id = latest["databaseId"]
        data = {
            "repo": pr["repo"],
            "number": pr["number"],
            "url": pr["url"],
            "workflow": workflow,
            "run_id": run_id,
            "head_sha": pr["head_sha"],
        }
        if _s(latest.get("conclusion")) in FAILED:
            text = f"✗ {ref(pr)} CI failed ({workflow})"
            out.append(event("ci_failed", text, f"{base}:run:{run_id}:failed", data))
        elif older and _s(older[0].get("conclusion")) in FAILED:
            text = f"✓ {ref(pr)} CI fixed"
            out.append(event("ci_fixed", text, f"{base}:run:{run_id}:fixed", data))
    return out


def cmd_watch(gh: Gh) -> dict[str, Any]:
    mine, review, _ = search(gh)
    events: list[dict[str, Any]] = []
    for pr in mine:
        if not pr["branch"] or not pr["head_sha"]:
            continue
        runs = gh.json(
            "run",
            "list",
            "--repo",
            pr["repo"],
            "--branch",
            pr["branch"],
            "--json",
            RUN_FIELDS,
            "--limit",
            str(RUN_LIMIT),
        )
        events += run_events(pr, _l(runs))
    for pr in review:
        key = f"{pr['repo']}#{pr['number']}:{pr['head_sha']}:review"
        text = f"👀 {ref(pr)} review requested: {pr['title']}"
        data = {"repo": pr["repo"], "number": pr["number"], "url": pr["url"]}
        events.append(event("review_requested", text, key, data))
    summary = f"{len(mine)} PRs watched, {len(events)} events"
    return emit({"summary": summary, "prs": len(mine), "events_found": len(events)}, events)


def digest_line(mine: list[dict[str, Any]], review: list[dict[str, Any]]) -> str:
    """``github: 3 open PRs, resonant#41 ✗ lint, 1 review requested``."""
    parts = [f"{len(mine)} open PR{'' if len(mine) == 1 else 's'}"]
    red = [p for p in mine if p["ci"] == "failing"]
    for p in red[:DIGEST_FAILING_MAX]:
        parts.append(f"{ref(p)} ✗ {clip(p['failing'][0], 24)}" if p["failing"] else ref(p))
    if len(red) > DIGEST_FAILING_MAX:
        parts.append(f"+{len(red) - DIGEST_FAILING_MAX} more failing")
    if review:
        parts.append(f"{len(review)} review{'' if len(review) == 1 else 's'} requested")
    return clip("github: " + ", ".join(parts), 200)


def cmd_digest(gh: Gh) -> dict[str, Any]:
    mine, review, _ = search(gh)
    line = digest_line(mine, review)
    due = os.environ.get("RESONANT_JOB_DUE_AT") or datetime.now(UTC).strftime("%Y-%m-%dT%H:%MZ")
    data = {
        "open": len(mine),
        "failing": [f"{p['repo']}#{p['number']}" for p in mine if p["ci"] == "failing"],
        "review_requested": len(review),
    }
    return emit({"summary": line}, [event("digest", line, f"digest:{clip(due, 64)}", data)])


def cmd_health(gh: Gh) -> dict[str, Any]:
    user = _d(gh.json("api", "user"))
    if not _s(user.get("login")):
        raise Failed("gh api user returned no login")
    return {"ok": True, "login": _s(user.get("login"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="github")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("prs", "watch", "digest", "health"):
        sub.add_parser(name)
    p = sub.add_parser("pr")
    p.add_argument("--repo", required=True)
    p.add_argument("--number", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        gh = Gh()
        if args.cmd == "pr":
            out = cmd_pr(gh, args.repo, args.number)
        else:
            out = {"prs": cmd_prs, "watch": cmd_watch, "digest": cmd_digest, "health": cmd_health}[
                args.cmd
            ](gh)
    except Failed as e:
        print(f"github: {e}", file=sys.stderr)
        return 1
    json.dump(out, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
