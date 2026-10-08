"""Task runners. Phase 1 ships ``runner="local"``; Claude (Phase 5) and cu (Phase 7) follow."""

from resonant.runners.local import LocalRunner

__all__ = ["LocalRunner"]
