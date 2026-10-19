"""Slash commands and chat tools must not drift apart.

Every builtin command that only reads is backed by a tool some intent can reach, so plain
words get what the slash command gets (``/pulse`` is ``pulse_preview``, which the ``updates``
intent offers). Control-plane commands stay slash-only: the model never reaches them.
"""

from __future__ import annotations

from resonant.router import commands
from resonant.router.intents import INTENT_TOOLS
from resonant.tools.builtin import BUILTIN_SPECS
from resonant_sdk import Effect

# Commands with no tool of their own and no chat equivalent, on purpose. Each is explicit
# syntax the owner types: the kill switch, approval handling, and /help (rendered per asker
# without a tool call; chat's "what can you do?" is the ``list_commands`` tool).
SLASH_ONLY = {
    "/kill",  # control plane: never decided by the model
    "/resume",  # control plane
    "/approvals",  # control plane: codes and args are for the owner's explicit eyes
    "/help",  # answered from the registry; chat has list_commands
}
# The only non-read tools an intent may reach (all chat-gated: L3 runs and reports, L2 asks,
# L1 needs a code). A new write tool in an intent must be added here deliberately; control
# plane (`mute_extension`, kill, resume, approvals) never is. The fast path also keeps
# `/pulse now` and `/ext run` as tool-less subcommands.
CHAT_WRITES = {
    "add_note",
    "clear_notes",
    "delete_note",
    "add_reminder",
    "cancel_reminder",
    "restart_ollama",
    "restart_resonant",
    "update_resonant",
    # Deliberately chat-reachable: the sandbox is the wall (start, cancel); merge asks yes.
    "start_run",
    "cancel_run",
    "merge_run",
    "answer_worker",
    "message_worker",
    # Repos (EM-8): add and forget only change Resonant's own list (L3); creating a repo,
    # opening the add-CI PR and merging it act on GitHub, so chat asks a plain yes (L2).
    "add_repo",
    "forget_repo",
    "create_repo",
    "add_ci",
    "merge_ci_pr",
    # Trust rules (EM-4): add tightens (L3); allow and forget can widen trust, so chat asks yes.
    "add_trust_rule",
    "allow_tools",
    "allow_trust_rule",
    "remove_trust_rule",
}

SPECS = {s.name: s for s in BUILTIN_SPECS}
REACHABLE = {t for tools in INTENT_TOOLS.values() for t in tools}


def test_every_read_command_has_a_reachable_tool() -> None:
    for cmd in commands.COMMANDS:
        if not cmd.tools:
            assert cmd.name in SLASH_ONLY, (
                f"{cmd.name} has no tool: add a read tool to an intent in "
                "router/intents.py, or list it in SLASH_ONLY with a reason"
            )
            continue
        for tool in cmd.tools:
            if SPECS[tool].effect is Effect.READ:
                assert tool in REACHABLE, f"{cmd.name}: read tool {tool} is in no intent"


def test_slash_only_commands_have_no_tool() -> None:
    for name in SLASH_ONLY:
        cmd = commands.get(name)
        assert cmd is not None and not cmd.tools, name


def test_control_plane_is_not_reachable_by_the_model() -> None:
    writes = {t for t in REACHABLE if SPECS[t].effect is not Effect.READ}
    assert writes == CHAT_WRITES, writes ^ CHAT_WRITES
    # `/pulse now`, `/ext run` and `/ext mute` are slash-only subcommands.
    pulse = commands.get("/pulse")
    assert pulse is not None and pulse.tool == "pulse_preview"
    assert next(s for s in pulse.subs if s.word == "now").tool is None
    ext = commands.get("/ext")
    assert ext is not None and next(s for s in ext.subs if s.word == "run").tool is None


def test_updates_tools_are_reads() -> None:
    for tool in INTENT_TOOLS["updates"]:
        assert SPECS[tool].effect is Effect.READ
