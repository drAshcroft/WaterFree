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
from .debug_tools import debug_tool_descriptors
from .knowledge_tools import knowledge_tool_descriptors
from .lint_tools import lint_tool_descriptors
from .task_tools import task_tool_descriptors
from .testing_tools import testing_tool_descriptors
from .types import ToolDescriptor
from .web_tools import web_tool_descriptors

_PERSONA_TOOL_CATEGORIES: dict[str, set[str]] = {
    "architect": {"graph", "knowledge", "backlog"},
    "pattern_expert": {"graph", "knowledge", "backlog", "lint"},
    "stub_wireframer": {"graph", "backlog", "filesystem", "testing", "lint"},
    "debug_detective": {"graph", "knowledge", "debug"},
    "market_researcher": {"graph", "knowledge", "web"},
    "bdd_test_designer": {"backlog", "testing"},
    "coding_agent": {"graph", "knowledge", "backlog", "filesystem", "testing", "lint"},
    "reviewer": {"graph", "knowledge", "backlog", "testing", "lint"},
    "tutorializer": {"graph", "knowledge", "filesystem"},
}


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

    def select_descriptors(
        self,
        *,
        persona: str = "",
        stage: str = "",
        preferred_categories: Optional[list[str]] = None,
        include_optional: bool = True,
    ) -> list[ToolDescriptor]:
        descriptors = self.list_descriptors(include_optional=include_optional)
        allowed_categories = _allowed_categories(persona, preferred_categories)
        stage_key = stage.strip().lower()
        selected: list[ToolDescriptor] = []
        for descriptor in descriptors:
            if allowed_categories is not None and descriptor.policy.category not in allowed_categories:
                continue
            if not _stage_allows_descriptor(stage_key, descriptor):
                continue
            selected.append(descriptor)
        return selected

    def policy_inventory(self, include_optional: bool = True) -> list[dict[str, Any]]:
        return [
            descriptor.to_policy_dict()
            for descriptor in self.list_descriptors(include_optional=include_optional)
        ]

    def describe_persona_tools(
        self,
        *,
        persona: str = "",
        preferred_categories: Optional[list[str]] = None,
        include_optional: bool = True,
    ) -> list[dict[str, Any]]:
        allowed_categories = _allowed_categories(persona, preferred_categories)
        descriptors = self.list_descriptors(include_optional=include_optional)
        if allowed_categories is not None:
            descriptors = [
                descriptor for descriptor in descriptors
                if descriptor.policy.category in allowed_categories
            ]
        return [descriptor.to_policy_dict() for descriptor in descriptors]

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
    descriptors.extend(debug_tool_descriptors())
    descriptors.extend(lint_tool_descriptors())
    descriptors.extend(testing_tool_descriptors())
    descriptors.extend(web_tool_descriptors(enabled=enable_optional_web_tools))
    return ToolRegistry(descriptors=descriptors)


def _allowed_categories(persona: str, preferred_categories: Optional[list[str]]) -> Optional[set[str]]:
    categories = _PERSONA_TOOL_CATEGORIES.get(persona.strip().lower())
    if categories is None:
        if preferred_categories:
            return set(preferred_categories)
        return None
    merged = set(categories)
    merged.update(preferred_categories or [])
    return merged


def _stage_allows_descriptor(stage: str, descriptor: ToolDescriptor) -> bool:
    if descriptor.policy.category == "debug":
        return stage == "live_debug"
    if stage == "execution":
        return True
    if descriptor.policy.category == "lint":
        return descriptor.policy.read_only
    if descriptor.policy.category == "filesystem":
        return descriptor.policy.read_only
    if descriptor.policy.category == "testing":
        return descriptor.policy.read_only and stage in {
            "annotation",
            "alter_annotation",
            "question_answer",
            "live_debug",
            "ripple_detection",
        }
    if descriptor.policy.category == "backlog":
        if descriptor.name == "delete_task":
            return False
        return True
    return descriptor.policy.read_only
