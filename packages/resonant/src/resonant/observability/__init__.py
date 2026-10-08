"""Structured logs and per-task traces (spans). Built so OTel/Phoenix can be added later."""

from resonant.observability.logging import configure_logging
from resonant.observability.spans import span

__all__ = ["configure_logging", "span"]
