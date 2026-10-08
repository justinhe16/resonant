"""End-to-end harness: in-process fakes for Ollama and osascript, and ``resonant bench e2e``.

Used by the Phase 1 acceptance tests and the latency bench. Nothing here touches
Messages.app, the real ``chat.db`` or the network (except ``bench e2e --model real``,
which talks to the local Ollama).
"""

from resonant.e2e.fakes import RecordingOsaRunner, ScriptedModel, SentMessage, wait_until

__all__ = ["RecordingOsaRunner", "ScriptedModel", "SentMessage", "wait_until"]
