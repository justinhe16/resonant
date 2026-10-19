#!/usr/bin/env python3
"""github extension CLI: PRs, a stateless CI/review watch, a digest, and two gated writes.

Stdlib only and a single file: Resonant runs it as ``python -I -S cli.py``, so nothing next to
it is importable. Every GitHub call goes through the ``gh`` CLI. The Keychain token arrives
on the secrets pipe (``RESONANT_SECRETS_FD``) and is never in any environment or argv: ``gh``
gets it from a ``hosts.yml`` in a private 0700 ``GH_CONFIG_DIR`` that is removed on exit.

Tools print one JSON object on stdout and exit 0. Any failure prints one line to stderr and
exits 1, and a failed run changed nothing: the writes (``rerun_failed``, ``merge_pr``) check
everything before their first mutating ``gh`` call, and once one has gone through they report
success with what happened rather than exit 1.

``watch`` keeps no state. It derives events from GitHub's current state, each with a
deterministic ``dedupe_key``, and Resonant's events table (UNIQUE on the key) is the memory.
"""

from __future__ import annotations

import argparse
import atexit
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, cast

DEFAULT_PATH = "/usr/bin:/bin"
# gh's absolute path, pinned by Resonant at install (the manifest's `bins: [gh]`). gh is never
# looked up on PATH: Claude workers can write the dirs it is usually installed in.
GH_ENV = "RESONANT_BIN_GH"
GH_TIMEOUT_S = 30
CONFIG_PREFIX = "resonant-gh-"
# A config dir this old was left by a killed run. It must stay well above the longest tool
# timeout in resonant.yaml (300 s), or a slow concurrent run could lose its dir.
STALE_CONFIG_S = 3600
SEARCH_LIMIT = 50  # PRs per search
RUN_LIMIT = 20  # workflow runs per PR branch
MAX_EVENTS = 100  # the process runner keeps at most 100 events per run
TEXT_MAX = 300  # notify clips at 300 chars
TITLE_MAX = 80
CHECKS_MAX = 30

