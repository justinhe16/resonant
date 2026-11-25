"""A run's milestones in the conversation that asked (PER-323): progress like a teammate's.

PER-319 says when a run starts and when it fails; the run's end text (``ClaudeRunner.
_send_reply``, voiced by PER-273) is its final result. In between, a run posts a short,
code-built line at its meaningful points only:

- ``R5 opened PR #12: https://github.com/o/r/pull/12`` (``pr_opened``)
- ``R5: CI failed on PR #12; fixing it.`` (``ci_failed``)
- ``R5: the review asked for changes on PR #12; working on them.`` (``changes_requested``)
- ``R5 merged PR #12.`` (``merged``)
- ``R5 deployed to staging.`` (``deployed``; production too, never an unknown target)

Where they come from. The worker's own tool calls and their results in the session's
stream-json (:class:`Tracker`): a call is classified when the assistant makes it (``gh pr
create``, ``gh pr merge``, ``gh pr checks``/``gh run watch|view``, ``gh pr view``/``gh api …
reviews``, a deploy command ``side_effects`` knows, and the GitHub MCP tools of the same
shapes), and it becomes a milestone only when its result comes back and says so: a PR link
for a create, no error for a merge or a deploy, a failing check for CI, ``CHANGES_REQUESTED``
for a review. Nothing from the result reaches a line but a PR number and a ``github.com``
pull link matched by a strict pattern; the rest of each line is fixed text.

Each kind is said at most once per run (``deployed`` once per target). The record lives in
``kv`` (``run.milestones:<code>``) and every send carries PER-319's dedupe key
(``status:<task>:milestone:<kind>``), so a restart or a retried send never repeats one.
Quiet runs stay quiet: nothing is posted before the session has worked ``QUIET_S`` (a
minute); what was seen by then is posted at that point, and a run that ends sooner folds its
milestones into the final result (:func:`fold`). ``runs.milestones: off`` (the default for an
iMessage-only instance) records them but posts none.

The record also feeds the final result: a deploy to staging (and none to production) adds
the suggested next step (:func:`next_step`).

Nothing here logs a command, a result or a message body.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, cast

from resonant.runners.side_effects import classify_command, commands, tool_calls, unwrap
from resonant.store.db import kv_get, kv_set

Kind = Literal["pr_opened", "ci_failed", "changes_requested", "merged", "deployed"]
KINDS: tuple[Kind, ...] = ("pr_opened", "ci_failed", "changes_requested", "merged", "deployed")
Call = Literal["pr_create", "pr_merge", "ci", "review", "deploy"]
Mode = Literal["thread", "off"]

QUIET_S = 60.0  # session seconds before any milestone is posted
KV_PREFIX = "run.milestones:"
MAX_CALLS = 200  # open tool calls remembered per session (a session that never answers one)
NEXT_PROD = "Staging has it now. Want me to ship it to prod? That needs an approval."

_PR_URL = re.compile(
    r"https://github\.com/[A-Za-z0-9._-]{1,100}/[A-Za-z0-9._-]{1,100}/pull/(\d{1,7})"
)
_PR_HASH = re.compile(r"(?<![A-Za-z0-9])#(\d{1,7})(?![0-9])")
_PR_ARG = re.compile(r"#?(\d{1,7})")
_FAIL = re.compile(r"\bfail(?:s|ed|ing|ure)?\b", re.IGNORECASE)
_FAILING = re.compile(r"\b[1-9]\d* failing\b|\bwere not successful\b|\bconclusion\W+failure\b",
                      re.IGNORECASE)  # fmt: skip
_CHANGES = re.compile(r"CHANGES_REQUESTED|\bchanges requested\b|\brequested changes\b",
                      re.IGNORECASE)  # fmt: skip
_STAGING = re.compile(r"(?:^|[^a-z0-9])(?:staging|stage|stg)(?:[^a-z0-9]|$)", re.IGNORECASE)
_PROD = re.compile(r"(?:^|[^a-z0-9])(?:prod|production)(?:[^a-z0-9]|$)", re.IGNORECASE)
_GH_VALUE = frozenset({"-R", "--repo", "--hostname"})
_MCP_CREATE = re.compile(r"create_(?:pull_request|pr)(?:$|_)", re.IGNORECASE)
_MCP_MERGE = re.compile(r"merge_(?:pull_request|pr)(?:$|_)", re.IGNORECASE)
_MCP_REVIEW = re.compile(r"review", re.IGNORECASE)
_MCP_CHECKS = re.compile(r"(?:check_runs|status_checks|workflow_run|ci_status)", re.IGNORECASE)


@dataclass(frozen=True)
class Milestone:
    """One milestone. ``pr``/``url``: the PR it's about, when the call or result named it.
    ``env``: a deploy's target (``staging`` or ``production``)."""

    kind: Kind
    pr: int | None = None
    url: str | None = None
    env: str | None = None

    @property
    def key(self) -> str:
        """Once per run: the kind, and a deploy's target."""
        return f"{self.kind}:{self.env}" if self.kind == "deployed" and self.env else self.kind


