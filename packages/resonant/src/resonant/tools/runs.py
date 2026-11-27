"""Claude runner tools: ``start_run`` (L3), ``list_runs`` (L0), ``cancel_run`` (L3),
``continue_run`` (L3) and ``merge_run`` (L2). The slash commands ``/run [<repo>] <ask>
[--merge]``, ``/runs``, ``/cancel``, ``/continue`` and ``/merge`` and the ``run_project`` intent
reach the same tools (the repo tools are ``tools/repos.py``).

They are global builtins, so the gate lets only principals who can request global tools (the
owner) call them. The handlers call ``ClaudeRunner`` (``components.runners["claude"]``, found
at call time: the runner is built after the tools are registered) and return code-built
``text`` that the local runner's report line and the fast path's render send as is.

- ``start_run{repo?, ask, report?}``: L3, no approval (the sandbox is the wall). ``repo`` is a free
  string (a registry name, ``owner/name`` or a GitHub URL) that the runner's resolver checks;
  the registered names and the extensions that link repos are listed in the spec
  (``specs_for``). Without ``repo`` the run
  works in a scratch dir and ends with Claude's summary, no PR (docs/employee-plan.md D1). A
  run that can't start (not set up, ambiguous or unknown repo, paused) is a normal result with
  ``started: false`` and the reason as ``text``. ``report``: ``digest`` (the default) puts
  the run's end in the hourly digest; ``now`` texts it when it ends (``/run`` passes it, and
  the model does when the owner asks to hear right away; docs/employee-plan.md D5, EM-6).
  Who says it started (PER-319, PER-352): a slash command's reply; a Claude brain turn's
  reply (one line for every run the turn started, ``runners/reply_lines.py``); else, chosen
  by a model, the run itself in its thread (``announced``).
- ``list_runs{code?}``: L0, active runs first. Run codes (``R3``) are not approval codes: the
  model sees them, never an approval code. Free text from Claude (summary, failure) is
  cleaned and clipped. ``known`` (PER-270): the owner was already texted how the run ended
  (its pulse item is told), so it is not news. ``list_tasks`` is unchanged.
- ``run_activity{code?, repo?, minutes?}``: L0 (TR-1). What a run is doing now, from its
  transcript's tail (``ClaudeRunner.run_activity``, ``runners/activity.py``): a code-built
  ``headline``, the step, the last few commands (redacted at write time, cleaned and
  clipped), the last text, the ``did:`` list with detected side effects, and seconds since
  the last event. No ``code``: ``repo``'s latest run, else the only active run, else the most
  recent (several active: "Which run? R4 dori, R6 trading."). ``/runs R4`` carries the same
  ``activity`` on ``list_runs``. No model is involved.
- ``cancel_run{code}``: L3.
- ``continue_run{code}``: L3 (PER-344). A failed run that a limit stopped (the time limit, the
  per-run disk cap, the disk nearly full) and whose work is still on disk resumes its Claude
  session in the same worktree with a fresh budget (``ClaudeRunner.continue_run``); anything
  else is a refusal (``continued: false`` and the reason as ``text``). The owner and approvers
  only: a global builtin with no member grant, and the handler checks the asker again. Chosen
  by the brain, the run says ``Continuing R5 (…)`` in its thread itself (``announced``),
  unless a Claude brain turn chose it: the turn's reply says it (PER-352, as ``start_run``).
  PER-351: the run moves to the calling task's thread and asker: its later updates, end and
  merge go where it was continued, and its old thread gets one pointer. It keeps its
  ``follow_up`` (PER-354), handed on in the new thread when it ends.
- ``merge_run{code}``: L2: ``/merge R3`` is explicit and runs. A model-chosen call follows
  the trust rules (``merge_level``, docs/employee-plan.md D4): it runs without asking when
  ``merge_decision`` is ok and the run flagged no sensitive paths, else it asks a plain yes.
  The approval prompt (``summary_action``) names the PR and the flagged paths (CI config,
  ``.claude/``, ``.mcp.json``, git hooks, CODEOWNERS; dependency manifests and lockfiles are
  not flagged, the merged reply lists them as ``deps changed: …``). A fork-mode repo's run is
  never merged (its upstream's maintainers merge). It is the owner's merge, so CI still
  running is a wait, not a refusal (``queued: true``, "R3: CI is still running; I'll merge
  PR #12 when it's green.", then the merge or why not is reported like an auto-merge's), and
  a repo with no CI at all merges with a warning (``merged_without_ci``, "⚠ merged without
  CI: <repo> has no CI checks"). Red CI still refuses.
- ``start_run{..., merge?}``: ``merge`` when the owner asked to merge it too; the run then
  merges itself once CI is green (same checks, same trust rules).
- ``start_run{..., follow_up?}`` (PER-354): a plain instruction for when the run ends ("if the
  review says LGTM, merge the PR"). When it ends (ready, no changes, done, failed; not
  cancelled; an auto-merge once it is merged or stops) the runner queues one brain ask in the
  run's current thread, as its current requester, tainted, with the instruction and the run's
  end. A continued run keeps it. Chains are bounded: a follow-up ask carries a depth, a run it
  starts or continues gets it, and at ``runs.FOLLOW_UP_DEPTH_MAX`` start_run refuses a
  follow-up and the run queues none.
- ``answer_worker{code, text}`` and ``message_worker{code, text}``: L3, no ask: it's the
  owner talking to their own worker (EM-5). The answer goes to the run's open question (a
  run with none gets it as a message); a message reaches the session at its next
  ``resonant`` call or turn. The router's relay (``router/relay.py``) handles ``R4 …`` and
  most replies without the model; these are for what only the model can place.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from resonant.executor import CallContext, ContextHandler, ToolFailed, ToolHandler
from resonant.gate.codes import clean_external
from resonant.gate.types import Intent
from resonant.store import pulse as pulse_store
from resonant.store import repos as repos_store
from resonant.store import runs as runs_store
from resonant.store import tasks as task_store
from resonant.tools.base import BuiltinContext
from resonant.trust import merge_decision
from resonant_sdk import Effect, Level, ToolSpec

if TYPE_CHECKING:
    from resonant.loop.types import Task
    from resonant.runners.claude import (
        Cancelled,
        Continued,
        Merged,
        MergeQueued,
        Refused,
        Started,
    )
    from resonant.runners.worker_desk import OpenQuestion, Relayed
    from resonant.store.runs import Run

RUNNER = "claude"
Report = Literal["now", "digest"]
REPORTS: tuple[Report, ...] = ("now", "digest")
ASK_MAX = 4000  # ClaudeRunner's MAX_ASK (a test checks they agree)
REPO_MAX = 200
RUNS_MAX = 8  # runs listed
RECENT_REPOS = 20  # registered repos named in the start_run description
TEXT_MAX = 120  # Claude's summary or a failure, as the model and the reply see it
ASK_SHOWN = 60
OWNER_TEXT_MAX = 2000  # an answer or message to a worker
QUESTION_SHOWN = 120
ACTIVITY_MINUTES = 10  # activity.WINDOW_MIN (a test checks they agree)
ACTIVITY_MINUTES_MAX = 24 * 60  # activity.WINDOW_MAX
ACTIVITY_RECENT = 50  # runs searched for a repo's latest

# Run codes: ``R`` and a number. ``R`` is not in the approval alphabet (``gate.codes``), so a run
# code can't be mistaken for, or parsed as, an approval code (``yes R3`` is not a reply).
RUN_CODE = re.compile(r"R(\d{1,9})", re.ASCII | re.IGNORECASE)


class RunnerApi(Protocol):
    """What the tools use of ``ClaudeRunner``."""

    async def setup_missing(self) -> str | None: ...

    async def capability_notes(self) -> list[str]: ...

    async def start_run(
        self,
        repo: str | None,
        ask: str,
        principal: str,
        *,
        merge: bool = False,
        report: Report = "digest",
        channel_ref: dict[str, Any] | None = None,
        requested_by: str | None = None,
        announce: bool = False,
        follow_up: str | None = None,
        follow_up_depth: int = 0,
    ) -> Started | Refused: ...

    def list_runs(self, limit: int = 20) -> list[Run]: ...

    def get_run(self, code: str) -> Run | None: ...

    async def cancel_run(self, code: str, *, reason: str = "cancelled") -> Cancelled | Refused: ...

    async def continue_run(
        self,
        code: str,
        *,
        announce: bool = False,
        channel_ref: dict[str, Any] | None = None,
        requested_by: str | None = None,
        follow_up_depth: int | None = None,
    ) -> Continued | Refused: ...

    async def merge_run(
        self, code: str, *, unattended: bool = False
    ) -> Merged | MergeQueued | Refused: ...

    def open_questions(self) -> list[OpenQuestion]: ...

    def run_activity(self, code: str, minutes: int = ...) -> dict[str, Any] | None: ...

    def answer_question(
        self, code: str, text: str, *, question_id: int | None = None
    ) -> Relayed | Refused: ...

    def deliver_message(self, code: str, text: str) -> Relayed | Refused: ...


def _code_schema() -> dict[str, Any]:
    return {"type": "string", "pattern": "^[Rr][0-9]{1,9}$", "description": "run code like R3"}


START_RUN = ToolSpec(
    name="start_run",
    description=(
        "Spawn a Claude worker. With `repo` (a name, owner/name, a GitHub URL or an extension "
        "that links one) it works in a worktree of that repo and opens a pull request; a repo "
        "the owner can't push to goes through their fork and asks before the upstream PR. "
        "`repo` is optional: leave it out for work not tied to a repo (ops, research, a data "
        "fix) and it runs in a scratch dir and reports back. `ask` is what to do, in the "
        "owner's words. Set `merge` only when the owner also asks to merge it: it then merges "
        "once CI is green, unless a trust rule says to ask. `follow_up`: what you will do "
        "when the run ends (e.g. 'if the review says LGTM, merge the PR', 'after the merge, "
        "reinstall the LaunchAgent'): you get one turn in this thread then, with how it ended."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "maxLength": REPO_MAX},
            "ask": {"type": "string", "minLength": 1, "maxLength": ASK_MAX},
            "merge": {"type": "boolean"},
            "follow_up": {
                "type": "string",
                "minLength": 1,
                "maxLength": runs_store.FOLLOW_UP_MAX,
                "description": (
                    "a step you promised for when the run ends, as an instruction to yourself"
                ),
            },
            "report": {
                "type": "string",
                "enum": list(REPORTS),
                "description": (
                    "now: text the result the moment the run ends (the owner asked to hear "
                    "right away); digest (default): in the hourly digest"
                ),
            },
        },
        "required": ["ask"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=60.0,
    # report (now or digest) only says when the owner hears of it: left out on purpose
    approval_summary='start a run[ on {repo}]: "{ask}"[{merge} (merges if clean)]{~report}',
)
LIST_RUNS = ToolSpec(
    name="list_runs",
    description=(
        "Claude runs, active ones first: code, name (code and repo), status, ask and PR; "
        "known: true means the owner was already told how it ended. "
        "Give `code` for one run. Read only."
    ),
    args_schema={
        "type": "object",
        "properties": {"code": _code_schema()},
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=15.0,
)
RUN_ACTIVITY = ToolSpec(
    name="run_activity",
    description=(
        "What a Claude run is doing right now, from its transcript: the current step, its "
        "last few commands, its last words, side effects seen in its commands and how long "
        "since it last did anything. For 'what's R4 doing', 'status on the trading run', "
        "'is the dori worker stuck'. Give `code` (R4), or `repo` for that repo's latest run, "
        "or neither for the only active run. Read only."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "code": _code_schema(),
            "repo": {"type": "string", "maxLength": REPO_MAX},
            "minutes": {"type": "integer", "minimum": 1, "maximum": ACTIVITY_MINUTES_MAX},
        },
        "additionalProperties": False,
    },
    effect=Effect.READ,
    timeout_s=15.0,
)
CANCEL_RUN = ToolSpec(
    name="cancel_run",
    description="Stop a Claude run that is queued, running or waiting, and discard its work.",
    args_schema={
        "type": "object",
        "properties": {"code": _code_schema()},
        "required": ["code"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=30.0,
    approval_summary="cancel {code}",
)
CONTINUE_RUN = ToolSpec(
    name="continue_run",
    description=(
        "Continue a failed Claude run that hit the time limit or the disk limit, from the work "
        "it kept: the same worker session and branch, with a fresh time budget; it finishes "
        "the original ask (commit, push, PR). For 'continue R12', 'pick R12 back up'. Refuses "
        "a run that failed for another reason or whose work was cleaned up."
    ),
    args_schema={
        "type": "object",
        "properties": {"code": _code_schema()},
        "required": ["code"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=30.0,
    approval_summary="continue {code}",
)
MERGE_RUN = ToolSpec(
    name="merge_run",
    description=(
        "Squash-merge the pull request of a finished Claude run (needs green CI). Asks the "
        "owner first when a trust rule says to or the run changed sensitive paths."
    ),
    args_schema={
        "type": "object",
        "properties": {"code": _code_schema()},
        "required": ["code"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=120.0,
)


def _text_schema() -> dict[str, Any]:
    return {"type": "string", "minLength": 1, "maxLength": OWNER_TEXT_MAX}


ANSWER_WORKER = ToolSpec(
    name="answer_worker",
    description=(
        "Answer a Claude run's open question (the owner got 'R4 asks: ...'). `text` is the "
        "owner's answer, in their words. list_runs shows each run's open question."
    ),
    args_schema={
        "type": "object",
        "properties": {"code": _code_schema(), "text": _text_schema()},
        "required": ["code", "text"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=15.0,
    approval_summary='answer {code}: "{text}"',
)
MESSAGE_WORKER = ToolSpec(
    name="message_worker",
    description=(
        "Pass the owner's message to a running Claude run, e.g. 'tell R4 to also update the "
        "README'. `text` is what to tell it, in the owner's words. It sees it at its next step."
    ),
    args_schema={
        "type": "object",
        "properties": {"code": _code_schema(), "text": _text_schema()},
        "required": ["code", "text"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=15.0,
    approval_summary='send {code} a message: "{text}"',
)

SPECS: tuple[ToolSpec, ...] = (
    START_RUN, LIST_RUNS, CANCEL_RUN, MERGE_RUN, ANSWER_WORKER, MESSAGE_WORKER, RUN_ACTIVITY,
    CONTINUE_RUN,
)  # fmt: skip


def specs_for(ctx: BuiltinContext) -> tuple[ToolSpec, ...]:
    """``SPECS`` with ``start_run``'s description naming the most recently used registered
    repos. ``repo`` stays a free string: the resolver checks it."""
    names = [r.name for r in repos_store.list_all(ctx.conn)[:RECENT_REPOS]]
    linked = repos_store.linking_extensions(ctx.conn)[:RECENT_REPOS]
    description = START_RUN.description
    if names:
        description = f"{description} Recent repos: {', '.join(names)}."
    if linked:
        description = f"{description} Extensions with repos: {', '.join(linked)}."
    start = START_RUN.model_copy(update={"description": description})
    return (start, *SPECS[1:])


def summary_action(conn: sqlite3.Connection, intent: Intent) -> str | None:
    """The approval and dry-run heading for ``merge_run`` (matched by registry identity):
    ``merge R7: PR #41 in dori https://github.com/o/dori/pull/41, changes Makefile``, built from
    the run's row. None for another tool or a run that isn't there."""
    if intent.extension is not None or intent.tool != MERGE_RUN.name:
        return None
    code = _code(intent.args.get("code"))
    run = runs_store.get(conn, code) if code is not None else None
    if run is None or run.pr_number is None:
        return None
    head = f"merge {run.code}: PR #{run.pr_number} in {run.repo}"
    if run.pr_url:
        head += f" {run.pr_url}"
    if run.flagged_paths:
        head += f", changes {', '.join(run.flagged_paths)}"
    return head


def merge_standable(conn: sqlite3.Connection, intent: Intent) -> bool:
    """PER-272: whether a standing allow on ``merge_run`` may cover this call: a run that
    flagged no sensitive paths, in a push-mode repo that no ``ask merge`` rule covers. A
    merge that then runs on the allow is unattended (green CI, CLEAN, always texted:
    ``ClaudeRunner.merge_run``). True for another tool (nothing to say about it)."""
    if intent.extension is not None or intent.tool != MERGE_RUN.name:
        return True
    code = _code(intent.args.get("code"))
    run = runs_store.get(conn, code) if code is not None else None
    repo = repos_store.find(conn, run.repo) if run is not None and run.repo else None
    if run is None or repo is None or repo.mode != "push" or run.flagged_paths:
        return False
    return merge_decision(conn, repo.name) == "ok"


def merge_level(conn: sqlite3.Connection, intent: Intent) -> Level | None:
    """``merge_run``'s level for one call, by the trust rules (``PolicyGate`` ``levels``): L3
    (runs without asking, even chosen from chat) when ``merge_decision`` is ok for the run's
    repo, the run flagged no sensitive paths and the repo has CI (a no-CI merge is on the
    owner's word only, GT-1); L2 (``/merge`` runs, chat asks a plain yes) otherwise. None (the
    registered L2) for another tool, a missing run or a fork repo."""
    if intent.extension is not None or intent.tool != MERGE_RUN.name:
        return None
    code = _code(intent.args.get("code"))
    run = runs_store.get(conn, code) if code is not None else None
    repo = repos_store.find(conn, run.repo) if run is not None and run.repo else None
    if run is None or repo is None or repo.mode != "push":
        return None
    if run.flagged_paths or repo.ci is not True:
        return Level.L2
    return Level.L3 if merge_decision(conn, repo.name) == "ok" else Level.L2


# --- helpers -----------------------------------------------------------------------------


TURN_RUNNER = "ask"  # ``runners.ask.NAME``: a Claude brain turn, whose reply says what ran


def _announces(call: CallContext | None, task: Task | None) -> bool:
    """PER-319: a run a model chose says it started (or continued) in its thread itself.
    PER-352: not when a Claude brain turn chose it: that turn's reply lists every run it
    started or continued in one line (``runners/reply_lines.py``), so the thread hears it
    once, not once per run and again in the reply."""
    if call is None or call.origin == "explicit":
        return False
    return task is None or task.runner != TURN_RUNNER


def _code(value: object) -> str | None:
    """``R3`` for ``"r3"`` or ``" R3 "``; None if it isn't a run code."""
    if not isinstance(value, str):
        return None
    m = RUN_CODE.fullmatch(value.strip())
    return f"R{int(m.group(1))}" if m else None


