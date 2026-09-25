from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

_TEST_TMP_ROOT = Path(__file__).resolve().parents[1] / ".tmp" / "tests"
_TEST_TMP_ROOT.mkdir(parents=True, exist_ok=True)


# The CLI appends a usage record on every dispatch. Tests drive the dispatcher
# constantly, and none of that is real usage, so the log is off unless a test
# points WATERFREE_USAGE_LOG at a file of its own.
os.environ.setdefault("WATERFREE_USAGE_LOG", "0")


def make_temp_dir(test_case: unittest.TestCase, *, prefix: str = "test-") -> Path:
    path = Path(tempfile.mkdtemp(prefix=prefix, dir=_TEST_TMP_ROOT))
    test_case.addCleanup(lambda: shutil.rmtree(path, ignore_errors=True))
    return path
