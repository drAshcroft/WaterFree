from __future__ import annotations

import unittest

from backend.knowledge.store import KnowledgeStore
from backend.llm.tools import build_default_tool_registry
from backend.todo.store import TaskStore


class ToolRegistryPersonaTests(unittest.TestCase):
    def test_debug_detective_gets_live_debug_tools(self) -> None:
        registry = build_default_tool_registry(
            graph=None,
            task_store_factory=lambda workspace_path: TaskStore(workspace_path),
            knowledge_store_factory=lambda: KnowledgeStore(),
            enable_optional_web_tools=False,
        )

        descriptors = registry.select_descriptors(
            persona="debug_detective",
            stage="live_debug",
            preferred_categories=[],
            include_optional=False,
        )
        names = {descriptor.name for descriptor in descriptors}

        self.assertIn("debug_status", names)
        self.assertIn("debug_eval", names)
        self.assertNotIn("run_linter", names)

    def test_pattern_expert_gets_lint_tools(self) -> None:
        registry = build_default_tool_registry(
            graph=None,
            task_store_factory=lambda workspace_path: TaskStore(workspace_path),
            knowledge_store_factory=lambda: KnowledgeStore(),
            enable_optional_web_tools=False,
        )

        descriptors = registry.select_descriptors(
            persona="pattern_expert",
            stage="planning",
            preferred_categories=[],
            include_optional=False,
        )
        names = {descriptor.name for descriptor in descriptors}

        self.assertIn("list_linters", names)
        self.assertIn("run_linter", names)
        self.assertIn("get_lint_logs", names)
        self.assertNotIn("debug_eval", names)


if __name__ == "__main__":
    unittest.main()
