"""``ask_claude`` (L0, read): hand a question to a read-only Claude session (docs/brain-plan.md
D8, ``runners/ask.py``).

``ask_claude{question, kind?, attachments?}`` queues an ask task (``runner="ask"``) and returns
at once with ``{"queued": true, "task_id", "text": "Looking into it…"}``; the answer reaches
the requester as its own message when Claude is done. A refusal (off, paused, usage limit,
daily cap, a bad attachment) is a normal result, ``{"queued": false, "text": <why>}``.
BR-11: a turn of the Claude brain adds ``"session": true`` and, when others are ahead of it,
``"after"`` (``after "why is R4 slow", then this``); a full queue is ``"busy": true``.

- ``kind``: ``general`` (default), ``research``, ``email``, ``calendar``, ``image``, ``link``.
  Only ``email`` and ``calendar`` asks get connector tools, and only their read tools.
- ``attachments``: up to 4 absolute paths (a screenshot or photo from Messages, a file under a
  read root); each is checked against ``brain.read_roots``, ``attachment_roots`` and
  ``read_deny`` here and again when it is copied into the session's folder.

**Resonant's tools** (BR-10): when the calling task is a brain turn (``runner="brain"``: its
triage, the model's own call, an escalation), the ask gets Resonant's tools through the gate
(``runners/ask_tools.py``), starting tainted when the call's origin was ``tainted``. Code sets
both from the calling task and the executor's ``CallContext``; the model can't ask for them.

Level: L0 with ``Effect.READ``. Claude's own tools are read-only by construction (``--restricted``,
the read built-ins, connector read tools only, ``dontAsk``): nothing it can do changes
anything, so it needs no approval, and a ``tainted`` task may call it (the gate asks only for
L2 and L3). What it costs is bounded by the concurrency and the daily cap. It is a global
builtin, so only the owner (or a principal granted builtins) can call it; the answer goes to
whoever the calling task belongs to. Its answer is untrusted, so the spec is
``untrusted=True``: the brain loop taints the calling task (BR-1, D6).
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Protocol, cast

from resonant.executor import CallContext, ContextHandler, ToolFailed, ToolHandler
from resonant.store import tasks as task_store
from resonant.tools.base import BuiltinContext
from resonant_sdk import Effect, Level, ToolSpec

if TYPE_CHECKING:
    from resonant.runners.ask import AskRefused, Kind, Queued

RUNNER = "ask"
# BR-11: the calling brain turn's taint hops (BR-10b), set by the brain around its
# ask_claude call (the executor's handler runs in a copy of that context). A Claude brain
# turn is tainted as many hops from outside content as the turn that asked, so the
# chain can't sustain itself. None: not set (or the turn is clean).
ASKING_HOPS: ContextVar[int | None] = ContextVar("asking_hops", default=None)
BRAIN_RUNNER = "brain"  # runners.brain.NAME
QUESTION_MAX = 4000  # runners.ask.QUESTION_MAX (a test checks they agree)
ATTACH_MAX = 4  # runners.ask.ATTACH_MAX
KIND_VALUES = ("general", "research", "email", "calendar", "image", "link")  # runners.ask.KINDS
NOT_RUNNING = "ask_claude isn't available: the ask runner isn't running (iMessage off?)."

ASK_CLAUDE = ToolSpec(
    name="ask_claude",
    description=(
        "Hand a question to Claude when you're stuck or it needs the web, email, calendar, an "
        "image or a link. It answers the owner by text in a minute or two; you get back only "
        "that it's queued, so tell the owner you're looking into it. `kind`: research (web), "
        "email, calendar, image (with `attachments`), link, or general. Claude uses Resonant's "
        "tools through the same approvals as yours; it never sends email or changes events."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "question": {"type": "string", "minLength": 1, "maxLength": QUESTION_MAX},
            "kind": {"type": "string", "enum": list(KIND_VALUES)},
            "attachments": {
                "type": "array",
                "items": {"type": "string", "maxLength": 1024},
                "maxItems": ATTACH_MAX,
            },
        },
        "required": ["question"],
        "additionalProperties": False,
    },
    effect=Effect.READ,
    level=Level.L0,
    timeout_s=10.0,
    untrusted=True,  # D6: its answer is outside content, so the calling task is tainted
)
SPECS: tuple[ToolSpec, ...] = (ASK_CLAUDE,)


class AskApi(Protocol):
    """What the tool uses of ``AskRunner``."""

    owner: str

    def enqueue(
        self,
        question: str,
        *,
        kind: Kind = ...,
        attachments: list[str] | tuple[str, ...] = ...,
        principal: str,
        requested_by: str | None = None,
        channel_ref: dict[str, Any] | None = None,
        trace_id: str | None = None,
        tools: bool = False,
        tainted: bool = False,
        taint_hops: int | None = None,
    ) -> Queued | AskRefused: ...


def parse_args(args: dict[str, Any]) -> tuple[str, str, list[str]]:
    """``(question, kind, attachments)``, or ``ToolFailed`` naming what's wrong."""
    unknown = set(args) - {"question", "kind", "attachments"}
    if unknown:
        raise ToolFailed(f"ask_claude: unknown arguments: {sorted(unknown)}")
    question, kind = args.get("question"), args.get("kind", "general")
    if not isinstance(question, str) or not question.strip():
        raise ToolFailed("ask_claude: question is a required string")
    if len(question) > QUESTION_MAX:
        raise ToolFailed(f"ask_claude: question is too long (at most {QUESTION_MAX} characters)")
    if kind not in KIND_VALUES:
        raise ToolFailed(f"ask_claude: kind must be one of {', '.join(KIND_VALUES)}")
    raw: Any = args.get("attachments", [])
    if not isinstance(raw, list) or not all(isinstance(a, str) for a in cast(list[Any], raw)):
        raise ToolFailed("ask_claude: attachments is a list of paths")
    attachments = cast(list[str], raw)
    if len(attachments) > ATTACH_MAX:
        raise ToolFailed(f"ask_claude: at most {ATTACH_MAX} attachments")
    return question, cast(str, kind), attachments


