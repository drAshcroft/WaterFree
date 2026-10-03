import subprocess
import sys
import unittest
from pathlib import Path


class CliDispatcherEntrypointTests(unittest.TestCase):
    def test_installed_entrypoint_help_mentions_asset_lookup(self) -> None:
        root = Path(__file__).resolve().parents[2]
        result = subprocess.run([sys.executable, '-m', 'backend.main', '--help'], cwd=root,
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0)
        self.assertIn('assetsearch.py', result.stderr)

    def test_dispatcher_module_prints_help(self) -> None:
        root = Path(__file__).resolve().parents[2]

        result = subprocess.run(
            [sys.executable, "-m", "backend.cli.dispatcher", "--help"],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0)
        self.assertIn("WaterFree workspace toolkit", result.stdout)
        self.assertIn("todos", result.stdout)
        self.assertIn("assetsearch.py", result.stdout)


if __name__ == "__main__":
    unittest.main()
