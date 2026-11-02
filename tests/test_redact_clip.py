"""``redact.clip_text`` (the one pass every dashboard text goes through) and the relay's secret
pattern (``SECRET_SQL_FAMILY``, which ``relay_ingest`` applies in Postgres)."""

from __future__ import annotations

import random
import re
from pathlib import Path

import pytest

from resonant.redact import (
    CUT_MARK,
    RC_LINK_SQL,
    RC_MARK,
    REDACT_SLACK,
    REDACTED,
    SECRET_SQL_FAMILY,
    Redactor,
    clip_text,
    redact_secrets,
    scrub_rc_links,
)

MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "20261102000000_relay_full_snapshot.sql"
)
# Postgres's ``\y`` (a word boundary) is Python's ``\b``; the rest is the same syntax.
SQL_RE = re.compile(SECRET_SQL_FAMILY.replace(r"\y", r"\b"))


# --- clip_text ---------------------------------------------------------------------------


def test_none_and_short_text_pass_through() -> None:
    assert clip_text(None, 10) is None
    assert clip_text("", 10) == ""
    assert clip_text("hello", 10) == "hello"
    assert clip_text("x" * 10, 10) == "x" * 10  # exactly the cap: not cut


@pytest.mark.parametrize("cap", [16, 40, 300, 800])
def test_a_long_text_is_cut_to_the_cap_with_the_mark_inside_it(cap: int) -> None:
    for n in (cap + 1, cap + REDACT_SLACK, cap + REDACT_SLACK + 1, cap * 10):
        out = clip_text("y" * n, cap)
        assert out is not None and len(out) == cap and out.endswith(CUT_MARK)
        assert out == "y" * (cap - len(CUT_MARK)) + CUT_MARK


def test_a_secret_across_the_cut_is_whole_when_it_is_seen() -> None:
    token = "ghp_" + "A1b2C3d4" * 4
    text = "x" * 290 + token + " tail"
    out = clip_text(text, 295)
    assert out is not None and len(out) <= 295 and out.endswith(CUT_MARK)
    assert "ghp_" not in out and "A1b2" not in out
    # And one that starts before the cap and runs past the slack is still replaced.
    huge = "x" * 279 + " sk-" + "Z" * 2000
    out = clip_text(huge, 300)
    assert out is not None and "ZZZZ" not in out and "sk-" not in out


def test_known_values_are_replaced_after_the_patterns_and_before_the_cut() -> None:
    token = "9f2c" * 16
    out = clip_text(f"a {token} b ghp_{'Q1w2' * 8} c", 100, known=Redactor([token]))
    assert out == f"a {REDACTED} b {REDACTED} c"
    out = clip_text(f"{token} " + "z" * 200, 40, known=Redactor([token]))
    assert out is not None and out.startswith(f"{REDACTED} zzz") and len(out) == 40
    assert clip_text(token, 100, known=Redactor()) == token  # an empty redactor is a no-op


def test_a_cut_after_a_redaction_that_shrinks_the_text() -> None:
    """A text longer than the cap whose redacted form fits: it was still cut (by the scan
    window), so it says so."""
    token = "ghp_" + "A1b2C3d4" * 40  # 324 characters, replaced by 10
    out = clip_text("a" + token + "b" * 600, 300)
    assert out is not None and out.endswith(CUT_MARK) and len(out) <= 300


# --- the relay's pattern -----------------------------------------------------------------

PREFIXES = [
    "ghp_", "gho_", "ghu_", "ghs_", "ghr_", "github_pat_", "sk-", "sk-ant-api03-", "xoxb-",
    "xoxa-", "xoxp-", "xoxe-", "AKIA", "ASIA", "AIza", "glpat-", "npm_", "lin_api_",
    "sb_secret_", "eyJ", "Bearer ", "bearer  ", "Bearer\t", "-----BEGIN PRIVATE KEY",
    "-----BEGIN RSA PRIVATE KEY", "-----BEGIN OPENSSH PRIVATE KEY",
]  # fmt: skip
TAIL = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-._~+/="
NOISE = " \n\t:;,'\"()[]{}<>é✓_-/=.@"


