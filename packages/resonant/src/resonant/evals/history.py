"""Draft a task-eval set from the owner's past texts (PER-248).

``resonant eval draft --from-history [--since …] [--limit N] [--out PATH] [--json]``

Every brain score so far came from the synthetic ``evals/task.example.jsonl``. This drafts
rows from what the owner really sent, so the owner only reviews and corrects them:

- **Reads** the store (read-only, ``mode=ro``): the owner's ``chat`` tasks and the
  ``imessage`` event each came from (the text), the gate's ``gate.evaluate`` audit rows on
  the task's trace (the tools called, by the local model or the Claude brain, and what the
  gate decided), the approvals the owner answered, the reply (the brain's, or Claude's for
  a hand-off) and the task's route and status.
- **Drafts** one ``task_eval`` row per text, with suggested expectations (``route``,
  ``tools``, ``tools_not``, ``ask``, ``writes``), ``review: true``, a ``note`` saying what
  can't be replayed faithfully, and a ``draft`` object (the task id, the time, the status
  and the reply then) for the owner's review. ``resonant eval task`` doesn't grade a row
  until the owner clears ``review``.
- **World.** Each row gets a world as it was when the text came in: the repos registered
  then, and, when the turn used them, the runs, notes and reminders of that moment, the
  facts (as of the draft) and the extension fixtures (``task_eval.FIXTURES``, canned
  defaults). Real files, git history, transcripts and extensions without a fixture aren't
  copied: the note names them.
- **Redaction.** Approval replies (``yes``, ``no K4``…) are skipped. Texts, replies and run
  asks lose anything that could pass for an approval code, credential-shaped strings, and
  other people's handles (every principal's identities, emails, phone numbers,
  ``@mentions``). A text that held a credential is skipped whole.
- **Dedupe.** Near-identical texts (normalized, then a 0.9 similarity ratio) are drafted
  once, the newest kept; texts and task ids already in the file are never drafted again.
- **Merge.** An existing set keeps every row (and the owner's edits) as it is; new rows are
  appended. The file is written 0600 under ``<home>/evals/`` and checked with
  ``task_eval.load_set`` before it replaces the old one.

Local only: it reads the store and writes one file under the home. Nothing is sent.
"""

from __future__ import annotations

import contextlib
import difflib
import hashlib
import json
import os
import re
import sqlite3
import tempfile
import unicodedata
import urllib.parse
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import yaml

from resonant.brain.facts import FactStore, facts_dir
from resonant.config import Settings
from resonant.evals.task_eval import (
    FIXTURES,
    EvalSetError,
    load_set,
    owner_set_path,
    parse_case,
    set_lines,
)
from resonant.gate.codes import mask_codes
from resonant.gate.replies import parse_reply
from resonant.principals import load_principals
from resonant.tools.files import redact_secrets

EXIT_OK = 0
EXIT_BAD = 2

DEFAULT_LIMIT = 200
SIMILAR = 0.9  # difflib ratio at or above which two normalized texts are one case
HISTORY_WINDOW = timedelta(minutes=30)
HISTORY_MAX = 3
REPLY_MAX = 500
RUNS_MAX = 5
RUNS_RECENT = timedelta(days=1)  # runs that ended this long before the text still show
REPOS_MAX = 30
NOTES_MAX = 20
REMINDERS_MAX = 20
FACTS_MAX = 20
ASK_CLAUDE = "ask_claude"
WRITE_EFFECTS = frozenset({"write", "pay"})

RUN_TOOLS = frozenset(
    {"list_runs", "run_activity", "read_transcript", "merge_run", "cancel_run",
     "message_worker", "answer_worker", "projects", "open_items", "start_run"}
)  # fmt: skip
NOTE_TOOLS = frozenset({"list_notes", "add_note", "delete_note", "clear_notes"})
REMINDER_TOOLS = frozenset({"list_reminders", "add_reminder", "cancel_reminder"})
FACT_TOOLS = frozenset({"list_facts", "remember", "forget"})
FILE_TOOLS = frozenset(
    {"read_file", "list_dir", "search", "tail_log", "recent_logs", "read_transcript",
     "recent_changes"}
)  # fmt: skip
MINI_TOOLS = frozenset(
    {"daemon_status", "system_status", "disk", "model_status", "restart_ollama",
     "restart_resonant", "update_resonant", "tail_log", "recent_logs", "task_counts"}
)  # fmt: skip