@dataclass(frozen=True)
class _Open:
    call: Call
    pr: int | None = None
    env: str | None = None


# --- classifying the session's calls -------------------------------------------------------


def _gh_words(argv: Sequence[str]) -> list[str]:
    """``gh``'s positional words (``pr``, ``create``, ``12``), its value options skipped."""
    out: list[str] = []
    skip = False
    for x in argv[1:]:
        if skip:
            skip = False
        elif x in _GH_VALUE:
            skip = True
        elif not x.startswith("-"):
            out.append(x)
    return out


def _pr_of(words: Iterable[str]) -> int | None:
    for w in words:
        m = _PR_ARG.fullmatch(w) or re.search(r"/pull/(\d{1,7})", w)
        if m is not None:
            return int(m[1])
    return None


def _env(text: str) -> str | None:
    if _PROD.search(text) or "--prod" in text:
        return "production"
    if _STAGING.search(text):
        return "staging"
    return None


def classify_bash(command: str) -> _Open | None:
    """What a shell command could show, or None. The first command of a compound line that
    counts wins (``git push && gh pr create`` is a PR create)."""
    for raw in commands(command):
        argv = unwrap(raw)
        if not argv or argv[0].rsplit("/", 1)[-1] != "gh":
            continue
        words = _gh_words(argv)
        group, sub = [*words, "", ""][:2]
        if group == "pr" and sub == "create":
            return _Open("pr_create")
        if group == "pr" and sub == "merge":
            return _Open("pr_merge", _pr_of(words[2:]))
        if (group == "pr" and sub == "checks") or (group == "run" and sub in ("watch", "view")):
            return _Open("ci", _pr_of(words[2:]) if group == "pr" else None)
        if group == "pr" and sub in ("view", "status"):
            return _Open("review", _pr_of(words[2:]))
        if group == "api" and "reviews" in sub:
            return _Open("review", _pr_of([sub]))
    for effect in classify_command(command):
        if effect.kind != "deploy" or effect.what.startswith(("ran workflow", "pushed image",
                                                             "built and pushed")):  # fmt: skip
            continue
        env = _env(command)
        return _Open("deploy", env=env) if env is not None else None
    return None


def classify_mcp(name: str, args: Mapping[str, Any]) -> _Open | None:
    """A GitHub MCP tool of one of the shapes, or None."""
    if not name.startswith("mcp__"):
        return None
    tool = name.rsplit("__", 1)[-1]
    raw: Any = args.get("pull_number", args.get("pullNumber", args.get("number")))
    pr = raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else None
    if _MCP_CREATE.search(tool):
        return _Open("pr_create")
    if _MCP_MERGE.search(tool):
        return _Open("pr_merge", pr)
    if _MCP_REVIEW.search(tool) and not tool.lower().startswith(("create", "submit", "add")):
        return _Open("review", pr)
    if _MCP_CHECKS.search(tool):
        return _Open("ci", pr)
    return None


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in cast(list[Any], content):
        if isinstance(block, dict):
            text = cast(dict[str, Any], block).get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def _results(message: Mapping[str, Any]) -> list[tuple[str, bool, str]]:
    """``(tool_use_id, is_error, text)`` of each ``tool_result`` in a ``user`` message."""
    if message.get("type") != "user":
        return []
    inner: Any = message.get("message")
    content: Any = cast(dict[str, Any], inner).get("content") if isinstance(inner, dict) else None
    if not isinstance(content, list):
        return []
    out: list[tuple[str, bool, str]] = []
    for item in cast(list[Any], content):
        if not isinstance(item, dict):
            continue
        block = cast(dict[str, Any], item)
        tid = block.get("tool_use_id")
        if block.get("type") == "tool_result" and isinstance(tid, str):
            out.append((tid, block.get("is_error") is True, _result_text(block.get("content"))))
    return out


def judge(call: _Open, is_error: bool, text: str) -> Milestone | None:
    """What a call's result shows, or None."""
    match call.call:
        case "pr_create":
            m = _PR_URL.search(text)
            if is_error or m is None:
                return None
            return Milestone("pr_opened", int(m[1]), m[0])
        case "pr_merge":
            if is_error:
                return None
            m = _PR_URL.search(text) or _PR_HASH.search(text)
            return Milestone("merged", call.pr or (int(m[1]) if m is not None else None))
        case "ci":
            if (is_error and _FAIL.search(text)) or _FAILING.search(text):
                return Milestone("ci_failed", call.pr)
            return None
        case "review":
            if not is_error and _CHANGES.search(text):
                return Milestone("changes_requested", call.pr)
            return None
        case "deploy":
            return None if is_error else Milestone("deployed", env=call.env)


