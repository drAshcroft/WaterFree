"""Real browser captures of a delayed SPA, without an LLM or external website."""
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock
from backend.test_support import make_temp_dir
from backend.vision import web


class _Page(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'''<html><head><title>Delayed app</title></head>
        <body style="margin:0;background:red;height:2400px"><script>
        setTimeout(() => {document.body.style.background='rgb(0,128,0)';
        const ready=document.createElement('div'); ready.id='ready';
        ready.textContent='Ready'; document.body.appendChild(ready)}, 200);
        </script></body></html>'''
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class VisionWebTests(unittest.TestCase):
    def test_spa_readiness_and_responsive_captures_use_viewports_for_analysis(self):
        from PIL import Image
        workspace = str(make_temp_dir(self, prefix="vision-web-"))
        server = ThreadingHTTPServer(("127.0.0.1", 0), _Page)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with mock.patch.object(web, "analyze", return_value={"answer": "A delayed app"}) as analyze:
                result = web.analyze_web(
                    f"http://127.0.0.1:{server.server_port}", workspace_path=workspace,
                    mobile=True, ready_selector="#ready", wait_ms=0,
                )
            self.assertEqual([c["breakpoint"] for c in result["captures"]], ["desktop", "mobile"])
            self.assertEqual(len(result["analyses"]), 2)
            for capture, invocation in zip(result["captures"], analyze.call_args_list):
                self.assertEqual(capture["title"], "Delayed app")
                self.assertEqual(invocation.args[0], [capture["viewport"]])
                with Image.open(capture["viewport"]) as image:
                    self.assertEqual(image.size, (capture["width"], capture["height"]))
                    self.assertEqual(image.convert("RGB").getpixel((30, 30)), (0, 128, 0))
                with Image.open(capture["full_page"]) as image:
                    self.assertGreater(image.height, capture["height"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_capture_only_does_not_require_a_vision_model(self):
        capture = {"breakpoint": "desktop", "viewport": "viewport.png", "full_page": "full.png"}
        with mock.patch.object(web, "capture_web", return_value=[capture]), mock.patch.object(web, "analyze") as analyze:
            result = web.analyze_web("https://example.com", capture_only=True)
        analyze.assert_not_called()
        self.assertEqual(result["captures"], [capture])
        self.assertEqual(result["analyses"], [])

    def test_full_page_analysis_requires_explicit_selection(self):
        capture = {"breakpoint": "desktop", "viewport": "viewport.png", "full_page": "full.png"}
        with mock.patch.object(web, "capture_web", return_value=[capture]), mock.patch.object(web, "analyze", return_value={}) as analyze:
            web.analyze_web("https://example.com", full_page=True)
        self.assertEqual(analyze.call_args.args[0], ["full.png"])

    def test_invalid_urls_are_rejected_before_browser_work(self):
        for url in ("file:///private.txt", "javascript:alert(1)", "https://user:password@example.com", "hello"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                web.capture_web(url)
