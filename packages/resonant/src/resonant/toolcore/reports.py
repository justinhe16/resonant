"""Code-built text for gated calls (TC-1, moved from ``runners/local.py``): the report line
that says what a side effect did (``Saved note 3: "buy milk"``, else ``Done: <summary>``), and
the templated replies for a call that was refused, dry-run, failed or ambiguous. Never from a
model: every word here is built from the intent and the tool's own result fields.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, cast

from resonant.executor import ToolResult
from resonant.gate import Decision, Deny, DryRun, Intent
from resonant.gate.codes import clean_external
from resonant.gate.policy import APPROVAL_EXPIRED, APPROVAL_USED, NOT_APPROVED
from resonant.gate.summary import summarize as summarize_args
from resonant.tools.ops import summary_action as ops_action

DONE_NOT_APPROVED = "Not done."
NOT_AVAILABLE_REPLY = "Not done: that tool isn't available."
BAD_ARGS_REPLY = "Not done: invalid arguments."
NOTE_TEXT_CHARS = 60
SHELL_SHOWN_CHARS = 160


def _note_saved(d: dict[str, Any]) -> str | None:
    note_id, text = d.get("id"), d.get("text")
    if not isinstance(note_id, int) or not isinstance(text, str):
        return None
    shown = text if len(text) <= NOTE_TEXT_CHARS else text[: NOTE_TEXT_CHARS - 1] + "…"
    return f'Saved note {note_id}: "{shown}"'


def _note_deleted(d: dict[str, Any]) -> str | None:
    note_id = d.get("id")
    return f"Deleted note {note_id}." if isinstance(note_id, int) else None


def _notes_cleared(d: dict[str, Any]) -> str | None:
    n = d.get("cleared")
    if not isinstance(n, int):
        return None
    return f"Cleared {n} note{'' if n == 1 else 's'}."


def _reminder_set(d: dict[str, Any]) -> str | None:
    n, due, text = d.get("id"), d.get("due"), d.get("text")
    if not isinstance(n, int) or not isinstance(due, str) or not isinstance(text, str):
        return None
    shown = text if len(text) <= NOTE_TEXT_CHARS else text[: NOTE_TEXT_CHARS - 1] + "…"
    return f'⏰ Reminder {n} set for {due}: "{shown}"'


def _reminder_cancelled(d: dict[str, Any]) -> str | None:
    n = d.get("id")
    return f"Cancelled reminder {n}." if d.get("cancelled") is True and isinstance(n, int) else None


def _ollama_restarted(d: dict[str, Any]) -> str | None:
    return "Restarted Ollama." if d.get("restarted") is True else None


def _resonant_restarting(d: dict[str, Any]) -> str | None:
    return "Restarting Resonant…" if d.get("restarting") is True else None


def _updated(d: dict[str, Any]) -> str | None:
    old, new = d.get("old"), d.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    if d.get("updated") is False:
        return f"Already up to date ({new})."
    if d.get("restarting") is True:
        return f"Updated {old} → {new}, restarting…"
    if d.get("synced") is False:
        error = clean_external(str(d.get("sync_error") or "failed"))[:60]
        return (
            f"Updated {old} → {new}, but uv sync failed ({error}): not restarting. "
            "Fix it on the Mini."
        )
    return None


def _run_text(d: dict[str, Any]) -> str | None:
    """A run tool's result is already the code-built reply (``Started.text``, a refusal)."""
    text = d.get("text")
    return text if isinstance(text, str) and text else None


def _muted(d: dict[str, Any]) -> str | None:
    ext, until = d.get("extension"), d.get("until_text")
    if not isinstance(ext, str):
        return None
    if isinstance(until, str):
        return f"Muted {ext} until {until}."
    return f"Unmuted {ext}." if d.get("muted_until") is None else None


def _rule_added(d: dict[str, Any]) -> str | None:
    rule_id, text = d.get("id"), d.get("text")
    if not isinstance(rule_id, int) or not isinstance(text, str):
        return None
    if d.get("existed") is True:
        return f"Rule already there: {text} (rule {rule_id})."
    line = f"Rule added: {text} (rule {rule_id})"
    old = d.get("replaced")
    if isinstance(old, dict):
        old_d = cast(dict[str, Any], old)
        line += f", replacing rule {old_d.get('id', '?')} ({old_d.get('text', '')})"
    return line + "."


def _tools_allowed(d: dict[str, Any]) -> str | None:
    """``allow_tools``: each rule it saved, by number (BA-1)."""
    rules = d.get("rules")
    if not isinstance(rules, list) or not rules:
        return None
    parts: list[str] = []
    for r in cast(list[Any], rules):
        rd = cast(dict[str, Any], r) if isinstance(r, dict) else {}
        rule_id, text = rd.get("id"), rd.get("text")
        if not isinstance(rule_id, int) or not isinstance(text, str):
            return None
        parts.append(f"{text} (rule {rule_id}{', already there' if rd.get('existed') else ''})")
    return "Rules: " + "; ".join(parts) + ". Changes after outside content still ask."


def _rule_removed(d: dict[str, Any]) -> str | None:
    rule_id, text = d.get("id"), d.get("text")
    if d.get("removed") is not True or not isinstance(rule_id, int) or not isinstance(text, str):
        return None
    return f"Rule removed: {text} (rule {rule_id})."


