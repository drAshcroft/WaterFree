import io
import json
import os
import sqlite3
import subprocess
import unittest
from contextlib import redirect_stdout, redirect_stderr
from unittest.mock import patch

from backend.cli.dispatcher import dispatch
from backend.graph.engine import GraphEngine
from backend.graph.indexer import collect_files, parse_file
from backend.graph.graphify.extract import collect_files as graphify_files
from backend.graph.graphify.extract import extract_csharp, extract_gdscript
from backend.graph.graphify_adapter import to_store_nodes
from backend.test_support import make_temp_dir
from backend.todo.store import TaskStore
from backend.testing.runners import UnittestRunner


class ReportedIssueTests(unittest.TestCase):
    def test_quiet_todo_update_returns_a_receipt_and_keeps_notes(self):
        root = self.workspace()
        store = TaskStore(str(root))
        self.addCleanup(store.close)
        task = store.add_task({'title': 'Receipt', 'description': 'large body', 'aiNotes': 'existing notes'})
        code, out, err = self.run_cli(['todos', 'update', task.id, '--workspace', str(root), '--append-ai-notes', 'new note', '--quiet'])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out), {'id': task.id, 'status': 'pending'})
        self.assertIn('existing notes', store.get_task(task.id).ai_notes)
        self.assertIn('new note', store.get_task(task.id).ai_notes)
    def workspace(self):
        return make_temp_dir(self, prefix='reported-issues-')

    def run_cli(self, args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = dispatch(args)
        return code, out.getvalue(), err.getvalue()

    def test_null_update_and_upsert_clear_blocker_and_phase(self):
        root = self.workspace()
        store = TaskStore(str(root))
        self.addCleanup(store.close)
        task = store.add_task({'key': 'NULL-001', 'title': 'Task', 'description': 'd', 'blockedReason': 'blocked', 'phase': 'old'})
        store.update_task(task.id, {'blockedReason': None, 'phase': None})
        self.assertIsNone(store.get_task(task.id).blocked_reason)
        self.assertIsNone(store.get_task(task.id).phase)
        store.update_task(task.id, {'blockedReason': 'again'})
        payload = [{'key': 'NULL-001', 'title': 'Task', 'description': 'd', 'blockedReason': None}]
        dry = store.import_tasks(payload, upsert=True, dry_run=True)
        self.assertIsNone(dry.updated[0].blocked_reason)
        self.assertEqual(store.get_task(task.id).blocked_reason, 'again')
        store.import_tasks(payload, upsert=True, dry_run=False)
        self.assertIsNone(store.get_task(task.id).blocked_reason)
        self.assertTrue(store.validate().ok)

    def test_missing_or_drive_relative_workspace_never_creates_backlog(self):
        missing = self.workspace() / 'missing'
        for path in (str(missing), 'C:ProjectsVillage'):
            code, out, err = self.run_cli(['todos', 'add', '--workspace', path, '--title', 'Wrong store'])
            self.assertEqual(code, 2)
            self.assertFalse(out)
            self.assertIn('workspace', err.lower())
        self.assertFalse(missing.exists())

    def test_windows_workspace_spellings_select_same_store(self):
        if os.name != 'nt':
            self.skipTest('Windows path normalization')
        root = self.workspace()
        store = TaskStore(str(root))
        self.addCleanup(store.close)
        task = store.add_task({'key': 'PATH-001', 'title': 'Path', 'description': 'd'})
        for spelling in (str(root), root.as_posix(), str(root).replace('\\', '\\\\')):
            code, out, err = self.run_cli(['todos', 'get', task.key, '--workspace', spelling])
            self.assertEqual(code, 0, err)
            self.assertEqual(json.loads(out)['id'], task.id)

    def test_field_projection_retains_paging_counts(self):
        root = self.workspace()
        store = TaskStore(str(root))
        self.addCleanup(store.close)
        for i in range(3):
            store.add_task({'key': f'F-{i}', 'title': f'Task {i}', 'description': 'long description'})
        for action in ('list', 'get-ready'):
            code, out, err = self.run_cli(['todos', action, '--workspace', str(root), '--fields', 'key,title,priority', '--limit', '2'])
            self.assertEqual(code, 0, err)
            payload = json.loads(out)
            self.assertEqual(len(payload['tasks']), 2)
            self.assertEqual(set(payload['tasks'][0]), {'key', 'title', 'priority'})
            if action == 'list':
                self.assertEqual(payload['total'], 3)
                self.assertTrue(payload['truncated'])

    def test_explicit_key_and_scope_guidance(self):
        root = self.workspace()
        code, out, err = self.run_cli(['todos', 'add', '--workspace', str(root), '--title', '[TASK-123] Task', '--description', 'd', '--key', 'TASK-123'])
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli(['todos', 'get', 'TASK-123', '--workspace', str(root)])
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)['key'], 'TASK-123')
        code, out, err = self.run_cli(['todos', 'schema'])
        self.assertIn('prerequisite', json.loads(out)['properties']['rationale']['description'])

    def test_unittest_failure_traceback_is_retained(self):
        from backend.testing.runners import _parse_unittest_output
        result = _parse_unittest_output('test_it (test_demo.Demo.test_it) ... FAIL\n\n======================================================================\nFAIL: test_it (test_demo.Demo.test_it)\n----------------------------------------------------------------------\nTraceback (most recent call last):\n  example.py line 3\nAssertionError: expected value\n\n----------------------------------------------------------------------\nRan 1 test in 0.1s\nFAILED (failures=1)\n')
        self.assertEqual(result.failed, 1)
        self.assertIn('AssertionError: expected value', result.results[0].error)

    def test_graph_search_filters_name_and_class(self):
        root = self.workspace()
        (root / 'sample.py').write_text('class FighterVisual:\n    def run(self):\n        pass\ndef unrelated():\n    pass\n', encoding='utf-8')
        engine = GraphEngine()
        self.addCleanup(engine.close)
        indexed = engine.index_repository(str(root))
        result = engine.search_graph(project=indexed['project'], query='FighterVisual', node_type='class')
        self.assertEqual(result['total'], 1)
        self.assertEqual(result['results'][0]['label'], 'Class')
        empty = engine.search_graph(project=indexed['project'], query='unrelated', node_type='class')
        self.assertEqual(empty['total'], 0)

    def test_gdscript_search_declarations_calls_and_noise_exclusion(self):
        root = self.workspace()
        gd = root / 'village.gd'
        gd.write_text('class_name VillageSpec\nextends Node\n# func fake():\nvar s = "func ghost():"\nfunc generate():\n    helper()\nfunc helper():\n    pass\nclass Inner:\n    func nested():\n        pass\n', encoding='utf-8')
        noise = root / '.claude' / 'worktrees' / 'old'
        noise.mkdir(parents=True)
        (noise / 'copy.gd').write_text('class_name Wrong', encoding='utf-8')
        with patch('pathlib.Path.rglob', side_effect=AssertionError('Collector must use a pruned walk')):
            gfiles = graphify_files(root)
        for files in (collect_files(str(root)), gfiles):
            self.assertIn(gd, files)
            self.assertFalse(any('.claude' in p.parts for p in files))
        parsed = parse_file(str(gd), 'test', str(root))
        self.assertEqual({s.name for s in parsed.symbols}, {'VillageSpec', 'generate', 'helper', 'Inner', 'nested'})
        extracted = extract_gdscript(gd)
        self.assertTrue(any(e['relation'] == 'calls' for e in extracted['edges']))
        nodes = to_store_nodes(extracted['nodes'], 'test', str(root))
        self.assertTrue(any(n['label'] == 'Class' and n['name'] == 'VillageSpec' for n in nodes))
        engine = GraphEngine()
        self.addCleanup(engine.close)
        indexed = engine.index_repository(str(root))
        self.assertEqual(engine.search_graph(project=indexed['project'], query='VillageSpec', node_type='class')['total'], 1)
        self.assertTrue(engine.search_code('class_name VillageSpec', project=indexed['project'])['matches'])

    def test_csharp_class_survives_adapter(self):
        root = self.workspace()
        path = root / 'visual.cs'
        path.write_text('public class FighterVisual { public void Run() {} }', encoding='utf-8')
        nodes = to_store_nodes(extract_csharp(path)['nodes'], 'test', str(root))
        self.assertTrue(any(n['name'] == 'FighterVisual' and n['label'] == 'Class' for n in nodes))

    def test_unittest_deadline_and_timeout_keep_failure_and_partial_logs(self):
        runner = UnittestRunner()
        with patch.dict(os.environ, {'WATERFREE_UNITTEST_TIMEOUT': '1800'}), patch('backend.testing.runners._run_command', side_effect=subprocess.TimeoutExpired('test', 1800, output=b'build progress', stderr=b'partial error')) as run:
            result = runner._run(['test'], str(self.workspace()))
        self.assertEqual(run.call_args.kwargs['timeout'], 1800)
        self.assertEqual(result.failed, 1)
        self.assertIn('build progress', result.raw_output)
        self.assertIn('partial error', result.raw_output)

    def test_permission_error_is_actionable_without_traceback(self):
        with patch('backend.cli.knowledge.KnowledgeStore', side_effect=sqlite3.OperationalError('attempt to write a readonly database')):
            code, out, err = self.run_cli(['knowledge', 'stats'])
        self.assertEqual(code, 1)
        self.assertIn('approved execution', err)
        self.assertNotIn('Traceback', err)

    def test_optional_usage_log_is_silent_unless_debug_enabled(self):
        from backend.cli import usage_log
        err = io.StringIO()
        with patch('pathlib.Path.mkdir', side_effect=PermissionError('denied')), patch.dict(os.environ, {'WATERFREE_USAGE_LOG_DEBUG': '0'}), redirect_stderr(err):
            self.assertFalse(usage_log.append({}, path=self.workspace() / 'usage.jsonl'))
        self.assertFalse(err.getvalue())


if __name__ == '__main__':
    unittest.main()
