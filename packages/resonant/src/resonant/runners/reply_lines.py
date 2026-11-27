"""PER-352: the code-built lines under a brain turn's reply, without repeating its prose.

A Claude brain turn's reply is Claude's text and then the code-built report line of every
change that ran (``with_reports``): the owner's record of what ran, never Claude's words. On
2026-11-26/27 those lines mostly said again what the prose had just said (``iOS PR #64 is
merged.`` then ``Done: merge PR #64 in …``), and each run a turn started posted its own start
line before the reply. The rules here are the same for every instance and channel:

- **A line the prose already reports is dropped** (:func:`covered`): some sentence of the
  prose, with no negation and nothing still to come in it (``couldn't``, ``asked``,
  ``I'll``, ``once``), has the line's action word (a 4-letter prefix match:
  ``merge`` / ``merged``) and every number or code the line names (``#64``, ``R12``,
  ``DORI-1173``, each a whole token); a line with none, or a sentence that names other
  numbers or codes too, needs two thirds of its other words as well. Anything else
  stays, so a change the prose doesn't mention is never hidden.
- **Runs a turn started or continued get one line** (:func:`runs_line`), listing those the
  prose doesn't name by code: ``Started R13 (voyager-ios) and R14 (voyager-web).`` The run
  tools' own text (``Started.text``, ``Continued.text``: ``/runs to check, /cancel R5 to
  stop``) is for a slash command's reply, not a turn's.
- **The progress line** (``Did: …``, Claude's own changes) keeps only the items the prose
  doesn't report (:func:`did_line`).

Never from a model: this only removes code-built lines or shortens the run tools' text.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

# ``Started.text`` / ``Continued.text`` (runners/claude.py); tests tie them to these
_STARTED = re.compile(r"^On it: (R\d+) · (.+?)(?: \(new repo: [^)]*\))?\. Up to \d+m\. ")
_CONTINUED = re.compile(r"^Continuing (R\d+) · (.+?): it was stopped by ")
_NO_REPO = "no repo (scratch dir)"

# the label, then an extension's ``linear:`` (its name isn't the action)
_LABEL = re.compile(r"^(?:Done|Did|Approved)\s*:\s*(?:[a-z][\w-]*:\s+)?", re.IGNORECASE)
# an id is one token, prefix and all (``r12``, ``dori-1173``, ``#64``): ``12 files`` isn't R12
_TOKEN = re.compile(r"[a-z]+-\d+|[a-z]+\d+|#?\d+|[a-z]+")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_NEGATION = re.compile(
    r"\b(?:not|never|nothing|couldn'?t|can'?t|cannot|didn'?t|won'?t|wasn'?t|isn'?t|"
    r"hasn'?t|haven'?t|failed|unable|"
    # not a report of what ran: waiting on someone, or still to come
    r"asked|ask|waiting|pending|will|\w*'ll|once|if)\b"
)
_STOP = frozenset({
    "the", "and", "with", "from", "into", "that", "this", "then", "than", "for", "its",
    "was", "were", "have", "has", "been", "done", "did", "now", "runs", "claude", "makes",
    "change", "approved", "repo", "about", "after", "before", "over", "their", "them",
})  # fmt: skip
WORD_SHARE = 2 / 3  # of a line's words, when it names no number or code
PREFIX = 4  # letters of a word that must match (``merge`` / ``merged``)


@dataclass(frozen=True)
class RunMention:
    """A run a turn started or continued, from its tool's code-built text."""

    verb: str  # "started" or "continuing"
    code: str
    where: str | None  # the repo, None for a scratch run
    extras: tuple[str, ...] = ()  # the text's further lines (a heads-up, a limit)


def run_mention(report: str) -> RunMention | None:
    """The run in a start or continue tool's text, else None (any other report line)."""
    head, *rest = report.split("\n")
    for verb, pattern in (("started", _STARTED), ("continuing", _CONTINUED)):
        if (m := pattern.match(head)) is not None:
            where = m.group(2).strip()
            extras = tuple(x.strip() for x in rest if x.strip())
            return RunMention(verb, m.group(1), None if where == _NO_REPO else where, extras)
    return None


def names_run(prose: str | None, code: str) -> bool:
    return bool(prose) and re.search(rf"(?<![A-Za-z0-9]){code}(?![0-9])", prose or "") is not None


def runs_line(runs: Sequence[RunMention], prose: str | None) -> str | None:
    """One line for the runs the prose doesn't name, or None when it names them all:
    ``Started R13 (voyager-ios) and R14 (voyager-web); continuing R12 (voyager-service).``"""
    groups: dict[str, list[str]] = {}
    for r in runs:
        if names_run(prose, r.code):
            continue
        shown = f"{r.code} ({r.where})" if r.where else r.code
        if shown not in groups.setdefault(r.verb, []):
            groups[r.verb].append(shown)
    parts = [f"{verb} {_and(items)}" for verb, items in groups.items() if items]
    if not parts:
        return None
    text = "; ".join(parts)
    return text[0].upper() + text[1:] + "."


def _and(items: Sequence[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + f" and {items[-1]}"


def covered(line: str, prose: str | None) -> bool:
    """Whether ``prose`` already reports what ``line`` (a code-built report line) says."""
    if not prose:
        return False
    tokens = _TOKEN.findall(_LABEL.sub("", line.split("\n", 1)[0]).lower())
    ids = {t.lstrip("#") for t in tokens if any(c.isdigit() for c in t)}
    words = [t for t in tokens if t.isalpha() and len(t) >= PREFIX and t not in _STOP]
    verb = next((t for t in tokens if t.isalpha() and len(t) >= 3), None)
    if verb is None or (not ids and len(words) < 2):
        return False
    for sentence in _SENTENCE.split(prose.lower()):
        if _NEGATION.search(sentence):
            continue
        said = _TOKEN.findall(sentence)
        said_ids = {t.lstrip("#") for t in said if any(c.isdigit() for c in t)}
        stems = {t[:PREFIX] for t in said if t.isalpha()}
        if verb[:PREFIX] not in stems or not ids <= said_ids:
            continue
        # a sentence about other things too (``created DORI-6 and linked DORI-5``) needs the
        # line's words as well, like a line that names no id
        if ids and said_ids <= ids:
            return True
        if words and sum(w[:PREFIX] in stems for w in words) >= WORD_SHARE * len(words):
            return True
    return False


def tidy(reports: Sequence[str], prose: str | None) -> list[str]:
    """``reports`` as they go under a turn's reply: the run tools' texts as one runs line
    (where the first of them was), and every other line the prose already reports dropped.
    ``prose``: Claude's text as sent with them, None when it isn't (every line stays, the
    runs still in one line)."""
    out: list[str] = []
    runs: list[RunMention] = []
    at: int | None = None
    for report in reports:
        if not report:
            continue
        if (run := run_mention(report)) is not None:
            runs.append(run)
            at = len(out) if at is None else at
            out.extend(x for x in run.extras if x not in out)
        elif not covered(report, prose) and report not in out:
            out.append(report)
    if at is not None and (line := runs_line(runs, prose)) is not None:
        out.insert(at, line)
    return out


def did_line(did: Sequence[str], prose: str | None) -> str | None:
    """The progress line of Claude's own changes the prose doesn't report, or None."""
    kept = [d for d in dict.fromkeys(did) if d and not covered(d, prose)]
    return "Did: " + ", ".join(kept) if kept else None
