"""Render GLBs to PNG views so an agent can check a mesh (``waterfree meshgen preview``).

Agents cannot look at a GLB. This renders front, three-quarter, side and back
views plus a 2x2 grid with Google's ``<model-viewer>`` in headless Chromium
(Playwright, which WaterFree's qa and vision areas already use), so the grid
can go to ``waterfree vision look`` to catch holes, a flat back or wrong scale
before a mesh is handed over.

The approach was proven on 2026-11-17 with the first trellis chest:
``--use-angle=d3d11`` gives Chromium real GPU WebGL on Windows, model-viewer
frames the model itself, and ``jumpCameraToGoal()`` snaps each view without the
orbit animation. model-viewer is vendored (``viewer/``) so it works offline.
"""

from __future__ import annotations

import functools
import hashlib
import http.server
import shutil
import socketserver
import sys
import tempfile
import threading
from pathlib import Path

from backend.engines.install import InstallError

VIEWER = Path(__file__).with_name("viewer") / "model-viewer.min.js"
VIEWER_SHA256 = "774edda21e1be2a0934e460ca5943af1fe3f88da130a9f98bd6a9d611576cacf"
# model-viewer camera orbits: azimuth, polar angle (from straight up), radius.
VIEWS = {
    "front": "0deg 75deg auto",
    "three-quarter": "35deg 65deg auto",
    "side": "90deg 75deg auto",
    "back": "180deg 75deg auto",
}
_LOAD_TIMEOUT_MS = 180_000     # a 300K-triangle GLB with 2K textures loads in a few seconds; be generous

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<script type="module" src="model-viewer.min.js"></script>
<style>html,body{margin:0;background:#2b2b2b} model-viewer{width:SIZEpx;height:SIZEpx;background:#3a3a3a}</style>
</head><body><model-viewer id="mv" src="SRC" camera-orbit="0deg 75deg auto" exposure="1.0"
shadow-intensity="0.6" environment-image="neutral" interaction-prompt="none"></model-viewer></body></html>"""


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *_args) -> None:
        return


def check_viewer() -> None:
    if not VIEWER.is_file():
        raise InstallError(f"The vendored model-viewer is missing ({VIEWER}); reinstall WaterFree.")
    digest = hashlib.sha256(VIEWER.read_bytes()).hexdigest()
    if digest != VIEWER_SHA256:
        raise InstallError(f"{VIEWER} does not match its pinned SHA-256; reinstall WaterFree.")


def render(glbs: list[str], *, size: int = 512, report=lambda _m: None) -> list[dict]:
    """Render every GLB; return ``[{glb, grid, views: {name: png}}]`` or ``{glb, error}`` per input."""
    if not 128 <= size <= 2048:
        raise ValueError("size must be between 128 and 2048 px")
    sources = []
    for path in glbs:
        source = Path(path).expanduser().resolve()
        if not source.is_file():
            raise ValueError(f"GLB does not exist: {path}")
        if source.read_bytes()[:4] != b"glTF":
            raise ValueError(f"Not a binary glTF (GLB): {path}")
        sources.append(source)
    check_viewer()
    try:
        from playwright.sync_api import Error as PlaywrightError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise InstallError("Mesh previews need Playwright and its Chromium: `pip install playwright` and "
                           "`playwright install chromium`.") from exc

    results = []
    with tempfile.TemporaryDirectory(prefix="waterfree-preview-") as folder:
        root = Path(folder)
        shutil.copyfile(VIEWER, root / VIEWER.name)
        handler = functools.partial(_QuietHandler, directory=str(root))
        with socketserver.TCPServer(("127.0.0.1", 0), handler) as httpd:
            port = httpd.server_address[1]
            threading.Thread(target=httpd.serve_forever, daemon=True).start()
            try:
                with sync_playwright() as playwright:
                    # Real GPU WebGL on Windows; without it Chromium falls back to slow software GL.
                    args = ["--use-angle=d3d11", "--enable-gpu", "--ignore-gpu-blocklist"] \
                        if sys.platform == "win32" else ["--ignore-gpu-blocklist"]
                    browser = playwright.chromium.launch(args=args)
                    try:
                        for index, source in enumerate(sources):
                            report(f"[{index + 1}/{len(sources)}] rendering {source.name}")
                            try:
                                results.append(_render_one(browser, root, port, index, source, size))
                            except PlaywrightError as exc:
                                results.append({"glb": str(source), "error": str(exc).splitlines()[0]})
                    finally:
                        browser.close()
            except PlaywrightError as exc:
                raise InstallError(f"Chromium could not start for the preview ({str(exc).splitlines()[0]}). "
                                   "Run `playwright install chromium`.") from exc
            finally:
                httpd.shutdown()
    return results


def _render_one(browser, root: Path, port: int, index: int, source: Path, size: int) -> dict:
    model = f"model_{index}.glb"
    shutil.copyfile(source, root / model)
    page_name = f"view_{index}.html"
    (root / page_name).write_text(_PAGE.replace("SIZE", str(size)).replace("SRC", model), encoding="utf-8")
    page = browser.new_page(viewport={"width": size, "height": size})
    try:
        page.goto(f"http://127.0.0.1:{port}/{page_name}")
        page.wait_for_function("() => { const mv = document.getElementById('mv'); return mv && mv.loaded; }",
                               timeout=_LOAD_TIMEOUT_MS)
        views = {}
        for name, orbit in VIEWS.items():
            page.evaluate("""(orbit) => {
                const mv = document.getElementById('mv');
                mv.cameraOrbit = orbit;
                mv.jumpCameraToGoal();
                return new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
            }""", orbit)
            target = source.with_name(f"{source.stem}.preview-{name}.png")
            page.locator("#mv").screenshot(path=str(target))
            views[name] = str(target)
    finally:
        page.close()
        (root / model).unlink(missing_ok=True)
    grid = make_grid(views, source.with_name(f"{source.stem}.preview.png"))
    return {"glb": str(source), "grid": grid, "views": views}


def make_grid(views: dict[str, str], target: Path) -> str:
    """The four views as one labelled 2x2 PNG -- one image for a vision model to judge."""
    from PIL import Image, ImageDraw

    tiles = [(name, Image.open(path).convert("RGB")) for name, path in views.items()]
    width, height = tiles[0][1].size
    grid = Image.new("RGB", (width * 2, height * 2), (43, 43, 43))
    draw = ImageDraw.Draw(grid)
    for slot, (name, tile) in enumerate(tiles[:4]):
        x, y = (slot % 2) * width, (slot // 2) * height
        grid.paste(tile, (x, y))
        draw.text((x + 8, y + 6), name, fill=(235, 235, 235))
        tile.close()
    grid.save(target)
    return str(target)
