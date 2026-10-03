"""Capture website viewports locally, then pass them to the vision reader."""
from __future__ import annotations

import uuid
from pathlib import Path
from urllib.parse import urlsplit

from backend.vision.analyze import VisionError, analyze
from backend.vision.models import DEFAULT_PURPOSE


def capture_web(
    url: str, *, workspace_path: str = "", output_dir: str = "", mobile: bool = False,
    ready_selector: str = "", wait_ms: int = 750, timeout_ms: int = 30_000,
) -> list[dict]:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("Supply an HTTP(S) URL without embedded credentials.")
    if not 0 <= wait_ms <= 30_000 or not 1000 <= timeout_ms <= 120_000:
        raise ValueError("wait-ms must be 0–30000 and timeout-ms must be 1000–120000.")
    try:
        from playwright.sync_api import sync_playwright, Error
    except ImportError as exc:
        raise VisionError("Playwright is required for web capture; install playwright and its Chromium browser.") from exc
    destination = Path(output_dir).expanduser() if output_dir else Path(workspace_path or ".") / ".waterfree" / "vision-web" / uuid.uuid4().hex
    destination.mkdir(parents=True, exist_ok=True)
    captures: list[dict] = []
    breakpoints = [("desktop", 1280, 800)] + ([("mobile", 390, 844)] if mobile else [])
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch()
            try:
                for name, width, height in breakpoints:
                    context = browser.new_context(
                        viewport={"width": width, "height": height}, device_scale_factor=1,
                        is_mobile=name == "mobile", has_touch=name == "mobile", reduced_motion="reduce",
                    )
                    try:
                        page = context.new_page()
                        page.set_default_timeout(timeout_ms)
                        response = page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                        if response and response.status >= 400:
                            raise VisionError(f"Website returned HTTP {response.status}: {page.url}")
                        if ready_selector:
                            page.locator(ready_selector).first.wait_for(state="visible")
                        page.evaluate("() => Promise.race([document.fonts.ready, new Promise(r => setTimeout(r, 1000))])")
                        if wait_ms:
                            page.wait_for_timeout(wait_ms)
                        viewport = destination / f"{name}-viewport.png"
                        full_page = destination / f"{name}-full-page.png"
                        page.screenshot(path=str(viewport), animations="disabled")
                        page.screenshot(path=str(full_page), full_page=True, animations="disabled")
                        captures.append({
                            "breakpoint": name, "width": width, "height": height,
                            "url": page.url, "title": page.title(),
                            "viewport": str(viewport.resolve()), "full_page": str(full_page.resolve()),
                        })
                    finally:
                        context.close()
            finally:
                browser.close()
    except Error as exc:
        raise VisionError(f"Web capture failed: {exc}") from exc
    return captures


def analyze_web(
    url: str, *, purpose: str = DEFAULT_PURPOSE, question: str = "", model: str = "", tier: str = "",
    workspace_path: str = "", output_dir: str = "", mobile: bool = False, ready_selector: str = "",
    wait_ms: int = 750, timeout_ms: int = 30_000, full_page: bool = False, capture_only: bool = False,
) -> dict:
    captures = capture_web(
        url, workspace_path=workspace_path, output_dir=output_dir, mobile=mobile,
        ready_selector=ready_selector, wait_ms=wait_ms, timeout_ms=timeout_ms,
    )
    analyses = [] if capture_only else [
        {"breakpoint": capture["breakpoint"], **analyze(
            [capture["full_page" if full_page else "viewport"]],
            purpose=purpose, question=question, model=model, tier=tier,
        )} for capture in captures
    ]
    return {"url": url, "capture_mode": "full_page" if full_page else "viewport", "captures": captures, "analyses": analyses}
