from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

_TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "tests"
_TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)


def make_temp_dir(test_case: unittest.TestCase, *, prefix: str = "test-") -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=_TEST_TMP_ROOT))
    test_case.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
    return path
