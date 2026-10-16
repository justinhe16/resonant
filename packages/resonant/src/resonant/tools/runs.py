"""Claude runner tools: ``start_run`` (L3), ``list_runs`` (L0), ``cancel_run`` (L3) and
``merge_run`` (L2). The slash commands ``/run``, ``/runs``, ``/cancel`` and ``/merge`` and the
``run_project`` intent reach the same tools.

They are global builtins, so the gate lets only principals who can request global tools (the
owner) call them. The handlers call ``ClaudeRunner`` (``components.runners["claude"]``, found
at call time: the runner is built after the tools are registered) and return code-built
``text`` that the local runner's report line and the fast path's render send as is.

- ``start_run{repo?, ask, report?}``: L3, no approval (the sandbox is the wall). ``repo`` is a free
  string (a registry name, ``owner/name`` or a GitHub URL) that the runner's resolver checks;
  the registered names are listed in the spec (``specs_for``). Without ``repo`` the run
  works in a scratch dir and ends with Claude's summary, no PR (docs/employee-plan.md D1). A
  run that can't start (not set up, ambiguous or unknown repo, paused) is a normal result with
  ``started: false`` and the reason as ``text``. ``report``: ``digest`` (the default) puts
  the run's end in the hourly digest; ``now`` texts it when it ends (``/run`` passes it, and
  the model does when the owner asks to hear right away; docs/employee-plan.md D5, EM-6).
- ``list_runs{code?}``: L0, active runs first. Run codes (``R3``) are not approval codes: the
  model sees them, never an approval code. Free text from Claude (summary, failure) is
  cleaned and clipped.
- ``cancel_run{code}``: L3.
- ``merge_run{code}``: L2: ``/merge R3`` is explicit and runs. A model-chosen call follows
  the trust rules (``merge_level``, docs/employee-plan.md D4): it runs without asking when
  ``merge_decision`` is ok and the run flagged no sensitive paths, else it asks a plain yes.
  The approval prompt (``summary_action``) names the PR and the flagged paths. A fork-mode
  repo's run is never merged (its upstream's maintainers merge).
- ``start_run{..., merge?}``: ``merge`` when the owner asked to merge it too; the run then
  merges itself once CI is green (same checks, same trust rules).
- ``answer_worker{code, text}`` and ``message_worker{code, text}``: L3, no ask: it's the
  owner talking to their own worker (EM-5). The answer goes to the run's open question (a
  run with none gets it as a message); a message reaches the session at its next
  ``resonant`` call or turn. The router's relay (``router/relay.py``) handles ``R4 …`` and
  most replies without the model; these are for what only the model can place.
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Any, Literal, Protocol, cast

from resonant.executor import ToolFailed, ToolHandler
from resonant.gate.codes import clean_external
from resonant.gate.types import Intent
from resonant.store import repos as repos_store
from resonant.store import runs as runs_store
from resonant.tools.base import BuiltinContext
from resonant.trust import merge_decision
from resonant_sdk import Effect, Level, ToolSpec

if TYPE_CHECKING:
    from resonant.runners.claude import Cancelled, Merged, Refused, Started
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

# Run codes: ``R`` and a number. ``R`` is not in the approval alphabet (``gate.codes``), so a run
# code can't be mistaken for, or parsed as, an approval code (``yes R3`` is not a reply).
RUN_CODE = re.compile(r"R(\d{1,9})", re.ASCII | re.IGNORECASE)


class RunnerApi(Protocol):
    """What the tools use of ``ClaudeRunner``."""

    async def setup_missing(self) -> str | None: ...

    async def start_run(
        self,
        repo: str | None,
        ask: str,
        principal: str,
        *,
        merge: bool = False,
        report: Report = "digest",
    ) -> Started | Refused: ...

    def list_runs(self, limit: int = 20) -> list[Run]: ...

    def get_run(self, code: str) -> Run | None: ...

    async def cancel_run(self, code: str, *, reason: str = "cancelled") -> Cancelled | Refused: ...

    async def merge_run(self, code: str) -> Merged | Refused: ...

    def open_questions(self) -> list[OpenQuestion]: ...

    def answer_question(
        self, code: str, text: str, *, question_id: int | None = None
    ) -> Relayed | Refused: ...

    def deliver_message(self, code: str, text: str) -> Relayed | Refused: ...


def _code_schema() -> dict[str, Any]:
    return {"type": "string", "pattern": "^[Rr][0-9]{1,9}$", "description": "run code like R3"}


START_RUN = ToolSpec(
    name="start_run",
    description=(
        "Start a Claude Code run on a GitHub repo (a name, owner/name or URL): it works in a "
        "worktree of the repo and opens a pull request. Leave out `repo` for work not tied to a "
        "repo (ops, research): it runs in a scratch dir and reports back. `ask` is what to do, "
        "in the owner's words. Set `merge` only when the owner also asks to merge it: it then "
        "merges once CI is green."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "repo": {"type": "string", "maxLength": REPO_MAX},
            "ask": {"type": "string", "minLength": 1, "maxLength": ASK_MAX},
            "merge": {"type": "boolean"},
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
)
LIST_RUNS = ToolSpec(
    name="list_runs",
    description=(
        "Claude runs, active ones first: code, repo, status and PR. Give `code` for one run. "
        "Read only."
    ),
    args_schema={
        "type": "object",
        "properties": {"code": _code_schema()},
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
)

SPECS: tuple[ToolSpec, ...] = (
    START_RUN, LIST_RUNS, CANCEL_RUN, MERGE_RUN, ANSWER_WORKER, MESSAGE_WORKER,
)  # fmt: skip


def specs_for(ctx: BuiltinContext) -> tuple[ToolSpec, ...]:
    """``SPECS`` with ``start_run``'s description naming the most recently used registered
    repos. ``repo`` stays a free string: the resolver checks it."""
    names = [r.name for r in repos_store.list_all(ctx.conn)[:RECENT_REPOS]]
    description = START_RUN.description
    if names:
        description = f"{description} Recent repos: {', '.join(names)}."
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


def merge_level(conn: sqlite3.Connection, intent: Intent) -> Level | None:
    """``merge_run``'s level for one call, by the trust rules (``PolicyGate`` ``levels``): L3
    (runs without asking, even chosen from chat) when ``merge_decision`` is ok for the run's
    repo and the run flagged no sensitive paths; L2 (``/merge`` runs, chat asks a plain yes)
    otherwise. None (the registered L2) for another tool, a missing run or a fork repo."""
    if intent.extension is not None or intent.tool != MERGE_RUN.name:
        return None
    code = _code(intent.args.get("code"))
    run = runs_store.get(conn, code) if code is not None else None
    repo = repos_store.find(conn, run.repo) if run is not None and run.repo else None
    if run is None or repo is None or repo.mode != "push":
        return None
    if run.flagged_paths:
        return Level.L2
    return Level.L3 if merge_decision(conn, repo.name) == "ok" else Level.L2


# --- helpers -----------------------------------------------------------------------------


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
    so is the run's open ``question`` (the worker's words)."""
    return {
        "code": run.code,
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

    async def _start_run(args: dict[str, Any]) -> dict[str, Any]:
        unknown = set(args) - {"repo", "ask", "merge", "report"}
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
        result = await claude.start_run(repo, ask, ctx.principals.owner, merge=merge, report=report)
        if (reason := _refusal(result)) is not None:
            return {"started": False, "text": reason}
        started = cast("Started", result)
        return {"started": True, "code": started.code, "repo": started.repo or None,
                "text": started.text}  # fmt: skip

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
            "runs": [run_json(r, asked.get(r.code)) for r in shown],
            "count": total,
            "truncated": code is None and total > RUNS_MAX,
            "code": code,
        }
        if not shown:
            out["setup"] = await claude.setup_missing()
        return out

    async def _cancel_run(args: dict[str, Any]) -> dict[str, Any]:
        code = _need_code("cancel_run", args)
        claude = runner()
        if claude is None:
            return {"cancelled": False, "code": code, "text": missing()}
        result = await claude.cancel_run(code)
        if (reason := _refusal(result)) is not None:
            return {"cancelled": False, "code": code, "text": reason}
        return {"cancelled": True, "code": code, "text": cast("Cancelled", result).text}

    async def _merge_run(args: dict[str, Any]) -> dict[str, Any]:
        code = _need_code("merge_run", args)
        claude = runner()
        if claude is None:
            return {"merged": False, "code": code, "text": missing()}
        result = await claude.merge_run(code)
        if (reason := _refusal(result)) is not None:
            return {"merged": False, "code": code, "text": reason}
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
        START_RUN.name: _start_run,
        LIST_RUNS.name: _list_runs,
        CANCEL_RUN.name: _cancel_run,
        MERGE_RUN.name: _merge_run,
    }
