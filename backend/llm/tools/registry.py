"""
Runtime tool catalog with policy metadata.

This centralizes tool definitions so provider adapters can consume a single
catalog while still exposing provider-specific tool schemas.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from backend.graph.client import GraphClient
from backend.knowledge.store import KnowledgeStore
from backend.todo.store import TaskStore

from .filesystem_tools import filesystem_tool_descriptors
from .graph_tools import graph_tool_descriptors
from .knowledge_tools import knowledge_tool_descriptors
from .task_tools import task_tool_descriptors
from .types import ToolDescriptor
from .web_tools import web_tool_descriptors


class ToolRegistry:
    def __init__(self, descriptors: Optional[list[ToolDescriptor]] = None):
        self._descriptors: dict[str, ToolDescriptor] = {}
        for descriptor in descriptors or []:
            self.register(descriptor)

    def register(self, descriptor: ToolDescriptor) -> None:
        self._descriptors[descriptor.name] = descriptor

    def get(self, name: str) -> Optional[ToolDescriptor]:
        return self._descriptors.get(name)

    def names(self) -> list[str]:
        return sorted(self._descriptors.keys())

    def list_descriptors(self, include_optional: bool = True) -> list[ToolDescriptor]:
        descriptors = list(self._descriptors.values())
        if include_optional:
            return descriptors
        return [descriptor for descriptor in descriptors if not descriptor.policy.optional]

    def anthropic_tools(self, include_optional: bool = True) -> list[dict[str, Any]]:
        return [
            descriptor.to_anthropic_tool()
            for descriptor in self.list_descriptors(include_optional=include_optional)
        ]

    def policy_inventory(self, include_optional: bool = True) -> list[dict[str, Any]]:
        return [
            descriptor.to_policy_dict()
            for descriptor in self.list_descriptors(include_optional=include_optional)
        ]

    def invoke(self, name: str, args: dict[str, Any], workspace_path: str) -> dict[str, Any]:
        descriptor = self.get(name)
        if descriptor is None:
            return {"error": f"Unsupported tool: {name}"}
        return descriptor.handler(args, workspace_path)


def build_default_tool_registry(
    *,
    graph: Optional[GraphClient],
    task_store_factory: Callable[[str], TaskStore],
    knowledge_store_factory: Callable[[], KnowledgeStore],
    enable_optional_web_tools: bool = False,
) -> ToolRegistry:
    descriptors: list[ToolDescriptor] = []
    descriptors.extend(graph_tool_descriptors(graph))
    descriptors.extend(task_tool_descriptors(task_store_factory))
    descriptors.extend(knowledge_tool_descriptors(knowledge_store_factory))
    descriptors.extend(filesystem_tool_descriptors())
    descriptors.extend(web_tool_descriptors(enabled=enable_optional_web_tools))
    return ToolRegistry(descriptors=descriptors)
