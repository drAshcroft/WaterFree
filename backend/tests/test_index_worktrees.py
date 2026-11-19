"""Nested worktrees are excluded from indexing and search-code by default (IDX-WT-001)."""

import tempfile
import unittest
from pathlib import Path

from backend.graph.indexer import collect_files
from backend.graph.query_service import QueryService


class WorktreeExclusionTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = root = Path(tmp.name)
        self._write("src/a.py", "class TempleRiteCheck: pass\n")
        # Tool worktree folder inside a dot-dir (BrickWild's .kilo/worktrees/salt-option).
        self._write(".kilo/worktrees/salt-option/src/a.py", "class TempleRiteCheck: pass  # stale\n")
        self._write(".kilo/settings.py", "KILO = 1\n")
        # A linked worktree checked out under an ordinary name.
        self._write("sweep/src/a.py", "class TempleRiteCheck: pass  # sweep copy\n")
        self._write("sweep/.git", f"gitdir: {root.as_posix()}/.git/worktrees/sweep\n")
        # A submodule also has a .git file, but it points into .git/modules and must stay.
        self._write("vendor/lib/b.py", "class Vendored: pass\n")
        self._write("vendor/lib/.git", "gitdir: ../../.git/modules/vendor/lib\n")

    def _write(self, rel: str, text: str) -> None:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")

    def _rel(self, files) -> set[str]:
        return {p.relative_to(self.root).as_posix() for p in files}

    def test_default_skips_tool_and_linked_worktrees_but_keeps_submodules(self) -> None:
        self.assertEqual(self._rel(collect_files(str(self.root))),
                         {"src/a.py", ".kilo/settings.py", "vendor/lib/b.py"})

    def test_include_worktrees_restores_them(self) -> None:
        files = self._rel(collect_files(str(self.root), include_worktrees=True))
        self.assertIn(".kilo/worktrees/salt-option/src/a.py", files)
        self.assertIn("sweep/src/a.py", files)

    def test_walking_from_inside_a_worktree_still_indexes_it(self) -> None:
        sweep = self.root / "sweep"
        self.assertEqual({p.name for p in collect_files(str(sweep))}, {"a.py"})

    def test_search_code_returns_only_the_active_checkout(self) -> None:
        query = QueryService()
        hits = query.search_code(str(self.root), "TempleRiteCheck")
        self.assertEqual([Path(m["file"]).relative_to(self.root).as_posix() for m in hits["matches"]],
                         ["src/a.py"])
        wide = query.search_code(str(self.root), "TempleRiteCheck", include_worktrees=True)
        self.assertEqual(wide["total"], 3)


if __name__ == "__main__":
    unittest.main()
