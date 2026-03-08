"""
Optional networked web/retrieval tool descriptors.
"""

from __future__ import annotations

from .types import ToolDescriptor, ToolPolicy


def web_tool_descriptors(enabled: bool) -> list[ToolDescriptor]:
    def unsupported(tool_name: str):
        def _handler(_args: dict, _workspace_path: str) -> dict:
            return {
                "error": (
                    f"Optional web tool '{tool_name}' is disabled. "
                    "Enable optional web tools in runtime configuration to use it."
                )
            }

        return _handler

    policy = ToolPolicy(
        read_only=True,
        requires_network=True,
        requires_approval=True,
        optional=True,
        category="web",
    )
    descriptors = [
        ToolDescriptor(
            name="web_search",
            title="web search",
            description="Search the web with freshness and domain filters.",
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "freshness": {"type": "string"},
                    "domains": {"type": "array", "items": {"type": "string"}},
                    "limit": {"type": "integer"},
                },
                "required": ["query"],
            },
            handler=unsupported("web_search"),
            policy=policy,
            server_id="waterfree-web",
        ),
        ToolDescriptor(
            name="fetch_url",
            title="fetch url",
            description="Fetch raw URL content for retrieval pipelines.",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}, "mode": {"type": "string"}},
                "required": ["url"],
            },
            handler=unsupported("fetch_url"),
            policy=policy,
            server_id="waterfree-web",
        ),
        ToolDescriptor(
            name="extract_article",
            title="extract article",
            description="Extract article text + metadata from a web URL.",
            input_schema={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
            handler=unsupported("extract_article"),
            policy=policy,
            server_id="waterfree-web",
        ),
    ]
    if enabled:
        return descriptors
    return descriptors
