"""ToolRegistry: registered ToolSpecs keyed by (extension, name).

Builtins use ``extension=None``. Two extensions may each define a tool with the same name.
Gates check every Intent against this registry, so an intent can't claim an effect or
level its tool wasn't registered with.
"""

from __future__ import annotations

from resonant_sdk import ToolSpec

ToolKey = tuple[str | None, str]


class ToolRegistry:
    def __init__(self) -> None:
        self._specs: dict[ToolKey, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        key = (spec.extension, spec.name)
        if key in self._specs:
            raise ValueError(f"tool {spec.name!r} already registered for {spec.extension!r}")
        self._specs[key] = spec

    def unregister_extension(self, extension: str) -> None:
        self._specs = {k: v for k, v in self._specs.items() if k[0] != extension}

    def get(self, extension: str | None, name: str) -> ToolSpec | None:
        return self._specs.get((extension, name))

    def specs(self, extension: str | None = None) -> list[ToolSpec]:
        return [
            s
            for (ext, _), s in sorted(
                self._specs.items(), key=lambda kv: (kv[0][0] or "", kv[0][1])
            )
            if extension is None or ext == extension
        ]

    def replace(self, spec: ToolSpec) -> None:
        """Swap a registered spec for one that differs only in ``args_schema`` (an MCP tool's
        accepted re-pin). Anything else differing is a bug: ValueError."""
        key = (spec.extension, spec.name)
        old = self._specs.get(key)
        if old is None:
            raise ValueError(f"tool {spec.name!r} is not registered for {spec.extension!r}")
        if old.model_copy(update={"args_schema": spec.args_schema}) != spec:
            raise ValueError(f"tool {spec.name!r}: only args_schema may change")
        self._specs[key] = spec
