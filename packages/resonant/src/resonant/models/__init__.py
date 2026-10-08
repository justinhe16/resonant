"""Local LLM client (Ollama). See ``resonant.models.client``."""

from resonant.models.client import (
    ChatMessage,
    ModelClient,
    ModelError,
    ModelOutputError,
    ModelReply,
    ModelUnavailableError,
    OllamaClient,
    ProbeResult,
    SpanFactory,
    ToolCall,
    tools_prefix,
)

__all__ = [
    "ChatMessage",
    "ModelClient",
    "ModelError",
    "ModelOutputError",
    "ModelReply",
    "ModelUnavailableError",
    "OllamaClient",
    "ProbeResult",
    "SpanFactory",
    "ToolCall",
    "tools_prefix",
]
