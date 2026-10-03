import unittest
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from backend.graph.graphify import extract as graphify_extract


class FailingFuture:
    def result(self):
        raise RuntimeError("worker process died")


class FakePool:
    def __init__(self, future: FailingFuture) -> None:
        self.future = future

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False

    def submit(self, func, item):
        return self.future


class GraphifyExtractParallelTests(unittest.TestCase):
    def test_extraction_progress_does_not_pollute_json_stdout(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            graphify_extract._extract_sequential([], [], Path('.'), total_files=100)
        self.assertFalse(out.getvalue())
        self.assertIn('AST extraction:', err.getvalue())

    def test_worker_future_exception_requests_sequential_fallback(self) -> None:
        future = FailingFuture()
        per_file: list[dict | None] = [None]

        with (
            patch("concurrent.futures.ProcessPoolExecutor", return_value=FakePool(future)),
            patch("concurrent.futures.as_completed", return_value=[future]),
        ):
            ran_parallel = graphify_extract._extract_parallel(
                [(0, Path("service.py"))],
                per_file,
                Path("."),
                max_workers=1,
                total_files=1,
            )

        self.assertFalse(ran_parallel)
        self.assertEqual(per_file, [None])


if __name__ == "__main__":
    unittest.main()
