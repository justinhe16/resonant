"""Slack message building (PER-295): outbound text, approval buttons, resolved prompts.

- :func:`format_text`: escape ``& < >`` (Slack mrkdwn), then turn PR URLs into
  ``<url|PR #12>`` and run codes into ``<link_base#/runs/R12|R12>`` links (run codes stay
  plain without a ``link_base``).
- :func:`approval_blocks`: the prompt's text in sections (one unless it is long: PER-300's
  ``detail`` block; split at line ends, a code fence closed and reopened across a split), then
  Approve and Deny buttons whose
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
SECTIONS_MAX = 20  # a long prompt's sections (Slack allows 50 blocks a message)
_ENTITY_TAIL = re.compile(r"&[a-z]{0,3}$")  # a cut inside &amp; &lt; &gt; moves before it
_FENCE = "```"

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


def split_sections(text: str) -> list[str]:
    """``text`` as section texts of at most ``SECTION_MAX`` characters: whole lines where
    they fit (a longer line is cut), a code fence open at a split closed there and reopened
    in the next part, at most ``SECTIONS_MAX`` parts (the last then ends with ``…``)."""
    if len(text) <= SECTION_MAX:
        return [text]
    room = SECTION_MAX - 2 * (len(_FENCE) + 1) - 1  # a reopened and a closed fence, and "…"
    lines: list[str] = []
    for line in text.split("\n"):
        while len(line) > room:
            cut = room
            if m := _ENTITY_TAIL.search(line[:cut]):
                cut = m.start() or room
            lines.append(line[:cut])
            line = line[cut:]
        lines.append(line)
    packed: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        if current and size + 1 + len(line) > room:
            packed.append("\n".join(current))
            current, size = [], 0
        size += (1 if current else 0) + len(line)
        current.append(line)
    packed.append("\n".join(current))
    out: list[str] = []
    fenced = False
    for part in packed:
        head = f"{_FENCE}\n" if fenced else ""
        fenced ^= part.count(_FENCE) % 2 == 1
        out.append(head + part + (f"\n{_FENCE}" if fenced else ""))
    if len(out) > SECTIONS_MAX:
        out = [*out[: SECTIONS_MAX - 1], out[SECTIONS_MAX - 1] + "…"]
    return out


def _sections(text: str) -> list[dict[str, Any]]:
    return [_section(part) for part in split_sections(text)]


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
        *_sections(text),
        {"type": "actions", "block_id": f"approval:{prompt.code}", "elements": [approve, deny]},
    ]


def resolved_blocks(text: str, outcome: str) -> list[dict[str, Any]]:
    """The prompt's ``text`` and the ``outcome`` line, buttons removed."""
    return [
        *_sections(text),
        {"type": "context", "elements": [{"type": "mrkdwn", "text": escape(outcome)}]},
    ]


def outcome_text(status: str, by: str | None) -> str:
    """``Approved by Brian`` / ``Denied by Justin`` / ``Expired, not done``."""
    if status == "approved":
        return f"Approved by {by}" if by else "Approved"
    if status == "rejected":
        return f"Denied by {by}" if by else "Denied"
    return "Expired, not done"
