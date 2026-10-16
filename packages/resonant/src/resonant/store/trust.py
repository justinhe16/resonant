"""Trust rules repository (migration 010). Plain functions over a sqlite3 connection.

The policy (precedence, defaults, urgency) is ``resonant.trust``'s. This module owns the
rows:

- ``trust_rules``: one rule per row. ``add_rule`` keeps at most one rule per (scope, action)
  in each family, ``allow``/``ask`` (trust) and ``notify_now``/``notify_digest`` (notify):
  a new rule replaces the old one of its family, so "don't ask about X" after "ask before
  X" is not shadowed by it. Adding the same rule again returns the existing one.
- ``trust_seen``: the (system, action) pairs that have had an ok check, for the default
  "new system" and "new kind of action" asks. ``mark_seen`` keeps the first time.

``scope`` and ``action`` are slugs (``slug``): lowercase, whitespace as ``-``, at most
``SLUG_MAX`` characters of ``[a-z0-9._:/@-]``, or ``*`` alone. Anything else raises
``ValueError``, so a rule or check can never carry free text into a prompt.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast, get_args

from resonant.store.db import atomic, iso

Effect = Literal["allow", "ask", "notify_now", "notify_digest"]
EFFECTS: tuple[Effect, ...] = get_args(Effect)
TRUST_EFFECTS: frozenset[Effect] = frozenset({"allow", "ask"})
NOTIFY_EFFECTS: frozenset[Effect] = frozenset({"notify_now", "notify_digest"})

# How a rule was made. ``default`` is reserved for rules Resonant ships (none yet).
CreatedFrom = Literal["chat", "yes always", "command", "default"]
CREATED_FROM: tuple[CreatedFrom, ...] = get_args(CreatedFrom)

ANY = "*"
SLUG_MAX = 64
NOTE_MAX = 200
_SLUG = re.compile(r"[a-z0-9][a-z0-9._:/@-]*", re.ASCII)


def slug(value: str) -> str:
    """``value`` as a scope, system or action slug, or ``ValueError``."""
    if not isinstance(value, str):  # pyright: ignore[reportUnnecessaryIsInstance]
        raise ValueError("must be a string")
    s = "-".join(value.strip().lower().split())
    if s == ANY:
        return s
    if not s or len(s) > SLUG_MAX or not _SLUG.fullmatch(s):
        raise ValueError(
            f"must be {ANY} or a short name of letters, digits and . _ : / @ - (at most {SLUG_MAX})"
        )
    return s


def family(effect: Effect) -> Literal["trust", "notify"]:
    return "trust" if effect in TRUST_EFFECTS else "notify"


@dataclass(frozen=True)
class Rule:
    id: int
    scope: str
    action: str
    effect: Effect
    note: str | None
    created_from: str
    created_at: datetime

    @property
    def text(self) -> str:
        """``ask before merge in trading``: the rule in words, built by code."""
        return describe(self.scope, self.action, self.effect)


def describe(scope: str, action: str, effect: Effect) -> str:
    """A rule in words: ``ask before merge in trading``, ``don't ask before deploy in
    dori-staging``, ``text right away on done in trading``, ``digest only for pr``."""
    what = "anything" if action == ANY else action
    where = "" if scope == ANY else f" in {scope}"
    if effect == "allow":
        return f"don't ask before {what}{where}"
    if effect == "ask":
        return f"ask before {what}{where}"
    if effect == "notify_now":
        return f"text right away on {what}{where}"
    return f"digest only for {what}{where}"


def _row(r: sqlite3.Row) -> Rule:
    return Rule(
        id=int(r["id"]),
        scope=r["scope"],
        action=r["action"],
        effect=cast(Effect, r["effect"]),
        note=r["note"],
        created_from=r["created_from"],
        created_at=datetime.fromisoformat(r["created_at"]),
    )


def rule_fields(rule: Rule | None) -> dict[str, object] | None:
    """A rule's fields for an audit row (a replaced or removed rule), or None."""
    if rule is None:
        return None
    return {
        "id": rule.id,
        "scope": rule.scope,
        "action": rule.action,
        "effect": rule.effect,
        "created_from": rule.created_from,
    }


@dataclass(frozen=True)
class Added:
    rule: Rule
    replaced: Rule | None = None  # the rule of the same family it replaced
    existed: bool = False  # the same rule was already there; nothing changed


def add_rule(
    conn: sqlite3.Connection,
    *,
    scope: str,
    action: str,
    effect: Effect,
    created_from: CreatedFrom,
    now: datetime,
    note: str | None = None,
) -> Added:
    """Add a rule, replacing the one of the same family on (scope, action). Validates every
    field (``ValueError``)."""
    scope, action = slug(scope), slug(action)
    if effect not in EFFECTS:
        raise ValueError(f"effect must be one of {', '.join(EFFECTS)}")
    if created_from not in CREATED_FROM:
        raise ValueError(f"unknown created_from {created_from!r}")
    note = " ".join(note.split())[:NOTE_MAX] if note else None
    same = TRUST_EFFECTS if family(effect) == "trust" else NOTIFY_EFFECTS
    with atomic(conn):
        old = [
            r
            for r in rules_for(conn, scope, action)
            if r.effect in same  # the same family: at most one
        ]
        for r in old:
            if r.effect == effect:
                return Added(r, existed=True)
        replaced = old[0] if old else None
        if replaced is not None:
            conn.execute("DELETE FROM trust_rules WHERE id = ?", (replaced.id,))
        cur = conn.execute(
            """INSERT INTO trust_rules (scope, action, effect, note, created_from, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (scope, action, effect, note, created_from, iso(now)),
        )
        rule_id = cur.lastrowid
        assert rule_id is not None
        rule = get_rule(conn, rule_id)
    assert rule is not None
    return Added(rule, replaced)


def get_rule(conn: sqlite3.Connection, rule_id: int) -> Rule | None:
    r = conn.execute("SELECT * FROM trust_rules WHERE id = ?", (rule_id,)).fetchone()
    return None if r is None else _row(r)


def rules_for(conn: sqlite3.Connection, scope: str, action: str) -> list[Rule]:
    rows = conn.execute(
        "SELECT * FROM trust_rules WHERE scope = ? AND action = ? ORDER BY id",
        (scope, action),
    ).fetchall()
    return [_row(r) for r in rows]


def list_rules(conn: sqlite3.Connection) -> list[Rule]:
    """Every rule, oldest first."""
    return [_row(r) for r in conn.execute("SELECT * FROM trust_rules ORDER BY id").fetchall()]


def remove_rule(conn: sqlite3.Connection, rule_id: int) -> Rule | None:
    """Delete one rule. Returns it, or None if there was none with that id."""
    with atomic(conn):
        rule = get_rule(conn, rule_id)
        if rule is not None:
            conn.execute("DELETE FROM trust_rules WHERE id = ?", (rule_id,))
    return rule


# --- seen ----------------------------------------------------------------------------------


def mark_seen(conn: sqlite3.Connection, system: str, action: str, now: datetime) -> bool:
    """Record an ok check on (system, action). True if it was new."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO trust_seen (system, action, first_ok_at) VALUES (?, ?, ?)",
        (slug(system), slug(action), iso(now)),
    )
    return cur.rowcount == 1


def seen_actions(conn: sqlite3.Connection, system: str) -> frozenset[str]:
    """The actions that have had an ok check on ``system`` (empty: a new system)."""
    rows = conn.execute("SELECT action FROM trust_seen WHERE system = ?", (system,)).fetchall()
    return frozenset(r["action"] for r in rows)
