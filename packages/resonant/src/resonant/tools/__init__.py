"""Tool registry: the only source of a tool's effect and level."""

from resonant.tools.registry import ToolRegistry
from resonant_sdk import ToolSpec

# Resonant-internal builtins that only touch Resonant's own store (notes, reminders). The
# gate lets them run under dry_run. Keyed on registry identity: a builtin (no extension)
# with one of these names and no secrets. An extension tool with the same name is not
# exempt. Ops tools are never exempt: they act on the system. A test pins this set.
STORE_INTERNAL_TOOLS: frozenset[str] = frozenset(
    {"add_note", "delete_note", "clear_notes", "add_reminder", "cancel_reminder"}
)


def is_store_internal(spec: ToolSpec) -> bool:
    """True for a registered builtin in ``STORE_INTERNAL_TOOLS`` (exempt from dry_run)."""
    return (
        spec.extension is None
        and spec.source == "builtin"
        and not spec.secrets
        and spec.name in STORE_INTERNAL_TOOLS
    )


__all__ = ["STORE_INTERNAL_TOOLS", "ToolRegistry", "is_store_internal"]