class AskClaude(ContextHandler):
    """The handler. Under the executor it gets a ``CallContext``: the calling task decides
    who gets the answer (its principal and channel) and is recorded as ``requested_by``.
    Called with args alone (no task), the answer goes to the owner."""

    def __init__(self, ctx: BuiltinContext) -> None:
        self.ctx = ctx

    def _runner(self) -> AskApi | None:
        components = self.ctx.state.components
        found = components.runners.get(RUNNER) if components is not None else None
        return cast("AskApi | None", found)

    async def __call__(
        self, args: dict[str, Any], ctx: CallContext | None = None
    ) -> dict[str, Any]:
        question, kind, attachments = parse_args(args)
        runner = self._runner()
        if runner is None:
            return {"queued": False, "text": NOT_RUNNING}
        principal, channel_ref, trace_id, requested_by = runner.owner, None, None, None
        tools, tainted = False, False
        if ctx is not None:
            task = task_store.get_task(self.ctx.conn, ctx.task_id)
            if task is not None:
                requested_by = task.id
                principal = task.principal or runner.owner
                channel_ref, trace_id = task.channel_ref, task.trace_id
                tools = task.runner == BRAIN_RUNNER  # BR-10: the brain's asks get its tools
                tainted = ctx.origin == "tainted"
        result = runner.enqueue(
            question,
            kind=cast("Kind", kind),
            attachments=attachments,
            principal=principal,
            requested_by=requested_by,
            channel_ref=channel_ref,
            trace_id=trace_id,
            tools=tools,
            tainted=tainted,
            taint_hops=ASKING_HOPS.get() if tainted else None,
        )
        task_id: object = getattr(result, "task_id", None)
        if isinstance(task_id, str):
            out: dict[str, Any] = {"queued": True, "task_id": task_id, "kind": kind,
                                   "text": result.text}  # fmt: skip
            if getattr(result, "session", False) is True:  # BR-11: a Claude brain turn
                out["session"] = True
                after: object = getattr(result, "after", None)
                if isinstance(after, str):
                    out["after"] = after
            return out
        refused: dict[str, Any] = {"queued": False, "text": result.text}
        if getattr(result, "busy", False) is True:  # BR-11: the Claude brain's queue is full
            refused["busy"] = True
        return refused


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    # A ContextHandler is also callable with args alone (``ctx`` defaults to None), so it
    # serves both the executor (which passes the CallContext) and a plain call.
    return {ASK_CLAUDE.name: cast(ToolHandler, AskClaude(ctx))}
