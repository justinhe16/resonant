"""Watch tools (BR-7, docs/brain-plan.md D10; ``brain/watches.py``):

- ``create_watch`` (**L2**, write): asks the owner first, with the code-built summary of
  exactly what will be checked. ``{what, tool?, args?, when?, text?, ask?, every_minutes?,
  days?, then?}``: either a read tool to call (``tool`` + ``args``, ``when`` ``contains`` /
  ``missing`` with ``text``, or ``changed``) or a yes/no ``ask`` for Claude. Refused (a
  ``ToolFailed`` the model sees) when the tool isn't an L0 read the principal may use, the
  args don't fit its schema, the interval is under the minimum, or ``max_active`` watches
  are already active.
- ``list_watches`` (L0): the active watches and those ended in the last 7 days.
- ``cancel_watch{id}`` (L3: do, then report): ``W3`` or ``3``.

They are global builtins, so only the owner (or a principal granted builtins) may call them;
the principal is the calling task's. No intent offers them; the brain loop lists them in its
catalog (``load_tools``). Report lines are code-built (``runners.local.REPORTS``).
"""

from __future__ import annotations

import re
from datetime import timedelta
from typing import Any, cast

import jsonschema

from resonant.brain import watches as store
from resonant.config import WatchConfig
from resonant.executor import CallContext, ContextHandler, ToolFailed, ToolHandler
from resonant.store import tasks as task_store
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant_sdk import Effect, Level, ToolSpec

# Never watched: they queue work, change state or are the loop's own.
NOT_WATCHABLE = frozenset(
    {"ask_claude", "create_watch", "list_watches", "cancel_watch", "load_tools"}
)
ENDED_SHOWN = timedelta(days=7)
_CODE = re.compile(r"^[Ww]?(\d{1,9})$")

