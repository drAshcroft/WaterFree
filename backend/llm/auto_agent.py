"""Tool-capable model fallback around each LangChain request, never agent runs."""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import AbstractContextManager, nullcontext
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.callbacks.manager import CallbackManager, AsyncCallbackManager
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from pydantic import Field

from backend.gpu import ollama as gpu_ollama
from backend.llm.chat_client import ChatTarget, ChatTrace

log = logging.getLogger(__name__)


def _gpu(target) -> AbstractContextManager:
    """The shared GPU lease for a local-Ollama candidate; a no-op for remote providers."""
    if target.provider_type != "ollama":
        return nullcontext()
    return gpu_ollama.access(target.base_url, label="agent")


def _enter_gpu(target) -> AbstractContextManager:
    """Enter :func:`_gpu` (it may wait for the GPU) -- run off the event loop by async callers."""
    context = _gpu(target)
    context.__enter__()
    return context


class AutoFallbackChatModel(BaseChatModel):
    candidates: list[Any] = Field(exclude=True, repr=False)
    trace: Any = Field(default_factory=ChatTrace, exclude=True, repr=False)

    @property
    def _llm_type(self) -> str:
        return "waterfree-auto-fallback"

    @staticmethod
    def _config(run_manager, *, asynchronous=False):
        if run_manager is None:
            return None
        manager = AsyncCallbackManager if asynchronous else CallbackManager
        return {"callbacks": manager(handlers=run_manager.handlers,
            inheritable_handlers=run_manager.inheritable_handlers, parent_run_id=run_manager.run_id,
            tags=run_manager.tags, inheritable_tags=run_manager.inheritable_tags,
            metadata=run_manager.metadata, inheritable_metadata=run_manager.inheritable_metadata)}

    def bind_tools(self, tools, *, tool_choice=None, **kwargs):
        options = dict(kwargs)
        if tool_choice is not None:
            options["tool_choice"] = tool_choice
        bound = [(target, model.bind_tools(tools, **options)) for target, model in self.candidates]
        return AutoFallbackChatModel(candidates=bound, trace=self.trace)

    def _links(self, attempts):
        for target, model in self.candidates:
            if self.trace.gated(target):
                attempts.append({"provider": target.provider_type, "model": target.model, "skipped": "agentic harness gate"})
                continue
            yield target, model

    def _failed(self, attempts, target, exc):
        if "Gate Free Endpoints by Agentic Harness" in str(exc):
            self.trace.gate(target)
        attempts.append({"provider": target.provider_type, "model": target.model, "error": str(exc)})
        log.warning("Agent model request failed for %s/%s: %s", target.provider_type, target.model, exc)

    def _winner(self, attempts, target):
        attempts.append({"provider": target.provider_type, "model": target.model})
        self.trace.record(attempts, target)
        log.info("Agent request served by %s/%s", target.provider_type, target.model)

    @staticmethod
    def _exhausted(attempts):
        return RuntimeError("No agent model could serve this request: " + "; ".join(
            f"{a['provider']}/{a['model']}: {a.get('error', a.get('skipped', 'failed'))}" for a in attempts
        ))

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        attempts = []
        config = self._config(run_manager)
        for target, model in self._links(attempts):
            try:
                with _gpu(target):
                    message = model.invoke(messages, config=config, stop=stop, **kwargs)
            except Exception as exc:
                self._failed(attempts, target, exc)
                continue
            self._winner(attempts, target)
            return ChatResult(generations=[ChatGeneration(message=message)])
        raise self._exhausted(attempts)

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs):
        attempts = []
        config = self._config(run_manager, asynchronous=True)
        for target, model in self._links(attempts):
            try:
                gpu = await asyncio.to_thread(_enter_gpu, target)
                try:
                    message = await model.ainvoke(messages, config=config, stop=stop, **kwargs)
                finally:
                    gpu.__exit__(None, None, None)
            except Exception as exc:
                self._failed(attempts, target, exc)
                continue
            self._winner(attempts, target)
            return ChatResult(generations=[ChatGeneration(message=message)])
        raise self._exhausted(attempts)

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        attempts = []
        config = self._config(run_manager)
        for target, model in self._links(attempts):
            emitted = False
            try:
                with _gpu(target):
                    for chunk in model.stream(messages, config=config, stop=stop, **kwargs):
                        emitted = True
                        yield ChatGenerationChunk(message=chunk)
                if not emitted:
                    raise RuntimeError("Model returned an empty stream")
            except Exception as exc:
                self._failed(attempts, target, exc)
                if emitted:
                    raise  # Never combine two models' partial tool-call streams.
                continue
            self._winner(attempts, target)
            return
        raise self._exhausted(attempts)

    async def _astream(self, messages, stop=None, run_manager=None, **kwargs):
        attempts = []
        config = self._config(run_manager, asynchronous=True)
        for target, model in self._links(attempts):
            emitted = False
            try:
                gpu = await asyncio.to_thread(_enter_gpu, target)
                try:
                    async for chunk in model.astream(messages, config=config, stop=stop, **kwargs):
                        emitted = True
                        yield ChatGenerationChunk(message=chunk)
                finally:
                    gpu.__exit__(None, None, None)
                if not emitted:
                    raise RuntimeError("Model returned an empty stream")
            except Exception as exc:
                self._failed(attempts, target, exc)
                if emitted:
                    raise
                continue
            self._winner(attempts, target)
            return
        raise self._exhausted(attempts)


def build_auto_agent_model(profile, sentinel: str, *, workspace_path: str = ""):
    from backend.llm.adapters.openrouter_adapter import OpenRouterAdapter, resolve_api_key
    from backend.llm.model_catalog import get_model
    from backend.llm.openrouter_catalog import CatalogUnavailable, load_models, select_candidates
    from backend.llm.provider_profiles import DEFAULT_STAGE_MODELS, OPENROUTER_BASE_URL

    api_key = resolve_api_key(profile.connection.api_key)
    candidates = []
    if api_key:
        try:
            ids = select_candidates(load_models(workspace_path=workspace_path, api_key=api_key),
                                    sentinel=sentinel, require_tools=True)
        except CatalogUnavailable as exc:
            log.warning("Agent catalog unavailable; trying local fallback: %s", exc)
            ids = []
        adapter = OpenRouterAdapter()
        for model_id in ids:
            model = adapter.create_llm(model_id, {"api_key": api_key,
                "base_url": profile.connection.base_url or OPENROUTER_BASE_URL, "max_retries": 0})
            if model is not None:
                target = ChatTarget("openrouter", profile.label, model_id, profile.connection.base_url, "")
                candidates.append((target, model))
    local_id = os.environ.get("WATERFREE_AGENT_FALLBACK_MODEL", DEFAULT_STAGE_MODELS["ollama"]["default"]).strip()
    descriptor = get_model(local_id)
    if descriptor and descriptor.provider == "ollama" and "tools" in descriptor.capabilities:
        try:
            from langchain_ollama import ChatOllama
            base = os.environ.get("WATERFREE_LOCAL_OLLAMA_BASE", os.environ.get("WATERFREE_OLLAMA_BASE", "http://localhost:11434"))
            candidates.append((ChatTarget("ollama", "Ollama (local)", local_id, base, ""),
                               ChatOllama(model=local_id, base_url=base)))
        except Exception as exc:
            log.warning("Local agent fallback unavailable: %s", exc)
    if not candidates:
        raise RuntimeError(f"Could not resolve {sentinel} to a tool-capable agent model. Check the OpenRouter credential/catalog or install a supported local tool model.")
    return AutoFallbackChatModel(candidates=candidates)