def _need_code(tool: str, args: dict[str, Any]) -> str:
    unknown = set(args) - {"code"}
    if unknown:
        raise ToolFailed(f"{tool}: unknown arguments: {sorted(unknown)}")
    code = _code(args.get("code"))
    if code is None:
        raise ToolFailed(f"{tool}: code must be a run code like R3")
    return code


def not_set_up(why: str, how: str) -> str:
    from resonant.runners.claude import not_set_up_text

    return not_set_up_text([why], [how])


def _refusal(result: object) -> str | None:
    """The sentence of a ``Refused`` result, None for any other."""
    from resonant.runners.claude import Refused

    return result.reason if isinstance(result, Refused) else None


def _clip(text: str | None, limit: int) -> str | None:
    if not text:
        return None
    flat = clean_external(text)
    return None if not flat else flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def run_json(run: Run, question: str | None = None) -> dict[str, Any]:
    """One run for the model and the reply templates. The ask is the owner's own text; the
    summary and failure can carry Claude's or a command's output, so they are cleaned, and
    so is the run's open ``question`` (the worker's words). ``name`` is the run as the owner
    knows it, its code with its repo (PER-249: replies name it that way, not by code alone)."""
    return {
        "code": run.code,
        "name": f"{run.code} ({_clip(run.repo, REPO_MAX) or 'no repo'})",
        "repo": run.repo,
        "status": run.status,
        "ask": _clip(run.ask, ASK_SHOWN),
        "elapsed_s": round(run.session_seconds),
        "pr_number": run.pr_number,
        "pr_url": run.pr_url,
        "files_changed": run.files_changed,
        "flagged_paths": list(run.flagged_paths),
        "summary": _clip(run.summary, TEXT_MAX),
        "failure": _clip(run.failure, TEXT_MAX),
        "question": _clip(question, QUESTION_SHOWN),
    }


