import unittest

from backend.wizard.models import WizardStageState
from backend.wizard.todo_exporter import TodoExporter


class TodoExporterTests(unittest.TestCase):
    def test_merge_todo_exports_keeps_stable_identity(self) -> None:
        exporter = TodoExporter()
        stage = WizardStageState(
            id="design:api-layer",
            kind="design_pattern_agent",
            title="Design Pattern Agent - API Layer",
            persona="pattern_expert",
            doc_path="design/api-layer.md",
        )
        raw = [{
            "title": "Define API policy",
            "description": "Capture retry and auth policy.",
            "taskType": "spike",
            "priority": "P1",
        }]

        first = exporter.merge_todo_exports(stage, raw)
        stage.todo_exports = first
        second = exporter.merge_todo_exports(stage, raw)

        self.assertEqual(first[0].id, second[0].id)


if __name__ == "__main__":
    unittest.main()
