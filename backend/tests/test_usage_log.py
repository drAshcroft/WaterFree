import io
import json
import os
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from backend.cli import usage as usage_cli
from backend.cli import usage_log
from backend.cli._common import EXIT_OK, EXIT_USAGE
from backend.cli.dispatcher import dispatch
from backend.test_support import make_temp_dir as make_test_dir


def _run(argv: list[str]) -> tuple[int, dict | None, str]:
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = dispatch(argv)
    text = out.getvalue().strip()
    return code, (json.loads(text) if text else None), err.getvalue()


class UsageLogRecordTests(unittest.TestCase):
    def test_envelope_fields_from_a_search_result(self) -> None:
        fields = usage_log.envelope_fields({
            "tasks": [{"id": "a"}, {"id": "b"}], "total": 7, "returned": 2,
            "truncated": True, "hint": "raise --limit",
        })
        self.assertEqual(fields, {"hits": 7, "returned": 2, "truncated": True, "hint": True, "hit_ids": ["a", "b"]})
        self.assertEqual(usage_log.envelope_fields({"entries": [], "total": 0}), {"hits": 0, "returned": 0})
        self.assertEqual(usage_log.envelope_fields({"id": "x"}), {})
        self.assertEqual(usage_log.envelope_fields(None), {})

    def test_query_and_argv_clipping(self) -> None:
        self.assertEqual(usage_log.query_from_argv("search", ["--workspace", "x", "mode lever", "--limit", "5"]), "mode lever")
        self.assertEqual(usage_log.query_from_argv("add", ["--title", "t"]), "")
        long = "y" * 300
        clipped = usage_log.sanitize_argv(["--description", long])[1]
        self.assertTrue(clipped.startswith("y" * 120))
        self.assertIn("+180", clipped)

    def test_identity_from_environment(self) -> None:
        with patch.dict(os.environ, {"AI_AGENT": "claude-code_2", "CLAUDE_CODE_SESSION_ID": "s1"}, clear=False):
            self.assertEqual(usage_log.agent_identity(), ("claude-code_2", "s1"))
        with patch.dict(os.environ, {"AI_AGENT": "", "CLAUDE_CODE_SESSION_ID": "", "CLAUDECODE": ""}, clear=False):
            self.assertEqual(usage_log.agent_identity()[0], "unknown")


class UsageLogDispatchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_test_dir(self, prefix="usage-log-")
        self.log = self.dir / "usage.jsonl"
        self.env = patch.dict(os.environ, {"WATERFREE_USAGE_LOG": str(self.log)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def records(self) -> list[dict]:
        return usage_log.read_records([self.log])

    def test_every_dispatch_appends_one_record_with_envelope_fields(self) -> None:
        ws = self.dir / "ws"
        ws.mkdir()
        code, _, _ = _run(["todos", "add", "--workspace", str(ws), "--title", "Alpha task", "--description", "d"])
        self.assertEqual(code, EXIT_OK)
        code, _, _ = _run(["todos", "search", "--workspace", str(ws), "alpha"])
        self.assertEqual(code, EXIT_OK)
        code, _, _ = _run(["todos", "search", "--workspace", str(ws), "nothing here"])
        self.assertEqual(code, EXIT_OK)

        records = self.records()
        self.assertEqual([r["action"] for r in records], ["add", "search", "search"])
        add, hit, miss = records
        self.assertEqual(add["area"], "todos")
        self.assertEqual(add["source"], "cli")
        self.assertGreater(add["result_bytes"], 10)
        self.assertNotIn("hits", add)
        self.assertEqual(hit["query"], "alpha")
        self.assertEqual(hit["hits"], 1)
        self.assertEqual(hit["returned"], 1)
        self.assertEqual(len(hit["hit_ids"]), 1)
        self.assertEqual(miss["hits"], 0)
        self.assertTrue(miss["hint"])
        self.assertEqual(Path(add["workspace"]).resolve(), ws.resolve())

    def test_usage_error_and_not_found_exit_codes_are_recorded(self) -> None:
        ws = self.dir / "ws2"
        ws.mkdir()
        code, _, _ = _run(["todos", "get", "--workspace", str(ws), "GOV-404"])
        self.assertEqual(code, 3)
        self.assertEqual(self.records()[-1]["exit_code"], 3)

    def test_usage_area_itself_is_not_logged_and_disabled_log_writes_nothing(self) -> None:
        code, payload, _ = _run(["usage", "path"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["live"], str(self.log))
        self.assertEqual(self.records(), [])

        with patch.dict(os.environ, {"WATERFREE_USAGE_LOG": "off"}, clear=False):
            ws = self.dir / "ws3"
            ws.mkdir()
            _run(["todos", "list", "--workspace", str(ws)])
        self.assertEqual(self.records(), [])


class UsageSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_test_dir(self, prefix="usage-summary-")
        self.log = self.dir / "usage.jsonl"
        self.env = patch.dict(os.environ, {"WATERFREE_USAGE_LOG": str(self.log)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def write(self, *records: dict) -> None:
        for record in records:
            usage_log.append(record, self.log)

    def test_summary_reports_zero_hit_rates_bytes_and_knowledge_retrieval(self) -> None:
        base = dict(source="cli", workspace="C:/p", exit_code=0, duration_ms=3.0, agent="claude-code", session="s")
        self.write(
            {**base, "ts": "2026-09-20T00:00:00+00:00", "area": "knowledge", "action": "search", "query": "retry", "result_bytes": 1000, "hits": 3, "hit_ids": ["e1", "e2"]},
            {**base, "ts": "2026-09-21T00:00:00+00:00", "area": "knowledge", "action": "search", "query": "zzz", "result_bytes": 40, "hits": 0},
            {**base, "ts": "2026-09-21T00:00:00+00:00", "area": "knowledge", "action": "add", "result_bytes": 200},
            {**base, "ts": "2026-09-22T00:00:00+00:00", "area": "todos", "action": "list", "result_bytes": 9000, "hits": 12, "exit_code": 0},
            {**base, "ts": "2026-09-22T00:00:00+00:00", "area": "todos", "action": "update", "result_bytes": 300, "exit_code": 3},
        )
        summary = usage_cli.summarize(usage_log.read_records([self.log]))
        self.assertEqual(summary["records"], 5)
        self.assertEqual(dict(summary["by_area"]), {"knowledge": 3, "todos": 2})
        search = next(row for row in summary["actions"] if row["action"] == "knowledge search")
        self.assertEqual(search["calls"], 2)
        self.assertEqual(search["zero_hit_rate"], 0.5)
        self.assertEqual(search["total_result_bytes"], 1040)
        update = next(row for row in summary["actions"] if row["action"] == "todos update")
        self.assertEqual(update["error_rate"], 1.0)
        self.assertEqual(summary["knowledge"]["distinct_entries_retrieved"], 2)
        self.assertEqual(summary["knowledge"]["adds_per_search"], 0.5)
        self.assertEqual(summary["top_empty_queries"], [("zzz", 1)])

    def test_summary_cli_filters_by_window_area_and_workspace(self) -> None:
        self.write(
            {"ts": "2020-01-01T00:00:00+00:00", "source": "cli", "area": "todos", "action": "list", "workspace": "C:/old", "exit_code": 0, "result_bytes": 1},
            {"ts": "2099-01-01T00:00:00+00:00", "source": "cli", "area": "todos", "action": "list", "workspace": "C:/new", "exit_code": 0, "result_bytes": 1},
            {"ts": "2099-01-01T00:00:00+00:00", "source": "transcript", "area": "knowledge", "action": "search", "workspace": "", "exit_code": 0, "result_bytes": 1},
        )
        code, all_time, _ = _run(["usage", "summary", "--since", "all"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(all_time["records"], 3)

        _, recent, _ = _run(["usage", "summary", "--since", "30d"])
        self.assertEqual(recent["records"], 2)

        _, knowledge_only, _ = _run(["usage", "summary", "--since", "all", "--area", "knowledge"])
        self.assertEqual(knowledge_only["records"], 1)

        _, one_ws, _ = _run(["usage", "summary", "--since", "all", "--workspace", "C:/new"])
        self.assertEqual(one_ws["records"], 1)

        _, live_only, _ = _run(["usage", "summary", "--since", "all", "--source", "cli"])
        self.assertEqual(live_only["records"], 2)

        code, _, err = _run(["usage", "summary", "--since", "soon"])
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("--since must look like", err)

    def test_tail_returns_most_recent_records_in_order(self) -> None:
        self.write(
            {"ts": "2026-09-02T00:00:00+00:00", "area": "todos", "action": "b", "workspace": "", "exit_code": 0},
            {"ts": "2026-09-01T00:00:00+00:00", "area": "todos", "action": "a", "workspace": "", "exit_code": 0},
        )
        code, payload, _ = _run(["usage", "tail", "-n", "1"])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual([r["action"] for r in payload["records"]], ["b"])


class UsageTranscriptImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_test_dir(self, prefix="usage-import-")
        self.log = self.dir / "usage.jsonl"
        self.env = patch.dict(os.environ, {"WATERFREE_USAGE_LOG": str(self.log)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_pairs_tool_calls_with_results_and_rewrites_wholesale(self) -> None:
        projects = self.dir / "projects"
        session_dir = projects / "c--Projects-dungeon"
        session_dir.mkdir(parents=True)
        lines = [
            {"type": "assistant", "timestamp": "2026-09-10T10:00:00.000Z", "message": {"content": [
                {"type": "tool_use", "id": "t1", "name": "Bash",
                 "input": {"command": 'cd /c/Projects/dungeon && waterfree knowledge search "phaser input lock" --limit 5 | head -c 400'}},
            ]}},
            {"type": "user", "timestamp": "2026-09-10T10:00:01.000Z", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": '{"entries": [{"id": "aaaaaaaa-1111-2222-3333-444444444444"}], "total": 1}'},
            ]}},
            {"type": "assistant", "timestamp": "2026-09-10T10:01:00.000Z", "message": {"content": [
                {"type": "tool_use", "id": "t2", "name": "PowerShell",
                 "input": {"command": "waterfree todos list --workspace C:\\Projects\\dungeon --limit 5"}},
            ]}},
            {"type": "user", "timestamp": "2026-09-10T10:01:01.000Z", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t2", "is_error": True, "content": [{"type": "text", "text": "error: boom"}]},
            ]}},
            {"type": "assistant", "timestamp": "2026-09-10T10:02:00.000Z", "message": {"content": [
                {"type": "tool_use", "id": "t3", "name": "Bash", "input": {"command": "waterfree todos get-next"}},
            ]}},
        ]
        (session_dir / "sess-1.jsonl").write_text("\n".join(json.dumps(l) for l in lines) + "\n", encoding="utf-8")

        code, payload, _ = _run(["usage", "import-transcripts", "--claude-projects", str(projects)])
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(payload["written"], 2)
        self.assertEqual(payload["calls"], 3)
        self.assertEqual(payload["unpaired"], 1)

        imported = usage_log.read_records([usage_log.transcript_log_path()])
        self.assertEqual(len(imported), 2)
        search, listing = imported
        self.assertEqual(search["source"], "transcript")
        self.assertEqual(search["session"], "sess-1")
        self.assertEqual(search["query"], "phaser input lock")
        self.assertEqual(search["hits"], 1)
        self.assertEqual(search["hit_ids"], ["aaaaaaaa-1111-2222-3333-444444444444"])
        self.assertEqual(search["workspace"], "C:/Projects/dungeon")
        self.assertEqual(listing["exit_code"], 1)
        self.assertEqual(listing["workspace"], "C:\\Projects\\dungeon")

        # Re-import replaces rather than appends.
        code, payload, _ = _run(["usage", "import-transcripts", "--claude-projects", str(projects)])
        self.assertEqual(payload["written"], 2)
        self.assertEqual(len(usage_log.read_records([usage_log.transcript_log_path()])), 2)

        _, summary, _ = _run(["usage", "summary", "--since", "all", "--source", "transcript"])
        self.assertEqual(summary["records"], 2)


class UsageExtensionPathTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_test_dir(self, prefix="usage-ext-")
        self.log = self.dir / "usage.jsonl"
        self.env = patch.dict(os.environ, {"WATERFREE_USAGE_LOG": str(self.log)}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_area_for_method_groups_extension_requests_like_the_cli(self) -> None:
        self.assertEqual(usage_log.area_for_method("searchTasks"), "todos")
        self.assertEqual(usage_log.area_for_method("getArchitecture"), "index")
        self.assertEqual(usage_log.area_for_method("searchKnowledge"), "knowledge")
        self.assertEqual(usage_log.area_for_method("createSession"), "server")

    def test_server_dispatch_logs_success_and_failure_with_source_extension(self) -> None:
        from backend.server import Server

        server = Server()
        self.addCleanup(server.close)
        ws = self.dir / "ws"
        ws.mkdir()

        response = server.dispatch({"id": 1, "method": "searchTasks",
                                    "params": {"workspacePath": str(ws), "query": "nothing"}})
        self.assertIn("result", response)
        failure = server.dispatch({"id": 2, "method": "updateTask",
                                   "params": {"workspacePath": str(ws), "taskId": "missing", "patch": {}}})
        self.assertIn("error", failure)
        unknown = server.dispatch({"id": 3, "method": "noSuchMethod", "params": {}})
        self.assertIn("error", unknown)

        records = usage_log.read_records([self.log])
        self.assertEqual([(r["area"], r["action"], r["source"], r["exit_code"]) for r in records],
                         [("todos", "searchTasks", "extension", 0), ("todos", "updateTask", "extension", 1)])
        self.assertIsInstance(records[0]["hits"], int)
        self.assertEqual(records[0]["agent"], "extension")
        self.assertEqual(Path(records[0]["workspace"]).resolve(), ws.resolve())

    def test_retriever_injection_is_logged_with_entry_ids(self) -> None:
        from backend.knowledge import retriever
        from backend.knowledge.models import KnowledgeEntry
        from backend.knowledge.store import KnowledgeStore

        store = KnowledgeStore(str(self.dir / "k.db"))
        self.addCleanup(store.close)
        entry = KnowledgeEntry.create(source_repo="r", source_file="f", snippet_type="pattern",
                                      title="Retry with backoff", description="how to retry",
                                      code="def retry(): ...", tags=["retry"])
        store.add_entry(entry)

        section = retriever.search_for_context("retry backoff", store=store)
        self.assertIn("Retry with backoff", section)
        self.assertEqual(retriever.search_for_context("zzzz", store=store), "")

        records = usage_log.read_records([self.log])
        self.assertEqual([(r["area"], r["action"], r["hits"]) for r in records],
                         [("knowledge", "inject", 1), ("knowledge", "inject", 0)])
        self.assertEqual(records[0]["hit_ids"], [entry.id])
        self.assertEqual(records[0]["result_bytes"], len(section))

        _, summary, _ = _run(["usage", "summary", "--since", "all", "--source", "extension"])
        self.assertEqual(summary["records"], 2)
        self.assertEqual(summary["knowledge"]["distinct_entries_retrieved"], 1)


if __name__ == "__main__":
    unittest.main()
