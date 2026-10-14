"""``FakeOpsRunner``: a scripted :class:`~resonant.ops.OpsRunner` for tests and the e2e harness.

It records every argv (with its timeout and cwd) and every detached spawn, and answers from
a script: the first rule whose argv prefix matches wins. Unmatched commands succeed with
empty output. A rule's result may be an exception (``TimeoutError``, ``OSError``) to raise.
Nothing here runs a real process.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from resonant.ops import ProcResult


@dataclass(frozen=True)
class RunCall:
    argv: tuple[str, ...]
    timeout_s: float
    cwd: Path | None


@dataclass
class FakeOpsRunner:
    # (argv prefix, result). A list of results is used in order, the last one repeating.
    rules: list[tuple[tuple[str, ...], list[ProcResult | BaseException]]] = field(
        default_factory=list[tuple[tuple[str, ...], list[ProcResult | BaseException]]]
    )
    calls: list[RunCall] = field(default_factory=list[RunCall])
    spawned: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])
    spawn_error: BaseException | None = None

    def on(self, *prefix: str, results: Sequence[ProcResult | BaseException]) -> FakeOpsRunner:
        """Answer argv starting with ``prefix`` with ``results`` (in order, last repeats)."""
        self.rules.append((tuple(prefix), list(results)))
        return self

    def ok(self, *prefix: str, stdout: str = "") -> FakeOpsRunner:
        return self.on(*prefix, results=[ProcResult(0, stdout, "")])

    def fail(self, *prefix: str, code: int = 1, stderr: str = "boom") -> FakeOpsRunner:
        return self.on(*prefix, results=[ProcResult(code, "", stderr)])

    async def run(
        self, argv: Sequence[str], *, timeout_s: float, cwd: Path | None = None
    ) -> ProcResult:
        args = tuple(argv)
        self.calls.append(RunCall(args, timeout_s, cwd))
        for prefix, results in self.rules:
            if args[: len(prefix)] == prefix:
                result = results.pop(0) if len(results) > 1 else results[0]
                if isinstance(result, BaseException):
                    raise result
                return result
        return ProcResult(0, "", "")

    def spawn_detached(self, argv: Sequence[str]) -> None:
        if self.spawn_error is not None:
            raise self.spawn_error
        self.spawned.append(tuple(argv))

    def argvs(self) -> list[tuple[str, ...]]:
        return [c.argv for c in self.calls]
