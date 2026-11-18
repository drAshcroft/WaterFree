"""Vision on a busy host (VIS-LOAD-001).

A fake Ollama daemon on 127.0.0.1 answers slowly on purpose, so these tests
drive the real urllib timeout paths -- the bare TimeoutError a slow response
raises is exactly what used to surface as "Unexpected error listing Ollama
models". Nothing here touches a real Ollama or the network.
"""

import argparse
import contextlib
import io
import json
import socket
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Self
from unittest import mock

from backend.cli import vision as cli_vision
from backend.llm import ollama_client
from backend.vision import analyze as analyze_mod
from backend.vision.analyze import VisionError, VisionTimeout, analyze
from backend.vision.models import VisionModelMissing


def _write_png(path: Path) -> Path:
    from PIL import Image

    Image.new("RGB", (64, 48), (200, 40, 40)).save(path)
    return path


class FakeOllama:
    """Minimal /api/tags + /api/chat with scripted delays and statuses.

    `chat_script` is consumed one entry per chat request: a float delays the
    answer by that many seconds, an int is returned as an HTTP error status.
    Once it runs out, chat answers immediately with `answer`.
    """

    def __init__(self, *, tags_delay: float = 0.0, chat_script=(), answer: str = "a red rectangle",
                 models=("qwen2.5vl:7b", "moondream:latest")) -> None:
        self.tags_delay = tags_delay
        self.chat_script = list(chat_script)
        self.answer = answer
        self.models = list(models)
        self.paths: list[str] = []
        self.chat_payloads: list[dict] = []
        self._lock = threading.Lock()
        self._release = threading.Event()
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                fake._record(self.path)
                if self.path != "/api/tags":
                    return self._send(404, {"error": "404 page not found"})
                fake._release.wait(fake.tags_delay)
                self._send(200, {"models": [{"name": name} for name in fake.models]})

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", "0")))
                fake._record(self.path)
                with fake._lock:
                    fake.chat_payloads.append(json.loads(body))
                    step = fake.chat_script.pop(0) if fake.chat_script else 0.0
                if isinstance(step, int):
                    detail = (
                        {"error": f"model '{fake.chat_payloads[-1]['model']}' not found"}
                        if step == 404 else
                        {"error": "server busy, please try again.  maximum pending requests exceeded"}
                    )
                    return self._send(step, detail)
                fake._release.wait(step)
                self._send(200, {"message": {"role": "assistant", "content": fake.answer}})

            def _send(self, status, payload):
                data = json.dumps(payload).encode()
                try:
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except OSError:
                    pass  # the client gave up first -- that is the point of the test

            def log_message(self, *args):
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def _record(self, path: str) -> None:
        with self._lock:
            self.paths.append(path)

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self._server.server_port}"

    def count(self, path: str) -> int:
        with self._lock:
            return self.paths.count(path)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._release.set()
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


def _closed_port_base() -> str:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    return f"http://127.0.0.1:{port}"


class OllamaClientTimeoutTests(unittest.TestCase):
    def test_slow_model_list_is_a_named_timeout_not_an_unexpected_error(self) -> None:
        with FakeOllama(tags_delay=3.0) as fake, self.assertRaises(ollama_client.OllamaTimeout) as ctx:
            ollama_client.list_models(base=fake.base, timeout=0.2)

        message = str(ctx.exception)
        self.assertNotIn("Unexpected error", message)
        self.assertIn("did not answer /api/tags in 0.2 s", message)
        self.assertIn("under load", message)
        self.assertEqual(ctx.exception.endpoint, "/api/tags")

    def test_list_timeout_is_still_an_ollama_error_for_existing_callers(self) -> None:
        with FakeOllama(tags_delay=3.0) as fake:
            self.assertFalse(ollama_client.has_model("moondream", base=fake.base, timeout=0.2))
            with self.assertRaises(ollama_client.OllamaError):
                ollama_client.has_model("moondream", base=fake.base, strict=True, timeout=0.2)

    def test_default_list_timeout_is_unchanged_for_other_callers(self) -> None:
        self.assertEqual(ollama_client.LIST_TIMEOUT_SECONDS, 5.0)

    def test_slow_chat_is_a_named_timeout(self) -> None:
        with FakeOllama(chat_script=[3.0]) as fake, self.assertRaises(ollama_client.OllamaTimeout) as ctx:
            ollama_client.chat("moondream", [{"role": "user", "content": "hi"}],
                               base=fake.base, timeout=0.2)

        self.assertIn("did not answer /api/chat in 0.2 s", str(ctx.exception))

    def test_http_errors_carry_their_status(self) -> None:
        with FakeOllama(chat_script=[404]) as fake, self.assertRaises(ollama_client.OllamaHTTPError) as ctx:
            ollama_client.chat("ghost", [{"role": "user", "content": "hi"}], base=fake.base)

        self.assertEqual(ctx.exception.status, 404)
        self.assertIn("Ollama HTTP 404", str(ctx.exception))  # message format unchanged

    def test_response_format_is_sent_as_ollama_format(self) -> None:
        schema = {"type": "object"}
        with FakeOllama() as fake:
            ollama_client.chat("m", [{"role": "user", "content": "hi"}], base=fake.base,
                               response_format=schema)
            ollama_client.chat("m", [{"role": "user", "content": "hi"}], base=fake.base)

        self.assertEqual(fake.chat_payloads[0]["format"], schema)
        self.assertNotIn("format", fake.chat_payloads[1])

    def test_refused_connection_is_not_a_timeout(self) -> None:
        with self.assertRaises(ollama_client.OllamaError) as ctx:
            ollama_client.list_models(base=_closed_port_base(), timeout=10)

        self.assertNotIsInstance(ctx.exception, ollama_client.OllamaTimeout)
        self.assertIn("Make sure Ollama is running", str(ctx.exception))


class AnalyzeUnderLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.image = str(_write_png(Path(tmp.name) / "shot.png"))
        sleeper = mock.patch.object(analyze_mod, "_sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_explicit_model_skips_the_model_list_entirely(self) -> None:
        with FakeOllama(tags_delay=30.0) as fake:
            result = analyze([self.image], model="qwen2.5vl:7b", base=fake.base, timeout=5)
            tags_calls = fake.count("/api/tags")

        self.assertEqual(result["answer"], "a red rectangle")
        self.assertEqual(result["model"], "qwen2.5vl:7b")
        self.assertEqual(tags_calls, 0)
        self.assertEqual(result["warnings"], [])

    def test_slow_model_list_proceeds_to_generation_with_a_warning(self) -> None:
        with FakeOllama(tags_delay=30.0) as fake:
            result = analyze([self.image], purpose="ui", base=fake.base, timeout=0.5)
            chat_calls = fake.count("/api/chat")

        self.assertEqual(result["answer"], "a red rectangle")
        self.assertEqual(chat_calls, 1)
        self.assertEqual([w["code"] for w in result["warnings"]], ["model-check-skipped"])
        self.assertIn("under load", result["warnings"][0]["message"])

    def test_missing_model_found_by_the_list_still_names_the_pull_command(self) -> None:
        with FakeOllama(models=["nomic-embed-text:latest"]) as fake:
            with self.assertRaises(VisionModelMissing) as ctx:
                analyze([self.image], purpose="ui", base=fake.base, timeout=5)
            chat_calls = fake.count("/api/chat")

        self.assertIn("waterfree vision pull --tier large", str(ctx.exception))
        self.assertEqual(chat_calls, 0)

    def test_missing_explicit_model_surfaces_from_generation_with_the_pull_command(self) -> None:
        with FakeOllama(chat_script=[404]) as fake:
            with self.assertRaises(VisionModelMissing) as ctx:
                analyze([self.image], model="llava:13b", base=fake.base, timeout=5)
            chat_calls = fake.count("/api/chat")

        self.assertIn("ollama pull llava:13b", str(ctx.exception))
        self.assertEqual(chat_calls, 1, "a missing model is not a load problem; no retry")

    def test_timeouts_are_retried_with_backoff_and_then_name_the_load_and_the_flag(self) -> None:
        with FakeOllama(chat_script=[30.0, 30.0, 30.0, 30.0, 30.0]) as fake:
            with self.assertRaises(VisionTimeout) as ctx:
                analyze([self.image], model="qwen2.5vl:7b", base=fake.base, timeout=0.3, retries=2)
            chat_calls = fake.count("/api/chat")

        self.assertEqual(chat_calls, 3, "retries are bounded: 1 attempt + 2 retries")
        self.assertEqual([c.args[0] for c in self.sleep.call_args_list], [2.0, 4.0])
        message = str(ctx.exception)
        self.assertIn("Ollama did not answer in 0.3 s", message)
        self.assertIn("3 attempts", message)
        self.assertIn("under load", message)
        self.assertIn("--timeout", message)

    def test_a_transient_timeout_recovers_on_retry(self) -> None:
        with FakeOllama(chat_script=[30.0]) as fake:
            result = analyze([self.image], model="qwen2.5vl:7b", base=fake.base, timeout=0.3, retries=1)
            chat_calls = fake.count("/api/chat")

        self.assertEqual(result["answer"], "a red rectangle")
        self.assertEqual(chat_calls, 2)

    def test_busy_503_is_retried(self) -> None:
        with FakeOllama(chat_script=[503]) as fake:
            result = analyze([self.image], model="qwen2.5vl:7b", base=fake.base, timeout=5, retries=1)

        self.assertEqual(result["answer"], "a red rectangle")

    def test_zero_retries_means_one_attempt(self) -> None:
        with FakeOllama(chat_script=[30.0, 30.0]) as fake:
            with self.assertRaises(VisionTimeout) as ctx:
                analyze([self.image], model="qwen2.5vl:7b", base=fake.base, timeout=0.3, retries=0)
            chat_calls = fake.count("/api/chat")

        self.assertEqual(chat_calls, 1)
        self.sleep.assert_not_called()
        self.assertIn("1 attempt", str(ctx.exception))

    def test_daemon_down_fails_fast_without_retrying(self) -> None:
        with mock.patch.object(ollama_client, "chat", wraps=ollama_client.chat) as chat,                 self.assertRaises(VisionError) as ctx:
            analyze([self.image], model="qwen2.5vl:7b", base=_closed_port_base(), timeout=10, retries=2)

        self.assertNotIsInstance(ctx.exception, VisionTimeout)
        self.assertEqual(chat.call_count, 1)
        self.assertIn("Make sure Ollama is running", str(ctx.exception))

    def test_retry_count_is_capped(self) -> None:
        with self.assertRaises(ValueError):
            analyze([self.image], model="m", retries=analyze_mod.MAX_RETRIES + 1)

    def test_timeout_must_be_positive(self) -> None:
        for bad in (0, -5, float("nan"), float("inf")):
            with self.subTest(timeout=bad), self.assertRaises(ValueError):
                analyze([self.image], model="m", timeout=bad)


def _run_cli(argv: list[str]) -> tuple[int, str, str]:
    """Parse and run a `waterfree vision ...` command line.

    Builds only the vision area's parser rather than the whole dispatcher, so
    these tests do not depend on every other area importing cleanly.
    """
    parser = argparse.ArgumentParser(prog="waterfree")
    cli_vision.register(parser.add_subparsers(dest="area"))
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            args = parser.parse_args(argv)
            code = args._runner(args)
        except SystemExit as exc:  # argparse usage errors
            code = exc.code if isinstance(exc.code, int) else 1
    return code, out.getvalue(), err.getvalue()


class VisionCliLoadTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.image = str(_write_png(Path(tmp.name) / "shot.png"))
        sleeper = mock.patch.object(analyze_mod, "_sleep")
        sleeper.start()
        self.addCleanup(sleeper.stop)

    def test_look_with_model_never_lists_models(self) -> None:
        slow_list = mock.Mock(side_effect=AssertionError("listed models despite --model"))
        with mock.patch.object(ollama_client, "list_models", slow_list), \
                mock.patch.object(ollama_client, "chat", return_value="ok") as chat:
            code, out, _ = _run_cli(["vision", "look", self.image, "--model", "qwen2.5vl:7b",
                                     "--timeout", "42"])

        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["answer"], "ok")
        self.assertEqual(chat.call_args.kwargs["timeout"], 42.0)
        slow_list.assert_not_called()

    def test_timeout_error_names_load_and_the_flag(self) -> None:
        timeout = ollama_client.OllamaTimeout("slow", timeout=7.0, endpoint="/api/chat")
        with mock.patch.object(ollama_client, "chat", side_effect=timeout) as chat:
            code, _, err = _run_cli(["vision", "look", self.image, "--model", "qwen2.5vl:7b",
                                     "--timeout", "7"])

        self.assertEqual(code, 4)
        self.assertIn("did not answer in 7 s", err)
        self.assertIn("under load", err)
        self.assertIn("--timeout", err)
        self.assertEqual(chat.call_count, analyze_mod.DEFAULT_RETRIES + 1)

    def test_non_positive_timeout_is_a_usage_error(self) -> None:
        code, _, err = _run_cli(["vision", "look", self.image, "--timeout", "0"])

        self.assertEqual(code, 2)
        self.assertIn("--timeout", err)

    def test_web_accepts_a_model_timeout_separate_from_page_load(self) -> None:
        with mock.patch("backend.vision.web.analyze_web", return_value={"analyses": []}) as web:
            code, _, _ = _run_cli(["vision", "web", "http://127.0.0.1:9", "--timeout", "90",
                                   "--timeout-ms", "5000", "--capture-only"])

        self.assertEqual(code, 0)
        self.assertEqual(web.call_args.kwargs["timeout"], 90.0)
        self.assertEqual(web.call_args.kwargs["timeout_ms"], 5000)

    def test_models_reports_unknown_rather_than_missing_when_ollama_is_busy(self) -> None:
        timeout = ollama_client.OllamaTimeout("slow", timeout=10.0, endpoint="/api/tags")
        with mock.patch.object(ollama_client, "list_models", side_effect=timeout):
            code, out, _ = _run_cli(["vision", "models"])

        payload = json.loads(out)
        self.assertEqual(code, 0)
        self.assertTrue(all(row["installed"] is None for row in payload["tiers"]))
        self.assertIn("unknown", payload["warning"])

    def test_models_reports_install_state_from_a_single_listing(self) -> None:
        with mock.patch.object(ollama_client, "list_models", return_value=["moondream:latest"]) as listing:
            code, out, _ = _run_cli(["vision", "models"])

        rows = {row["tier"]: row["installed"] for row in json.loads(out)["tiers"]}
        self.assertEqual(code, 0)
        self.assertEqual(listing.call_count, 1)
        self.assertTrue(rows["small"])
        self.assertFalse(rows["large"])


if __name__ == "__main__":
    unittest.main()
