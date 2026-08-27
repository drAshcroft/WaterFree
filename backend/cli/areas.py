"""The canonical list of CLI areas.

Its own module so that `backend/main.py` -- the frozen executable's entry point
-- can gate on the area names without importing the dispatcher, which would pull
every area module (and their dependencies) into `waterfree serve` startup.

This tuple and the parsers registered in `dispatcher._build_parser()` must agree.
They drifted once: a new area was registered in the dispatcher while main.py kept
its own hardcoded copy, so the frozen `waterfree imagegen ...` printed usage and
exited 1 while the same command worked from source. `test_cli_areas.py` now fails
on any divergence.
"""

from __future__ import annotations

CLI_AREAS: tuple[str, ...] = (
    "todos",
    "knowledge",
    "index",
    "testing",
    "qa-summary",
    "vision",
    "imagegen",
)