FAILED = frozenset({"failure", "timed_out", "startup_failure"})
SUCCEEDED = frozenset({"success"})
# Check-run conclusions and status-context states that count as red, in GraphQL's casing.
_RED = frozenset({"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "ACTION_REQUIRED", "ERROR"})
_PENDING_STATES = frozenset({"PENDING", "EXPECTED"})

_REPO = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_AUTHOR = re.compile(r"^(@me|[A-Za-z0-9](?:[A-Za-z0-9-]{0,38}))$")

_ROLLUP = """statusCheckRollup { state contexts(first: 50) { nodes { __typename
  ... on CheckRun { name status conclusion startedAt completedAt }
  ... on StatusContext { context state createdAt } } } }"""
_PR_FIELDS = f"""... on PullRequest {{ number title url isDraft createdAt updatedAt
  headRefName headRefOid reviewDecision repository {{ nameWithOwner }}
  commits(last: 1) {{ nodes {{ commit {{ {_ROLLUP} }} }} }} }}"""
SEARCH_QUERY = f"""query($mine: String!, $review: String!) {{
  mine: search(query: $mine, type: ISSUE, first: {SEARCH_LIMIT}) {{
    issueCount nodes {{ {_PR_FIELDS} }} }}
  review: search(query: $review, type: ISSUE, first: {SEARCH_LIMIT}) {{
    issueCount nodes {{ {_PR_FIELDS} }} }}
}}"""
CLOSED_LIMIT = 20  # recently closed PRs per watch
CLOSED_DAYS = 2  # how far back watch looks for merged and closed PRs
CLOSED_QUERY = f"""query($q: String!) {{
  closed: search(query: $q, type: ISSUE, first: {CLOSED_LIMIT}) {{ nodes {{
    ... on PullRequest {{ number title url merged mergedAt closedAt
      repository {{ nameWithOwner }} }} }} }}
}}"""
PR_VIEW_FIELDS = (
    "number,title,url,state,isDraft,author,headRefName,headRefOid,baseRefName,"
    "reviewDecision,mergeable,statusCheckRollup,additions,deletions,changedFiles,"
    "createdAt,updatedAt"
)
RECENT_LIMIT = 15  # recent_prs
RECENT_FIELDS = "number,title,url,state,isDraft,author,createdAt,mergedAt,closedAt"
RUN_FIELDS = "databaseId,workflowName,status,conclusion,headSha,createdAt"
MERGE_VIEW_FIELDS = (
    "number,title,url,state,isDraft,headRefName,headRefOid,mergeable,mergeStateStatus,"
    "statusCheckRollup"
)
# Merge states gh can't merge without --admin (which this extension never passes) or a fix.
_UNMERGEABLE_STATES = {
    "BLOCKED": "blocked by branch protection (required reviews or checks)",
    "BEHIND": "behind its base branch; update it first",
    "DIRTY": "in conflict with its base branch",
    "DRAFT": "a draft",
    "UNKNOWN": "still being checked for mergeability by GitHub; try again in a minute",
}
_SHA = re.compile(r"^[0-9a-f]{7,64}$")
# A fine-grained token missing a scope gets this from GitHub (REST says it with a 403).
_DENIED = ("resource not accessible by",)


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


# --- secrets (the SDK's secret.py, inlined: this file must stay a single file) ---------


_secrets: dict[str, str] | None = None


def secret(name: str) -> str:
    """A declared secret from the pipe at ``RESONANT_SECRETS_FD``, read once and cached.
    "" when it was not delivered. Secrets are never in the environment or argv."""
    global _secrets
    if _secrets is None:
        _secrets = {}
        fd_text = os.environ.pop("RESONANT_SECRETS_FD", "")
        if fd_text.isdigit():
            try:
                with os.fdopen(int(fd_text), "rb") as f:
                    parsed = json.loads(f.read(65537)[:65536])
                _secrets = {k: v for k, v in _d(parsed).items() if isinstance(v, str)}
            except (OSError, ValueError):
                pass
    return _secrets.get(name, "")


def _sweep_stale(base: Path) -> None:
    """Remove ``resonant-gh-*`` dirs left by killed runs. Only a real directory (lstat: a
    symlink is never followed), owned by us, mode 0700 and old enough; anything else stays."""
    try:
        entries = list(base.iterdir())
    except OSError:
        return
    for entry in entries:
        try:
            st = entry.lstat()
            if (
                entry.name.startswith(CONFIG_PREFIX)
                and stat.S_ISDIR(st.st_mode)
                and st.st_uid == os.getuid()
                and stat.S_IMODE(st.st_mode) == 0o700
                and time.time() - st.st_mtime > STALE_CONFIG_S
            ):
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            pass


class Gh:
    """Runs ``gh`` by absolute path with a minimal env and no token in it or in argv: ``gh``
    reads the token from ``hosts.yml`` in a private config dir (keyring off), removed at exit."""

    def __init__(self) -> None:
        token = secret("token")
        if not token:
            raise Failed(
                "secret github/token is missing; on the Mini: resonant secret set github/token"
            )
        if "\n" in token or "\r" in token or '"' in token or "\\" in token:
            raise Failed("secret github/token has characters a GitHub token never has")
        pinned = os.environ.get(GH_ENV, "")
        if not (os.path.isabs(pinned) and os.path.isfile(pinned) and os.access(pinned, os.X_OK)):
            raise Failed(
                f"gh isn't pinned ({GH_ENV} is unset or not a program);"
                " on the Mini: resonant ext update github"
            )
        self.path = pinned
        self.token = token
        self.env = {
            "PATH": os.environ.get("PATH") or DEFAULT_PATH,
            "HOME": str(data_dir()),
            "LANG": "en_US.UTF-8",
            "GH_CONFIG_DIR": self._config_dir(token),
            "GH_PROMPT_DISABLED": "1",
            "GH_NO_UPDATE_NOTIFIER": "1",
            "GH_SPINNER_DISABLED": "1",
            "NO_COLOR": "1",
        }
        if tmp := os.environ.get("TMPDIR"):
            self.env["TMPDIR"] = tmp

    @staticmethod
    def _config_dir(token: str) -> str:
        """A fresh 0700 dir (mkdtemp) with a 0600 hosts.yml holding the token as a plaintext
        ``oauth_token`` (gh uses the config file when no keyring entry exists). Removed at exit."""
        base = data_dir() / "tmp"
        base.mkdir(mode=0o700, parents=True, exist_ok=True)
        _sweep_stale(base)
        directory = Path(tempfile.mkdtemp(prefix=CONFIG_PREFIX, dir=base))
        atexit.register(shutil.rmtree, directory, ignore_errors=True)
        for sig in (signal.SIGTERM, signal.SIGINT):  # Resonant ends a run with SIGTERM first
            signal.signal(sig, lambda *_: sys.exit(1))
        fd = os.open(directory / "hosts.yml", os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f'github.com:\n    oauth_token: "{token}"\n    git_protocol: https\n')
        return str(directory)

    def json(self, *args: str) -> Any:
        out = self.text(*args)
        try:
            return json.loads(out)
        except ValueError:
            raise Failed(f"gh {args[0]} returned invalid JSON") from None

    def text(self, *args: str, need: str = "") -> str:
        """``gh``'s stdout. ``need`` names the token permission a write call needs, so a
        permission error reads as what to widen rather than gh's raw HTTP text."""
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
            if need and any(d in tail.lower() for d in _DENIED):
                raise Failed(
                    f"the token can't do this: it needs {need} (see the github extension "
                    "README); nothing was changed"
                )
            raise Failed(f"gh {args[0]} failed (exit {proc.returncode}): {tail}")
        return proc.stdout

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


def active_at(updated_at: str, contexts: list[Any]) -> str:
    """When the PR last moved: its ``updatedAt`` (commits, comments, reviews) or its latest
    check start, finish or status, whichever is later. ISO 8601 UTC strings compare as text."""
    times = [updated_at]
    for c in map(_d, contexts):
        times += [_s(c.get(k)) for k in ("startedAt", "completedAt", "createdAt")]
    return max((t for t in times if t), default="")


def shape_pr(node: dict[str, Any]) -> dict[str, Any]:
    """A search node as the compact PR dict ``prs``, ``watch`` and ``digest`` share."""
    commits = _l(_d(node.get("commits")).get("nodes"))
    rollup = _d(_d(_d(commits[-1] if commits else {}).get("commit")).get("statusCheckRollup"))
    contexts = _l(_d(rollup.get("contexts")).get("nodes"))
    ci, failing, _ = checks_of(contexts)
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
        "active_at": active_at(_s(node.get("updatedAt")), contexts),
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
    summary = summary_line(mine, review)
    return {"summary": summary, "prs": mine, "review_requested": review, "truncated": truncated}


def cmd_recent_prs(gh: Gh, repo: str) -> dict[str, Any]:
    """The newest ``RECENT_LIMIT`` PRs in ``repo`` (any state), newest first: what changed
    lately, which ``prs`` (open only) and ``pr`` (needs a number) can't answer."""
    if not _REPO.match(repo):
        raise Failed("repo must look like owner/name")
    raw = gh.json(
        "pr", "list", "--repo", repo, "--state", "all", "--limit", str(RECENT_LIMIT),
        "--json", RECENT_FIELDS,
    )  # fmt: skip
    prs: list[dict[str, Any]] = []
    for v in (_d(x) for x in _l(raw)):
        if not isinstance(v.get("number"), int) or isinstance(v.get("number"), bool):
            continue
        prs.append(
            {
                "number": v["number"],
                "title": clip(_s(v.get("title")), TITLE_MAX),
                "url": _s(v.get("url")),
                "state": _s(v.get("state")).lower(),
                "draft": v.get("isDraft") is True,
                "author": _s(_d(v.get("author")).get("login")),
                "created_at": _s(v.get("createdAt")),
                "merged_at": _s(v.get("mergedAt")) or None,
                "closed_at": _s(v.get("closedAt")) or None,
            }
        )
    prs.sort(key=lambda p: p["number"], reverse=True)
    merged = [p for p in prs if p["state"] == "merged"]
    latest = f"; latest merged #{merged[0]['number']} {merged[0]['title']}" if merged else ""
    return {"repo": repo, "summary": f"{len(prs)} recent PRs{latest}", "prs": prs}


def check_target(repo: str, number: int) -> None:
    if not _REPO.match(repo):
        raise Failed("repo must look like owner/name")
    if number <= 0:
        raise Failed("number must be a positive integer")


def cmd_pr(gh: Gh, repo: str, number: int) -> dict[str, Any]:
    check_target(repo, number)
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


def pr_data(pr: dict[str, Any]) -> dict[str, Any]:
    return {"repo": pr["repo"], "number": pr["number"], "title": pr["title"], "url": pr["url"]}


def active_events(mine: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``pr_active`` for each open PR of the owner's, once per moment it last moved (the key
    holds ``active_at``): Resonant's hourly rollup reads them for ``Still working on`` and
    to flag one that has gone quiet. Never texted by default."""
    out: list[dict[str, Any]] = []
    for pr in mine:
        if not pr["active_at"]:
            continue
        data = {**pr_data(pr), "active_at": pr["active_at"], "draft": pr["draft"]}
        key = f"{pr['repo']}#{pr['number']}:active:{pr['active_at']}"
        out.append(event("pr_active", f"{ref(pr)} {pr['title']}", key, data))
    return out


def closed_events(nodes: list[Any]) -> list[dict[str, Any]]:
    """``pr_merged`` (key ``<repo>#<n>:merged``) or ``pr_closed`` (keyed by when, so a PR
    reopened and closed again says it again) for the owner's recently closed PRs."""
    out: list[dict[str, Any]] = []
    for node in map(_d, nodes):
        number = node.get("number")
        repo = _s(_d(node.get("repository")).get("nameWithOwner"))
        if not isinstance(number, int) or not _REPO.match(repo):
            continue
        pr = {"repo": repo, "number": number, "title": clip(_s(node.get("title")), TITLE_MAX),
              "url": _s(node.get("url"))}  # fmt: skip
        base = f"{repo}#{number}"
        if node.get("merged") is True:
            data = {**pr_data(pr), "merged_at": _s(node.get("mergedAt"))}
            out.append(event("pr_merged", f"merged {ref(pr)} {pr['title']}", f"{base}:merged",
                             data))  # fmt: skip
        else:
            closed_at = _s(node.get("closedAt"))
            data = {**pr_data(pr), "closed_at": closed_at}
            out.append(event("pr_closed", f"closed {ref(pr)} {pr['title']}",
                             f"{base}:closed:{closed_at}", data))  # fmt: skip
    return out


def search_closed(gh: Gh) -> list[Any]:
    """The owner's PRs closed (merged or not) in the last ``CLOSED_DAYS`` days."""
    author, repos = load_config()
    since = time.strftime("%Y-%m-%d", time.gmtime(time.time() - CLOSED_DAYS * 86400))
    scope = " ".join(
        ["is:pr", "is:closed", "archived:false", f"author:{author}", f"closed:>={since}",
         *(f"repo:{r}" for r in repos)]
    )  # fmt: skip
    data = _d(gh.json("api", "graphql", "-f", f"query={CLOSED_QUERY}", "-f", f"q={scope}"))
    return _l(_d(_d(data.get("data")).get("closed")).get("nodes"))


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
        events.append(event("review_requested", text, key, pr_data(pr)))
    closed = closed_events(search_closed(gh))
    # A PR merged between the two searches is in both: it is closed, never active again.
    done = {(_d(e["data"]).get("repo"), _d(e["data"]).get("number")) for e in closed}
    events += closed
    events += active_events([p for p in mine if (p["repo"], p["number"]) not in done])
    summary = f"{len(mine)} PRs watched, {len(events)} events"
    return emit({"summary": summary, "prs": len(mine), "events_found": len(events)}, events)


def summary_line(mine: list[dict[str, Any]], review: list[dict[str, Any]]) -> str:
    """``3 open PRs, 1 failing CI, 2 awaiting your review``."""
    failing = sum(1 for p in mine if p["ci"] == "failing")
    return f"{len(mine)} open PRs, {failing} failing CI, {len(review)} awaiting your review"


def report_event(
    key: str, title: str, state: dict[str, Any], *, urgency: str = "low", quiet: bool = False
) -> dict[str, Any]:
    """A pulse item (what ``resonant_sdk.emit.report`` sends), built inline: this file can't
    import the SDK. Every call gets a unique dedupe key; Resonant decides whether it's news
    from the fingerprint of ``state``. ``quiet``: nothing to say, so Resonant closes the
    item without a word (left out of the briefing and the pulse)."""
    data = {
        "key": key,
        "urgency": urgency,
        "state": state,
        "clear": False,
        "ttl": "30h",
        "quiet": quiet,
    }
    return event("item", title, f"item:{key}:{time.time_ns():x}", data)


def cmd_digest(gh: Gh) -> dict[str, Any]:
    """Reports ``github/summary`` (low: the 08:00 briefing's Updates section). The state is
    the counts, so an unchanged day is not news. All zero: reported ``quiet``, so the
    briefing leaves it out instead of saying ``0 open PRs, 0 failing CI, ...``."""
    mine, review, _ = search(gh)
    line = summary_line(mine, review)
    state = {
        "open": len(mine),
        "failing": sum(1 for p in mine if p["ci"] == "failing"),
        "review_requested": len(review),
    }
    quiet = not any(state.values())
    return emit({"summary": line}, [report_event("github/summary", line, state, quiet=quiet)])


def cmd_health(gh: Gh) -> dict[str, Any]:
    user = _d(gh.json("api", "user"))
    if not _s(user.get("login")):
        raise Failed("gh api user returned no login")
    return {"ok": True, "login": _s(user.get("login"))}


# --- writes ----------------------------------------------------------------------------


def rerun_targets(head_sha: str, runs: list[Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """``(runs to rerun, workflows already running)`` on ``head_sha``: per workflow, its
    latest run on the head, if that run failed. A workflow whose latest run is queued or in
    progress (e.g. already re-running) is left alone."""
    latest: dict[str, dict[str, Any]] = {}
    for r in sorted(
        (_d(r) for r in runs if isinstance(_d(r).get("databaseId"), int)),
        key=lambda r: (_s(r.get("createdAt")), r["databaseId"]),
    ):
        if _s(r.get("headSha")) == head_sha:
            latest[_s(r.get("workflowName"))] = r
    failed = [
        r
        for _, r in sorted(latest.items())
        if _s(r.get("status")) == "completed" and _s(r.get("conclusion")) in FAILED
    ]
    running = [w for w, r in sorted(latest.items()) if _s(r.get("status")) != "completed"]
    return failed, running


def cmd_rerun_failed(gh: Gh, repo: str, number: int) -> dict[str, Any]:
    """Re-runs the failed jobs of every failed workflow run on the PR's current head."""
    check_target(repo, number)
    v = _d(gh.json("pr", "view", str(number), "--repo", repo, "--json", MERGE_VIEW_FIELDS))
    pr = {"repo": repo, "number": number}
    branch, head = _s(v.get("headRefName")), _s(v.get("headRefOid"))
    if _s(v.get("state")).upper() != "OPEN":
        raise Failed(f"{ref(pr)} is {_s(v.get('state')).lower() or 'not open'}; nothing to rerun")
    if not branch or not _SHA.match(head):
        raise Failed(f"gh pr view returned no head for {ref(pr)}")
    runs = gh.json(
        "run",
        "list",
        "--repo",
        repo,
        "--branch",
        branch,
        "--json",
        RUN_FIELDS,
        "--limit",
        str(RUN_LIMIT),
    )
    failed, running = rerun_targets(head, _l(runs))
    if not failed:
        why = f"{', '.join(running)} still running" if running else "no failed runs"
        raise Failed(f"nothing to rerun on {ref(pr)} ({head[:7]}): {why}")
    rerun: list[dict[str, Any]] = []
    errors: list[str] = []
    for r in failed:
        try:
            gh.text(
                "run",
                "rerun",
                str(r["databaseId"]),
                "--repo",
                repo,
                "--failed",
                need="Actions: write",
            )
        except Failed as e:
            if not rerun:
                raise  # nothing has changed yet: fail the whole call
            errors.append(f"{clip(_s(r.get('workflowName')), 60)}: {e}")
            continue
        rerun.append({"workflow": clip(_s(r.get("workflowName")), 60), "run_id": r["databaseId"]})
    names = ", ".join(r["workflow"] for r in rerun)
    summary = f"re-running failed jobs on {ref(pr)} ({head[:7]}): {names}"
    if errors:
        summary += f"; {len(errors)} could not be re-run"
    return {
        "summary": clip(summary, TEXT_MAX),
        "repo": repo,
        "number": number,
        "head_sha": head,
        "rerun": rerun,
        "errors": errors,
        "already_running": running,
    }


def cmd_merge_pr(gh: Gh, repo: str, number: int, head_sha: str) -> dict[str, Any]:
    """Squash-merges the PR, only if its head is still ``head_sha`` (what was approved),
    CI is green and GitHub says it's mergeable. Never ``--admin``."""
    check_target(repo, number)
    want = head_sha.lower()
    if not _SHA.match(want):
        raise Failed("head_sha must be the PR's head commit SHA (at least 7 hex characters)")
    pr = {"repo": repo, "number": number}
    v = _d(gh.json("pr", "view", str(number), "--repo", repo, "--json", MERGE_VIEW_FIELDS))
    state = _s(v.get("state")).upper()
    head = _s(v.get("headRefOid")).lower()
    if state == "MERGED":
        raise Failed(f"{ref(pr)} is already merged")
    if state != "OPEN":
        raise Failed(f"{ref(pr)} is {state.lower() or 'not open'}; not merging")
    if not _SHA.match(head):
        raise Failed(f"gh pr view returned no head for {ref(pr)}")
    if not head.startswith(want):
        raise Failed(
            f"{ref(pr)} has new commits since it was approved (head {head[:7]}, approved "
            f"{want[:7]}); not merging"
        )
    if v.get("isDraft") is True:
        raise Failed(f"{ref(pr)} is a draft; not merging")
    ci, failing, _ = checks_of(_l(v.get("statusCheckRollup")))
    if ci == "failing":
        raise Failed(f"{ref(pr)} has failing checks ({', '.join(failing[:5])}); not merging")
    if ci == "pending":
        raise Failed(f"{ref(pr)} still has checks running; not merging")
    merge_state = _s(v.get("mergeStateStatus")).upper()
    if merge_state in _UNMERGEABLE_STATES:
        raise Failed(f"{ref(pr)} is {_UNMERGEABLE_STATES[merge_state]}; not merging")
    if _s(v.get("mergeable")).upper() != "MERGEABLE":
        raise Failed(f"{ref(pr)} is not mergeable ({_s(v.get('mergeable')).lower()})")
    # --match-head-commit makes GitHub itself refuse if the head moved since this check.
    gh.text(
        "pr",
        "merge",
        str(number),
        "--repo",
        repo,
        "--squash",
        "--match-head-commit",
        head,
        need="Contents: write and Pull requests: write",
    )
    return {
        "summary": f"merged {ref(pr)} (squash) at {head[:7]}",
        "repo": repo,
        "number": number,
        "head_sha": head,
        "method": "squash",
        "url": _s(v.get("url")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="github")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("prs", "watch", "digest", "health"):
        sub.add_parser(name)
    sub.add_parser("recent_prs").add_argument("--repo", required=True)
    for name in ("pr", "rerun_failed", "merge_pr"):
        p = sub.add_parser(name)
        p.add_argument("--repo", required=True)
        p.add_argument("--number", required=True, type=int)
        if name == "merge_pr":
            p.add_argument("--head-sha", required=True)
    args = parser.parse_args(argv)
    try:
        gh = Gh()
        if args.cmd == "pr":
            out = cmd_pr(gh, args.repo, args.number)
        elif args.cmd == "recent_prs":
            out = cmd_recent_prs(gh, args.repo)
        elif args.cmd == "rerun_failed":
            out = cmd_rerun_failed(gh, args.repo, args.number)
        elif args.cmd == "merge_pr":
            out = cmd_merge_pr(gh, args.repo, args.number, args.head_sha)
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
