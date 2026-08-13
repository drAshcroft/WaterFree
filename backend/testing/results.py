"""
Shared result shapes for every test runner.

Kept in their own module so framework adapters (`runners.py`, `godot.py`, …)
can depend on the shapes without depending on each other.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TestResult:
    name: str
    passed: bool
    error: str | None = None
    duration_ms: float | None = None


@dataclass
class TestRunResult:
    passed: int
    failed: int
    results: list[TestResult] = field(default_factory=list)
    raw_output: str = ""
