"""
Error types shared by every test-runner adapter.

These all mean "we could not get as far as running your tests" — a setup or
discovery problem, never a red suite. The CLI maps them to a dedicated exit
code so an agent can tell "your tests failed" apart from "this machine cannot
run your tests", which is the distinction the old code lost when it let a
missing `npx` escape as a raw FileNotFoundError traceback.
"""

from __future__ import annotations


class TestingError(RuntimeError):
    """Base for every recoverable testing-setup problem."""


class ToolchainError(TestingError):
    """A required external executable is missing or could not be launched.

    Carries the command the user should run to fix it, in the message, so the
    CLI never has to guess what advice to print.
    """


class NoFrameworkError(TestingError):
    """No supported test framework could be detected in this workspace.

    Raised instead of silently falling back to a runner that will discover
    nothing: a confident `0 passed, 0 failed, exit 0` reads as a green suite to
    every caller, which is the worst possible answer.
    """


class NoTestsDiscoveredError(TestingError):
    """A runner was selected and ran, but found no tests at all.

    Same reasoning as NoFrameworkError: zero discovered tests is not evidence
    that anything passes.
    """


class NoTestsMatchedError(NoTestsDiscoveredError):
    """`run-one` executed nothing: the filter matched no runnable test.

    Used to come back as `passed=0, failed=1` with a synthetic failing result,
    so the CLI exited 1 and agents read a typo'd or wrong-runner test name as a
    red test — then chased a non-bug or stopped trusting the helper. It is a
    selection problem, so it shares the setup-error exit code (4).

    Carries enough structure for the CLI to render a JSON error: which runner
    looked, with what filter, and how many tests that runner does know about.
    `other_runners` is filled in by the CLI when the runner was auto-detected
    in a polyglot workspace (see `detect_all_runners`).
    """

    def __init__(
        self,
        *,
        runner: str,
        filter: str,
        available: int | None = None,
        skipped: int = 0,
        raw_output: str = "",
        detail: str = "",
    ) -> None:
        self.runner = runner
        self.filter = filter
        self.available = available
        self.skipped = skipped
        self.raw_output = raw_output
        self.other_runners: list[dict] = []
        parts = [f"{runner} executed no tests matching '{filter}'"]
        if available is not None:
            parts.append(f"({available} test{'s' if available != 1 else ''} known to {runner})")
        message = " ".join(parts) + "."
        if skipped:
            message += f" {skipped} reported as skipped/pending, which is not a run."
        if detail:
            message += f" {detail}"
        message += (
            " This is not a failing test: check the name with `waterfree testing "
            "list`, or force a different framework with --runner."
        )
        super().__init__(message)


class RunIncompleteError(TestingError):
    """A run was stopped before it could produce a verdict.

    Raised by the bounded subprocess wrapper when `--budget` (wall time) or
    `--stall` (no output) fires and the process tree is killed. An incomplete
    run is never a pass and never a red suite: it is "we do not know", which is
    the setup/incomplete exit code (4).

    `partial` is whatever result the runner had assembled before the stop (for
    example the npm scripts that did finish); `raw_output` is everything the
    process printed, so `waterfree testing logs` still shows the hang.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        elapsed: float,
        limit: float | None = None,
        first_error: str | None = None,
        first_error_location: str | None = None,
        raw_output: str = "",
        partial=None,
    ) -> None:
        self.reason = reason
        self.elapsed = elapsed
        self.limit = limit
        self.first_error = first_error
        self.first_error_location = first_error_location
        self.raw_output = raw_output
        self.partial = partial
        super().__init__(message)
