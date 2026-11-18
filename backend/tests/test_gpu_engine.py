"""EngineServer: start/health/requests/stop, crash reporting, and kill-on-parent-death."""

import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path

from backend.gpu import lease
from backend.gpu.engine import EngineError, EngineServer
from backend.test_support import make_temp_dir

REPO_ROOT = Path(__file__).resolve().parents[2]

FAKE_SERVER = textwrap.dedent("""
    import http.server, os, sys, time
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1])
    mode = os.environ.get("FAKE_MODE", "ok")
    print("fake engine starting", flush=True)
    if mode == "crash":
        print("fatal: failed to allocate 9.1 GB on CUDA0", flush=True)
        sys.exit(3)
    time.sleep(float(os.environ.get("FAKE_LOAD_SECONDS", "0")))

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                self._send(200, b"ok")
            else:
                self._send(404, b"nope")

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            if self.path == "/die":
                os._exit(5)
            if self.path == "/reject":
                self._send(400, b"bad seed")
                return
            self._send(200, body, extra={"X-MOSS-Generate-Seconds": "1.5"})

        def _send(self, code, body, extra=None):
            self.send_response(code)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):
            pass

    http.server.HTTPServer(("127.0.0.1", port), Handler).serve_forever()
""")


class EngineServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = make_temp_dir(self, prefix="engine-")
        self.script = self.dir / "fake_engine.py"
        self.script.write_text(FAKE_SERVER, encoding="utf-8")
        self.messages: list[str] = []

    def server(self, **env) -> EngineServer:
        return EngineServer(name="fake", argv=[sys.executable, str(self.script)],
                            env={**os.environ, **env}, startup_timeout=20, report=self.messages.append)

    def test_serves_requests_and_stops_cleanly(self) -> None:
        with self.server(FAKE_LOAD_SECONDS="0.5") as server:
            self.assertTrue(server.running())
            self.assertGreater(server.load_seconds, 0.4)
            body, headers = server.post_json("/tts", {"text": "hi"}, timeout=10)
            self.assertEqual(body, b'{"text": "hi"}')
            self.assertEqual(headers["x-moss-generate-seconds"], "1.5")
            process = server.process
        self.assertIsNotNone(process.poll(), "stop() must end the server process")

    def test_a_crash_while_loading_quotes_the_engine_log(self) -> None:
        with self.assertRaises(EngineError) as ctx:
            self.server(FAKE_MODE="crash").start()
        self.assertIn("exited while loading", str(ctx.exception))
        self.assertIn("failed to allocate", ctx.exception.log_tail)
        self.assertTrue(ctx.exception.log_path.is_file())

    def test_a_rejected_request_carries_the_status(self) -> None:
        with self.server() as server, self.assertRaises(EngineError) as ctx:
            server.post_json("/reject", {}, timeout=10)
        self.assertEqual(ctx.exception.status, 400)
        self.assertIn("bad seed", str(ctx.exception))

    def test_a_crash_during_a_request_is_reported_as_a_crash(self) -> None:
        with self.server() as server:
            with self.assertRaises(EngineError) as ctx:
                server.post_json("/die", {}, timeout=10)
            # The exit can race the request's connection error; either way the
            # caller must learn the server is gone.
            deadline = time.monotonic() + 5
            while server.running() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(server.running())
        self.assertTrue("crashed" in str(ctx.exception) or "failed" in str(ctx.exception))

    def test_multipart_upload_reaches_the_server(self) -> None:
        image = self.dir / "in.png"
        image.write_bytes(b"\x89PNG fake")
        with self.server() as server:
            body, _ = server.post_multipart("/generate", fields={"seed": "7"}, files={"image": image},
                                            timeout=10)
        self.assertIn(b'name="seed"', body)
        self.assertIn(b"\x89PNG fake", body)


@unittest.skipUnless(sys.platform == "win32", "Job Objects are Windows-only")
class KillOnParentDeathTests(unittest.TestCase):
    def test_engine_dies_with_a_hard_killed_parent(self) -> None:
        directory = make_temp_dir(self, prefix="engine-orphan-")
        script = directory / "fake_engine.py"
        script.write_text(FAKE_SERVER, encoding="utf-8")
        parent = subprocess.Popen(
            [sys.executable, "-c", textwrap.dedent(f"""
                import sys, time
                from backend.gpu.engine import EngineServer
                server = EngineServer(name="fake", argv=[sys.executable, r"{script}"], startup_timeout=20,
                                      report=lambda m: None)
                server.start()
                print(server.process.pid, flush=True)
                time.sleep(60)
            """)],
            env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}, stdout=subprocess.PIPE, text=True)
        try:
            engine_pid = int(parent.stdout.readline().strip())
            self.assertIsNotNone(lease.process_token(engine_pid))
            parent.kill()  # TerminateProcess: no finally, no atexit
            parent.wait(timeout=10)
            deadline = time.monotonic() + 10
            while lease.process_token(engine_pid) is not None and time.monotonic() < deadline:
                time.sleep(0.2)
            self.assertIsNone(lease.process_token(engine_pid),
                              "the engine outlived its parent; it would keep holding VRAM")
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.stdout.close()


if __name__ == "__main__":
    unittest.main()
