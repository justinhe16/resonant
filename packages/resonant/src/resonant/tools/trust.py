"""Trust rule tools (docs/employee-plan.md D4): the ``rules`` intent and ``/rules``.

- ``list_trust_rules`` (L0): every rule, oldest first, with its number and words.
- ``add_trust_rule{scope, action, effect, note?}`` (L3): a rule that only tightens trust or
  sets urgency: ``ask`` ("ask before merging in trading"), ``notify_now`` ("text me right
  away when trading runs finish") or ``notify_digest``. Chat runs it and reports
  ``Rule added: ask before merge in trading (rule 3)``.
- ``allow_trust_rule{scope, action, note?}`` (L2): a rule that *widens* trust ("don't ask
  me about deploys to dori-staging"). Model-chosen, so the gate asks the owner a plain yes
  first, with a code-built prompt (``allow deploy in dori-staging without asking``): a
  misread message or a steered model can't widen trust silently.
- ``remove_trust_rule{id | description}`` (L2): forgetting a rule can widen trust too (an
  ``ask`` rule), so chat asks a plain yes; ``/rules forget <n>`` is explicit and runs.
  ``description`` matches the rule's words (``merge trading``), or ``last`` for the newest
  rule; it must match exactly one.

Rules made here have ``created_from="chat"``; ``yes always`` to a worker's check makes the
others (``trust.checks``). Each change is audited (``trust.rule_added``,
``trust.rule_removed``) on top of the executor's own rows.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from typing import Any

from resonant.executor import ToolFailed, ToolHandler
from resonant.gate.types import Intent
from resonant.store import trust as store
from resonant.store.audit import audit
from resonant.store.db import atomic
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant_sdk import Effect, Level, ToolSpec

DESCRIPTION_MAX = 100
LAST = frozenset({"last", "latest", "newest", "that", "that rule", "last rule", "the last rule"})
_STOP = frozenset(
    {"a", "an", "the", "rule", "rules", "my", "me", "about", "for", "in", "on", "to", "of"}
    | {"and", "that", "this", "there", "before", "when", "runs", "run", "forget", "remove"}
)

_SLUG_ARG: dict[str, Any] = {"type": "string", "minLength": 1, "maxLength": store.SLUG_MAX}
_SCOPE = {
    **_SLUG_ARG,
    "description": "the system, repo or area (dori-staging, trading), or * for everywhere",
}
_ACTION = {
    **_SLUG_ARG,
    "description": (
        "the kind of action (deploy, merge, email, spend, delete), or for notify effects the "
        "event (done, pr, merged, failed), or * for any"
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
        "owner right away (notify_now) or only in the digest (notify_digest) for an event."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "scope": _SCOPE,
            "action": _ACTION,
            "effect": {"type": "string", "enum": ["ask", "notify_now", "notify_digest"]},
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
    description="Let Claude runs do an action in a scope without asking the owner first.",
    args_schema={
        "type": "object",
        "properties": {"scope": _SCOPE, "action": _ACTION, "note": _NOTE},
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
        "Forget one trust rule: by its number, or by a description of it (its words, or "
        "'last' for the newest)."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "id": {"type": "integer", "minimum": 1},
            "description": {"type": "string", "minLength": 1, "maxLength": DESCRIPTION_MAX},
        },
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
    REMOVE_TRUST_RULE,
)


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
                    replaced=added.replaced.id if added.replaced else None,
                )
    except ValueError as e:
        raise ToolFailed(f"{name}: {e}") from None
    out = _rule_dict(added.rule)
    out["existed"] = added.existed
    out["replaced"] = _rule_dict(added.replaced) if added.replaced else None
    return out


def _stem(word: str) -> str:
    """A light stem, so ``deploys``, ``merging`` and ``merged`` find ``deploy`` and ``merge``."""
    if len(word) > 4:
        for suffix in ("ing", "es", "ed", "s", "e"):
            if word.endswith(suffix):
                return word[: -len(suffix)]
    return word


def _words(text: str) -> set[str]:
    return {_stem(w) for w in re.split(r"[^a-z0-9*]+", text.lower()) if w and w not in _STOP}


def find_rule(conn: sqlite3.Connection, description: str) -> store.Rule:
    """The one rule ``description`` names, or ``ToolFailed`` saying why not."""
    rules = store.list_rules(conn)
    if not rules:
        raise ToolFailed("there are no rules")
    desc = " ".join(description.lower().split()).rstrip("?!. ")
    if desc in LAST:
        return rules[-1]
    if m := re.fullmatch(r"(?:rule )?#?(\d{1,9})", desc):
        rule = next((r for r in rules if r.id == int(m.group(1))), None)
        if rule is None:
            raise ToolFailed(f"no rule {m.group(1)}")
        return rule
    wanted = _words(desc)
    if not wanted:
        raise ToolFailed("say which rule: its number or its words")

    def haystack(r: store.Rule) -> set[str]:
        return _words(" ".join([r.scope, r.action, r.text, r.effect, r.note or ""]))

    found = [r for r in rules if wanted <= haystack(r)]
    if len(found) == 1:
        return found[0]
    if not found:
        raise ToolFailed("no rule matches that; list the rules and say its number")
    shown = "; ".join(f"rule {r.id}: {r.text}" for r in found[:5])
    raise ToolFailed(f"several rules match ({shown}); say which number")


def _target(conn: sqlite3.Connection, args: dict[str, Any]) -> store.Rule:
    name = REMOVE_TRUST_RULE.name
    _check_keys(name, args, {"id", "description"})
    rule_id, description = args.get("id"), args.get("description")
    if (rule_id is None) == (description is None):
        raise ToolFailed(f"{name}: pass exactly one of id or description")
    if rule_id is not None:
        if not isinstance(rule_id, int) or isinstance(rule_id, bool) or rule_id < 1:
            raise ToolFailed(f"{name}: id must be a positive integer")
        rule = store.get_rule(conn, rule_id)
        if rule is None:
            raise ToolFailed(f"{name}: no rule {rule_id}")
        return rule
    if not isinstance(description, str) or len(description) > DESCRIPTION_MAX:
        raise ToolFailed(f"{name}: description must be a short string")
    try:
        return find_rule(conn, description)
    except ToolFailed as e:
        raise ToolFailed(f"{name}: {e}") from None


def remove_trust_rule(conn: sqlite3.Connection, args: dict[str, Any]) -> dict[str, Any]:
    with atomic(conn):
        rule = _target(conn, args)
        store.remove_rule(conn, rule.id)
        audit(
            conn,
            "trust.rule_removed",
            tool=REMOVE_TRUST_RULE.name,
            rule_id=rule.id,
            scope=rule.scope,
            action_kind=rule.action,
            effect=rule.effect,
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
        where = "everywhere" if scope == store.ANY else f"in {scope}"
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
        if effect not in ("ask", "notify_now", "notify_digest"):
            raise ToolFailed(f"{name}: effect must be ask, notify_now or notify_digest")
        return add_rule(conn, name, args, effect, ctx.clock())

    async def _allow(args: dict[str, Any]) -> dict[str, Any]:
        name = ALLOW_TRUST_RULE.name
        _check_keys(name, args, {"scope", "action", "note"})
        return add_rule(conn, name, args, "allow", ctx.clock())

    async def _remove(args: dict[str, Any]) -> dict[str, Any]:
        return remove_trust_rule(conn, args)

    return {
        LIST_TRUST_RULES.name: _list,
        ADD_TRUST_RULE.name: _add,
        ALLOW_TRUST_RULE.name: _allow,
        REMOVE_TRUST_RULE.name: _remove,
    }