# --- reading the store ------------------------------------------------------------------


@dataclass(frozen=True)
class Call:
    """One tool call the gate evaluated on a chat's trace."""

    tool: str
    extension: str | None
    effect: str | None
    decision: str  # Allow, NeedsApproval, Deny
    approval_id: str | None


@dataclass(frozen=True)
class Chat:
    """One past owner text and what came of it, as stored (not yet scrubbed)."""

    task_id: str
    trace_id: str
    at: datetime
    text: str
    reply: str
    status: str
    runner: str
    route: str | None  # the brain's route (local or claude); None on the intents path
    handoff: str | None
    tainted: bool
    voice: bool
    calls: tuple[Call, ...] = ()
    rejected: tuple[str, ...] = ()  # tools whose approval the owner said no to


def open_store(path: Path) -> sqlite3.Connection:
    """The store, read-only: drafting never writes to it."""
    if not path.is_file():
        raise FileNotFoundError(path)
    uri = f"file:{urllib.parse.quote(str(path))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _json(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        v: Any = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return cast(dict[str, Any], v) if isinstance(v, dict) else {}


def _when(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def read_chats(
    conn: sqlite3.Connection, owner: str, *, since: datetime | None = None
) -> list[Chat]:
    """The owner's chat texts (``kind = 'chat'`` from an ``imessage`` message), oldest
    first, with their reply, route and status. Calls are filled in by ``with_calls``."""
    rows = conn.execute(
        """SELECT t.id, t.trace_id, t.runner, t.status, t.checkpoint, t.result,
                  e.payload, e.occurred_at
           FROM tasks t JOIN events e ON e.id = t.origin_event_id
           WHERE t.kind = 'chat' AND t.principal = ? AND e.source = 'imessage'
             AND e.type = 'message'""",
        (owner,),
    ).fetchall()
    answers = _claude_answers(conn)
    out: list[Chat] = []
    for r in rows:
        at = _when(r["occurred_at"])
        payload = _json(r["payload"])
        text = payload.get("text")
        if at is None or not isinstance(text, str) or not text.strip():
            continue
        if since is not None and at < since:
            continue
        cp, result = _json(r["checkpoint"]), _json(r["result"])
        said = answers.get(r["trace_id"]) or cp.get("reply") or cp.get("fast_reply")
        route = result.get("route") if r["runner"] == "brain" else None
        out.append(
            Chat(
                task_id=r["id"],
                trace_id=r["trace_id"],
                at=at,
                text=text,
                reply=said if isinstance(said, str) else "",
                status=str(r["status"]),
                runner=str(r["runner"]),
                route=route if route in ("local", "claude") else None,
                handoff=h if isinstance(h := result.get("handoff"), str) else None,
                tainted=result.get("tainted") is True,
                voice=bool(payload.get("voice")),
            )
        )
    out.sort(key=lambda c: (c.at, c.task_id))
    return out


def _claude_answers(conn: sqlite3.Connection) -> dict[str, str]:
    """trace id -> the first ask task's answer (a turn handed to Claude)."""
    out: dict[str, str] = {}
    for r in conn.execute(
        "SELECT trace_id, checkpoint FROM tasks WHERE runner = 'ask' ORDER BY rowid"
    ):
        said = _json(r["checkpoint"]).get("reply")
        if isinstance(said, str) and said and r["trace_id"] not in out:
            out[r["trace_id"]] = said
    return out


def with_calls(conn: sqlite3.Connection, chats: Sequence[Chat]) -> list[Chat]:
    """Each chat with the gate's evaluations on its trace and the approvals refused."""
    wanted = {c.trace_id for c in chats}
    calls: dict[str, list[Call]] = {}
    for r in conn.execute(
        "SELECT trace_id, tool, extension, effect, decision, detail FROM audit_log"
        " WHERE action = 'gate.evaluate' AND trace_id IS NOT NULL ORDER BY id"
    ):
        if r["trace_id"] not in wanted or not r["tool"]:
            continue
        approval = _json(r["detail"]).get("approval_id")
        calls.setdefault(r["trace_id"], []).append(
            Call(
                tool=str(r["tool"]),
                extension=r["extension"],
                effect=r["effect"],
                decision=str(r["decision"] or ""),
                approval_id=approval if isinstance(approval, str) else None,
            )
        )
    rejected: dict[str, list[str]] = {}
    for r in conn.execute(
        "SELECT t.trace_id, a.tool FROM approvals a JOIN tasks t ON t.id = a.task_id"
        " WHERE a.status = 'rejected' ORDER BY a.created_at"
    ):
        if r["trace_id"] in wanted:
            rejected.setdefault(r["trace_id"], []).append(str(r["tool"]))
    return [
        _with(c, tuple(calls.get(c.trace_id, ())), tuple(rejected.get(c.trace_id, ())))
        for c in chats
    ]


def _with(chat: Chat, calls: tuple[Call, ...], rejected: tuple[str, ...]) -> Chat:
    return replace(chat, calls=calls, rejected=rejected)


# --- redaction --------------------------------------------------------------------------

_EMAIL = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})+")
_PHONE = re.compile(r"(?<![\w+])\+?\(?\d[\d\s().-]{7,20}\d(?!\w)")
_MENTION = re.compile(r"(?<![\w@])@[A-Za-z0-9_][A-Za-z0-9_.-]{0,38}")
HANDLE = "[handle]"
WITHHELD = "[withheld: it held a credential]"


