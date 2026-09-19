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
