"""assetsearch --packs/--subjects grouping, shared by text and JSON output (ASSET-JSON-001)."""

import importlib
import sys
import unittest
from pathlib import Path

BIN = Path(__file__).resolve().parents[2] / "skills" / "waterfree-assets" / "bin"


def _load():
    sys.path.insert(0, str(BIN))
    try:
        return importlib.import_module("assetsearch")
    finally:
        sys.path.remove(str(BIN))


def _row(pack, subject, kind="texture", **extra):
    row = {"pack": pack, "subject": subject, "kind": kind, "publisher": "Pub", "licence": "CC0",
           "imported": 1, "pack_path": f"/{pack}", "prefab": "", "mesh": "", "context": "",
           "variant": "", "path": f"{pack}/{subject}.{kind}"}
    row.update(extra)
    return row


class GroupingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.mod = _load()

    def test_subjects_collapse_variants_in_first_hit_order(self) -> None:
        hits = [_row("A", "rock", "texture"), _row("A", "rock", "model", mesh="rock.glb"),
                _row("B", "tree"), _row("A", "rock", "texture")]
        groups = self.mod.group_subjects(hits)
        self.assertEqual([(g["pack"], g["subject"], g["files"]) for g in groups],
                         [("A", "rock", 3), ("B", "tree", 1)])
        self.assertEqual(groups[0]["kinds"], ["model", "texture"])
        self.assertEqual(groups[0]["mesh"], "rock.glb")
        self.assertIs(groups[0]["imported"], True)

    def test_packs_sorted_by_matching_files(self) -> None:
        hits = [_row("A", "x"), _row("B", "y"), _row("B", "z")]
        packs = self.mod.group_packs(hits)
        self.assertEqual([(p["pack"], p["files"]) for p in packs], [("B", 2), ("A", 1)])

    def test_groups_are_json_serialisable(self) -> None:
        import json
        json.dumps(self.mod.group_subjects([_row("A", "rock")]))


if __name__ == "__main__":
    unittest.main()
