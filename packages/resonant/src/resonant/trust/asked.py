"""An explicit ask is the approval (PER-272): code decides whether the owner's own message
this turn directly asks for exactly one change.

When the owner texts "merge the dark mode pr", "file a ticket in PER: <title>" or "don't ask
me before Linear tickets", the change they asked for runs and is reported, with no "OK to
…? yes / no" in between. It is a trust widening, so it is decided here, by code, from the
owner's own text and the call's args; the model's say-so is never enough:

1. **The brain marks the call** (``owner_asked: true`` on a tool call over the Claude bridge,
   ``runners/ask_tools.py``). Unmarked calls never get here.
2. **The text is the owner's own**, read by code: the bridge takes it from the inbound
   message event that started the asking chat turn (``imessage``, type ``message``, from
   the owner), never from the model, the question it was handed, history or a tool result.
   A job, a watch, Resonant's own follow-up turn (``approved``) or a member has none.
3. **The turn is untainted** (``Intent.origin == "inferred"``). The one exception is
   ``merge_run``, which needs a run code: a read of Resonant's own run list (``list_runs``,
   ``RUN_LOOKUPS``) doesn't stop it, since the run is still bound to the owner's words below
   (``OwnerAsk.lookups``). Any other outside content (an extension's result: GitHub, Linear,
   mycelium; Claude's own tools; a tainted turn or history) stops it.
4. **The gate's own rules**: the owner's call, an L2 change (never L1, pay or L3), autonomy
   on, not dry run, not past the task's unasked changes (``capped``), not one the owner
   already said no to (``PolicyGate._decide``).
5. **The text names this change** (``OwnerAsks.check``), per tool (a tool without a matcher
   never qualifies):

   - every tool: no relayed instruction ("do what R1 said", "as it suggests"), no question
     or hypothetical about it ("should I merge #41?", "if CI passes …"), and the change's
     verb not negated ("don't merge it").
   - ``merge_run{code}``: the verb ``merge`` and the run named: its code (``R3``), its PR
     (``#41``, ``pr 41``) or a word of its ask or repo ("the dark mode pr"), and no other
     ready run named the same way (ambiguous: "which PR?" asks).
   - ``linear.save_issue`` creating an issue (no ``id``): a filing verb (file, make, create,
     open, add, log, raise) and a ticket word (ticket, issue, bug, linear), and the title's
     words in the text (at least two, and most of them). An update by id (close, edit)
     still asks.
   - ``allow_tools{tools}``: a "don't ask" phrase ("don't ask", "stop asking", "without
     asking") and every tool or extension the call names, named in the text ("linear",
     "merging" for ``merge_run``).

A call that passes runs as an approval would (``PolicyGate._owner_asked``): an approval row
already ``approved`` by the owner (``decided_by`` ``owner_asked``), an ``approval.resolve``
audit row with ``decision: owner_asked`` and the event id, and the executor spends it like
any other approval, so the report line, ``/approvals`` history, the dashboard and the
briefing see it like a yes. Anything that fails a check asks the plain yes, as before.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import cast

from resonant.gate.types import Intent, OwnerAsk
from resonant.store import repos as repos_store
from resonant.store import runs as runs_store
from resonant.trust import merge_decision
from resonant_sdk import Effect, Level, ToolSpec

# Resonant's own reads that may stand between the owner's ask and a merge: the run list (a
# merge needs the run's code). Their result is outside content (a worker's summary), so the
# turn is still tainted for everything else.
RUN_LOOKUPS: frozenset[str] = frozenset({"list_runs"})

TEXT_MAX = 1000  # a longer message is a letter, not one direct ask

_WORD = re.compile(r"[a-z0-9]+(?:'[a-z]+)?")
_CONTRACTIONS: dict[str, str] = {
    "don't": "dont", "can't": "cant", "won't": "wont", "doesn't": "doesnt",
    "shouldn't": "shouldnt", "isn't": "isnt", "didn't": "didnt",
}  # fmt: skip
NEGATORS: frozenset[str] = frozenset(
    {"dont", "not", "never", "no", "cant", "wont", "doesnt", "shouldnt", "didnt", "stop",
     "without", "avoid", "hold", "wait"}
)  # fmt: skip
# A relayed instruction: the ask is someone else's ("do what R1 said").
RELAYED: frozenset[str] = frozenset(
    {"said", "says", "saying", "told", "tells", "suggest", "suggests", "suggested",
     "recommend", "recommends", "recommended", "according", "wants", "wanted", "asked",
     "asks", "instructed", "instructions"}
)  # fmt: skip
# A question or a hypothetical about the change, not an ask for it.
_NOT_AN_ASK = re.compile(
    r"^(should|shall|would it|is it|was it|do you think|what if|if|whether|when|once|after|"
    r"maybe|might|in case)\b|\b(should|shall|can|could|do|may) (i|we)\b|\bwhat if\b|"
    r"\bwould (it|that) be\b|\bis it (ok|okay|safe|fine)\b"
)
# A question asked as a request ("can you merge #41?").
_REQUEST = re.compile(r"^(can|could|would|will) you\b|^please\b|^pls\b")
_QUESTION_START = re.compile(
    r"^(what|whats|how|hows|why|where|wheres|who|which|is|are|was|were|did|does|has|have|"
    r"had|will|wont|am|any)\b"
)
_STOP: frozenset[str] = frozenset(
    {"a", "an", "the", "and", "or", "to", "for", "of", "in", "on", "at", "by", "with", "it",
     "its", "this", "that", "these", "those", "my", "me", "i", "you", "your", "we", "our",
     "is", "are", "be", "was", "now", "just", "please", "pls", "go", "ahead", "then", "so",
     "up", "out", "into", "from", "as", "all", "any", "one", "new", "also", "too", "can",
     "could", "would", "will", "do", "get", "let", "lets", "ok", "okay", "thanks", "hey"}
)  # fmt: skip
# Words that say what kind of thing a run or PR is, not which one.
_GENERIC: frozenset[str] = frozenset(
    {"pr", "prs", "pull", "request", "merge", "merging", "run", "runs", "branch", "change",
     "changes", "add", "fix", "update", "make", "remove", "use", "support", "code", "repo",
     "ticket", "issue", "file", "create"}
)  # fmt: skip

ExtTools = Callable[[str], Sequence[ToolSpec]]  # an extension's loaded chat tools


def words(text: str) -> list[str]:
    """The text's words, lower case, curly quotes straightened, contractions joined
    (``don't`` -> ``dont``)."""
    flat = text.lower().replace("\u2019", "'").replace("\u2018", "'")
    found: list[str] = _WORD.findall(flat)
    return [_CONTRACTIONS.get(w) or w.replace("'", "") for w in found]


def stem(word: str) -> str:
    """A crude stem, enough to match ``merging``/``merge`` and ``tickets``/``ticket``."""
    for suffix in ("ing", "ed", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 3:
            word = word[: -len(suffix)]
            break
    return word.rstrip("e") or word


def _stems(ws: Iterable[str]) -> set[str]:
    return {stem(w) for w in ws}


def _content(ws: Iterable[str]) -> list[str]:
    """The words that pick a thing out: not stop words, not generic, at least 3 letters."""
    return [w for w in ws if len(w) >= 3 and w not in _STOP and w not in _GENERIC]


def negated(ws: Sequence[str], at: int) -> bool:
    """A negator in the two words before ``ws[at]`` ("don't merge", "do not merge")."""
    return any(w in NEGATORS for w in ws[max(0, at - 3) : at])


def _verb_at(ws: Sequence[str], verbs: frozenset[str]) -> list[int]:
    return [i for i, w in enumerate(ws) if w in verbs]


# A word after the verb that makes it a noun ("the merge status", "a merge conflict").
_NOUN_AFTER: frozenset[str] = frozenset(
    {"status", "state", "conflict", "conflicts", "queue", "check", "checks", "commit",
     "request", "requests", "button", "rules", "rule", "time", "window"}
)  # fmt: skip


def _asks(ws: Sequence[str], verbs: frozenset[str]) -> bool:
    """One of ``verbs`` is in the text as a verb ("merge #41", not "the merge status") and
    isn't negated."""
    found = [i for i in _verb_at(ws, verbs) if i + 1 >= len(ws) or ws[i + 1] not in _NOUN_AFTER]
    return bool(found) and not all(negated(ws, i) for i in found)


@dataclass(frozen=True)
class _Text:
    raw: str
    ws: list[str]

    @property
    def joined(self) -> str:
        return " ".join(self.ws)


def text_problem(text: str) -> str | None:
    """Why the owner's text can't be a direct ask for any change, or None."""
    if not text.strip():
        return "no text"
    if len(text) > TEXT_MAX:
        return "too long"
    ws = words(text)
    if any(w in RELAYED for w in ws):
        return "relayed"
    joined = " ".join(ws)
    request = _REQUEST.match(joined) is not None
    if _NOT_AN_ASK.search(joined) or (not request and _QUESTION_START.match(joined)):
        return "a question"
    if text.rstrip().endswith("?") and not request:
        return "a question"
    return None


# --- per tool ---------------------------------------------------------------------------------

MERGE_VERBS: frozenset[str] = frozenset({"merge"})
FILE_VERBS: frozenset[str] = frozenset(
    {"file", "make", "create", "open", "add", "log", "raise", "submit", "write", "new"}
)
TICKET_WORDS: frozenset[str] = frozenset(
    {"ticket", "tickets", "issue", "issues", "bug", "bugs", "linear"}
)
# A standing "don't ask": about a kind of change from now on ("don't ask me before Linear
# tickets", "stop asking before merging"), never a one-off ("merge R3 without asking").
_DONT_ASK = re.compile(
    r"\b(dont|do not|stop|quit|no need to|never)\s+(ask|asking|prompt|prompting)"
    r"(\s+(me|us))?\s+(before|about|for|when|on|again|anymore|first)\b"
    r"|\bno more (permission )?(prompts|asking|approvals)\b"
)
_RUN_CODE = re.compile(r"r(\d{1,9})")  # a whole word
_PR_REF = re.compile(r"(?:#|\bpr #?|\bpull #?|\bnumber #?)(\d{1,9})\b")
TITLE_MIN_WORDS = 2
TITLE_SHARE = 0.6  # of the title's content words, in the owner's text
# Words that stand for a builtin tool's name in a "don't ask" text, besides its own words.
_SYNONYMS: Mapping[str, frozenset[str]] = {
    "issu": frozenset({"ticket", "issu", "bug"}),
    "comment": frozenset({"comment"}),
    "watch": frozenset({"watch", "monitor"}),
}
# The args a new ticket may carry on the owner's ask; anything else asks.
CREATE_ARGS: frozenset[str] = frozenset({"title", "team", "description", "priority", "labels"})
# The kinds of change an extension-wide "don't ask" must name.
CHANGE_WORDS: frozenset[str] = frozenset({"ticket", "issu", "bug", "comment", "chang"})
_NAME_GENERIC: frozenset[str] = frozenset(
    {"create", "save", "add", "list", "set", "tool", "tools", "run", "start", "update", "get"}
)


def _code(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    m = re.fullmatch(r"\s*[rR](\d{1,9})\s*", value)
    return f"R{int(m.group(1))}" if m else None


def _ready(run: runs_store.Run) -> bool:
    return run.status == "ready" and run.pr_number is not None


class OwnerAsks:
    """The gate's check of an ``owner_asked`` call (``PolicyGate`` ``owner_asks``)."""

    def __init__(self, conn: sqlite3.Connection, ext_tools: ExtTools | None = None) -> None:
        self.conn = conn
        self.ext_tools: ExtTools = ext_tools or (lambda _name: ())
        self._matchers: dict[tuple[str | None, str], Callable[[Intent, _Text], str | None]] = {
            (None, "merge_run"): self._merge_run,
            (None, "allow_tools"): self._allow_tools,
            ("linear", "save_issue"): self._save_issue,
        }

    def askable(self, spec: ToolSpec) -> bool:
        """Whether a call of ``spec`` can be the owner's direct ask at all (the bridge offers
        the ``owner_asked`` mark only on these)."""
        return (
            (spec.extension, spec.name) in self._matchers
            and spec.effect is Effect.WRITE
            and spec.effective_level is Level.L2
        )

    def check(self, intent: Intent) -> str | None:
        """Why this call is not the owner's direct ask (a short code-built reason for the
        audit), or None when it is. The gate checks the origin, principal, level and kill
        switch itself; this re-checks the parts that are this module's."""
        ask = intent.asked
        if ask is None:
            return "not marked"
        if not self.askable(intent.spec):
            return "not an askable tool"
        if intent.origin == "tainted":
            lookups_ok = (
                intent.extension is None
                and intent.tool == "merge_run"
                and ask.lookups
                and ask.lookups <= RUN_LOOKUPS
            )
            if not lookups_ok:
                return "tainted"
        elif intent.origin != "inferred":
            return f"origin {intent.origin}"
        elif ask.lookups:
            return "lookups without taint"  # the bridge never sends this: fail closed
        if (why := text_problem(ask.text)) is not None:
            return why
        matcher = self._matchers[(intent.extension, intent.tool)]
        return matcher(intent, _Text(ask.text, words(ask.text)))

    # -- merge_run --

    def _named_run(self, run: runs_store.Run, t: _Text) -> bool:
        """The text names this run: its code, its PR number, or by words: every content word
        of the text is a word of the run's ask or repo, and there are at least two of them,
        or the one is the repo's name ("merge the dark mode pr", "merge the site pr"). One
        shared word with a longer text ("merge the auth pr" for "refactor auth middleware"
        plus more) is not enough."""
        lowered = t.raw.lower()
        if run.code.lower() in t.ws:
            return True
        if run.pr_number is not None and any(
            int(n) == run.pr_number for n in _PR_REF.findall(lowered)
        ):
            return True
        mine = _stems(_content(words(f"{run.ask} {run.repo}")))
        said = _stems(_content(t.ws))
        if not said or not said <= mine:
            return False
        return len(said) >= 2 or said == _stems(_content(words(run.repo)))

    def _merge_run(self, intent: Intent, t: _Text) -> str | None:
        if not _asks(t.ws, MERGE_VERBS):
            return "no merge verb"
        code = _code(intent.args.get("code"))
        run = runs_store.get(self.conn, code) if code is not None else None
        if run is None or not _ready(run):
            return "no such ready run"
        if not self._named_run(run, t):
            return "run not named"
        # What the yes/no prompt would have shown asks still: flagged paths, a rule that says
        # to ask before merging there. (A repo with no CI is refused by the strict merge.)
        if run.flagged_paths:
            return "flagged paths"
        repo = repos_store.find(self.conn, run.repo) if run.repo else None
        if repo is None or repo.mode != "push":
            return "not a repo I merge"
        if merge_decision(self.conn, repo.name) != "ok":
            return "a rule asks"
        # Another run named (by code, PR or words): ambiguous, ask which.
        named_codes = {
            f"R{int(m.group(1))}" for w in t.ws if (m := _RUN_CODE.fullmatch(w)) is not None
        }
        if named_codes - {run.code}:
            return "another run named"
        # Another ready or still-working run named the same way: which one? Ask.
        others = [
            r
            for r in runs_store.recent(self.conn, 50)
            if r.code != run.code and (_ready(r) or r.active)
        ]
        if any(self._named_run(r, t) for r in others):
            return "ambiguous"
        return None

    # -- linear save_issue (create) --

    def _save_issue(self, intent: Intent, t: _Text) -> str | None:
        args = intent.args
        if any(args.get(k) not in (None, "") for k in ("id", "identifier")):
            return "an update"  # closing or editing an issue still asks
        if set(args) - CREATE_ARGS:
            return "extra args"  # an assignee, a parent, a state: what the text didn't say
        if not _asks(t.ws, FILE_VERBS) or not any(w in TICKET_WORDS for w in t.ws):
            return "no filing verb"
        title = args.get("title")
        if not isinstance(title, str):
            return "no title"
        wanted = _stems(_content(words(title)))
        have = _stems(t.ws)
        if len(wanted) < TITLE_MIN_WORDS:
            return "title too short"
        if len(wanted & have) < max(TITLE_MIN_WORDS, round(TITLE_SHARE * len(wanted))):
            return "title not named"
        return None

    # -- allow_tools --

    def _named_tool(self, raw: str, t: _Text) -> bool:
        name = raw.strip().lower().removeprefix("mcp__resonant__")
        have = _stems(t.ws)
        if "." in name:
            ext, tool = name.split(".", 1)
            return stem(ext) in have and self._tool_words(tool, have)
        if self.ext_tools(name):
            # an extension covers its changes (tickets and comments for linear): the text
            # names it and a kind of change ("linear tickets", "linear comments")
            return stem(name) in have and bool(have & CHANGE_WORDS)
        return self._tool_words(name, have)

    @staticmethod
    def _tool_words(name: str, have: set[str]) -> bool:
        """Every word of a tool's name (but generic verbs) is in the text, or a synonym:
        ``merge_run`` by "merging", ``save_issue`` by "tickets"."""
        parts = [stem(p) for p in re.split(r"[_\-]", name) if p and p not in _NAME_GENERIC]
        if not parts:
            return False
        return all(have & _SYNONYMS.get(p, frozenset({p})) for p in parts)

    def _allow_tools(self, intent: Intent, t: _Text) -> str | None:
        if not _DONT_ASK.search(t.joined):
            return "no dont-ask phrase"
        raw: object = intent.args.get("tools")
        items = cast(list[object], raw) if isinstance(raw, list) else []
        names = [n for n in items if isinstance(n, str)]
        if not names or len(names) != len(items):
            return "no tools"
        if not all(self._named_tool(n, t) for n in names):
            return "tool not named"
        return None


__all__ = ["RUN_LOOKUPS", "OwnerAsk", "OwnerAsks", "text_problem", "words"]