@dataclass
class Scrubber:
    """Removes approval codes, credential-shaped text and handles. ``found`` collects what
    it removed (``code``, ``secret``, ``handle``), per call to ``reset``."""

    handles: Sequence[str] = ()
    found: set[str] = field(default_factory=set[str])

    def __post_init__(self) -> None:
        known = sorted({h for h in self.handles if h}, key=len, reverse=True)
        self._known = (
            re.compile("|".join(re.escape(h) for h in known), re.IGNORECASE) if known else None
        )

    def reset(self) -> set[str]:
        found, self.found = self.found, set()
        return found

    def __call__(self, text: str) -> str:
        # NFKC first, as the reply parser does: a fullwidth A7 is A7; format characters
        # (zero-width spaces, joiners, bidi marks) go, so they can't split a handle or a code.
        norm = unicodedata.normalize("NFKC", text)
        out = "".join(ch for ch in norm if unicodedata.category(ch) != "Cf")
        if self._known is not None:
            out = self._sub(self._known, HANDLE, out, "handle")
        cleaned = redact_secrets(out)
        if cleaned != out:
            self.found.add("secret")
            out = cleaned
        out = self._sub(_EMAIL, HANDLE, out, "handle")
        out = _PHONE.sub(self._phone, out)
        out = self._sub(_MENTION, HANDLE, out, "handle")
        masked = mask_codes(out)
        if masked != out:
            self.found.add("code")
        return masked

    def _sub(self, pattern: re.Pattern[str], repl: str, text: str, kind: str) -> str:
        new, n = pattern.subn(repl, text)
        if n:
            self.found.add(kind)
        return new

    def _phone(self, m: re.Match[str]) -> str:
        if sum(ch.isdigit() for ch in m.group(0)) < 10:
            return m.group(0)  # a date, a PR number, a price: not a phone number
        self.found.add("handle")
        return HANDLE


def principal_handles(path: Path) -> list[str]:
    """Every principal's identity handles (``imessage:+1555…`` -> ``+1555…``)."""
    try:
        principals = load_principals(path)
    except (OSError, ValueError, yaml.YAMLError):
        return []
    out: list[str] = []
    for p in principals.principals.values():
        for ident in p.identities:
            handle = ident.split(":", 1)[1] if ":" in ident else ident
            if len(handle) >= 3:
                out.append(handle)
    return out


def _clip(text: str, n: int = REPLY_MAX) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


# --- dedupe -----------------------------------------------------------------------------


def normalize(text: str) -> str:
    """The text as dedupe compares it: NFKC, case-folded, punctuation and emoji dropped,
    whitespace collapsed."""
    norm = unicodedata.normalize("NFKC", text).casefold()
    kept = "".join(ch if ch.isalnum() else " " for ch in norm)
    return " ".join(kept.split())


class Deduper:
    """Near-identical texts: equal once normalized, or a similarity ratio >= ``SIMILAR``."""

    def __init__(self, seen: Iterable[str] = ()) -> None:
        self.seen: list[str] = []
        self.keys: set[str] = set()
        for text in seen:
            self.add(text)

    def add(self, text: str) -> None:
        key = normalize(text)
        self.keys.add(key)
        self.seen.append(key)

    def is_dupe(self, text: str) -> bool:
        key = normalize(text)
        if key in self.keys:
            return True
        for other in self.seen:
            m = difflib.SequenceMatcher(None, key, other, autojunk=False)
            if (
                m.real_quick_ratio() >= SIMILAR
                and m.quick_ratio() >= SIMILAR
                and m.ratio() >= SIMILAR
            ):
                return True
        return False


