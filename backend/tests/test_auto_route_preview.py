import unittest
from unittest import mock
from backend.handlers.runtime_handler import handle_preview_auto_route
from backend.llm.openrouter_catalog import parse_models
from backend.llm.provider_profiles import normalize_provider_profile


class AutoRoutePreviewTests(unittest.TestCase):
    def test_reader_preview_and_agent_preview_use_the_runtime_filters(self):
        models = parse_models({"data": [
            {"id": "v/reader", "context_length": 128000, "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "v/tools", "context_length": 64000, "pricing": {"prompt": "0", "completion": "0"}, "supported_parameters": ["tools"]},
        ]})
        server = mock.Mock()
        server._get_provider_profile.return_value = normalize_provider_profile({"catalog": []})
        with mock.patch("backend.llm.openrouter_catalog.load_models", return_value=models):
            reader = handle_preview_auto_route(server, {"stage": "testing", "model": "auto:free"})
            agent = handle_preview_auto_route(server, {"stage": "execution", "model": "auto:free"})
        self.assertEqual([p['model'] for p in reader['chain'][:-1]], ['v/reader', 'v/tools'])
        self.assertEqual([p['model'] for p in agent['chain'][:-1]], ['v/tools'])
        self.assertEqual(agent['chain'][-1]['provider'], 'ollama')

    def test_invalid_preview_does_not_fetch(self):
        with mock.patch("backend.llm.openrouter_catalog.load_models") as fetch:
            with self.assertRaises(ValueError):
                handle_preview_auto_route(mock.Mock(), {"stage": "testing", "model": "v/concrete"})
        fetch.assert_not_called()
