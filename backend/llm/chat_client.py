"""Provider-dispatching chat for the map/reduce reader stages.

`waterfree qa-summary` and tutorial generation are bulk readers: they fan a
large document out into chunks, run one cheap completion per chunk, then reduce.
They historically ran only against local Ollama. This module lets them target a
remote OpenAI-compatible gateway (OpenRouter) instead, chosen from
`.waterfree/providers.json`.

Two constraints shape the implementation:

* **Stdlib only.** These run from the standalone `waterfree` CLI, which must not
  pull in langchain just to POST a chat completion. The agent runtime keeps
  using `provider_factory` and the langchain adapters; this is a parallel, much
  smaller path for the reader stages.
* **No VS Code SecretStorage.** The CLI runs outside the extension host, so a
  profile loaded from disk always has `apiKey: ""`. Keys therefore fall back to
  the environment (see `_API_KEY_ENV`).

Routing is opt-in: a provider must name `qa_summary` / `tutorial` in its
`routing.useForStages` to claim them. With no such provider, readers stay on
local Ollama exactly as before.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, replace

from backend.llm import ollama_client
from backend.llm.provider_profiles import (
    OPENROUTER_API_KEY_ENV,
    OPENROUTER_BASE_URL,
    ProviderProfileDocument,
    load_provider_profile,
)
from backend.llm.provider_resolver import resolve_provider

log = logging.getLogger(__name__)

# Provider types this module can talk to. The agent runtime supports more, but
# these readers only need a local daemon and an OpenAI-compatible gateway --
# and OpenRouter already fronts every other vendor worth routing to.
_OPENAI_COMPATIBLE: frozenset[str] = frozenset({"openrouter", "openai", "groq", "qwen"})

_API_KEY_ENV: dict[str, str] = {
    "openrouter": OPENROUTER_API_KEY_ENV,
    "openai": "OPENAI_API_KEY",
    "groq": "GROQ_API_KEY",
    "qwen": "DASHSCOPE_API_KEY",
}

_DEFAULT_TIMEOUT_SECONDS = 240
_LOCAL_KEEP_ALIVE = os.environ.get("WATERFREE_QA_SUMMARY_KEEP_ALIVE", "30m")

# Retained for callers that still hardcode the local daemon.
LOCAL_OLLAMA_BASE = os.environ.get("WATERFREE_OLLAMA_BASE", "http://localhost:11434")

_ATTRIBUTION_REFERER = "https://github.com/drAshcroft/WaterFree"
_ATTRIBUTION_TITLE = "WaterFree"


class ChatUnavailable(RuntimeError):
    """The selected provider cannot serve the request.

    Covers an unreachable local daemon, a missing local model, a missing API
    key, and a provider type this module does not speak.
    """


@dataclass(frozen=True)
class ChatTarget:
    """A fully-resolved destination for one reader stage.

    `alternates` is the ordered fallback chain tried when this target fails --
    the mechanism behind the `auto:free` / `auto:floor` sentinels, where one
    logical selection expands to several concrete models plus a local tail.
    Empty for a target that names a single model outright.
    """

    provider_type: str
    provider_label: str
    model: str
    base_url: str
    api_key: str
    alternates: tuple["ChatTarget", ...] = ()

    @property
    def is_local(self) -> bool:
        return self.provider_type == "ollama"

    def describe(self) -> str:
        return f"{self.provider_label} ({self.provider_type}) / {self.model}"

    def chain(self) -> tuple["ChatTarget", ...]:
        """This target followed by its fallbacks, each stripped of its own chain."""
        head = self if not self.alternates else replace(self, alternates=())
        return (head, *self.alternates)


def local_ollama_target(model: str) -> ChatTarget:
    """The pre-existing behaviour: local daemon, model from the environment."""
    return ChatTarget(
        provider_type="ollama",
        provider_label="Ollama (local)",
        model=model,
        base_url=LOCAL_OLLAMA_BASE,
        api_key="",
    )


def resolve_chat_target(
    *,
    stage: str,
    workspace_path: str = "",
    document: ProviderProfileDocument | None = None,
    fallback_model: str = "",
) -> ChatTarget:
    """Pick a target for `stage`, falling back to local Ollama.

    `document` is the in-memory profile the extension host synced (it carries
    API keys from SecretStorage). When absent -- the CLI case -- the profile is
    read from `.waterfree/providers.json`, which never contains keys.
    """
    profile_document = document
    if profile_document is None and workspace_path:
        try:
            profile_document = load_provider_profile(workspace_path)
        except Exception:
            profile_document = None

    if profile_document is not None:
        resolved = resolve_provider(profile_document, stage=stage, persona="")
        if resolved is not None:
            profile = resolved.profile
            if profile.type in _OPENAI_COMPATIBLE:
                remote = ChatTarget(
                    provider_type=profile.type,
                    provider_label=profile.label or profile.type,
                    model=resolved.model_name,
                    base_url=profile.connection.base_url or _default_base_url(profile.type),
                    api_key=_resolve_api_key(profile.type, profile.connection.api_key),
                )
                return _expand_auto_model(
                    remote,
                    workspace_path=workspace_path,
                    fallback_model=fallback_model,
                )
            if profile.type == "ollama":
                return ChatTarget(
                    provider_type="ollama",
                    provider_label=profile.label or "Ollama",
                    model=resolved.model_name or fallback_model,
                    base_url=profile.connection.base_url or LOCAL_OLLAMA_BASE,
                    api_key="",
                )

    return local_ollama_target(fallback_model)


def _expand_auto_model(
    target: ChatTarget,
    *,
    workspace_path: str,
    fallback_model: str,
) -> ChatTarget:
    """Turn an `auto:free` / `auto:floor` model into a concrete fallback chain.

    Only OpenRouter is fronted by a live catalog, so the sentinels are a no-op
    for the other OpenAI-compatible providers -- resolving them there would mean
    guessing prices we cannot see. The chain always ends at local Ollama, which
    is what "free" means when the gateway has nothing to give.
    """
    # Deferred import: the catalog module reaches the network, and callers that
    # never use a sentinel should not pay for importing it.
    from backend.llm.openrouter_catalog import (  # noqa: PLC0415
        AUTO_FREE,
        MIN_READER_CONTEXT,
        CatalogUnavailable,
        is_auto_model,
        load_models,
        select_candidates,
    )

    if not is_auto_model(target.model):
        return target
    sentinel = target.model.strip().lower()
    if target.provider_type != "openrouter":
        raise ChatUnavailable(
            f"Model '{sentinel}' is only supported for OpenRouter providers; "
            f"'{target.provider_label}' is of type '{target.provider_type}'. "
            "Name a concrete model id for this provider."
        )

    local_tail = (local_ollama_target(fallback_model),) if fallback_model else ()
    try:
        models = load_models(workspace_path=workspace_path, api_key=target.api_key)
        candidates = select_candidates(models, sentinel=sentinel)
    except CatalogUnavailable as exc:
        if not local_tail:
            raise ChatUnavailable(
                f"Could not resolve '{sentinel}' against the OpenRouter catalog: {exc}"
            ) from exc
        log.warning("Falling back to local Ollama: %s", exc)
        return local_tail[0]

    if not candidates:
        message = (
            f"No OpenRouter model matched '{sentinel}' "
            f"(needs a context window of at least {MIN_READER_CONTEXT:,} tokens)."
        )
        if not local_tail:
            raise ChatUnavailable(message)
        log.warning("%s Falling back to local Ollama.", message)
        return local_tail[0]

    chain = [replace(target, model=model_id, alternates=()) for model_id in candidates]
    if sentinel == AUTO_FREE:
        log.info("Resolved %s to free-first chain: %s", sentinel, ", ".join(candidates))
    else:
        log.info("Resolved %s to price-floor chain: %s", sentinel, ", ".join(candidates))
    return replace(chain[0], alternates=(*chain[1:], *local_tail))


def preflight(target: ChatTarget) -> None:
    """Fail fast with an actionable message before fanning out N chunk calls.

    A reader issues one request per chunk; discovering a dead daemon or a bad
    key on chunk 1 of 40 wastes the caller's time, so we check up front.

    With a fallback chain the check passes if *any* link is usable: an
    unreachable free endpoint is exactly what the chain exists to absorb.
    """
    problems: list[str] = []
    for link in target.chain():
        try:
            _preflight_one(link)
            return
        except ChatUnavailable as exc:
            problems.append(f"{link.describe()}: {exc}")
    raise ChatUnavailable(
        "No usable target for this stage. Tried:\n  " + "\n  ".join(problems)
    )


def _preflight_one(target: ChatTarget) -> None:
    if target.is_local:
        _preflight_local(target)
        return
    if not target.api_key:
        env_var = _API_KEY_ENV.get(target.provider_type, "")
        hint = (
            f"Set ${env_var} in the environment, or add the key through the WaterFree "
            "provider settings when running inside the extension."
            if env_var
            else "No API key source is configured for this provider type."
        )
        raise ChatUnavailable(f"No API key for provider '{target.provider_label}'. {hint}")


def chat(
    messages: list[dict[str, str]],
    *,
    target: ChatTarget,
    max_tokens: int,
    timeout: int = _DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """Send one chat completion and return the assistant text.

    `max_tokens` is the caller's output budget; it maps to Ollama's
    `options.num_predict` and to `max_tokens` on OpenAI-compatible gateways.

    When `target` carries a fallback chain (an `auto:free` / `auto:floor`
    expansion), each link is tried in order and the first success wins. Failures
    are per-request, not per-run: a free model that 429s on chunk 7 falls
    through for chunk 7 and is tried again on chunk 8, since these limits are
    usually a short window rather than an exhausted quota.
    """
    links = target.chain()
    if len(links) == 1:
        return _chat_once(messages, target=links[0], max_tokens=max_tokens, timeout=timeout)

    problems: list[str] = []
    for link in links:
        try:
            return _chat_once(messages, target=link, max_tokens=max_tokens, timeout=timeout)
        except ChatUnavailable as exc:
            problems.append(f"{link.describe()}: {exc}")
            log.info("Falling through from %s: %s", link.describe(), exc)
    raise ChatUnavailable(
        "Every candidate model failed for this request. Tried:\n  " + "\n  ".join(problems)
    )


def _chat_once(
    messages: list[dict[str, str]],
    *,
    target: ChatTarget,
    max_tokens: int,
    timeout: int,
) -> str:
    if target.is_local:
        # Normalize OllamaError into ChatUnavailable so callers handle exactly
        # one failure type regardless of which provider they were routed to.
        try:
            return ollama_client.chat(
                model=target.model,
                messages=messages,
                base=target.base_url,
                timeout=timeout,
                keep_alive=_LOCAL_KEEP_ALIVE,
                options={"num_predict": max_tokens},
            ).strip()
        except ollama_client.OllamaError as exc:
            raise ChatUnavailable(str(exc)) from exc
    if target.provider_type in _OPENAI_COMPATIBLE:
        return _chat_openai_compatible(
            messages,
            target=target,
            max_tokens=max_tokens,
            timeout=timeout,
        ).strip()
    raise ChatUnavailable(
        f"Provider type '{target.provider_type}' is not supported for reader stages. "
        "Route qa_summary/tutorial to an ollama or OpenAI-compatible provider "
        "(OpenRouter fronts Anthropic, Google, and others)."
    )


# ---------------------------------------------------------------------------
# OpenAI-compatible transport
# ---------------------------------------------------------------------------

def _chat_openai_compatible(
    messages: list[dict[str, str]],
    *,
    target: ChatTarget,
    max_tokens: int,
    timeout: int,
) -> str:
    payload = {
        "model": target.model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": 0,
        "stream": False,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {target.api_key}",
    }
    if target.provider_type == "openrouter":
        # Attribution only; OpenRouter shows these on its activity dashboard.
        headers["HTTP-Referer"] = _ATTRIBUTION_REFERER
        headers["X-Title"] = _ATTRIBUTION_TITLE

    url = f"{target.base_url.rstrip('/')}/chat/completions"
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise ChatUnavailable(
            f"{target.provider_label} returned HTTP {exc.code}: {_trim(detail)}"
        ) from exc
    except urllib.error.URLError as exc:
        raise ChatUnavailable(f"Cannot reach {target.provider_label} at {url}: {exc}") from exc
    except ChatUnavailable:
        raise
    except Exception as exc:
        raise ChatUnavailable(f"{target.provider_label} request failed: {exc}") from exc

    return _extract_content(data, target)


def _extract_content(data: object, target: ChatTarget) -> str:
    """Pull the message text out of an OpenAI-shaped response.

    OpenRouter can return a body carrying an `error` member with HTTP 200 when
    an upstream vendor fails, so a missing `choices` is reported explicitly
    rather than surfacing as an opaque KeyError.
    """
    if not isinstance(data, dict):
        raise ChatUnavailable(f"{target.provider_label} returned a non-object response.")
    error = data.get("error")
    if error:
        message = error.get("message") if isinstance(error, dict) else error
        raise ChatUnavailable(f"{target.provider_label} error: {_trim(str(message))}")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ChatUnavailable(
            f"{target.provider_label} returned no choices for model '{target.model}'. "
            "Check that the model id exists on the gateway."
        )
    first = choices[0]
    message = first.get("message") if isinstance(first, dict) else None
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        raise ChatUnavailable(f"{target.provider_label} returned an empty message.")
    return content


# ---------------------------------------------------------------------------
# Local daemon
# ---------------------------------------------------------------------------

def _preflight_local(target: ChatTarget) -> None:
    if not ollama_client.ensure_daemon(base=target.base_url):
        raise ChatUnavailable(
            f"Ollama is not reachable at {target.base_url}. Start it with `ollama serve`."
        )
    try:
        installed = [name.lower() for name in ollama_client.list_models(base=target.base_url)]
    except ollama_client.OllamaError as exc:
        raise ChatUnavailable(str(exc)) from exc
    wanted = target.model.lower()
    if ":" in wanted:
        present = wanted in installed or f"{wanted}:latest" in installed
    else:
        present = any(name.split(":", 1)[0] == wanted for name in installed)
    if not present:
        available = ", ".join(installed[:12]) or "<none>"
        raise ChatUnavailable(
            f"Ollama model '{target.model}' is not available. Installed models: {available}"
        )


def _default_base_url(provider_type: str) -> str:
    if provider_type == "openrouter":
        return OPENROUTER_BASE_URL
    return ""


def _resolve_api_key(provider_type: str, profile_key: str) -> str:
    key = str(profile_key or "").strip()
    if key:
        return key
    env_var = _API_KEY_ENV.get(provider_type, "")
    return os.environ.get(env_var, "").strip() if env_var else ""


def _trim(text: str, limit: int = 400) -> str:
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[:limit] + "..."
