import sys
import types
import unittest

if "anthropic" not in sys.modules:
    sys.modules["anthropic"] = types.SimpleNamespace(Anthropic=object, types=types.SimpleNamespace(Message=object))

from backend.llm.claude_client import ClaudeClient


class FakeToolUseBlock:
    def __init__(self, block_id: str, name: str, tool_input: dict):
        self.type = "tool_use"
        self.id = block_id
        self.name = name
        self.input = tool_input

    def model_dump(self, exclude_none: bool = True) -> dict:
        return {
            "type": self.type,
            "id": self.id,
            "name": self.name,
            "input": self.input,
        }


class FakeTextBlock:
    def __init__(self, text: str):
        self.type = "text"
        self.text = text

    def model_dump(self, exclude_none: bool = True) -> dict:
        return {"type": self.type, "text": self.text}


class FakeResponse:
    def __init__(self, content):
        self.content = content


class FakeMessagesAPI:
    def __init__(self, responses):
        self._responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(kwargs)
        if not self._responses:
            raise AssertionError("No fake responses remaining")
        return self._responses.pop(0)


class FakeAnthropicClient:
    def __init__(self, responses):
        self.messages = FakeMessagesAPI(responses)


class FakeGraph:
    def __init__(self):
        self.calls = []

    def index_status(self, project: str = "", repo_path: str = "") -> dict:
        self.calls.append(("index_status", {"project": project, "repo_path": repo_path}))
        return {"status": "ready", "project": "demo-project"}

    def search_graph(self, **kwargs) -> dict:
        self.calls.append(("search_graph", kwargs))
        return {
            "results": [
                {
                    "name": "UserService",
                    "qualified_name": "demo.UserService",
                    "file_path": "src/user.py",
                }
            ],
            "total": 1,
            "has_more": False,
            "limit": kwargs.get("limit", 10),
            "offset": kwargs.get("offset", 0),
        }

    def detect_changes(self, scope: str = "all", depth: int = 3) -> dict:
        self.calls.append(("detect_changes", {"scope": scope, "depth": depth}))
        return {"changed_files": ["src/user.py"], "changed_symbols": [], "impacted_callers": []}


class ClaudeClientToolLoopTests(unittest.TestCase):
    def make_client(self, responses, graph=None) -> ClaudeClient:
        client = ClaudeClient.__new__(ClaudeClient)
        client._client = FakeAnthropicClient(responses)
        client._graph = graph
        return client

    def test_generate_plan_executes_graph_tool_before_submit_tool(self) -> None:
        graph = FakeGraph()
        client = self.make_client(
            [
                FakeResponse(
                    [FakeToolUseBlock("tool-1", "search_graph", {"namePattern": "UserService", "limit": 5})]
                ),
                FakeResponse(
                    [
                        FakeTextBlock("Using indexed results."),
                        FakeToolUseBlock(
                            "tool-2",
                            "submit_plan",
                            {
                                "tasks": [
                                    {
                                        "title": "Update UserService",
                                        "description": "Modify the service implementation.",
                                        "targetFile": "src/user.py",
                                        "targetFunction": "UserService",
                                        "priority": 0,
                                    }
                                ],
                                "questions": [],
                            },
                        ),
                    ]
                ),
            ],
            graph=graph,
        )

        tasks, questions = client.generate_plan(
            "Improve user lookup",
            "ARCHITECTURE: demo",
            workspace_path="c:/repo",
        )

        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].target_file, "src/user.py")
        self.assertEqual(questions, [])
        self.assertEqual(graph.calls[0], ("index_status", {"project": "", "repo_path": "c:/repo"}))
        self.assertEqual(graph.calls[1], ("index_status", {"project": "", "repo_path": "c:/repo"}))
        self.assertEqual(graph.calls[2][0], "search_graph")

        requests = client._client.messages.requests
        self.assertEqual(len(requests), 2)
        tool_result_message = next(
            message
            for message in requests[1]["messages"]
            if message["role"] == "user" and isinstance(message["content"], list)
            and message["content"]
            and message["content"][0].get("type") == "tool_result"
        )
        tool_results = tool_result_message["content"]
        self.assertEqual(tool_results[0]["type"], "tool_result")
        self.assertIn("UserService", tool_results[0]["content"])

    def test_detect_ripple_can_use_graph_tool_before_text_reply(self) -> None:
        graph = FakeGraph()
        client = self.make_client(
            [
                FakeResponse([FakeToolUseBlock("tool-1", "detect_changes", {"scope": "unstaged", "depth": 2})]),
                FakeResponse([FakeTextBlock("One changed file; no impacted callers detected.")]),
            ],
            graph=graph,
        )

        result = client.detect_ripple(
            task=None,  # type: ignore[arg-type]
            scan_context="SCAN: changes present",
            workspace_path="c:/repo",
        )

        self.assertEqual(result, "One changed file; no impacted callers detected.")
        self.assertEqual(graph.calls[0], ("index_status", {"project": "", "repo_path": "c:/repo"}))
        self.assertEqual(graph.calls[1], ("index_status", {"project": "", "repo_path": "c:/repo"}))
        self.assertEqual(graph.calls[2], ("detect_changes", {"scope": "unstaged", "depth": 2}))


if __name__ == "__main__":
    unittest.main()
