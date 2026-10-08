"""structlog setup: JSON lines to ~/.resonant/logs/resonant.jsonl (rotated) plus stderr.

Standard-library loggers (``logging.getLogger(__name__)``) flow through the same pipeline,
so modules don't need to import structlog.
"""

from __future__ import annotations

import logging
import logging.handlers
from pathlib import Path

import structlog

_SHARED: list[structlog.types.Processor] = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.stdlib.add_logger_name,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
]


def configure_logging(logs_dir: Path, *, level: int = logging.INFO, stderr: bool = True) -> None:
    logs_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    structlog.configure(
        processors=[*_SHARED, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    json_fmt = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED, processor=structlog.processors.JSONRenderer()
    )
    handlers: list[logging.Handler] = []
    file_handler = logging.handlers.RotatingFileHandler(
        logs_dir / "resonant.jsonl", maxBytes=20_000_000, backupCount=5
    )
    file_handler.setFormatter(json_fmt)
    handlers.append(file_handler)
    if stderr:
        console = logging.StreamHandler()
        console.setFormatter(
            structlog.stdlib.ProcessorFormatter(
                foreign_pre_chain=_SHARED,
                processor=structlog.dev.ConsoleRenderer(colors=False),
            )
        )
        handlers.append(console)
    root = logging.getLogger()
    for old in root.handlers:
        old.close()
    root.handlers = handlers
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # httpx logs request URLs at INFO. The healthchecks ping URL works as a credential.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
