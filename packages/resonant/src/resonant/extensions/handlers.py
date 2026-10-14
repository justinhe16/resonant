"""Executor handlers for extension tools and jobs: one process run per call.

An ``ExtensionHandler`` is built once at load (its ``ExtCommand`` is compiled then) and is
a ``ContextHandler``: the executor passes the call's secrets (only the ones this spec
declares) in the ``CallContext``. Each call renders argv from the args, builds the whole
environment from scratch (``scratch_env``), runs it through the process runner with a
``Redactor`` over those secret values, and returns the JSON object. Events go to
``ctx.events``; the executor redacts them again with the result.

Jobs take ``{due_at}``: it becomes ``RESONANT_JOB_DUE_AT`` (and fills ``{due_at}`` if the
job's ``run`` uses it).
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from resonant.executor import CallContext, ContextHandler, ToolFailed
from resonant.extensions.process import (
    ExtCommand,
    ExtRequest,
    ExtRunner,
    ensure_data_dir,
    scratch_env,
)
from resonant.redact import Redactor
from resonant.secrets import values_of
from resonant_sdk import ToolSpec


@dataclass(frozen=True, eq=False)
class ExtensionHandler(ContextHandler):
    ext: str
    spec: ToolSpec
    command: ExtCommand
    root: Path  # the approved, read-only copy (the process cwd)
    data_dir: Path  # HOME; <data>/tmp is TMPDIR
    runner: ExtRunner
    path_env: str
    job: bool = False

    async def __call__(self, args: dict[str, Any], ctx: CallContext) -> dict[str, Any]:
        due_at: str | None = None
        if self.job:
            value = args.get("due_at")
            if not isinstance(value, str) or not value or "\x00" in value:
                raise ToolFailed("a job needs a due_at string")
            due_at = value
        argv = self.command.render(args)
        undeclared = set(ctx.secrets) - {f"RESONANT_SECRET_{s.upper()}" for s in self.spec.secrets}
        if undeclared:  # the executor passes only declared ones; never widen
            raise ToolFailed("secrets not declared by this tool")
        ensure_data_dir(self.data_dir)
        env = scratch_env(
            ext=self.ext,
            data_dir=self.data_dir,
            secrets=ctx.secrets,
            job_due_at=due_at,
            path=self.path_env,
        )
        redactor = Redactor(values_of(env))
        out = await self.runner.run(
            ExtRequest(
                argv=argv,
                cwd=self.root,
                env=env,
                timeout_s=self.spec.timeout_s,
                label=f"{self.ext}/{self.spec.name}",
            ),
            redact=redactor.text,
        )
        ctx.events.extend(redactor.deep(asdict(e)) for e in out.events)
        return redactor.deep(out.data)