def _fact_saved(d: dict[str, Any]) -> str | None:
    name, description = d.get("name"), d.get("description")
    if not isinstance(name, str) or not isinstance(description, str):
        return None
    # The whole description (at most 120): the owner sees exactly what went into the prompt.
    verb = "Remembered" if d.get("created") is True else "Updated fact"
    return f'{verb} {name}: "{description}"'


def _fact_forgotten(d: dict[str, Any]) -> str | None:
    name = d.get("name")
    return f"Forgot {name}." if d.get("forgotten") is True and isinstance(name, str) else None


def _watch_created(d: dict[str, Any]) -> str | None:
    code, what, every = d.get("id"), d.get("what"), d.get("every_minutes")
    if d.get("created") is not True or not isinstance(code, str) or not isinstance(what, str):
        return None
    often = f"{every // 60}h" if isinstance(every, int) and every % 60 == 0 else f"{every}m"
    until = d.get("expires_at")
    end = f" until {until[:10]}" if isinstance(until, str) else ""
    return f"Watching {code}: {what}, every {often}{end}."


def _watch_cancelled(d: dict[str, Any]) -> str | None:
    code, what = d.get("id"), d.get("what")
    if not isinstance(code, str) or not isinstance(what, str):
        return None
    if d.get("cancelled") is True:
        return f"Stopped watch {code}: {what}."
    return f"Watch {code} had already ended ({d.get('state')})."


def summarize_intent(intent: Intent) -> str:
    """The code-built phrase for ``intent`` in replies: the ops heading (``update Resonant``)
    when there is one, as in approval prompts and dry-run replies, else the generic summary."""
    return summarize_args(intent, action=ops_action(intent))


# Code-built report lines for builtin side effects, keyed by tool name. Anything else
# reports ``Done: <summary>``. A renderer returns None for a result it doesn't recognize.
def _shell_permitted(d: dict[str, Any]) -> str | None:
    """PER-258: the owner's yes to a Claude brain change after its turn; Claude runs it in a
    new turn (``runners/ask.py``), and its own line says what ran."""
    shown = d.get("shown")
    if d.get("permitted") is not True or not isinstance(shown, str):
        return None
    what = clean_external(shown)[:SHELL_SHOWN_CHARS]
    return (
        f"Approved: `{what}`; Claude runs it now."
        if d.get("tool") == "Bash"
        else (f"Approved: {what}; Claude makes the change now.")
    )


REPORTS: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "shell": _shell_permitted,
    "add_note": _note_saved,
    "delete_note": _note_deleted,
    "clear_notes": _notes_cleared,
    "add_reminder": _reminder_set,
    "cancel_reminder": _reminder_cancelled,
    "restart_ollama": _ollama_restarted,
    "restart_resonant": _resonant_restarting,
    "update_resonant": _updated,
    "mute_extension": _muted,
    "start_run": _run_text,
    "cancel_run": _run_text,
    "merge_run": _run_text,
    "answer_worker": _run_text,
    "message_worker": _run_text,
    "create_repo": _run_text,
    "add_ci": _run_text,
    "merge_ci_pr": _run_text,
    "add_repo": _run_text,
    "forget_repo": _run_text,
    "add_trust_rule": _rule_added,
    "allow_trust_rule": _rule_added,
    "allow_tools": _tools_allowed,
    "remove_trust_rule": _rule_removed,
    "remember": _fact_saved,
    "forget": _fact_forgotten,
    "create_watch": _watch_created,
    "cancel_watch": _watch_cancelled,
}


def report_line(intent: Intent, data: dict[str, Any]) -> str:
    """The code-built line that says what a side effect did. Never from a model."""
    render = REPORTS.get(intent.tool) if intent.extension is None else None
    line = render(data) if render is not None else None
    return line or f"Done: {summarize_intent(intent)}"


def refusal_reply(intent: Intent, decision: Deny) -> str:
    if decision.reason == NOT_APPROVED:
        return DONE_NOT_APPROVED
    if decision.reason == APPROVAL_EXPIRED:
        return f"Not done: the approval for {summarize_intent(intent)} expired."
    if decision.reason == APPROVAL_USED:
        return "Not done: that approval was already used."
    return f"Not done: {decision.reason}."


def dry_run_reply(decision: DryRun) -> str:
    return f"Dry run: would {decision.would}"


def not_run_reply(intent: Intent, decision: Decision) -> str:
    """The templated reply for a gated call the gate didn't allow (and isn't asking about)."""
    if isinstance(decision, DryRun):
        return dry_run_reply(decision)
    if isinstance(decision, Deny):
        return refusal_reply(intent, decision)
    return DONE_NOT_APPROVED  # Veto: never returned in Phase 2


def failure_reply(intent: Intent, result: ToolResult) -> str:
    """A gated call that ran and failed: the executor's notice (a missing secret), the
    ambiguous warning, or ``Failed: <summary>`` with a handler's own ``ToolFailed`` reason."""
    if result.notice is not None:  # code-built by the executor (a missing secret)
        return result.notice
    if result.ambiguous:
        return (
            f"Not sure whether {summarize_intent(intent)} ran: it has no recorded outcome. "
            "Check before trying again."
        )
    text = f"Failed: {summarize_intent(intent)}"
    # Only a handler's ToolFailed message is meant for the user; other errors stay in audit.
    error = result.error or ""
    if error.startswith("ToolFailed: "):
        detail = clean_external(error.removeprefix("ToolFailed: "))[:120]
        if detail:
            text += f" ({detail})"
    return text
