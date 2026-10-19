"""Trust rule tools (docs/employee-plan.md D4): the ``rules`` intent and ``/rules``.

Anything that tightens trust or raises urgency runs from chat and reports; anything that
loosens it is L2, so a model-chosen call asks the owner a plain yes first, with a
code-built prompt. A misread message or a steered model can't loosen trust silently.

- ``list_trust_rules`` (L0): every rule, oldest first, with its number and words.
- ``add_trust_rule{scope, action, effect: ask | notify_now, note?}`` (L3): "ask before
  merging in trading", "text me right away when trading runs finish". Reports
  ``Rule added: ask before merge in trading (rule 3).``
- ``allow_trust_rule{scope, action, effect?: allow | notify_digest, note?}`` (L2): "don't
  ask me about deploys to dori-staging" (``allow``), "just put trading PRs in the digest"
  (``notify_digest``). Prompt: ``allow deploy in dori-staging without asking``.
- ``remove_trust_rule{id}`` (L2): forgetting a rule can loosen trust too (an ``ask`` rule).
  By number only, so the rule the owner approves forgetting is the one removed; the model
  takes the number from the earlier ``Rule added: … (rule 3)`` reply or from
  ``list_trust_rules``. ``/rules forget <n>`` is explicit and runs.

Rules made here have ``created_from="chat"``; ``yes always`` to a worker's check
(``trust.checks``) or to an owner's L2 chat tool call (a standing allow on ``tool:<name>``,
``trust.tools``) makes the others. ``/rules allow <what> [in <scope>]`` is the explicit path
for widening (``allow_trust_rule``, which runs from a slash command). Each change is audited
(``trust.rule_added`` with any replaced rule's fields, ``trust.rule_removed``) on top of the
executor's own rows.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, cast

from resonant.executor import ToolFailed, ToolHandler
from resonant.gate.types import Intent
from resonant.store import trust as store
from resonant.store.audit import audit
from resonant.store.db import atomic
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant.tools.registry import ToolRegistry
from resonant.trust.tools import allow_targets, tool_action, tool_name
from resonant_sdk import Effect, Level, ToolSpec

TIGHTEN: tuple[store.Effect, ...] = ("ask", "notify_now")
LOOSEN: tuple[store.Effect, ...] = ("allow", "notify_digest")

_SLUG_ARG: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": store.SLUG_MAX}
_SCOPE = {
    **_SLUG_ARG,
    "description": "the system, repo or area (dori-staging, trading), or * for everywhere",
}
_ACTION = {
    **_SLUG_ARG,
    "description": (
        "the kind of action (deploy, merge, email, spend, delete), or for notify effects the "
        "event (done, pr, merged, failed, or an extension's: ci_failed, ci_fixed, "
        "review_requested), or * for any"
    ),
}
_NOTE = {"type": "string", "maxLength": store.NOTE_MAX, "description": "why, in a few words"}

LIST_TRUST_RULES = ToolSpec(
    name="list_trust_rules",
    description="The owner's trust rules: when Claude runs ask first, and what texts right away.",
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)
ADD_TRUST_RULE = ToolSpec(
    name="add_trust_rule",
    description=(
        "Add a rule that makes Claude runs ask the owner first (effect ask), or that texts the "
        "owner right away for an event (notify_now)."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "scope": _SCOPE,
            "action": _ACTION,
            "effect": {"type": "string", "enum": list(TIGHTEN)},
            "note": _NOTE,
        },
        "required": ["scope", "action", "effect"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
)
ALLOW_TRUST_RULE = ToolSpec(
    name="allow_trust_rule",
    description=(
        "Loosen trust (asks the owner first): let Claude runs do an action without asking "
        "(effect allow, the default), or send an event only in the digest instead of texting "
        "right away (notify_digest)."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "scope": _SCOPE,
            "action": _ACTION,
            "effect": {"type": "string", "enum": list(LOOSEN)},
            "note": _NOTE,
        },
        "required": ["scope", "action"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=5.0,
)
REMOVE_TRUST_RULE = ToolSpec(
    name="remove_trust_rule",
    description=(
        "Forget one trust rule by its number (from list_trust_rules or the Rule added reply). "
        "Asks the owner first."
    ),
    args_schema={
        "type": "object",
        "properties": {"id": {"type": "integer", "minimum": 1}},
        "required": ["id"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=5.0,
)

ALLOW_TOOLS = ToolSpec(
    name="allow_tools",
    description=(
        "Stop asking the owner before Resonant's own tools that change things, when the owner "
        'asks for them in chat (asks the owner first): "don\'t ask for Linear tickets" is '
        "tools [linear] (an extension's name covers its changes: tickets and comments); one "
        "tool is linear.save_comment or create_watch. Changes after outside content still ask."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "tools": {
                "type": "array",
                "items": {"type": "string", "minLength": 1, "maxLength": 80},
                "minItems": 1,
                "maxItems": 8,
                "description": "extension names (linear) or tools (linear.save_issue)",
            },
            "note": _NOTE,
        },
        "required": ["tools"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L2,
    timeout_s=5.0,
)

SPECS: tuple[ToolSpec, ...] = (
    LIST_TRUST_RULES,
    ADD_TRUST_RULE,
    ALLOW_TRUST_RULE,
    ALLOW_TOOLS,
    REMOVE_TRUST_RULE,
)

ExtTools = Callable[[str], Sequence[ToolSpec]]  # an extension's loaded chat tools


def no_ext_tools(_extension: str) -> Sequence[ToolSpec]:
    return ()


def _tool_names(args: Mapping[str, Any]) -> list[str] | None:
    raw = args.get("tools")
    if not isinstance(raw, list):
        return None
    names = [n for n in cast(list[Any], raw) if isinstance(n, str) and n.strip()]
    return names if names and len(names) == len(cast(list[Any], raw)) else None


def allow_tools(
    conn: sqlite3.Connection,
    args: dict[str, Any],
    *,
    registry: ToolRegistry,
    ext_tools: ExtTools,
    now: datetime,
) -> dict[str, Any]:
    """BA-1: a standing allow per named tool (``trust.tools.allow_targets``): ``allow
    tool:<ext>.<name>`` on the extension (a builtin's on ``*``), ``created_from="chat"``, the
    same rule ``yes always`` saves. The gate then runs the owner's own model-chosen calls of
    them without asking (never tainted ones, never L1, never under the kill switch)."""
    name = ALLOW_TOOLS.name
    _check_keys(name, args, {"tools", "note"})
    names = _tool_names(args)
    if names is None:
        raise ToolFailed(f"{name}: tools must be a list of tool or extension names")
    specs, why = allow_targets(names, registry=registry, ext_tools=ext_tools)
    if why is not None:
        raise ToolFailed(f"{name}: {why}")
    note = _str_arg(name, args, "note", required=False)
    out: list[dict[str, Any]] = []
    with atomic(conn):
        for spec in specs:
            action = tool_action(spec.extension, spec.name)
            assert action is not None  # can_stand checked it
            added = store.add_rule(
                conn,
                scope=spec.extension or store.ANY,
                action=action,
                effect="allow",
                created_from="chat",
                now=now,
                note=note,
            )
            if not added.existed:
                audit(
                    conn,
                    "trust.rule_added",
                    tool=name,
                    rule_id=added.rule.id,
                    scope=added.rule.scope,
                    action_kind=added.rule.action,
                    effect=added.rule.effect,
                    created_from="chat",
                    replaced=store.rule_fields(added.replaced),
                )
            out.append({**_rule_dict(added.rule), "existed": added.existed})
    return {"rules": out, "tools": [tool_name(s) for s in specs]}


def allow_tools_action(
    intent: Intent, *, registry: ToolRegistry, ext_tools: ExtTools
) -> str | None:
    """The approval heading for ``allow_tools``: exactly the tools that would stop asking,
    from the registry (``stop asking before linear.save_issue and linear.save_comment when
    you ask in chat``), or None (a name that covers nothing: the default summary, and the
    call fails if approved)."""
    names = _tool_names(intent.args)
    if names is None:
        return None
    specs, why = allow_targets(names, registry=registry, ext_tools=ext_tools)
    if why is not None or not specs:
        return None
    shown = [tool_name(s) for s in specs]
    listed = shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]
    return f"stop asking before {listed} when you ask in chat"


def _rule_dict(r: store.Rule) -> dict[str, Any]:
    return {
        "id": r.id,
        "scope": r.scope,
        "action": r.action,
        "effect": r.effect,
        "text": r.text,
        "note": r.note,
        "created_from": r.created_from,
    }


def list_trust_rules(conn: sqlite3.Connection) -> dict[str, Any]:
    rules = [_rule_dict(r) for r in store.list_rules(conn)]
    return {"rules": rules, "count": len(rules)}


def _str_arg(name: str, args: dict[str, Any], key: str, *, required: bool = True) -> str | None:
    value = args.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ToolFailed(f"{name}: {key} must be a non-empty string")
    return value


def _check_keys(name: str, args: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(args) - allowed
    if unknown:
        raise ToolFailed(f"{name}: unknown arguments: {sorted(unknown)}")


def add_rule(
    conn: sqlite3.Connection,
    name: str,
    args: dict[str, Any],
    effect: store.Effect,
    now: datetime,
) -> dict[str, Any]:
    """Add one rule from a tool call (``created_from="chat"``) and audit it."""
    scope = _str_arg(name, args, "scope")
    action = _str_arg(name, args, "action")
    note = _str_arg(name, args, "note", required=False)
    assert scope is not None and action is not None
    try:
        with atomic(conn):
            added = store.add_rule(
                conn,
                scope=scope,
                action=action,
                effect=effect,
                created_from="chat",
                now=now,
                note=note,
            )
            if not added.existed:
                audit(
                    conn,
                    "trust.rule_added",
                    tool=name,
                    rule_id=added.rule.id,
                    scope=added.rule.scope,
                    action_kind=added.rule.action,
                    effect=added.rule.effect,
                    created_from="chat",
                    replaced=store.rule_fields(added.replaced),
                )
    except ValueError as e:
        raise ToolFailed(f"{name}: {e}") from None
    out = _rule_dict(added.rule)
    out["existed"] = added.existed
    out["replaced"] = _rule_dict(added.replaced) if added.replaced else None
    return out


def _target(conn: sqlite3.Connection, args: dict[str, Any]) -> store.Rule:
    name = REMOVE_TRUST_RULE.name
    _check_keys(name, args, {"id"})
    rule_id = args.get("id")
    if not isinstance(rule_id, int) or isinstance(rule_id, bool) or rule_id < 1:
        raise ToolFailed(f"{name}: id must be a positive integer")
    rule = store.get_rule(conn, rule_id)
    if rule is None:
        raise ToolFailed(f"{name}: no rule {rule_id}")
    return rule


def remove_trust_rule(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    with atomic(conn):
        rule = _target(conn, args)
        store.remove_rule(conn, rule.id)
        audit(
            conn,
            "trust.rule_removed",
            tool=REMOVE_TRUST_RULE.name,
            rule_id=rule.id,
            removed=store.rule_fields(rule),
        )
    return {**_rule_dict(rule), "removed": True}


def summary_action(conn: sqlite3.Connection, intent: Intent) -> str | None:
    """The code-built approval heading for the L2 rule tools, or None for the default.

    ``allow deploy in dori-staging without asking``; ``forget rule 3: ask before merge in
    trading``. Matched by registry identity (a builtin), never an extension tool.
    """
    if intent.extension is not None:
        return None
    args = intent.args
    if intent.tool == ALLOW_TRUST_RULE.name:
        try:
            scope = store.slug(str(args.get("scope", "")))
            action = store.slug(str(args.get("action", "")))
        except ValueError:
            return None
        what = "anything" if action == store.ANY else action
        if action.startswith(store.TOOL_PREFIX):
            what = f"running {action.removeprefix(store.TOOL_PREFIX)}"
        where = "everywhere" if scope == store.ANY else f"in {scope}"
        if args.get("effect") == "notify_digest":
            return f"stop texting right away on {what} {where} (digest only)"
        return f"allow {what} {where} without asking"
    if intent.tool == REMOVE_TRUST_RULE.name:
        try:
            rule = _target(conn, dict(args))
        except ToolFailed:
            return None
        return f"forget rule {rule.id}: {rule.text}"
    return None


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    conn = ctx.conn

    async def _list(args: dict[str, Any]) -> dict[str, Any]:
        no_args(LIST_TRUST_RULES.name, args)
        return list_trust_rules(conn)

    async def _add(args: dict[str, Any]) -> dict[str, Any]:
        name = ADD_TRUST_RULE.name
        _check_keys(name, args, {"scope", "action", "effect", "note"})
        effect = args.get("effect")
        if effect not in TIGHTEN:
            raise ToolFailed(f"{name}: effect must be ask or notify_now")
        return add_rule(conn, name, args, effect, ctx.clock())

    async def _allow(args: dict[str, Any]) -> dict[str, Any]:
        name = ALLOW_TRUST_RULE.name
        _check_keys(name, args, {"scope", "action", "effect", "note"})
        effect = args.get("effect", "allow")
        if effect not in LOOSEN:
            raise ToolFailed(f"{name}: effect must be allow or notify_digest")
        return add_rule(conn, name, args, effect, ctx.clock())

    async def _remove(args: dict[str, Any]) -> dict[str, Any]:
        return remove_trust_rule(conn, args)

    async def _allow_tools(args: dict[str, Any]) -> dict[str, Any]:
        components = ctx.state.components
        host = components.extensions if components is not None else None
        ext_tools: ExtTools = host.tool_specs if host is not None else no_ext_tools
        return allow_tools(conn, args, registry=ctx.registry, ext_tools=ext_tools,
                           now=ctx.clock())  # fmt: skip

    return {
        LIST_TRUST_RULES.name: _list,
        ADD_TRUST_RULE.name: _add,
        ALLOW_TRUST_RULE.name: _allow,
        ALLOW_TOOLS.name: _allow_tools,
        REMOVE_TRUST_RULE.name: _remove,
    }