CREATE_WATCH = ToolSpec(
    name="create_watch",
    description=(
        "Watch something and text the owner once when it happens ('watch X and tell me when "
        "Y'). Either call a read tool every few minutes (tool + args) and match its result "
        "(when: contains/missing with text, or changed), or ask Claude a yes/no question "
        "(ask; for web pages). Asks the owner first. It ends after it fires or after `days`."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "what": {
                "type": "string",
                "minLength": 1,
                "maxLength": store.WHAT_MAX,
                "description": "a short label, like 'iPhone 17 in stock'",
            },
            "tool": {"type": "string", "maxLength": 64, "description": "a read tool's name"},
            "args": {"type": "object", "description": "that tool's arguments"},
            "when": {"type": "string", "enum": list(store.TOOL_OPS)},
            "text": {"type": "string", "minLength": 1, "maxLength": store.VALUE_MAX},
            "ask": {
                "type": "string",
                "minLength": 1,
                "maxLength": store.QUESTION_MAX,
                "description": "a yes/no question for Claude, e.g. 'Is X in stock at <url>?'",
            },
            "every_minutes": {"type": "integer", "minimum": 15, "maximum": 1440},
            "days": {"type": "integer", "minimum": 1, "maximum": 90},
            "then": {"type": "string", "enum": ["stop", "continue"]},
        },
        "required": ["what"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=5.0,
)
LIST_WATCHES = ToolSpec(
    name="list_watches",
    description="The owner's watches: active ones, and those that ended this week.",
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)
CANCEL_WATCH = ToolSpec(
    name="cancel_watch",
    description="Stop one watch by its id (like W3).",
    args_schema={
        "type": "object",
        "properties": {"id": {"type": "string", "minLength": 1, "maxLength": 12}},
        "required": ["id"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
)
SPECS: tuple[ToolSpec, ...] = (CREATE_WATCH, LIST_WATCHES, CANCEL_WATCH)


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolFailed(f"create_watch: {key} must be a non-empty string")
    return value.strip()


def _int(args: dict[str, Any], key: str, default: int) -> int:
    value = args.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolFailed(f"create_watch: {key} must be a whole number")
    return value


class _Tools:
    """The three handlers over one ``BuiltinContext``."""

    def __init__(self, ctx: BuiltinContext) -> None:
        self.ctx = ctx

    @property
    def cfg(self) -> WatchConfig:
        return self.ctx.state.settings.brain.watches

    def _principal(self, ctx: CallContext | None) -> tuple[str, str | None]:
        owner = self.ctx.principals.owner
        if ctx is None:
            return owner, None
        task = task_store.get_task(self.ctx.conn, ctx.task_id)
        if task is None:
            return owner, None
        return task.principal or owner, task.id

    def _watchable(self, name: str, principal: str) -> ToolSpec:
        """The read tool a watch may call: builtins first, then a loaded extension's chat
        tool (never a job spec), L0 ``Effect.READ``, one the principal may request."""
        components = self.ctx.state.components
        host = components.extensions if components is not None else None
        found = [
            s
            for s in self.ctx.registry.specs()
            if s.name == name
            and name not in NOT_WATCHABLE
            and (s.extension is None or (host is not None and not host.hidden(s)))
        ]
        found.sort(key=lambda s: s.extension is not None)
        if not found:
            raise ToolFailed(f"create_watch: no tool named {name!r} to watch with")
        spec = found[0]
        if spec.effect is not Effect.READ or spec.effective_level is not Level.L0:
            raise ToolFailed(f"create_watch: {name} isn't a read-only tool; only reads are watched")
        if not self.ctx.principals.may_request(principal, spec):
            raise ToolFailed(f"create_watch: you may not use {name}")
        return spec

    async def create(self, args: dict[str, Any], ctx: CallContext | None = None) -> dict[str, Any]:
        cfg = self.cfg
        if not cfg.enabled:
            raise ToolFailed("create_watch: watches are off (brain.watches.enabled)")
        unknown = set(args) - set(cast(dict[str, Any], CREATE_WATCH.args_schema["properties"]))
        if unknown:
            raise ToolFailed(f"create_watch: unknown arguments: {sorted(unknown)}")
        principal, task_id = self._principal(ctx)
        what = _opt_str(args, "what")
        if what is None:
            raise ToolFailed("create_watch: what is required")
        what = " ".join(what.split())[: store.WHAT_MAX]
        tool, ask = _opt_str(args, "tool"), _opt_str(args, "ask")
        if (tool is None) == (ask is None):
            raise ToolFailed("create_watch: give either tool (with args) or ask, not both")
        then = args.get("then", "stop")
        if then not in ("stop", "continue"):
            raise ToolFailed("create_watch: then is stop or continue")
        minimum = cfg.min_interval_minutes if tool is not None else cfg.ask_min_interval_minutes
        every = _int(args, "every_minutes", max(60, minimum))
        if every < minimum:
            raise ToolFailed(f"create_watch: at most every {minimum} minutes for this kind")
        days = _int(args, "days", cfg.default_days)
        if not 1 <= days <= cfg.max_days:
            raise ToolFailed(f"create_watch: days is 1 to {cfg.max_days}")
        fields: dict[str, Any] = {}
        if tool is not None:
            spec = self._watchable(tool, principal)
            raw: Any = args.get("args", {})
            if not isinstance(raw, dict):
                raise ToolFailed("create_watch: args must be an object")
            tool_args = cast(dict[str, Any], raw)
            try:
                jsonschema.validate(tool_args, spec.args_schema)
            except jsonschema.ValidationError as e:
                raise ToolFailed(f"create_watch: args don't fit {tool}: {e.message[:120]}") from e
            op = args.get("when")
            if op not in store.TOOL_OPS:
                raise ToolFailed("create_watch: when is contains, missing or changed")
            value = _opt_str(args, "text")
            if op != "changed" and value is None:
                raise ToolFailed(f"create_watch: when={op} needs text")
            fields = {
                "check_kind": "tool",
                "tool": spec.name,
                "extension": spec.extension,
                "args": tool_args,
                "op": op,
                "value": value if op != "changed" else None,
            }
        else:
            if any(k in args for k in ("args", "when", "text")):
                raise ToolFailed("create_watch: an ask watch takes no args, when or text")
            fields = {"check_kind": "ask", "question": ask, "op": "yes"}
        if store.active_count(self.ctx.conn, principal) >= cfg.max_active:
            raise ToolFailed(
                f"create_watch: {cfg.max_active} watches are already active; cancel one first"
            )
        now = self.ctx.clock()
        w = store.create(
            self.ctx.conn,
            principal=principal,
            what=what,
            interval_s=every * 60,
            now=now,
            expires_at=now + timedelta(days=days),
            then=then,
            created_by_task=task_id,
            **fields,
        )
        return {**store.describe(w), "created": True}

    async def list(self, args: dict[str, Any], ctx: CallContext | None = None) -> dict[str, Any]:
        no_args(LIST_WATCHES.name, args)
        principal, _ = self._principal(ctx)
        found = store.list_for(self.ctx.conn, principal, ended_since=self.ctx.clock() - ENDED_SHOWN)
        active = sum(1 for w in found if w.state == "active")
        return {
            "watches": [store.describe(w) for w in found],
            "active": active,
            "max_active": self.cfg.max_active,
        }

    async def cancel(self, args: dict[str, Any], ctx: CallContext | None = None) -> dict[str, Any]:
        if set(args) - {"id"}:
            raise ToolFailed(f"cancel_watch: unknown arguments: {sorted(set(args) - {'id'})}")
        raw = args.get("id")
        m = _CODE.match(raw.strip()) if isinstance(raw, str) else None
        if m is None:
            raise ToolFailed("cancel_watch: id looks like W3")
        principal, _ = self._principal(ctx)
        w = store.get(self.ctx.conn, int(m.group(1)))
        if w is None or w.principal != principal:
            raise ToolFailed(f"cancel_watch: no watch W{m.group(1)}")
        if w.state != "active":
            return {"id": w.code, "what": w.what, "cancelled": False, "state": w.state}
        done = store.end(self.ctx.conn, w.id, "cancelled", self.ctx.clock())
        return {"id": w.code, "what": w.what, "cancelled": done, "state": "cancelled"}


class _Handler(ContextHandler):
    def __init__(self, fn: Any) -> None:
        self.fn = fn

    async def __call__(
        self, args: dict[str, Any], ctx: CallContext | None = None
    ) -> dict[str, Any]:
        return cast(dict[str, Any], await self.fn(args, ctx))


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    tools = _Tools(ctx)
    return {
        CREATE_WATCH.name: cast(ToolHandler, _Handler(tools.create)),
        LIST_WATCHES.name: cast(ToolHandler, _Handler(tools.list)),
        CANCEL_WATCH.name: cast(ToolHandler, _Handler(tools.cancel)),
    }
