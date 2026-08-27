"""The area list must not drift between the entry point and the dispatcher.

Regression guard. `backend/main.py` gates on a tuple of area names before it
imports the dispatcher; when that tuple was a separate hardcoded copy, an area
registered in the dispatcher was unreachable from the frozen executable --
`waterfree imagegen ...` printed usage and exited 1 while working from source.
Neither the unit tests nor a `python -m backend.cli.dispatcher` smoke test caught
it, because both bypass main.py entirely.
"""

import subprocess
import sys
import unittest

from backend.cli.areas import CLI_AREAS
from backend.cli.dispatcher import _build_parser


def _registered_areas() -> set[str]:
    parser = _build_parser()
    for action in parser._subparsers._group_actions:
        if action.choices:
            return set(action.choices)
    raise AssertionError("The dispatcher registered no subparsers.")


class AreaRegistrationTests(unittest.TestCase):
    def test_every_registered_area_is_reachable_from_the_entry_point(self) -> None:
        missing = _registered_areas() - set(CLI_AREAS)

        self.assertEqual(
            missing, set(),
            f"Areas registered in the dispatcher but missing from CLI_AREAS: "
            f"{sorted(missing)}. The built executable would reject them.",
        )

    def test_every_advertised_area_actually_exists(self) -> None:
        extra = set(CLI_AREAS) - _registered_areas()

        self.assertEqual(
            extra, set(),
            f"Areas in CLI_AREAS with no dispatcher parser: {sorted(extra)}. "
            f"The usage text would advertise a command that does not run.",
        )

    def test_the_entry_point_routes_every_area(self) -> None:
        """Drive main() itself -- the layer the unit tests otherwise skip."""
        for area in CLI_AREAS:
            with self.subTest(area=area):
                result = subprocess.run(
                    [sys.executable, "-c",
                     "from backend.main import main; main()", area, "--help"],
                    capture_output=True, text=True, timeout=120,
                )
                self.assertEqual(
                    result.returncode, 0,
                    f"`waterfree {area} --help` failed via main().\n"
                    f"stderr: {result.stderr[:400]}",
                )


if __name__ == "__main__":
    unittest.main()