def pick_run(recent: list[Run], repo: str | None) -> Run | str:
    """The run ``run_activity`` means without a code: ``repo``'s latest (active first), else
    the only active run, else the most recent; or a sentence asking which."""
    if repo is not None:
        want = repo.lower().removesuffix(".git").rsplit("/", 1)[-1]
        mine = [r for r in recent if r.repo and r.repo.lower().rsplit("/", 1)[-1] == want]
        if not mine:
            return f"No run on {_clip(repo, 40) or 'that repo'} lately."
        active = [r for r in mine if r.active]
        return (active or mine)[0]
    if not recent:
        return "No runs yet."
    active = [r for r in recent if r.active]
    if len(active) == 1:
        return active[0]
    if not active:
        return recent[0]
    named = ", ".join(f"{r.code} ({r.repo or 'no repo'})" for r in active[:5])
    return f"Which run? {named}."


# --- handlers ----------------------------------------------------------------------------


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    def runner() -> RunnerApi | None:
        components = ctx.state.components
        found = components.runners.get(RUNNER) if components is not None else None
        return cast("RunnerApi | None", found)

    def missing() -> str:
        return not_set_up(
            "the Claude runner isn't running",
            "set channels.imessage.enabled in ~/.resonant/config.yaml and restart Resonant",
        )

    async def _start_run(args: dict[str, Any], call: CallContext | None = None) -> dict[str, Any]:
        unknown = set(args) - {"repo", "ask", "merge", "report", "follow_up"}
        if unknown:
            raise ToolFailed(f"start_run: unknown arguments: {sorted(unknown)}")
        repo, ask, merge = args.get("repo"), args.get("ask"), args.get("merge", False)
        if not isinstance(merge, bool):
            raise ToolFailed("start_run: merge is a boolean")
        if not (repo is None or isinstance(repo, str)) or not isinstance(ask, str):
            raise ToolFailed("start_run: ask is a required string, repo an optional one")
        if not ask.strip():
            raise ToolFailed("start_run: ask is a required string, repo an optional one")
        repo = (repo or "").strip() or None
        report = args.get("report", "digest")
        if report not in REPORTS:
            raise ToolFailed("start_run: report must be now or digest")
        if len(ask) > ASK_MAX:
            raise ToolFailed(f"start_run: ask is too long (at most {ASK_MAX} characters)")
        claude = runner()
        if claude is None:
            return {"started": False, "text": missing()}
        if repo is not None and len(repo) > REPO_MAX:
            raise ToolFailed(f"start_run: repo is too long (at most {REPO_MAX} characters)")
        follow_up = args.get("follow_up")
        if follow_up is not None and not isinstance(follow_up, str):
            raise ToolFailed("start_run: follow_up is a string")
        follow_up = " ".join((follow_up or "").split())[: runs_store.FOLLOW_UP_MAX] or None
        # PER-292: the run's texts go to the thread it was asked in (the calling task's).
        # PER-294: the run stays the owner's, but it keeps who asked (``requested_by``): its
        # questions, checks and outcome go to their thread, addressed to them.
        task = task_store.get_task(ctx.conn, call.task_id) if call is not None else None
        owner = ctx.principals.owner
        asker = None if task is None else task.principal
        thread: dict[str, Any] = {}
        if task is not None and task.channel_ref is not None:
            thread["channel_ref"] = task.channel_ref
        if asker is not None and asker != owner:
            thread["requested_by"] = asker
        # PER-319: a run a slash command started is announced by its reply (``On it: R5 ·
        # …``); one the brain started says ``On it: starting R5 (…).`` in its thread itself,
        # unless a Claude brain turn started it (PER-352): its reply lists the runs in one line
        announce = _announces(call, task)
        # PER-354: a follow-up ask starting a run passes its depth on; the chain stops at a cap
        depth = runs_store.chain_depth(None if task is None else task.checkpoint)
        if follow_up is not None and depth >= runs_store.FOLLOW_UP_DEPTH_MAX:
            raise ToolFailed(
                f"start_run: follow-ups are already {depth} deep, so this run can't carry "
                "another; start it without follow_up and say what is left"
            )
        chain: dict[str, Any] = {}
        if follow_up is not None:
            chain["follow_up"] = follow_up
        if depth:
            chain["follow_up_depth"] = depth
        result = await claude.start_run(repo, ask, owner, merge=merge, report=report,
                                        announce=announce, **thread, **chain)  # fmt: skip
        if (reason := _refusal(result)) is not None:
            return {"started": False, "text": reason}
        started = cast("Started", result)
        out: dict[str, Any] = {"started": True, "code": started.code,
                               "repo": started.repo or None, "text": started.text}  # fmt: skip
        if announce:  # the run posts its own start line: the reply must not repeat it
            out["announced"] = True
        return out

    def _known(conn: sqlite3.Connection, code: str) -> bool:
        """PER-270: the owner was already texted how this run ended (its pulse item is
        cleared and told at its current version): not news, don't announce it again."""
        try:
            item = pulse_store.get(conn, "core", pulse_store.run_key(code))
        except sqlite3.Error:
            return False
        return item is not None and item.cleared and item.known

    async def _list_runs(args: dict[str, Any]) -> dict[str, Any]:
        unknown = set(args) - {"code"}
        if unknown:
            raise ToolFailed(f"list_runs: unknown arguments: {sorted(unknown)}")
        code = None
        if args.get("code") is not None:
            code = _code(args["code"])
            if code is None:
                raise ToolFailed("list_runs: code must be a run code like R3")
        claude = runner()
        if claude is None:
            return {"runs": [], "count": 0, "code": code, "setup": missing()}
        if code is not None:
            one = claude.get_run(code)
            shown = [] if one is None else [one]
            total = len(shown)
        else:
            found = claude.list_runs(RUNS_MAX + 1)
            shown, total = found[:RUNS_MAX], len(found)
        asked = {q.run_code: q.question for q in claude.open_questions()}
        out: dict[str, Any] = {
            "runs": [
                {**run_json(r, asked.get(r.code)), "known": _known(ctx.conn, r.code)} for r in shown
            ],
            "count": total,
            "truncated": code is None and total > RUNS_MAX,
            "code": code,
        }
        if code is not None and shown:  # /runs R4: what it is doing too (TR-1)
            out["activity"] = claude.run_activity(code)
        if not shown:
            out["setup"] = await claude.setup_missing()
        if limits := await claude.capability_notes():  # what workers can't do yet (RX-1)
            out["limits"] = limits
        return out

    async def _run_activity(args: dict[str, Any]) -> dict[str, Any]:
        unknown = set(args) - {"code", "repo", "minutes"}
        if unknown:
            raise ToolFailed(f"run_activity: unknown arguments: {sorted(unknown)}")
        code = None
        if args.get("code") is not None:
            code = _code(args["code"])
            if code is None:
                raise ToolFailed("run_activity: code must be a run code like R3")
        repo = args.get("repo")
        if repo is not None and (not isinstance(repo, str) or len(repo) > REPO_MAX):
            raise ToolFailed(f"run_activity: repo is a string (at most {REPO_MAX})")
        minutes = args.get("minutes", ACTIVITY_MINUTES)
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            raise ToolFailed("run_activity: minutes is a whole number")
        if not 1 <= minutes <= ACTIVITY_MINUTES_MAX:
            raise ToolFailed(f"run_activity: minutes is 1 to {ACTIVITY_MINUTES_MAX}")
        claude = runner()
        if claude is None:
            return {"found": False, "text": missing()}
        if code is None:
            picked = pick_run(claude.list_runs(ACTIVITY_RECENT), (repo or "").strip() or None)
            if isinstance(picked, str):
                return {"found": False, "text": picked}
            code = picked.code
        found = claude.run_activity(code, minutes)
        if found is None:
            return {"found": False, "code": code, "text": f"No run {code}."}
        return {"found": True, **found}

    async def _cancel_run(args: dict[str, Any]) -> dict[str, Any]:
        code = _need_code("cancel_run", args)
        claude = runner()
        if claude is None:
            return {"cancelled": False, "code": code, "text": missing()}
        result = await claude.cancel_run(code)
        if (reason := _refusal(result)) is not None:
            return {"cancelled": False, "code": code, "text": reason}
        return {"cancelled": True, "code": code, "text": cast("Cancelled", result).text}

    async def _continue_run(
        args: dict[str, Any], call: CallContext | None = None
    ) -> dict[str, Any]:
        code = _need_code("continue_run", args)
        # PER-344: the owner and approvers only (the gate denies members every global
        # builtin; this says it again in words, whatever path reached the tool)
        task = task_store.get_task(ctx.conn, call.task_id) if call is not None else None
        asker = None if task is None else task.principal
        owner = ctx.principals.owner
        if asker is not None and asker != owner and not ctx.principals.is_approver(asker):
            text = "Only the owner or an approver can continue a run."
            return {"continued": False, "code": code, "text": text}
        claude = runner()
        if claude is None:
            return {"continued": False, "code": code, "text": missing()}
        announce = _announces(call, task)  # as start_run's (PER-319, PER-352)
        # PER-351: the run moves to the thread the continue was asked in, and to who asked
        moved: dict[str, Any] = {}
        if task is not None and task.channel_ref is not None:
            moved = {"channel_ref": task.channel_ref, "requested_by": asker}
        # PER-354: continued by a follow-up ask: the run takes its depth (the chain stays bounded)
        if (depth := runs_store.chain_depth(None if task is None else task.checkpoint)) > 0:
            moved["follow_up_depth"] = depth
        result = await claude.continue_run(code, announce=announce, **moved)
        if (reason := _refusal(result)) is not None:
            return {"continued": False, "code": code, "text": reason}
        out: dict[str, Any] = {"continued": True, "code": code,
                               "text": cast("Continued", result).text}  # fmt: skip
        if announce:  # the run posts its own ``Continuing R5 …``: the reply must not repeat it
            out["announced"] = True
        return out

    async def _merge_run(args: dict[str, Any], ctx: CallContext | None = None) -> dict[str, Any]:
        code = _need_code("merge_run", args)
        claude = runner()
        if claude is None:
            return {"merged": False, "code": code, "text": missing()}
        # PER-272: on nobody's word for this call (a standing allow, a trust rule): stricter
        unattended = ctx is not None and ctx.unattended
        result = await claude.merge_run(code, unattended=unattended)
        if (reason := _refusal(result)) is not None:
            return {"merged": False, "code": code, "text": reason}
        from resonant.runners.claude import MergeQueued

        if isinstance(result, MergeQueued):  # CI still running: the run's merge watch has it
            return {
                "merged": False,
                "queued": True,
                "code": code,
                "pr_number": result.pr_number,
                "text": result.text,
            }
        merged = cast("Merged", result)
        run = claude.get_run(code)
        text = merged.text
        if run is not None and run.pr_url:
            text += f"\n{run.pr_url}"
        return {
            "merged": True,
            "code": code,
            "pr_number": merged.pr_number,
            "flagged_paths": list(merged.flagged),
            "deps_changed": list(merged.deps),
            "merged_without_ci": merged.no_ci is not None,
            "text": text,
        }

    def _owner_args(tool: str, args: dict[str, Any]) -> tuple[str, str]:
        unknown = set(args) - {"code", "text"}
        if unknown:
            raise ToolFailed(f"{tool}: unknown arguments: {sorted(unknown)}")
        code = _code(args.get("code"))
        text = args.get("text")
        if code is None:
            raise ToolFailed(f"{tool}: code must be a run code like R3")
        if not isinstance(text, str) or not text.strip() or len(text) > OWNER_TEXT_MAX:
            raise ToolFailed(f"{tool}: text is a required string (at most {OWNER_TEXT_MAX})")
        return code, text.strip()

    async def _answer_worker(args: dict[str, Any]) -> dict[str, Any]:
        code, text = _owner_args("answer_worker", args)
        claude = runner()
        if claude is None:
            return {"sent": False, "code": code, "text": missing()}
        if not any(q.run_code == code for q in claude.open_questions()):
            result = claude.deliver_message(code, text)  # nothing open: it's a message
        else:
            result = claude.answer_question(code, text)
        if (reason := _refusal(result)) is not None:
            return {"sent": False, "code": code, "text": reason}
        return {"sent": True, "code": code, "text": cast("Relayed", result).text}

    async def _message_worker(args: dict[str, Any]) -> dict[str, Any]:
        code, text = _owner_args("message_worker", args)
        claude = runner()
        if claude is None:
            return {"sent": False, "code": code, "text": missing()}
        result = claude.deliver_message(code, text)
        if (reason := _refusal(result)) is not None:
            return {"sent": False, "code": code, "text": reason}
        return {"sent": True, "code": code, "text": cast("Relayed", result).text}

    return {
        ANSWER_WORKER.name: _answer_worker,
        MESSAGE_WORKER.name: _message_worker,
        START_RUN.name: cast(ToolHandler, _WithContext(_start_run)),
        LIST_RUNS.name: _list_runs,
        RUN_ACTIVITY.name: _run_activity,
        CANCEL_RUN.name: _cancel_run,
        CONTINUE_RUN.name: cast(ToolHandler, _WithContext(_continue_run)),
        MERGE_RUN.name: cast(ToolHandler, _WithContext(_merge_run)),
    }


class _WithContext(ContextHandler):
    """A builtin handler that reads the executor's ``CallContext`` (``unattended``); called
    with args alone (a test, a direct call) it gets None."""

    def __init__(
        self, fn: Callable[[dict[str, Any], CallContext | None], Awaitable[dict[str, Any]]]
    ) -> None:
        self.fn = fn

    async def __call__(
        self, args: dict[str, Any], ctx: CallContext | None = None
    ) -> dict[str, Any]:
        return await self.fn(args, ctx)
