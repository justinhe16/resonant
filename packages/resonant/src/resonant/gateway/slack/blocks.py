"""Slack message building (PER-295): outbound text, approval buttons, resolved prompts.

- :func:`format_text`: escape ``& < >`` (Slack mrkdwn), then turn PR URLs into
  ``<url|PR #12>`` and run codes into ``<link_base#/runs/R12|R12>`` links (run codes stay
  plain without a ``link_base``).
- :func:`approval_blocks`: the prompt's text in a section, then Approve and Deny buttons whose
  ``value`` is ``{"a": approval_id, "c": code}`` (``events.parse_click`` reads it back).
  An L1 (destructive) prompt is one click too (the owner's call); its text carries the "This
  can't be undone" wording and its Approve button is red. A pay prompt gets no buttons
  (``SlackChannel`` sends it as text: only a typed ``yes <code>`` approves pay).
- :func:`resolved_blocks`: the prompt's text with the outcome line and no buttons:
  ``Approved by Brian``, ``Denied by Justin``, ``Expired, not done``.
"""

from __future__ import annotations

import json
import re
from typing import Any

from resonant.gateway.channel import ApprovalPrompt
from resonant.gateway.slack.events import APPROVE_ACTION, DENY_ACTION
from resonant_sdk import Level

MAX_CHUNK = 3000  # a message is split into parts of at most this many characters
SECTION_MAX = 3000  # Slack's limit for a section's text

_PR = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/pull/(\d+)(?![\w/])")
_RUN = re.compile(r"(?<![A-Za-z0-9/#|])R([1-9]\d{0,8})(?![A-Za-z0-9])")


def escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_text(text: str, link_base: str | None = None) -> str:
    """``text`` as Slack mrkdwn: escaped, with PR URLs and (given ``link_base``) run codes
    as links."""
    out = escape(text)
    parts: list[str] = []
    last = 0
    for m in _PR.finditer(out):
        parts.append(_runs(out[last : m.start()], link_base))
        parts.append(f"<{m.group(0)}|PR #{m.group(1)}>")
        last = m.end()
    parts.append(_runs(out[last:], link_base))
    return "".join(parts)


def _runs(text: str, link_base: str | None) -> str:
    if link_base is None:
        return text
    return _RUN.sub(lambda m: f"<{link_base}#/runs/R{m.group(1)}|R{m.group(1)}>", text)


def _section(text: str) -> dict[str, Any]:
    shown = text if len(text) <= SECTION_MAX else text[: SECTION_MAX - 1] + "…"
    return {"type": "section", "text": {"type": "mrkdwn", "text": shown}}


def button_value(approval_id: str, code: str) -> str:
    return json.dumps({"a": approval_id, "c": code}, separators=(",", ":"))


def destructive(prompt: ApprovalPrompt) -> bool:
    return prompt.destructive or prompt.level == Level.L1.value


def approval_blocks(prompt: ApprovalPrompt, text: str) -> list[dict[str, Any]]:
    """The prompt (``text``: ``prompt.text()`` formatted) with Approve and Deny buttons."""
    value = button_value(prompt.approval_id, prompt.code)
    approve: dict[str, Any] = {
        "type": "button",
        "action_id": APPROVE_ACTION,
        "text": {"type": "plain_text", "text": "Approve"},
        "style": "primary",
        "value": value,
    }
    if destructive(prompt):
        approve["style"] = "danger"  # the text says "This can't be undone"; still one click
    deny: dict[str, Any] = {
        "type": "button",
        "action_id": DENY_ACTION,
        "text": {"type": "plain_text", "text": "Deny"},
        "value": value,
    }
    return [
        _section(text),
        {"type": "actions", "block_id": f"approval:{prompt.code}", "elements": [approve, deny]},
    ]


def resolved_blocks(text: str, outcome: str) -> list[dict[str, Any]]:
    """The prompt's ``text`` and the ``outcome`` line, buttons removed."""
    return [
        _section(text),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": escape(outcome)}]},
    ]


def outcome_text(status: str, by: str | None) -> str:
    """``Approved by Brian`` / ``Denied by Justin`` / ``Expired, not done``."""
    if status == "approved":
        return f"Approved by {by}" if by else "Approved"
    if status == "rejected":
        return f"Denied by {by}" if by else "Denied"
    return "Expired, not done"
