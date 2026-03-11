"""
Optional networked web/retrieval tool descriptors.

Providers are selected via WATERFREE_WEB_SEARCH_PROVIDER (brave | tavily | exa).
The matching API key must be in WATERFREE_WEB_SEARCH_API_KEY.

When enabled=False, no web tools are registered at all.
"""

from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from typing import Any

from .types import ToolDescriptor, ToolHandler, ToolPolicy


# ---------------------------------------------------------------------------
# Provider handlers
# ---------------------------------------------------------------------------

def _brave_handler(api_key: str) -> ToolHandler:
    def _search(args: dict[str, Any], _workspace_path: str) -> dict[str, Any]:
        query = args.get("query", "")
        limit = min(int(args.get("limit") or 5), 20)
        domains = args.get("domains") or []
        params: dict[str, Any] = {"q": query, "count": limit}
        if domains:
            params["site"] = " OR ".join(f"site:{d}" for d in domains)
        url = "https://api.search.brave.com/res/v1/web/search?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "X-Subscription-Token": api_key,
        })
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            return {"error": str(exc)}
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("description", ""),
            }
            for item in data.get("web", {}).get("results", [])
        ]
        return {"query": query, "results": results}
    return _search


def _tavily_handler(api_key: str) -> ToolHandler:
    def _search(args: dict[str, Any], _workspace_path: str) -> dict[str, Any]:
        query = args.get("query", "")
        limit = min(int(args.get("limit") or 5), 20)
        body = json.dumps({
            "api_key": api_key,
            "query": query,
            "search_depth": "basic",
            "max_results": limit,
            "include_answer": False,
        }).encode()
        req = urllib.request.Request(
            "https://api.tavily.com/search",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            return {"error": str(exc)}
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": item.get("content", ""),
            }
            for item in data.get("results", [])
        ]
        return {"query": query, "results": results}
    return _search


def _exa_handler(api_key: str) -> ToolHandler:
    def _search(args: dict[str, Any], _workspace_path: str) -> dict[str, Any]:
        query = args.get("query", "")
        limit = min(int(args.get("limit") or 5), 20)
        body = json.dumps({
            "query": query,
            "numResults": limit,
            "type": "auto",
            "contents": {"text": {"maxCharacters": 500}},
        }).encode()
        req = urllib.request.Request(
            "https://api.exa.ai/search",
            data=body,
            headers={
                "Content-Type": "application/json",
                "x-api-key": api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as exc:
            return {"error": str(exc)}
        results = [
            {
                "title": item.get("title", ""),
                "url": item.get("url", ""),
                "description": (item.get("text") or "")[:500],
            }
            for item in data.get("results", [])
        ]
        return {"query": query, "results": results}
    return _search


def _fetch_url_handler(args: dict[str, Any], _workspace_path: str) -> dict[str, Any]:
    url = args.get("url", "")
    if not url:
        return {"error": "url is required"}
    req = urllib.request.Request(url, headers={"User-Agent": "WaterFree/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        content = raw.decode("utf-8", errors="replace")
        return {"url": url, "content": content[:50_000]}
    except Exception as exc:
        return {"error": str(exc)}


def _extract_article_handler(args: dict[str, Any], _workspace_path: str) -> dict[str, Any]:
    """Fetch a URL and strip HTML tags to return article-like plain text."""
    import re
    url = args.get("url", "")
    if not url:
        return {"error": "url is required"}
    req = urllib.request.Request(url, headers={"User-Agent": "WaterFree/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as exc:
        return {"error": str(exc)}
    # Strip scripts/styles then all tags, collapse whitespace
    no_scripts = re.sub(r"<(script|style)[^>]*>.*?</\1>", "", raw, flags=re.I | re.S)
    no_tags = re.sub(r"<[^>]+>", " ", no_scripts)
    text = re.sub(r"\s+", " ", no_tags).strip()
    return {"url": url, "text": text[:20_000]}


# ---------------------------------------------------------------------------
# Unconfigured stub
# ---------------------------------------------------------------------------

def _unconfigured_handler(tool_name: str) -> ToolHandler:
    def _handler(_args: dict[str, Any], _workspace_path: str) -> dict[str, Any]:
        provider = os.environ.get("WATERFREE_WEB_SEARCH_PROVIDER", "")
        if not provider:
            return {
                "error": (
                    f"Web tool '{tool_name}' is enabled but no provider is configured. "
                    "Set WATERFREE_WEB_SEARCH_PROVIDER (brave | tavily | exa) and "
                    "WATERFREE_WEB_SEARCH_API_KEY, or configure waterfree.webSearch.provider "
                    "in VS Code settings."
                )
            }
        return {
            "error": (
                f"Web tool '{tool_name}': unknown provider '{provider}'. "
                "Supported: brave, tavily, exa."
            )
        }
    return _handler


# ---------------------------------------------------------------------------
# Public factory
# ---------------------------------------------------------------------------

def web_tool_descriptors(enabled: bool) -> list[ToolDescriptor]:
    """Return web tool descriptors.

    When *enabled* is False, returns an empty list so no web tools appear in
    the registry. When True, returns descriptors wired to the provider
    selected by WATERFREE_WEB_SEARCH_PROVIDER.
    """
    if not enabled:
        return []

    provider = os.environ.get("WATERFREE_WEB_SEARCH_PROVIDER", "").lower().strip()
    api_key = os.environ.get("WATERFREE_WEB_SEARCH_API_KEY", "").strip()

    _PROVIDERS: dict[str, ToolHandler] = {}
    if provider == "brave" and api_key:
        _PROVIDERS["web_search"] = _brave_handler(api_key)
    elif provider == "tavily" and api_key:
        _PROVIDERS["web_search"] = _tavily_handler(api_key)
    elif provider == "exa" and api_key:
        _PROVIDERS["web_search"] = _exa_handler(api_key)

    search_handler = _PROVIDERS.get("web_search") or _unconfigured_handler("web_search")

    policy = ToolPolicy(
        read_only=True,
        requires_network=True,
        requires_approval=True,
        optional=True,
        category="web",
    )
    return [
        ToolDescriptor(
            name="web_search",
            title="web search",
            description=(
                "Search the web for recent information. "
                "Use freshness and domains to narrow results."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"},
                    "freshness": {
                        "type": "string",
                        "description": "Recency filter, e.g. 'past week'",
                    },
                    "domains": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Restrict results to these domains",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results (1–20, default 5)",
                    },
                },
                "required": ["query"],
            },
            handler=search_handler,
            policy=policy,
            server_id="waterfree-web",
        ),
        ToolDescriptor(
            name="fetch_url",
            title="fetch url",
            description="Fetch raw content from a URL.",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=_fetch_url_handler,
            policy=policy,
            server_id="waterfree-web",
        ),
        ToolDescriptor(
            name="extract_article",
            title="extract article",
            description="Fetch a URL and return its plain-text article content.",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=_extract_article_handler,
            policy=policy,
            server_id="waterfree-web",
        ),
    ]
