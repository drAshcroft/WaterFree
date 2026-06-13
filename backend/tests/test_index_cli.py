import unittest
from unittest.mock import patch

from backend.cli.index import _client_indexed


class FakeGraphClient:
    next_status = {"status": "ready"}

    def __init__(self) -> None:
        self.index_calls: list[str] = []

    def index_status(self, repo_path: str) -> dict:
        self.repo_path = repo_path
        return dict(self.next_status)

    def index(self, workspace_path: str) -> dict:
        self.index_calls.append(workspace_path)
        return {"project": "test"}


class IndexCliTests(unittest.TestCase):
    def test_client_indexed_does_not_reindex_ready_workspace(self) -> None:
        FakeGraphClient.next_status = {"status": "ready"}
        with patch("backend.cli.index.GraphClient", FakeGraphClient):
            client = _client_indexed("C:/repo")

        self.assertEqual(client.index_calls, [])

    def test_client_indexed_indexes_unready_workspace(self) -> None:
        FakeGraphClient.next_status = {"status": "not_indexed"}
        with patch("backend.cli.index.GraphClient", FakeGraphClient):
            client = _client_indexed("C:/repo")

        self.assertEqual(client.index_calls, ["C:/repo"])


if __name__ == "__main__":
    unittest.main()
