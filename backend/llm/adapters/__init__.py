"""Provider adapter registry."""
from __future__ import annotations

from typing import TYPE_CHECKING

from backend.llm.adapters.anthropic_adapter import AnthropicAdapter
from backend.llm.adapters.gemini_adapter import GeminiAdapter
from backend.llm.adapters.groq_adapter import GroqAdapter
from backend.llm.adapters.ollama_adapter import OllamaAdapter
from backend.llm.adapters.openai_adapter import OpenAIAdapter
from backend.llm.adapters.qwen_adapter import QwenAdapter

if TYPE_CHECKING:
    from backend.llm.provider_adapter import ProviderAdapter

ADAPTERS: dict[str, "ProviderAdapter"] = {
    "anthropic": AnthropicAdapter(),
    "openai": OpenAIAdapter(),
    "groq": GroqAdapter(),
    "ollama": OllamaAdapter(),
    "gemini": GeminiAdapter(),
    "qwen": QwenAdapter(),
}


def get_adapter(provider_type: str) -> "ProviderAdapter | None":
    return ADAPTERS.get(provider_type)


__all__ = [
    "ADAPTERS",
    "get_adapter",
    "AnthropicAdapter",
    "OpenAIAdapter",
    "GroqAdapter",
    "OllamaAdapter",
    "GeminiAdapter",
    "QwenAdapter",
]
