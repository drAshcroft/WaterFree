"""Ollama under the GPU scheduler: who counts as local, eviction, the shared lease, preflight."""

import json
import os
import subprocess
import sys
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from backend.gpu import lease, ollama, preflight, vram
from backend.llm import ollama_client
from backend.test_support import make_temp_dir

REPO_ROOT = Path(__file__).resolve().parents[2]


class FakeOllama(BaseHTTPRequestHandler):
    resident: list[str] = []
    calls: list[tuple[str, dict]] = []

    def do_GET(self):
        if self.path == "/api/ps":
            self._json({"models": [{"name": n, "size_vram": 6 * 1024 ** 3} for n in self.resident]})
        else:
            self._json({}, 404)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        FakeOllama.calls.append((self.path, body))
        if self.path == "/api/generate" and body.get("keep_alive") == 0:
            FakeOllama.resident = [n for n in FakeOllama.resident if n != body["model"]]
            self._json({"done_reason": "unload"})
        elif self.path == "/api/chat":
            self._json({"message": {"content": "hello"}})
        else:
            self._json({"error": "unsupported"}, 400)

    def _json(self, payload, code=200):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *a):
        pass


class OllamaSchedulingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeOllama)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def setUp(self) -> None:
        self.state_dir = make_temp_dir(self, prefix="gpu-ollama-")
        patcher = mock.patch.dict(os.environ, {
            "WATERFREE_GPU_SCHEDULER": "1",
            "WATERFREE_GPU_DIR": str(self.state_dir),
            "WATERFREE_GPU_PROGRESS": "0",
            "WATERFREE_OLLAMA_BASE": self.base,   # this fake *is* the local daemon
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeOllama.resident = []
        FakeOllama.calls = []
        lease.release_all()
        self.addCleanup(lease.release_all)
        ollama.set_scope(ollama.SCOPE_PROCESS)
        self.addCleanup(ollama.set_scope, ollama.SCOPE_PROCESS)

    def test_only_the_local_daemon_competes_for_this_gpu(self) -> None:
        self.assertTrue(ollama.uses_local_gpu(self.base))
        self.assertTrue(ollama.uses_local_gpu(self.base.replace("127.0.0.1", "localhost") + "/v1"))
        self.assertFalse(ollama.uses_local_gpu("http://127.0.0.1:1"), "a test double on another port")
        self.assertFalse(ollama.uses_local_gpu("https://ollama.example.com:11434"))

    def test_default_daemon_address_honours_ollama_host(self) -> None:
        with mock.patch.dict(os.environ, {"OLLAMA_HOST": "0.0.0.0:11500"}):
            os.environ.pop("WATERFREE_OLLAMA_BASE")
            self.assertEqual(ollama.local_base(), "http://localhost:11500")

    def test_evict_unloads_every_resident_model(self) -> None:
        FakeOllama.resident = ["qwen2.5vl:7b", "qwen3.5:9b"]
        evicted = ollama.evict_all()
        self.assertEqual(evicted, ["qwen2.5vl:7b", "qwen3.5:9b"])
        self.assertEqual(FakeOllama.resident, [])
        self.assertTrue(all(body["keep_alive"] == 0 for _, body in FakeOllama.calls))

    def test_chat_holds_a_shared_lease_for_the_rest_of_the_command(self) -> None:
        lease.set_command_label("vision look")
        self.addCleanup(lease.set_command_label, "")
        self.assertEqual(ollama_client.chat("m", [{"role": "user", "content": "hi"}], base=self.base), "hello")
        holders = lease.snapshot()["holders"]
        self.assertEqual([(h["mode"], h["label"]) for h in holders], [("shared", "vision look")])

    def test_chat_in_call_scope_releases_after_each_request(self) -> None:
        ollama.set_scope(ollama.SCOPE_CALL)
        ollama_client.chat("m", [{"role": "user", "content": "hi"}], base=self.base)
        self.assertEqual(lease.snapshot()["holders"], [])

    def test_chat_to_a_remote_daemon_takes_no_lease(self) -> None:
        with mock.patch.dict(os.environ, {"WATERFREE_OLLAMA_BASE": "http://127.0.0.1:1"}):
            ollama_client.chat("m", [{"role": "user", "content": "hi"}], base=self.base)
        self.assertEqual(lease.snapshot()["holders"], [])

    def test_chat_waits_out_a_heavy_job_and_reports_it(self) -> None:
        holder = subprocess.Popen([sys.executable, "-c", textwrap.dedent("""
            import time
            from backend.gpu import lease
            lease.acquire(lease.EXCLUSIVE, label="meshgen make")
            print("held", flush=True)
            time.sleep(60)
        """)], env={**os.environ, "PYTHONPATH": str(REPO_ROOT)}, stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (holder.kill(), holder.wait(timeout=10), holder.stdout.close()))
        self.assertEqual(holder.stdout.readline().strip(), "held")
        with mock.patch.dict(os.environ, {"WATERFREE_GPU_WAIT_TIMEOUT": "0"}):
            with self.assertRaises(ollama_client.OllamaError) as ctx:
                ollama_client.chat("m", [{"role": "user", "content": "hi"}], base=self.base)
        self.assertIn("meshgen make", str(ctx.exception))
        self.assertFalse(any(path == "/api/chat" for path, _ in FakeOllama.calls),
                         "no model may be loaded while a heavy job holds the GPU")


class PreflightTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state_dir = make_temp_dir(self, prefix="gpu-preflight-")
        patcher = mock.patch.dict(os.environ, {
            "WATERFREE_GPU_SCHEDULER": "1",
            "WATERFREE_GPU_DIR": str(self.state_dir),
            "WATERFREE_GPU_PROGRESS": "0",
        })
        patcher.start()
        self.addCleanup(patcher.stop)
        lease.release_all()
        self.addCleanup(lease.release_all)
        for target, value in (("evict_all", []),):
            p = mock.patch.object(ollama, target, return_value=value)
            self.evict = p.start()
            self.addCleanup(p.stop)

    def gpu(self, free_mb: int) -> vram.GpuMemory:
        return vram.GpuMemory(0, "RTX 3060", 12288, 12288 - free_mb, free_mb)

    def test_refuses_a_job_that_would_spill_into_system_ram(self) -> None:
        with mock.patch.object(vram, "query", return_value=self.gpu(4096)), \
                mock.patch.object(vram, "gpu_processes", return_value=[{"pid": 1, "name": "chrome.exe", "usedMb": None}]), \
                mock.patch("backend.gpu.preflight.time.sleep"):
            with self.assertRaises(preflight.InsufficientVram) as ctx:
                with preflight.exclusive_gpu(label="meshgen make", vram_gb=9.0):
                    self.fail("must not run")
        self.assertIn("chrome.exe", str(ctx.exception))
        self.assertEqual(lease.snapshot()["holders"], [], "a refused job must release the lease")

    def test_evicts_ollama_then_runs_when_there_is_room(self) -> None:
        with mock.patch.object(vram, "query", return_value=self.gpu(10240)):
            with preflight.exclusive_gpu(label="audiogen sfx", vram_gb=8.0) as grant:
                self.assertEqual(grant.gpu.free_mb, 10240)
                self.assertEqual(lease.snapshot()["holders"][0]["label"], "audiogen sfx")
        self.evict.assert_called_once()

    def test_skip_vram_check_runs_anyway(self) -> None:
        with mock.patch.object(vram, "query", return_value=self.gpu(1024)):
            with preflight.exclusive_gpu(label="x", vram_gb=9.0, check_vram=False):
                pass

    def test_no_nvidia_smi_means_no_gate(self) -> None:
        with mock.patch.object(vram, "query", return_value=None):
            with preflight.exclusive_gpu(label="x", vram_gb=9.0) as grant:
                self.assertIsNone(grant.gpu)


if __name__ == "__main__":
    unittest.main()