# --- drafting a row ---------------------------------------------------------------------


def _fixture_tools() -> dict[str, frozenset[str]]:
    out: dict[str, frozenset[str]] = {}
    for name, fx in FIXTURES.items():
        out[name] = fx.tool_names
    return out


def row_id(task_id: str) -> str:
    return "own-" + hashlib.sha256(task_id.encode()).hexdigest()[:10]


def category(chat: Chat, history: bool) -> str:
    """A first guess, from the tools the turn called; the owner corrects it."""
    used = {c.tool for c in chat.calls}
    exts = {c.extension for c in chat.calls if c.extension}
    if used & RUN_TOOLS:
        return "runs"
    if exts:
        return "extensions"
    if used & (NOTE_TOOLS | REMINDER_TOOLS | FACT_TOOLS):
        return "notes"
    if used & MINI_TOOLS:
        return "mini"
    if chat.route == "claude":
        return "research"
    if history and len(chat.text) < 40:
        return "followup"
    return "chat"


def suggest_expect(chat: Chat) -> dict[str, Any]:
    """Expectations from what happened: the owner checks them (``review: true``)."""
    asked = list(dict.fromkeys(c.tool for c in chat.calls if c.decision == "NeedsApproval"))
    # The gate's Deny is a call the brain shouldn't have made. The owner's "no" to an
    # approval isn't: asking was right (ask: approval), so it isn't forbidden here.
    refused = {c.tool for c in chat.calls if c.decision == "Deny"}
    tools = [
        t for t in dict.fromkeys(c.tool for c in chat.calls) if t != ASK_CLAUDE and t not in refused
    ]
    writes = list(
        dict.fromkeys(
            c.tool
            for c in chat.calls
            if c.effect in WRITE_EFFECTS
            and c.decision == "Allow"
            and c.approval_id is None
            and c.tool not in asked
            and c.tool not in refused
        )
    )
    expect: dict[str, Any] = {}
    if chat.route is not None:
        expect["route"] = chat.route
    if tools:
        expect["tools"] = tools
    if refused:
        expect["tools_not"] = sorted(refused)
    if asked:
        expect["ask"] = "approval"
    elif chat.reply.rstrip().endswith("?") and not writes:
        expect["ask"] = True  # it asked back: a clarifying question
    else:
        expect["ask"] = False
    if writes:
        expect["writes"] = writes
    return expect


