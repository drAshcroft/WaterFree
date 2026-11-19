"""Engine/model installs: adopt what is present, resume, verify, refuse unsafe archives."""

import hashlib
import http.server
import io
import threading
import unittest
import zipfile
from unittest import mock

from backend.engines import install
from backend.engines.catalog import (
    ENGINES,
    MODEL_SETS,
    EngineRelease,
    ModelSet,
    RemoteFile,
)
from backend.test_support import make_temp_dir


class _Files(http.server.BaseHTTPRequestHandler):
    files: dict[str, bytes] = {}
    requests: list[tuple[str, str | None]] = []
    cut_after: int | None = None   # simulate a dropped connection on the first full GET

    def do_GET(self):
        data = self.files.get(self.path)
        range_header = self.headers.get("Range")
        self.requests.append((self.path, range_header))
        if data is None:
            self.send_response(404)
            self.end_headers()
            return
        if range_header:
            start = int(range_header.split("=")[1].split("-")[0])
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{len(data) - 1}/{len(data)}")
            body = data[start:]
        else:
            self.send_response(200)
            body = data
            if _Files.cut_after is not None:
                body = data[:_Files.cut_after]
                _Files.cut_after = None
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class InstallTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Files)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        threading.Thread(target=cls.httpd.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()

    def setUp(self) -> None:
        self.dir = make_temp_dir(self, prefix="install-")
        _Files.files = {}
        _Files.requests = []
        _Files.cut_after = None
        self.messages: list[str] = []

    def remote(self, name: str, data: bytes) -> RemoteFile:
        _Files.files[f"/{name}"] = data
        return RemoteFile(url=f"{self.base}/{name}", name=name, size=len(data),
                          sha256=hashlib.sha256(data).hexdigest())

    def test_downloads_and_verifies(self) -> None:
        remote = self.remote("weights.gguf", b"x" * 5000)
        dest = self.dir / "weights.gguf"
        self.assertEqual(install.fetch(remote, dest, report=self.messages.append), "downloaded")
        self.assertEqual(dest.read_bytes(), b"x" * 5000)
        self.assertFalse(dest.with_name("weights.gguf.part").exists())

    def test_existing_correct_file_is_adopted_without_a_download(self) -> None:
        remote = self.remote("weights.gguf", b"abc" * 1000)
        dest = self.dir / "weights.gguf"
        dest.write_bytes(b"abc" * 1000)
        self.assertEqual(install.fetch(remote, dest, report=self.messages.append), "adopted")
        self.assertEqual(_Files.requests, [], "an adopted file must not be fetched again")

    def test_a_corrupt_file_of_the_right_size_is_replaced(self) -> None:
        remote = self.remote("weights.gguf", b"good" * 100)
        dest = self.dir / "weights.gguf"
        dest.write_bytes(b"evil" * 100)
        self.assertEqual(install.fetch(remote, dest, report=self.messages.append), "downloaded")
        self.assertEqual(dest.read_bytes(), b"good" * 100)
        self.assertTrue(dest.with_name("weights.gguf.corrupt").exists())

    def test_an_interrupted_download_resumes_with_a_range_request(self) -> None:
        data = bytes(range(256)) * 40
        remote = self.remote("big.gguf", data)
        _Files.cut_after = 3000
        dest = self.dir / "big.gguf"
        with mock.patch("backend.engines.install.time.sleep"):
            install.fetch(remote, dest, report=self.messages.append)
        self.assertEqual(dest.read_bytes(), data)
        self.assertIn(("/big.gguf", "bytes=3000-"), _Files.requests)

    def test_a_hash_mismatch_is_refused(self) -> None:
        remote = self.remote("weights.gguf", b"real")
        tampered = RemoteFile(url=remote.url, name=remote.name, size=remote.size, sha256="0" * 64)
        with self.assertRaises(install.InstallError):
            install.fetch(tampered, self.dir / "weights.gguf", report=self.messages.append)
        self.assertFalse((self.dir / "weights.gguf").exists())

    def test_zip_slip_is_refused(self) -> None:
        archive = self.dir / "evil.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("../../outside.txt", "nope")
        with self.assertRaises(install.InstallError):
            install.safe_extract(archive, self.dir / "out")
        self.assertFalse((self.dir.parent / "outside.txt").exists())

    def test_model_set_install_then_status_is_fast_and_exact(self) -> None:
        files = (self.remote("a.gguf", b"A" * 10), self.remote("a.extras.gguf", b"B" * 20))
        model_set = ModelSet(key="toy", label="Toy", engine="openmoss", repo="r", revision="v",
                             files=files, licence="MIT", vram_gb=1.0, notes="")
        with mock.patch("backend.engines.catalog.models_dir", return_value=self.dir / "models"):
            self.assertFalse(install.model_status(model_set)["installed"])
            result = install.install_models(model_set, report=self.messages.append)
            self.assertTrue(result["installed"])
            self.assertEqual(result["files"], {"a.gguf": "downloaded", "a.extras.gguf": "downloaded"})
            # A second install touches nothing.
            _Files.requests.clear()
            again = install.install_models(model_set, report=self.messages.append)
            self.assertEqual(set(again["files"].values()), {"present"})
            self.assertEqual(_Files.requests, [])
            # A file swapped behind our back is no longer "installed".
            (self.dir / "models" / "toy" / "a.gguf").write_bytes(b"Z" * 10)
            status = install.model_status(model_set)
            self.assertFalse(status["installed"])
            self.assertEqual(status["unverified"], ["a.gguf"])

    def test_engine_install_extracts_and_drops_the_archive(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            bundle.writestr("engine-server.exe", b"MZ")
            bundle.writestr("ggml.dll", b"MZ")
        archive = self.remote("engine.zip", buffer.getvalue())
        release = EngineRelease(key="toy", project="me/toy", version="v1", archive=archive,
                                executables={"server": "engine-server.exe"}, licence="MIT")
        with mock.patch("backend.engines.catalog.engines_dir", return_value=self.dir / "engines"):
            result = install.install_engine(release, report=self.messages.append)
            self.assertTrue(result["installed"])
            self.assertTrue(release.executable("server").is_file())
            self.assertFalse((self.dir / "engines" / "toy" / "_downloads" / "engine.zip").exists())
            self.assertEqual(install.install_engine(release, report=self.messages.append)["action"], "present")

    def test_require_names_the_install_command(self) -> None:
        with self.assertRaises(install.InstallError) as ctx:
            install.require("trellis", "trellis2-q8", install_hint="waterfree meshgen install")
        self.assertIn("waterfree meshgen install", str(ctx.exception))


class CatalogTests(unittest.TestCase):
    def test_every_file_is_pinned(self) -> None:
        for model_set in MODEL_SETS.values():
            self.assertEqual(len(model_set.revision), 40, model_set.key)
            for remote in model_set.files:
                self.assertEqual(len(remote.sha256), 64, remote.name)
                self.assertIn(model_set.revision, remote.url, "weights must come from the pinned commit")
                self.assertGreater(remote.size, 0)
        for release in ENGINES.values():
            self.assertEqual(len(release.archive.sha256), 64)
            self.assertIn(release.version, release.archive.url)

    def test_each_openmoss_set_has_one_backbone_and_its_sidecar(self) -> None:
        for model_set in MODEL_SETS.values():
            if model_set.engine != "openmoss":
                continue
            names = [f.name for f in model_set.files]
            backbones = [n for n in names if not n.endswith(".extras.gguf")]
            self.assertEqual(len(backbones), 1, model_set.key)
            self.assertIn(backbones[0].replace(".gguf", ".extras.gguf"), names)

    def test_dinov3_licence_is_surfaced(self) -> None:
        self.assertIn("DINOv3 License", MODEL_SETS["trellis2-q8"].licence)


if __name__ == "__main__":
    unittest.main()