def _sample(rng: random.Random) -> str:
    prefix = rng.choice(PREFIXES)
    body = "".join(rng.choice(TAIL) for _ in range(rng.randint(0, 60)))
    if prefix == "eyJ":  # a JWT is three parts
        parts = [
            "".join(rng.choice(TAIL[:64]) for _ in range(rng.randint(5, 14))) for _ in range(3)
        ]
        body = f"{parts[0]}.eyJ{parts[1]}.{parts[2]}"
    noise = lambda: "".join(rng.choice(NOISE) for _ in range(rng.randint(0, 3)))  # noqa: E731
    return f"{noise()}{prefix}{body}{noise()}"


def test_the_migration_holds_this_exact_pattern() -> None:
    sql = MIGRATION.read_text()
    assert SECRET_SQL_FAMILY in sql
    assert sql.count(SECRET_SQL_FAMILY) == 1 and "p_data::text ~ '" + SECRET_SQL_FAMILY + "'" in sql


@pytest.mark.parametrize(
    "sample",
    [
        "ghp_" + "A" * 20,
        "ghs_" + "a1" * 15,
        "github_pat_" + "11ABCDEFG0abcdefghij_klmnopq",
        "sk-" + "x" * 20,
        "sk-ant-api03-" + "Y" * 40,
        "xoxb-" + "1234567890-abc",
        "xoxe-" + "1" * 10,
        "AKIA" + "IOSFODNN7EXAMPLE",
        "ASIA" + "A" * 16,
        "AIza" + "a" * 35,
        "glpat-" + "g" * 20,
        "npm_" + "n" * 36,
        "lin_api_" + "l" * 20,
        "sb_secret_" + "s" * 10,
        "eyJhbGciOiJI" + ".eyJzdWIiOiIx" + ".SflKxwRJSM",
        "Bearer " + "t" * 12,
        "bearer\t" + "t" * 30,
        "-----BEGIN PRIVATE KEY",
        "-----BEGIN OPENSSH " + "PRIVATE KEY-----",
        "token: Bearer " + "ab" * 10 + " end",
        'secret "sk-' + "q" * 25 + '"',
    ],
)
def test_every_family_of_the_sql_pattern_is_replaced_in_python(sample: str) -> None:
    assert SQL_RE.search(sample), "the sample must be one the relay refuses"
    assert not SQL_RE.search(redact_secrets(sample)), redact_secrets(sample)


@pytest.mark.parametrize("seed", range(40))
def test_python_replaces_everything_the_sql_pattern_refuses(seed: int) -> None:
    """Over random credential-shaped text (right and wrong lengths, noise around, unicode
    next to the token): whatever the relay's pattern matches, ``redact_secrets`` has replaced,
    so a snapshot that went through it never trips the database."""
    rng = random.Random(seed)
    for _ in range(200):
        text = " ".join(_sample(rng) for _ in range(rng.randint(1, 4)))
        if SQL_RE.search(text):
            cleaned = redact_secrets(text)
            assert not SQL_RE.search(cleaned), (text, cleaned)


def test_sql_pattern_leaves_ordinary_text_alone() -> None:
    for text in (
        "fix the flaky test in dori",
        "task-force-aaaaaaaaaaaaaaaaaaaaaaaa",
        "risk-assessment-aaaaaaaaaaaaaaaaaaaa",
        "ask-me-anything-aaaaaaaaaaaaaaaaaaaa",
        "resonant/r12-per-276-dashboard",
        "https://github.com/me/dori/pull/9",
        "Bearer token",
        "AKIA short",
        "9f2c1e7ab3d4" * 5,
    ):
        assert not SQL_RE.search(text), text


def test_remote_control_links_are_scrubbed_and_the_migration_refuses_them() -> None:
    url = "https://claude.ai/code/session_" + "01AbC-xyz"
    assert scrub_rc_links(f"a {url}, b {url}!") == f"a {RC_MARK}, b {RC_MARK}!"
    assert clip_text(f"see {url} please", 100) == f"see {RC_MARK} please"
    assert "https://claude.ai/code/" not in (clip_text("x" * 290 + url, 295) or "")
    assert f"p_data::text ~ '{RC_LINK_SQL}'" in MIGRATION.read_text()
    assert re.search(RC_LINK_SQL.replace("\\.", "."), url)