@dataclass
class World:
    """What the store held when a text came in (reads only; the store is read-only)."""

    conn: sqlite3.Connection
    home: Path
    scrub: Scrubber
    _facts: list[dict[str, str]] | None = None

    def repos(self, at: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.conn.execute(
            "SELECT slug, name, base, added_at FROM repos ORDER BY added_at, slug"
        ):
            added = _when(r["added_at"])
            if added is not None and added > at:
                continue
            out.append({"name": r["name"], "slug": r["slug"], "base": r["base"] or "main"})
        return out[:REPOS_MAX]

    def runs(self, at: datetime) -> list[dict[str, Any]]:
        """Runs started by then that were active, or ended within a day before; each with
        its status at that time (an end after the text means it was still running)."""
        out: list[dict[str, Any]] = []
        for r in self.conn.execute("SELECT * FROM runs ORDER BY created_at DESC LIMIT 200"):
            created = _when(r["created_at"])
            ended = _when(r["ended_at"])
            if created is None or created > at:
                continue
            if ended is not None and ended < at - RUNS_RECENT:
                continue
            started = _when(r["started_at"]) or created
            ended_by_then = ended is not None and ended <= at
            ask = self.scrub(str(r["ask"] or "")).strip() or "a run"
            run: dict[str, Any] = {
                "ask": _clip(ask, 300),
                "status": str(r["status"]) if ended_by_then else "running",
                "minutes_ago": max(0, round((at - started).total_seconds() / 60)),
            }
            if r["repo"]:
                run["repo"] = str(r["repo"])
            if ended_by_then:
                if r["pr_number"] is not None:
                    run["pr_number"] = int(r["pr_number"])
                for key in ("pr_url", "summary", "failure"):
                    if r[key]:
                        run[key] = _clip(self.scrub(str(r[key])), 300)
            out.append(run)
            if len(out) >= RUNS_MAX:
                break
        out.reverse()
        return out

    def notes(self, at: datetime) -> list[str]:
        out: list[str] = []
        for r in self.conn.execute(
            "SELECT value FROM memory WHERE scope = 'notes' ORDER BY CAST(key AS INTEGER)"
        ):
            v = _json(r["value"])
            made, gone = _when(v.get("at")), _when(v.get("deleted_at"))
            text = v.get("text")
            if not isinstance(text, str) or made is None or made > at:
                continue
            if gone is not None and gone <= at:
                continue
            if cleaned := self.scrub(text).strip():
                out.append(cleaned)
        return out[-NOTES_MAX:]

    def reminders(self, at: datetime) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for r in self.conn.execute(
            "SELECT checkpoint, status, created_at, updated_at FROM tasks"
            " WHERE kind = 'reminder' ORDER BY created_at"
        ):
            cp = _json(r["checkpoint"])
            due, created = _when(cp.get("at")), _when(r["created_at"])
            text = cp.get("text")
            if due is None or created is None or not isinstance(text, str):
                continue
            if created > at or due <= at:
                continue
            updated = _when(r["updated_at"])
            if r["status"] == "cancelled" and updated is not None and updated <= at:
                continue
            if cleaned := self.scrub(text).strip():
                minutes = max(1, round((due - at).total_seconds() / 60))
                out.append({"in_minutes": minutes, "text": cleaned})
        return out[:REMINDERS_MAX]

    def facts(self) -> list[dict[str, str]]:
        if self._facts is None:
            facts: list[dict[str, str]] = []
            for f in FactStore(facts_dir(self.home)).list()[:FACTS_MAX]:
                desc = self.scrub(f.description).strip()
                if not desc:
                    continue
                item = {"name": f.name, "description": desc}
                if text := self.scrub(f.text).strip():
                    item["text"] = text
                facts.append(item)
            self._facts = facts
        return self._facts


def draft_row(
    chat: Chat,
    history: Sequence[Chat],
    world: World,
    fixture_tools: Mapping[str, frozenset[str]],
) -> dict[str, Any] | None:
    """One task-eval row for ``chat``, or None when its text can't be used (a credential
    in it, or nothing left once scrubbed)."""
    scrub = world.scrub
    scrub.reset()
    text = scrub(chat.text).strip()
    found = scrub.reset()
    if "secret" in found or not text:
        return None
    notes: list[str] = [f"drafted from a past text ({chat.at.date().isoformat()})"]
    expect = suggest_expect(chat)
    used = {c.tool for c in chat.calls}
    turns: list[dict[str, str]] = []
    for h in history:
        user, reply = scrub(h.text).strip(), _clip(scrub(h.reply).strip())
        turn_found = scrub.reset()
        if "secret" in turn_found or parse_reply(h.text) is not None:
            continue  # a turn that held a credential, or an approval reply: left out
        if user and reply:
            turns.append({"user": user, "reply": reply})
            found |= turn_found
    reply = _clip(scrub(chat.reply).strip())
    if "secret" in scrub.reset():
        reply = WITHHELD

    w: dict[str, Any] = {}
    if repos := world.repos(chat.at):
        w["repos"] = repos
    if used & RUN_TOOLS and (runs := world.runs(chat.at)):
        w["runs"] = runs
        notes.append("runs as they stood then; their transcripts aren't copied")
    if used & NOTE_TOOLS and (saved := world.notes(chat.at)):
        w["notes"] = saved
    if used & REMINDER_TOOLS and (due := world.reminders(chat.at)):
        w["reminders"] = due
    if used & FACT_TOOLS and (facts := world.facts()):
        w["facts"] = facts
        notes.append("facts as of the draft, not the time of the text")
    exts: dict[str, Any] = {}
    for c in chat.calls:
        if c.extension is None:
            continue
        if c.extension not in FIXTURES:
            notes.append(f"extension {c.extension!r} has no eval fixture: its tools can't run")
        elif c.tool not in fixture_tools.get(c.extension, frozenset()):
            notes.append(f"{c.extension}/{c.tool} isn't in the eval fixture")
        else:
            exts[c.extension] = {}
    if exts:
        w["extensions"] = exts
        notes.append("extension results are the fixture's canned defaults, not the real ones")
    if files := sorted(used & FILE_TOOLS):
        notes.append(f"{', '.join(files)} read real files or git history: not seeded")
    if turns:
        notes.append("earlier turns are seeded; the brain's rolling summary isn't")
    if chat.route == "claude":
        notes.append(f"handed to Claude ({chat.handoff or 'triage'}): a live run grades routing")
    if chat.tainted:
        notes.append("the turn read outside text (tainted)")
    if chat.voice:
        notes.append("a voice note's transcript")
    if rejected := sorted(set(chat.rejected)):
        notes.append(f"you said no to {', '.join(rejected)}")
    if chat.status != "done":
        notes.append(f"the task ended {chat.status}")
    if found:
        notes.append(f"redacted: {', '.join(sorted(found))}")

    row: dict[str, Any] = {
        "id": row_id(chat.task_id),
        "category": category(chat, bool(turns)),
        "text": text,
    }
    if turns:
        row["history"] = turns
    if w:
        row["world"] = w
    row["expect"] = expect
    row["review"] = True
    row["note"] = "; ".join(dict.fromkeys(notes))
    row["draft"] = {
        "task": chat.task_id,
        "at": chat.at.isoformat(timespec="seconds"),
        "status": chat.status,
        "reply": reply,
    }
    return row


def _history_for(chats: Sequence[Chat], i: int) -> list[Chat]:
    """Up to ``HISTORY_MAX`` earlier texts within ``HISTORY_WINDOW``, oldest first."""
    at = chats[i].at
    prior = [c for c in chats[:i] if at - c.at <= HISTORY_WINDOW and c.reply]
    return prior[-HISTORY_MAX:]


# --- the file -----------------------------------------------------------------------------


@dataclass
class Existing:
    lines: list[str]  # kept verbatim
    ids: set[str]
    texts: list[str]
    tasks: set[str]


def read_existing(path: Path) -> Existing:
    """An existing set's rows, kept as they are. A malformed set is an error: it is never
    overwritten."""
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        return Existing([], set(), [], set())  # nothing yet (an empty file is nothing)
    load_set(path)  # raises EvalSetError (line numbers only) on a bad set
    lines = [ln for ln in set_lines(path.read_text(encoding="utf-8")) if ln.strip()]
    ids: set[str] = set()
    texts: list[str] = []
    tasks: set[str] = set()
    for ln in lines:
        row = cast(dict[str, Any], json.loads(ln))
        ids.add(str(row["id"]))
        texts.append(str(row["text"]))
        d: Any = row.get("draft")
        if isinstance(d, dict) and isinstance(task := cast(dict[str, Any], d).get("task"), str):
            tasks.add(task)
    return Existing(lines, ids, texts, tasks)


def write_set(path: Path, lines: Sequence[str]) -> None:
    """Write atomically, 0600, after checking the new set loads."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".task-", suffix=".jsonl", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write("".join(ln + "\n" for ln in lines))
        os.chmod(tmp, 0o600)
        load_set(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class DraftError(RuntimeError):
    """Drafting can't start (the message names no text or handle)."""


@dataclass
class DraftReport:
    path: Path
    drafted: int = 0
    total: int = 0
    pending_review: int = 0
    skipped: dict[str, int] = field(
        default_factory=lambda: {
            "duplicate": 0, "approval_reply": 0, "secret": 0, "already_drafted": 0,
            "invalid": 0,
        }
    )  # fmt: skip

    def to_json(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "drafted": self.drafted,
            "total": self.total,
            "pending_review": self.pending_review,
            "skipped": dict(self.skipped),
        }


def draft(
    settings: Settings,
    *,
    since: datetime | None = None,
    limit: int = DEFAULT_LIMIT,
    out: Path | None = None,
) -> DraftReport:
    """Draft rows from the store at ``settings.db_path`` into ``out`` (default
    ``<home>/evals/task.jsonl``), merging with what's there. Raises ``FileNotFoundError``
    (no store), ``EvalSetError`` (the existing set is malformed) or ``sqlite3.Error``."""
    if limit < 1:
        raise ValueError("limit must be >= 1")
    path = (out or owner_set_path(settings)).expanduser()
    existing = read_existing(path)
    report = DraftReport(path)
    principals = settings.principals_path
    try:
        owner = load_principals(principals).owner if principals.exists() else "owner"
    except (OSError, ValueError, yaml.YAMLError):
        raise DraftError(f"cannot read {principals}") from None  # never echo its values
    scrub = Scrubber(principal_handles(principals))
    conn = open_store(settings.db_path)
    try:
        chats = read_chats(conn, owner, since=since)
        dedupe = Deduper(existing.texts)
        picked: list[int] = []
        for i in range(len(chats) - 1, -1, -1):  # newest first: the newest of a dupe wins
            chat = chats[i]
            if chat.task_id in existing.tasks:
                report.skipped["already_drafted"] += 1
                continue
            if parse_reply(chat.text) is not None:
                report.skipped["approval_reply"] += 1
                continue
            scrub.reset()
            cleaned = scrub(chat.text)
            if "secret" in scrub.reset():
                report.skipped["secret"] += 1
                continue
            if dedupe.is_dupe(cleaned):
                report.skipped["duplicate"] += 1
                continue
            dedupe.add(cleaned)
            picked.append(i)
            if len(picked) >= limit:
                break
        picked.reverse()
        full = with_calls(conn, [chats[i] for i in picked])
        world = World(conn, settings.home, scrub)
        fixture_tools = _fixture_tools()
        rows: list[str] = []
        for i, chat in zip(picked, full, strict=True):
            row = draft_row(chat, _history_for(chats, i), world, fixture_tools)
            if row is None:
                report.skipped["secret"] += 1
                continue
            if row["id"] in existing.ids:
                report.skipped["already_drafted"] += 1
                continue
            try:
                parse_case(row, 0)  # one bad row is skipped, never the whole batch
            except ValueError:
                report.skipped["invalid"] += 1
                continue
            # ASCII-escaped: U+2028 and friends stay inside the JSON string on one line
            rows.append(json.dumps(row, ensure_ascii=True))
    finally:
        conn.close()
    lines = existing.lines + rows
    report.drafted = len(rows)
    report.total = len(lines)
    if rows:
        write_set(path, lines)
    if lines:
        report.pending_review = sum(c.review for c in load_set(path))
    return report


# --- CLI entry ----------------------------------------------------------------------------

_AGO = re.compile(r"(\d{1,4})([dhw])")


def parse_since(raw: str, now: datetime) -> datetime:
    """``2026-09-01``, an ISO 8601 time, or a span back from now: ``30d``, ``12h``, ``2w``."""
    s = raw.strip()
    if m := _AGO.fullmatch(s):
        n, unit = int(m.group(1)), m.group(2)
        span = {"d": timedelta(days=n), "h": timedelta(hours=n), "w": timedelta(weeks=n)}
        return now - span[unit]
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        raise ValueError("--since is a date (2026-09-01), an ISO time, or 30d / 12h / 2w") from None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def run(
    settings: Settings,
    *,
    since: str | None,
    limit: int,
    out: Path | None,
    as_json: bool,
    echo: Callable[[str], None],
    echo_err: Callable[[str], None],
    now: datetime | None = None,
) -> int:
    """``resonant eval draft --from-history``. Prints counts and the path, never a text."""
    try:
        start = parse_since(since, now or datetime.now(UTC)) if since else None
        report = draft(settings, since=start, limit=limit, out=out)
    except FileNotFoundError:
        echo_err(f"error: no store at {settings.db_path}")
        return EXIT_BAD
    except EvalSetError as e:
        echo_err(f"error: the existing set is malformed, left as is: {e}")
        return EXIT_BAD
    except DraftError as e:
        echo_err(f"error: {e}")
        return EXIT_BAD
    except (ValueError, sqlite3.Error) as e:
        echo_err(f"error: {e}")
        return EXIT_BAD
    if as_json:
        echo(json.dumps(report.to_json(), indent=2))
        return EXIT_OK
    s = report.skipped
    echo(
        f"drafted {report.drafted} row(s) into {report.path} ({report.total} in all, "
        f"{report.pending_review} awaiting review)"
    )
    echo(
        f"skipped: {s['duplicate']} near-duplicate, {s['approval_reply']} approval reply, "
        f"{s['secret']} with a credential, {s['already_drafted']} already drafted, "
        f"{s['invalid']} invalid"
    )
    if report.pending_review:
        echo(
            'Check each row\'s expect and note, then set "review": false. '
            "`resonant eval task --set owner` grades only the reviewed rows."
        )
    return EXIT_OK


__all__ = [
    "Call",
    "Chat",
    "Deduper",
    "DraftReport",
    "Scrubber",
    "draft",
    "draft_row",
    "normalize",
    "parse_since",
    "read_chats",
    "run",
    "suggest_expect",
    "with_calls",
]
