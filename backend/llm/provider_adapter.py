"""
ProviderAdapter Protocol — the interface all provider backends must satisfy.

This is a typing-only module.  The existing build_runtime_spec() / extract_usage()
logic in provider_factory.py satisfies this contract; the Protocol makes it
explicit so type checkers and future implementations have a clear target.

A concrete adapter is identified by its provider_type string and implements:
  create_llm()          → LangChain BaseChatModel (or compatible)
  extract_usage()       → token-usage dict from a raw response
  supports_model()      → True if the adapter can serve a given model id
  supports_capability() → True if the adapter can provide a capability at runtime

Future work: extract AnthropicAdapter, OpenAIAdapter, GroqAdapter, OllamaAdapter
from provider_factory.py and register them here.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ProviderAdapter(Protocol):
    """Contract for provider-specific LLM adapters."""

    provider_type: str
    """Canonical provider identifier, e.g. "claude", "openai", "groq", "ollama"."""

    def create_llm(self, model_id: str, config: Any) -> Any:
        """Return a LangChain BaseChatModel (or compatible) for the given model.

        model_id — the model identifier string (e.g. "claude-sonnet-4-6")
        config   — provider-specific configuration dict (api_key, base_url, etc.)
        """
        ...

    def extract_usage(self, response: Any) -> dict[str, int]:
        """Extract token-usage statistics from a raw LangChain response.

        Returns a dict with at minimum:
            input_tokens, output_tokens
        Optionally:
            cache_read_tokens, cache_creation_tokens
        """
        ...

    def supports_model(self, model_id: str) -> bool:
        """Return True if this adapter can serve the given model id."""
        ...

    def supports_capability(self, capability: str) -> bool:
        """Return True if this adapter provides the named capability at runtime.

        Capability strings match those in model_catalog.ModelDescriptor.capabilities
        (e.g. "tools", "vision", "caching").
        """
        ...
