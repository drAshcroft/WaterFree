"""sys.path hardening and shadow detection (IMG-SYSPATH-001)."""

import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from backend.runtime_paths import clean_path_entries, find_shadowing

REPO_ROOT = Path(__file__).resolve().parents[2]


class CleanPathEntriesTests(unittest.TestCase):
    VENV = r"C:\Projects\.local"
    BASE = r"C:\Python314"

    def _clean(self, entries, *, cwd=r"C:\Projects\game", backend_parent=r"C:\Tools\WaterFree\bin",
               prefix=VENV, base_prefix=BASE):
        return clean_path_entries(entries, prefix=prefix, base_prefix=base_prefix,
                                  cwd=cwd, backend_parent=backend_parent)

    def test_drops_venv_root_and_foreign_cwd(self) -> None:
        entries = ["", r"C:\Tools\WaterFree\bin", self.BASE + r"\Lib", self.VENV,
                   self.VENV + r"\Lib\site-packages", r"C:\Projects\game"]
        kept, removed = self._clean(entries)
        self.assertEqual(removed, ["", self.VENV, r"C:\Projects\game"])
        self.assertIn(self.VENV + r"\Lib\site-packages", kept)
        self.assertIn(r"C:\Tools\WaterFree\bin", kept)

    def test_keeps_cwd_when_it_holds_the_backend_package(self) -> None:
        kept, removed = self._clean(["", r"C:\Projects\WaterFree"], cwd=r"C:\Projects\WaterFree",
                                    backend_parent=r"C:\Projects\WaterFree")
        self.assertEqual(removed, [])
        self.assertEqual(kept, ["", r"C:\Projects\WaterFree"])

    def test_outside_a_venv_the_prefix_is_kept(self) -> None:
        kept, removed = self._clean([self.BASE], prefix=self.BASE, base_prefix=self.BASE)
        self.assertEqual(removed, [])

    def test_match_is_case_and_separator_insensitive_on_windows(self) -> None:
        if os.name != "nt":
            self.skipTest("normcase only folds case on Windows")
        _, removed = self._clean([r"c:/projects/.LOCAL"])
        self.assertEqual(removed, [r"c:/projects/.LOCAL"])


class FindShadowingTests(unittest.TestCase):
    def test_reports_a_loose_module_named_like_an_installed_package(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # find_spec only locates the file; the body must never run.
            Path(tmp, "pip.py").write_text("raise SystemExit('shadow executed')\n", encoding="utf-8")
            sys.path.insert(0, tmp)
            try:
                hits = find_shadowing(["pip"])
            finally:
                sys.path.remove(tmp)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["module"], "pip")
        self.assertTrue(hits[0]["file"].endswith("pip.py"))

    def test_clean_environment_reports_nothing_for_real_packages(self) -> None:
        self.assertEqual(find_shadowing(["pip", "json"]), [])


class MainEntryHardeningTests(unittest.TestCase):
    def test_backend_main_ignores_a_shadow_in_the_callers_cwd(self) -> None:
        """End to end: `python -m backend.main` from a folder holding a bogus `json.py`-style
        shadow for a third-party package must still import the real package."""
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "pip.py").write_text("raise SystemExit('shadow executed')\n", encoding="utf-8")
            probe = textwrap.dedent("""
                import runpy, sys
                sys.argv = ['backend.main', '--help']
                import backend.main  # hardens sys.path at import
                import pip
                print('PIP=' + pip.__file__)
            """)
            env = dict(os.environ, PYTHONPATH=str(REPO_ROOT))
            result = subprocess.run([sys.executable, "-c", probe], cwd=tmp, env=env,
                                    capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("site-packages", result.stdout)
        self.assertNotIn("shadow executed", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
