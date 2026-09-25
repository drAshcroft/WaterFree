import unittest

from backend.cli.index import _DEFAULT_ASPECTS, fit_budget


class FitBudgetTests(unittest.TestCase):
    def big_trace(self) -> dict:
        nodes = [{"id": f"n{i}", "name": f"fn_{i}", "file_path": f"src/mod_{i}.py"} for i in range(60)]
        edges = [{"source": f"n{i}", "target": f"n{i+1}", "type": "CALLS", "confidence": 1.0 - i / 100} for i in range(59)]
        return {"nodes": nodes, "edges": edges, "impact_summary": {"callers": 59}}

    def test_zero_budget_only_reports_the_estimate(self) -> None:
        out = fit_budget(self.big_trace(), action="trace", budget_tokens=0)
        self.assertEqual(len(out["nodes"]), 60)
        self.assertFalse(out["budget"]["truncated"])
        self.assertGreater(out["budget"]["estimated_tokens"], 100)

    def test_trace_drops_edges_before_nodes_and_keeps_nearest_nodes(self) -> None:
        out = fit_budget(self.big_trace(), action="trace", budget_tokens=400)
        self.assertTrue(out["budget"]["truncated"])
        self.assertLessEqual(out["budget"]["estimated_tokens"], 400)
        self.assertGreater(out["budget"]["dropped"].get("edges", 0), 0)
        # nodes are BFS order (nearest first); trimming keeps the head
        self.assertEqual(out["nodes"][0]["id"], "n0")
        self.assertIn("hint", out["budget"])
        self.assertEqual(out["impact_summary"], {"callers": 59})

    def test_ranked_lists_keep_the_highest_degree_rows(self) -> None:
        arch = {
            "languages": [{"language": "python", "files": 10}],
            "god_nodes": [{"qualified_name": f"g{i}", "degree": i} for i in range(80)],
            "module_graph": [{"source": f"m{i}", "target": f"m{i+1}"} for i in range(200)],
        }
        out = fit_budget(arch, action="architecture", budget_tokens=300)
        self.assertLessEqual(out["budget"]["estimated_tokens"], 300)
        self.assertGreater(out["budget"]["dropped"]["module_graph"], 0)
        if out["god_nodes"]:
            self.assertEqual(out["god_nodes"][0]["degree"], 79)
        self.assertEqual(out["languages"], [{"language": "python", "files": 10}])

    def test_default_aspects_are_the_small_overview(self) -> None:
        self.assertEqual(_DEFAULT_ASPECTS, ("languages", "layers", "god_nodes"))


if __name__ == "__main__":
    unittest.main()
