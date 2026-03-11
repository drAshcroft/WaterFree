import unittest
from unittest.mock import patch

from backend.llm.providers.deep_agents_runtime import DeepAgentsRuntime
from backend.session.models import AnnotationStatus, CodeCoord, IntentAnnotation, Task


class DeepAgentsRuntimeStructuredTests(unittest.TestCase):
    def make_runtime(self) -> DeepAgentsRuntime:
        return DeepAgentsRuntime(workspace_path="c:/repo")

    def test_detect_ripple_returns_structured_summary(self) -> None:
        runtime = self.make_runtime()
        with patch.object(
            runtime,
            "_run_deepagents_structured",
            return_value={"summary": "One changed file; no impacted callers detected."},
        ):
            result = runtime.detect_ripple(
                task=Task(title="Update UserService"),
                scan_context="SCAN: changes present",
                workspace_path="c:/repo",
            )

        self.assertEqual(result, "One changed file; no impacted callers detected.")

    def test_detect_ripple_falls_back_to_text_reply(self) -> None:
        runtime = self.make_runtime()
        with patch.object(runtime, "_run_deepagents_structured", return_value=None):
            with patch.object(runtime, "_run_deepagents_text", return_value="Self-contained change."):
                result = runtime.detect_ripple(
                    task=Task(title="Update UserService"),
                    scan_context="SCAN: no impacted callers",
                    workspace_path="c:/repo",
                )

        self.assertEqual(result, "Self-contained change.")

    def test_alter_annotation_returns_revised_annotation(self) -> None:
        runtime = self.make_runtime()
        task = Task(
            id="task-1",
            title="Update UserService",
            description="Adjust the service implementation.",
            target_coord=CodeCoord(file="src/user.py", method="UserService"),
        )
        old_annotation = IntentAnnotation(
            task_id=task.id,
            target_coord=CodeCoord(file="src/user.py", method="UserService"),
            summary="Old summary",
            detail="Old detail",
            will_modify=["src/user.py"],
            status=AnnotationStatus.PENDING,
        )
        payload = {
            "summary": "Revise the service without changing callers.",
            "detail": "Keep the public signature stable and update the internal lookup path.",
            "approach": "Preserve the service boundary and limit changes to internals.",
            "willCreate": [],
            "willModify": ["src/user.py"],
            "willDelete": [],
            "sideEffectWarnings": ["Check auth callers for assumptions about error shape."],
            "assumptionsMade": ["The public API must remain stable."],
            "questionsBeforeProceeding": [],
        }

        with patch.object(runtime, "_run_deepagents_structured", return_value=payload):
            revised = runtime.alter_annotation(
                task,
                old_annotation,
                "Do not change the public method signature.",
                "CONTEXT",
                workspace_path="c:/repo",
            )

        self.assertEqual(revised.task_id, "task-1")
        self.assertEqual(revised.summary, payload["summary"])
        self.assertEqual(revised.approach, payload["approach"])
        self.assertEqual(revised.will_modify, ["src/user.py"])

    def test_analyze_debug_context_returns_extension_shape(self) -> None:
        runtime = self.make_runtime()
        payload = {
            "diagnosis": "The breakpoint shows a null config object.",
            "likelyCause": "The service was constructed before configuration loaded.",
            "suggestedFix": {
                "summary": "Guard service construction on config readiness.",
                "detail": "Delay initialization until config is loaded or inject a fallback.",
                "targetFile": "src/app.ts",
                "targetLine": 41,
                "willModify": ["src/app.ts"],
                "willCreate": [],
                "sideEffectWarnings": ["Check startup timing in tests."],
            },
            "questions": ["Can startup proceed without remote config?"],
        }

        with patch.object(runtime, "_run_deepagents_structured", return_value=payload):
            analysis = runtime.analyze_debug_context(
                "CODE AT BREAKPOINT",
                workspace_path="c:/repo",
                persona="debug_detective",
            )

        self.assertEqual(analysis["diagnosis"], payload["diagnosis"])
        self.assertEqual(analysis["likelyCause"], payload["likelyCause"])
        self.assertEqual(analysis["suggestedFix"]["summary"], payload["suggestedFix"]["summary"])
        self.assertEqual(analysis["questions"], payload["questions"])

    def test_answer_question_returns_default_shape(self) -> None:
        runtime = self.make_runtime()
        payload = {
            "answer": "The plan should stay the same.",
            "shouldUpdatePlan": False,
            "followupTasks": [],
            "questions": [],
        }

        with patch.object(runtime, "_run_deepagents_structured", return_value=payload):
            answer = runtime.answer_question(
                "Does this require a plan change?",
                "CURRENT TASK: none",
                workspace_path="c:/repo",
            )

        self.assertEqual(answer["answer"], payload["answer"])
        self.assertFalse(answer["shouldUpdatePlan"])

    def test_generate_plan_routes_structural_goals_to_pattern_expert(self) -> None:
        runtime = self.make_runtime()
        with patch.object(runtime, "_run_deepagents_structured", return_value={"tasks": [], "questions": []}) as run_mock:
            runtime.generate_plan(
                "Design an API client integration with explicit interfaces and data contracts",
                "CURRENT ARCHITECTURE",
                workspace_path="c:/repo",
            )

        self.assertEqual(run_mock.call_args.kwargs["persona"], "pattern_expert")

    def test_generate_plan_parses_rich_task_fields_and_dependencies(self) -> None:
        runtime = self.make_runtime()
        payload = {
            "tasks": [
                {
                    "id": "policy",
                    "title": "Define client policy",
                    "description": "Document timeout, retry, and auth policy.",
                    "rationale": "Integration behavior should be explicit before implementation.",
                    "targetFile": "docs/integration.md",
                    "priority": "P1",
                    "taskType": "spike",
                    "phase": "Design",
                    "estimatedMinutes": 20,
                    "aiNotes": "Confidence medium until vendor docs are confirmed.",
                },
                {
                    "id": "impl",
                    "title": "Build API adapter",
                    "description": "Implement the adapter behind a narrow interface.",
                    "targetFile": "src/integration.py",
                    "targetFunction": "VendorAdapter",
                    "priority": "P1",
                    "taskType": "impl",
                    "dependsOn": [{"title": "Define client policy", "type": "blocks"}],
                    "contextCoords": [{"file": "docs/integration.md", "line": 1, "anchorType": "read-only-context"}],
                },
            ],
            "questions": [],
        }

        with patch.object(runtime, "_run_deepagents_structured", return_value=payload):
            tasks, _ = runtime.generate_plan(
                "Add vendor API integration",
                "CURRENT ARCHITECTURE",
                workspace_path="c:/repo",
                persona="pattern_expert",
            )

        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].rationale, payload["tasks"][0]["rationale"])
        self.assertEqual(tasks[0].estimated_minutes, 20)
        self.assertEqual(tasks[0].ai_notes, payload["tasks"][0]["aiNotes"])
        self.assertEqual(tasks[1].target_function, "VendorAdapter")
        self.assertEqual(tasks[1].context_coords[0].file, "docs/integration.md")
        self.assertEqual(tasks[1].depends_on[0].task_id, tasks[0].id)

    def test_alter_annotation_routes_interface_drift_to_pattern_expert(self) -> None:
        runtime = self.make_runtime()
        task = Task(
            id="task-1",
            title="Refactor API adapter",
            description="Adjust interface and integration error handling.",
            target_coord=CodeCoord(file="src/integration.py", method="VendorAdapter"),
        )
        old_annotation = IntentAnnotation(
            task_id=task.id,
            target_coord=CodeCoord(file="src/integration.py", method="VendorAdapter"),
            summary="Old summary",
            detail="Old detail",
            status=AnnotationStatus.PENDING,
        )
        payload = {
            "summary": "Preserve the adapter contract.",
            "detail": "No interface drift.",
            "approach": "Keep the boundary stable.",
            "willCreate": [],
            "willModify": ["src/integration.py"],
            "willDelete": [],
            "sideEffectWarnings": [],
            "assumptionsMade": [],
            "questionsBeforeProceeding": [],
        }

        with patch.object(runtime, "_run_deepagents_structured", return_value=payload) as run_mock:
            runtime.alter_annotation(
                task,
                old_annotation,
                "Do not change the interface contract.",
                "CONTEXT",
                workspace_path="c:/repo",
            )

        self.assertEqual(run_mock.call_args.kwargs["persona"], "pattern_expert")

    def test_answer_question_routes_interface_questions_to_pattern_expert(self) -> None:
        runtime = self.make_runtime()
        payload = {
            "answer": "Use a boundary adapter with explicit contracts.",
            "shouldUpdatePlan": True,
            "followupTasks": [],
            "designArtifacts": {"interfaces": [{"name": "VendorGateway"}]},
            "questions": [],
        }

        with patch.object(runtime, "_run_deepagents_structured", return_value=payload) as run_mock:
            answer = runtime.answer_question(
                "How should we structure this API client and interface boundary?",
                "CURRENT CONTEXT",
                workspace_path="c:/repo",
            )

        self.assertEqual(run_mock.call_args.kwargs["persona"], "pattern_expert")
        self.assertIn("designArtifacts", answer)


if __name__ == "__main__":
    unittest.main()