class Tracker:
    """One session's open tool calls, and the milestones their results show. Pure: never
    raises on odd input, never keeps a result."""

    def __init__(self) -> None:
        self._open: dict[str, _Open] = {}

    def calls(self, message: Mapping[str, Any]) -> None:
        """Note the calls in an ``assistant`` message that could show a milestone."""
        for tid, name, args in tool_calls(message):
            if not tid:
                continue
            command: Any = args.get("command")
            found = (
                classify_bash(command)
                if name == "Bash" and isinstance(command, str)
                else classify_mcp(name, args)
            )
            if found is not None:
                if len(self._open) >= MAX_CALLS:
                    self._open.pop(next(iter(self._open)))
                self._open[tid] = found

    def results(self, message: Mapping[str, Any]) -> list[Milestone]:
        """The milestones a ``user`` message's tool results show."""
        out: list[Milestone] = []
        for tid, is_error, text in _results(message):
            call = self._open.pop(tid, None)
            if call is not None and (m := judge(call, is_error, text)) is not None:
                out.append(m)
        return out


# --- the record ------------------------------------------------------------------------------


def _key(code: str) -> str:
    return KV_PREFIX + code


def _load(conn: sqlite3.Connection, code: str) -> dict[str, dict[str, Any]]:
    raw: Any = kv_get(conn, _key(code), {})
    if not isinstance(raw, dict):
        return {}
    return {
        str(k): cast(dict[str, Any], v)
        for k, v in cast(dict[Any, Any], raw).items()
        if isinstance(v, dict)
    }


def _from(row: Mapping[str, Any]) -> Milestone | None:
    kind, pr, url, env = row.get("kind"), row.get("pr"), row.get("url"), row.get("env")
    if kind not in KINDS:
        return None
    return Milestone(
        kind,
        pr if isinstance(pr, int) and not isinstance(pr, bool) else None,
        url if isinstance(url, str) else None,
        env if isinstance(env, str) else None,
    )


def record(conn: sqlite3.Connection, code: str, m: Milestone) -> bool:
    """Keep ``m`` for run ``code`` unless its key is there already. True when it is new. A CI
    failure or review without a PR number takes the run's opened PR's."""
    seen = _load(conn, code)
    if m.key in seen:
        return False
    pr = m.pr
    if pr is None and m.kind in ("ci_failed", "changes_requested", "merged"):
        opened = seen.get("pr_opened")
        pr_any: Any = opened.get("pr") if opened is not None else None
        pr = pr_any if isinstance(pr_any, int) else None
    seen[m.key] = {"kind": m.kind, "pr": pr, "url": m.url, "env": m.env, "state": "new"}
    kv_set(conn, _key(code), seen)
    return True


def pending(conn: sqlite3.Connection, code: str) -> list[tuple[str, Milestone]]:
    """The run's milestones not yet posted nor folded, in the order they were seen."""
    out: list[tuple[str, Milestone]] = []
    for key, row in _load(conn, code).items():
        m = _from(row)
        if m is not None and row.get("state") == "new":
            out.append((key, m))
    return out


def mark(conn: sqlite3.Connection, code: str, key: str, state: str) -> None:
    """Set one milestone's state: ``posted`` or ``folded``."""
    seen = _load(conn, code)
    if key in seen:
        seen[key]["state"] = state
        kv_set(conn, _key(code), seen)


def fold(conn: sqlite3.Connection, code: str) -> None:
    """The run ended: what wasn't posted is said by the final result instead."""
    seen = _load(conn, code)
    changed = False
    for row in seen.values():
        if row.get("state") == "new":
            row["state"] = "folded"
            changed = True
    if changed:
        kv_set(conn, _key(code), seen)


def deployed_envs(conn: sqlite3.Connection, code: str) -> list[str]:
    """The targets run ``code`` deployed to (``staging``, ``production``)."""
    out: list[str] = []
    for row in _load(conn, code).values():
        m = _from(row)
        if m is not None and m.kind == "deployed" and m.env and m.env not in out:
            out.append(m.env)
    return out


def next_step(envs: Sequence[str]) -> str | None:
    """The final result's suggested next step: staging has it and production doesn't."""
    return NEXT_PROD if "staging" in envs and "production" not in envs else None


# --- the lines -------------------------------------------------------------------------------


def line(code: str, m: Milestone) -> str:
    """The code-built line for one milestone."""
    on = f" on PR #{m.pr}" if m.pr is not None else ""
    match m.kind:
        case "pr_opened":
            if m.pr is not None and m.url:
                return f"{code} opened PR #{m.pr}: {m.url}"
            return f"{code} opened a PR."
        case "ci_failed":
            return f"{code}: CI failed{on}; fixing it."
        case "changes_requested":
            return f"{code}: the review asked for changes{on}; working on them."
        case "merged":
            return f"{code} merged PR #{m.pr}." if m.pr is not None else f"{code} merged its PR."
        case "deployed":
            return f"{code} deployed to {m.env or 'its target'}."


def dedupe_suffix(key: str) -> str:
    """The state part of PER-319's status key for one milestone (``milestone:pr_opened``)."""
    return f"milestone:{key}"
