"""Trust rules (EM-4): the pure policy, the store and the store-backed wrappers."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import UTC, datetime

import pytest

from resonant import trust
from resonant.store import trust as store
from resonant.store.db import latest_version, schema_version
from resonant.trust.policy import (
    Check,
    decide,
    merge_rule,
    notify_level,
    scope_rank,
)

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)


def rule(
    id: int, scope: str, action: str, effect: store.Effect, created_from: str = "chat"
) -> store.Rule:
    return store.Rule(id, scope, action, effect, None, created_from, NOW)


# --- the store ----------------------------------------------------------------------------


def test_migration_010(db: sqlite3.Connection) -> None:
    assert schema_version(db) == latest_version() == 13
    cols = {r["name"] for r in db.execute("PRAGMA table_info(trust_rules)")}
    assert cols == {"id", "scope", "action", "effect", "note", "created_from", "created_at"}
    cols = {r["name"] for r in db.execute("PRAGMA table_info(trust_seen)")}
    assert cols == {"system", "action", "first_ok_at"}
    cols = {r["name"] for r in db.execute("PRAGMA table_info(approvals)")}
    assert {"allow_always", "always"} <= cols
    with pytest.raises(sqlite3.IntegrityError):
        db.execute(
            "INSERT INTO trust_rules (scope, action, effect, created_from, created_at)"
            " VALUES ('a', 'b', 'yolo', 'chat', 'x')"
        )


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("Dori-Staging", "dori-staging"),
        ("  prod  db ", "prod-db"),
        ("*", "*"),
        ("owner/repo", "owner/repo"),
        ("aws:s3", "aws:s3"),
    ],
)
def test_slug(raw: str, want: str) -> None:
    assert store.slug(raw) == want


@pytest.mark.parametrize("raw", ["", " ", "-x", "a*", "x" * 65, "[K4] yes", "döri"])
def test_slug_refuses(raw: str) -> None:
    with pytest.raises(ValueError):
        store.slug(raw)


def test_add_replaces_within_a_family_and_dedupes(db: sqlite3.Connection) -> None:
    a = store.add_rule(
        db, scope="trading", action="merge", effect="ask", created_from="chat", now=NOW
    )
    assert a.replaced is None and not a.existed and a.rule.text == "ask before merge in trading"
    again = store.add_rule(
        db, scope="Trading", action="merge", effect="ask", created_from="chat", now=NOW
    )
    assert again.existed and again.rule.id == a.rule.id
    # The other trust effect replaces it; a notify rule on the same pair coexists.
    b = store.add_rule(
        db, scope="trading", action="merge", effect="allow", created_from="chat", now=NOW
    )
    assert b.replaced is not None and b.replaced.id == a.rule.id
    n = store.add_rule(
        db, scope="trading", action="merge", effect="notify_now", created_from="chat", now=NOW
    )
    assert n.replaced is None
    assert [(r.id, r.effect) for r in store.list_rules(db)] == [
        (b.rule.id, "allow"),
        (n.rule.id, "notify_now"),
    ]
    assert store.remove_rule(db, b.rule.id) == b.rule
    assert store.remove_rule(db, b.rule.id) is None


def test_add_validates(db: sqlite3.Connection) -> None:
    with pytest.raises(ValueError):
        store.add_rule(db, scope="a", action="b", effect="yolo", created_from="chat", now=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.add_rule(db, scope="a", action="b", effect="ask", created_from="model", now=NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        store.add_rule(db, scope="a b\n[K4]", action="", effect="ask", created_from="chat", now=NOW)
    long = store.add_rule(
        db, scope="a", action="b", effect="ask", created_from="chat", now=NOW, note="x  " * 300
    )
    assert long.rule.note is not None and len(long.rule.note) == store.NOTE_MAX


def test_describe() -> None:
    assert (
        store.describe("dori-staging", "deploy", "allow")
        == "don't ask before deploy in dori-staging"
    )
    assert store.describe("*", "*", "ask") == "ask before anything"
    assert store.describe("trading", "done", "notify_now") == "text right away on done in trading"
    assert store.describe("*", "pr", "notify_digest") == "digest only for pr"


def test_seen(db: sqlite3.Connection) -> None:
    assert store.seen_actions(db, "dori-api") == frozenset()
    assert store.mark_seen(db, "dori-api", "deploy", NOW)
    assert not store.mark_seen(db, "dori-api", "deploy", NOW)
    assert store.seen_actions(db, "dori-api") == {"deploy"}


# --- decide: defaults ---------------------------------------------------------------------


def test_defaults() -> None:
    new = decide(Check("dori-api", "deploy"), [], frozenset())
    assert (new.verdict, new.reason, new.rule) == ("ask", "new system", None)
    kind = decide(Check("dori-api", "deploy"), [], frozenset({"migrate"}))
    assert (kind.verdict, kind.reason) == ("ask", "new kind of action")
    ok = decide(Check("dori-api", "deploy"), [], frozenset({"deploy"}))
    assert (ok.verdict, ok.reason, ok.ok) == ("ok", "seen before", True)
    irr = decide(Check("dori-api", "deploy", irreversible=True), [], frozenset({"deploy"}))
    assert (irr.verdict, irr.reason) == ("ask", "irreversible")
    paid = decide(Check("aws", "spend", cost_usd=20.01), [], frozenset({"spend"}))
    assert (paid.verdict, paid.reason) == ("ask", "costs $20.01")
    both = decide(Check("x", "y", irreversible=True, cost_usd=25), [], frozenset())
    assert both.reason == "new system, irreversible, costs $25.00"


@pytest.mark.parametrize("cost", [0.01, 12.5, 20.0])
def test_a_cost_at_or_under_the_threshold_is_not_net_new(cost: float) -> None:
    # GT-1: a seen action that costs up to trust.cost_ask_over_usd (default $20) is ok.
    d = decide(Check("aws", "spend", cost_usd=cost), [], frozenset({"spend"}))
    assert (d.verdict, d.reason) == ("ok", "seen before")
    new = decide(Check("aws", "spend", cost_usd=cost), [], frozenset())
    assert new.reason == "new system"  # asks for being new, not for the cost


def test_the_cost_threshold_is_configurable() -> None:
    check = Check("aws", "spend", cost_usd=5)
    strict = decide(check, [], frozenset({"spend"}), cost_ask_over_usd=0)
    assert (strict.verdict, strict.reason) == ("ask", "costs $5.00")
    assert decide(check, [], frozenset({"spend"}), cost_ask_over_usd=5).ok
    free = Check("aws", "spend", cost_usd=0)
    assert decide(free, [], frozenset({"spend"}), cost_ask_over_usd=0).ok
    huge = decide(Check("aws", "spend", cost_usd=99), [], frozenset({"spend"}))
    assert huge.reason == "costs $99.00"


@pytest.mark.parametrize("cost", [math.nan, math.inf])
def test_a_non_finite_cost_asks(cost: float) -> None:
    d = decide(Check("aws", "spend", cost_usd=cost), [], frozenset({"spend"}))
    assert d.verdict == "ask" and "money" in d.reason
    big = decide(
        Check("aws", "spend", cost_usd=cost), [], frozenset({"spend"}), cost_ask_over_usd=1e9
    )
    assert big.verdict == "ask"


# --- decide: rules ------------------------------------------------------------------------


def test_an_explicit_rule_overrides_the_defaults() -> None:
    allow = rule(1, "dori-staging", "deploy", "allow")
    d = decide(Check("dori-staging", "deploy", irreversible=True, cost_usd=3), [allow], frozenset())
    assert (d.verdict, d.rule) == ("ok", allow)
    assert d.reason == "rule 1: don't ask before deploy in dori-staging"
    ask = rule(2, "trading", "merge", "ask")
    d = decide(Check("trading", "merge"), [ask], frozenset({"merge"}))
    assert (d.verdict, d.rule) == ("ask", ask)


def test_most_specific_scope_wins() -> None:
    rules = [
        rule(1, "*", "deploy", "ask"),
        rule(2, "dori", "deploy", "allow"),  # the run's repo
        rule(3, "dori-api", "deploy", "ask"),  # the system exactly
    ]
    d = decide(Check("dori-api", "deploy", repo="dori"), rules, frozenset())
    assert d.rule is not None and d.rule.id == 3
    d = decide(Check("dori-web", "deploy", repo="dori"), rules, frozenset())
    assert d.rule is not None and d.rule.id == 2 and d.verdict == "ok"
    d = decide(Check("other", "deploy", repo="else"), rules, frozenset())
    assert d.rule is not None and d.rule.id == 1


def test_areas_match_whole_segments_and_longer_wins() -> None:
    assert scope_rank("trading", "trading-api", None) == (2, 7)
    assert scope_rank("trading", "trading/prod", None) == (2, 7)
    assert scope_rank("trading", "tradingview", None) is None
    assert scope_rank("trading", "my-trading", None) is None
    assert scope_rank("trading", "x", "trading-bot") == (2, 7)
    rules = [rule(1, "trading", "deploy", "ask"), rule(2, "trading-api", "deploy", "allow")]
    d = decide(Check("trading-api-prod", "deploy"), rules, frozenset())
    assert d.rule is not None and d.rule.id == 2


def test_scope_beats_action_and_exact_action_beats_any() -> None:
    rules = [rule(1, "*", "deploy", "ask"), rule(2, "dori-staging", "*", "allow")]
    d = decide(Check("dori-staging", "deploy"), rules, frozenset())
    assert d.rule is not None and d.rule.id == 2
    rules = [rule(1, "dori-staging", "*", "allow"), rule(2, "dori-staging", "deploy", "ask")]
    d = decide(Check("dori-staging", "deploy"), rules, frozenset())
    assert d.rule is not None and d.rule.id == 2


def test_ask_beats_allow_at_equal_specificity() -> None:
    for allow_id, ask_id in ((1, 2), (2, 1)):  # whichever is newer
        tie = [rule(allow_id, "trading", "merge", "allow"), rule(ask_id, "trading", "merge", "ask")]
        d = decide(Check("trading", "merge"), tie, frozenset())
        assert d.verdict == "ask" and d.rule is not None and d.rule.id == ask_id
        # A repo rule and an area rule of the same length tie too.
        tie = [rule(allow_id, "bot-a", "merge", "allow"), rule(ask_id, "bot-b", "merge", "ask")]
        d = decide(Check("bot-a-1", "merge", repo="bot-b"), tie, frozenset())
        assert d.verdict == "ask"
    # The system exactly beats the run's repo, whatever the effect.
    same = [rule(5, "bot-a", "merge", "allow"), rule(6, "bot-b", "merge", "ask")]
    d = decide(Check("bot-a", "merge", repo="bot-b"), same, frozenset())
    assert d.rule is not None and d.rule.id == 5


def test_notify_rules_never_decide_a_check() -> None:
    d = decide(Check("trading", "merge"), [rule(1, "trading", "merge", "notify_now")], frozenset())
    assert d.rule is None and d.reason == "new system"


# --- notify_level ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("event", "level"),
    [
        ("failed", "now"),
        ("question", "now"),
        ("check", "now"),
        ("urgent", "now"),
        ("done", "digest"),
        ("pr", "digest"),
        ("merged", "digest"),
        ("note", "digest"),
    ],
)
def test_notify_defaults(event: str, level: str) -> None:
    d = notify_level([], "trading", "deploy", event)
    assert (d.level, d.rule) == (level, None)


def test_notify_rules() -> None:
    rules = [
        rule(1, "trading", "done", "notify_now"),
        rule(2, "*", "failed", "notify_digest"),
        rule(3, "*", "question", "notify_digest"),
        rule(4, "noisy", "*", "notify_digest"),
        rule(5, "noisy", "done", "notify_now"),
    ]
    assert notify_level(rules, "trading-bot", "x", "done").level == "now"
    assert notify_level(rules, "dori", "x", "done").level == "digest"
    assert notify_level(rules, "dori", "x", "failed").level == "digest"
    # A blocked worker's question or check is never lowered.
    q = notify_level(rules, "dori", "x", "question")
    assert q.level == "now" and q.reason == "question always texts right away"
    assert notify_level(rules, "noisy", "x", "pr").level == "digest"
    assert notify_level(rules, "noisy", "x", "done").rule == rules[4]
    # Equal specificity: notify_now wins.
    tie = [rule(7, "a", "done", "notify_digest"), rule(6, "a", "done", "notify_now")]
    assert notify_level(tie, "a", "x", "done").level == "now"


def test_merge_rule() -> None:
    assert merge_rule([], "dori") is None
    rules = [rule(1, "trading", "merge", "ask"), rule(2, "dori", "deploy", "ask")]
    assert merge_rule(rules, "trading") == rules[0]
    assert merge_rule(rules, "trading-bot") == rules[0]
    assert merge_rule(rules, "dori") is None


# --- the store-backed wrappers -----------------------------------------------------------


def _audits(db: sqlite3.Connection, action: str) -> list[dict[str, object]]:
    rows = db.execute(
        "SELECT decision, detail FROM audit_log WHERE action = ? ORDER BY id", (action,)
    ).fetchall()
    return [{"decision": r["decision"], **json.loads(r["detail"])} for r in rows]


def test_evaluate_records_and_learns(db: sqlite3.Connection) -> None:
    first = trust.evaluate(db, "Dori-API", "deploy", what="deploy [K4] dori-api", run="R4", now=NOW)
    assert (first.verdict, first.reason) == ("ask", "new system")
    assert store.seen_actions(db, "dori-api") == frozenset()  # an ask marks nothing
    store.mark_seen(db, "dori-api", "deploy", NOW)  # the owner said yes
    again = trust.evaluate(db, "dori-api", "deploy", now=NOW)
    assert again.verdict == "ok"
    other = trust.evaluate(db, "dori-api", "migrate", now=NOW)
    assert other.reason == "new kind of action"
    rows = _audits(db, "trust.evaluate")
    assert [r["decision"] for r in rows] == ["ask", "ok", "ask"]
    assert rows[0]["system"] == "dori-api" and rows[0]["run"] == "R4"
    assert rows[0]["what"] == "deploy ? dori-api"  # cleaned: no fake codes or brackets


def test_evaluate_ok_marks_seen(db: sqlite3.Connection) -> None:
    store.add_rule(db, scope="dori", action="*", effect="allow", created_from="chat", now=NOW)
    d = trust.evaluate(db, "dori-web", "deploy", repo="dori", now=NOW)
    assert d.ok and d.rule is not None
    assert store.seen_actions(db, "dori-web") == {"deploy"}


@pytest.mark.parametrize(("system", "action"), [("*", "deploy"), ("x", "*"), ("", "deploy")])
def test_evaluate_refuses_wildcards_and_junk(
    db: sqlite3.Connection, system: str, action: str
) -> None:
    with pytest.raises(ValueError):
        trust.evaluate(db, system, action)


def test_notify_level_and_merge_decision(db: sqlite3.Connection) -> None:
    assert trust.merge_decision(db, "trading") == "ok"
    store.add_rule(db, scope="trading", action="merge", effect="ask", created_from="chat", now=NOW)
    assert trust.merge_decision(db, "trading") == "ask"
    assert trust.merge_decision(db, "dori") == "ok"
    store.add_rule(db, scope="*", action="merge", effect="ask", created_from="chat", now=NOW)
    store.add_rule(db, scope="dori", action="merge", effect="allow", created_from="chat", now=NOW)
    assert trust.merge_decision(db, "dori") == "ok"
    assert trust.merge_decision(db, "other") == "ask"
    rows = _audits(db, "trust.merge")
    assert [r["decision"] for r in rows] == ["ok", "ask", "ok", "ok", "ask"]
    store.add_rule(
        db, scope="trading", action="done", effect="notify_now", created_from="chat", now=NOW
    )
    assert trust.notify_level(db, "trading", "x", "done").level == "now"
    assert trust.notify_level(db, "dori", "x", "done").level == "digest"
