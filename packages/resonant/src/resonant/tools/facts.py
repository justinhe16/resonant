"""Facts tools (docs/brain-plan.md D9): ``list_facts`` (L0), ``remember`` (L3), ``forget`` (L3).

The store is ``brain.facts.FactStore`` over ``<home>/brain/facts`` (files, no migration).
``remember`` and ``forget`` are L3: they run without asking and the reply reports them with
a code-built line (``runners.local.REPORTS``). Both are ``taint_exempt`` (PER-245): they
run in a tainted task too. Like every builtin they are global scope, so
only the owner may call them. No intent in ``router/intents.py`` offers them; the brain loop
(BR-1) does.

Replay can't double-apply: the executor records each call under its idempotency key, and
``remember`` of the same fact again only rewrites the same file.
"""

from __future__ import annotations

from typing import Any

from resonant.brain.facts import (
    DESCRIPTION_MAX,
    FACTS_MAX,
    NAME_MAX,
    TEXT_MAX,
    FactError,
    FactStore,
    facts_dir,
)
from resonant.executor import ToolFailed, ToolHandler
from resonant.tools.base import NO_ARGS, BuiltinContext, no_args
from resonant_sdk import Effect, Level, ToolSpec

LIST_TEXT_MAX = 500  # each fact's text in list_facts

LIST_FACTS = ToolSpec(
    name="list_facts",
    description="The facts saved with remember, with their text (by name).",
    args_schema=NO_ARGS,
    effect=Effect.READ,
    timeout_s=5.0,
)
REMEMBER = ToolSpec(
    name="remember",
    description=(
        "Save a long-term fact the owner asked you to remember, or replace the one with the "
        f"same name (at most {FACTS_MAX}). Only when the owner asks."
    ),
    args_schema={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "pattern": f"^[a-z0-9][a-z0-9-]{{0,{NAME_MAX - 1}}}$",
                "description": "a short slug, like mycelium-path",
            },
            "description": {
                "type": "string",
                "minLength": 1,
                "maxLength": DESCRIPTION_MAX,
                "description": "the fact in one line",
            },
            "text": {"type": "string", "maxLength": TEXT_MAX, "description": "more detail"},
        },
        "required": ["name", "description"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
    taint_exempt=True,  # PER-245: runs on its level even when tainted
)
FORGET = ToolSpec(
    name="forget",
    description="Delete one saved fact by its name. Only when the owner asks.",
    args_schema={
        "type": "object",
        "properties": {"name": {"type": "string", "minLength": 1, "maxLength": NAME_MAX}},
        "required": ["name"],
        "additionalProperties": False,
    },
    effect=Effect.WRITE,
    level=Level.L3,
    timeout_s=5.0,
    taint_exempt=True,  # PER-245: runs on its level even when tainted
)

SPECS: tuple[ToolSpec, ...] = (LIST_FACTS, REMEMBER, FORGET)


def _str(name: str, args: dict[str, Any], key: str, *, required: bool) -> str:
    value = args.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ToolFailed(f"{name}: {key} must be a string")
    return value


def _check_keys(name: str, args: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(args) - allowed
    if unknown:
        raise ToolFailed(f"{name}: unknown arguments: {sorted(unknown)}")


def list_facts(store: FactStore) -> dict[str, Any]:
    facts = store.list()
    return {
        "facts": [
            {"name": f.name, "description": f.description, "text": f.text[:LIST_TEXT_MAX]}
            for f in facts
        ],
        "count": len(facts),
        "max": FACTS_MAX,
    }


def remember(store: FactStore, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(REMEMBER.name, args, {"name", "description", "text"})
    name = _str(REMEMBER.name, args, "name", required=True)
    description = _str(REMEMBER.name, args, "description", required=True)
    text = _str(REMEMBER.name, args, "text", required=False)
    try:
        fact, new = store.remember(name, description, text)
    except FactError as e:
        raise ToolFailed(f"remember: {e}") from e
    return {"name": fact.name, "description": fact.description, "created": new}


def forget(store: FactStore, args: dict[str, Any]) -> dict[str, Any]:
    _check_keys(FORGET.name, args, {"name"})
    name = _str(FORGET.name, args, "name", required=True)
    try:
        fact = store.forget(name)
    except FactError as e:
        raise ToolFailed(f"forget: {e}") from e
    return {"name": fact.name, "description": fact.description, "forgotten": True}


def handlers(ctx: BuiltinContext) -> dict[str, ToolHandler]:
    store = FactStore(facts_dir(ctx.state.settings.home))

    async def _list_facts(args: dict[str, Any]) -> dict[str, Any]:
        no_args(LIST_FACTS.name, args)
        return list_facts(store)

    async def _remember(args: dict[str, Any]) -> dict[str, Any]:
        return remember(store, args)

    async def _forget(args: dict[str, Any]) -> dict[str, Any]:
        return forget(store, args)

    return {LIST_FACTS.name: _list_facts, REMEMBER.name: _remember, FORGET.name: _forget}
