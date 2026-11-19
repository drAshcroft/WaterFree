"""The test suite must never open the user's real global stores (see conftest.py)."""

import io
import json
import os
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from backend.knowledge.store import _DB_PATH, _global_db_path


class StoreIsolationTests(unittest.TestCase):
    def test_knowledge_store_points_at_a_throwaway_db(self) -> None:
        self.assertTrue(os.environ.get("WATERFREE_KNOWLEDGE_DB"))
        self.assertNotEqual(_global_db_path().resolve(), _DB_PATH.resolve())
        self.assertNotIn(str(Path.home() / ".waterfree" / "global"), str(_global_db_path()))

    def test_dispatcher_hook_pretool_route_matches_the_fast_path(self) -> None:
        """`knowledge hook-pretool` through the dispatcher behaves like backend/main.py's fast path."""
        import sys
        from backend.cli.dispatcher import dispatch

        event = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
        stdin, buf = sys.stdin, io.StringIO()
        sys.stdin = io.StringIO(json.dumps(event))
        try:
            with redirect_stdout(buf):
                code = dispatch(["knowledge", "hook-pretool"])
        finally:
            sys.stdin = stdin
        self.assertEqual((code, buf.getvalue()), (0, ""))


if __name__ == "__main__":
    unittest.main()
