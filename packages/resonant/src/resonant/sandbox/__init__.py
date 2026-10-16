"""The thin guard: Seatbelt sandbox for Claude workers (docs/employee-plan.md, D2)."""

from resonant.sandbox.profile import (
    SandboxError,
    SandboxParams,
    checked_path,
    default_deny_exec,
    default_protected,
    default_read_paths,
    link_tools,
    prepare_dirs,
    profile_hash,
    render,
    resolve_tool,
    sandbox_exec_available,
    scratch_env,
    wrap,
)

__all__ = [
    "SandboxError",
    "SandboxParams",
    "checked_path",
    "default_deny_exec",
    "default_protected",
    "default_read_paths",
    "link_tools",
    "prepare_dirs",
    "profile_hash",
    "render",
    "resolve_tool",
    "sandbox_exec_available",
    "scratch_env",
    "wrap",
]
