"""Sidebar layout regression check.

Renders `src/ui/sidebar/sidebar.js` + `sidebar.css` in a headless browser at the
range of widths a VS Code sidebar can actually be dragged to, and fails if any
interactive control lands outside the viewport.

This exists because of a real trap: `.settings-title` was `flex: 1` with no
`min-width: 0`, so under a ~200px sidebar the settings header overflowed and
pushed `Close` off-screen. A webview sidebar has no horizontal scroll and the
settings panel replaces the whole view, so the only way out was to restart the
editor. A control that renders but cannot be reached is invisible to every
assertion that only checks the DOM -- this checks geometry instead.

Run:  python scripts/sidebar-layout/check.py
      npm run test:sidebar
Needs Playwright (available in the shared venv at C:/Projects/.local).
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile

REPO = pathlib.Path(__file__).resolve().parents[2]
SIDEBAR = REPO / "src" / "ui" / "sidebar"

# VS Code will not drag a sidebar narrower than ~170px; 120 gives real headroom.
WIDTHS = [340, 260, 220, 180, 150, 120]

# Stand-ins for the theme variables the real webview inherits from VS Code.
# Without them every color resolves to nothing and buttons collapse to bare text,
# which would make the geometry meaningless.
THEME_VARS = """:root{
--vscode-sideBar-background:#1f1f1f;--vscode-editorWidget-border:#454545;
--vscode-textLink-foreground:#4daafc;--vscode-descriptionForeground:#9d9d9d;
--vscode-focusBorder:#0078d4;--vscode-inputValidation-warningForeground:#ff8c00;
--vscode-testing-iconPassed:#73c991;--vscode-errorForeground:#f48771;
--vscode-button-background:#0078d4;--vscode-button-foreground:#fff;
--vscode-button-hoverBackground:#026ec1;--vscode-button-secondaryBackground:#313131;
--vscode-button-secondaryForeground:#ccc;--vscode-button-secondaryHoverBackground:#3c3c3c;
--vscode-list-hoverBackground:#2a2d2e;--vscode-foreground:#ccc;
--vscode-font-family:"Segoe UI",sans-serif;--vscode-input-background:#313131;
--vscode-editor-font-family:Consolas,monospace;}"""

STUB_API = (
    "<script>window.__posted=[];window.acquireVsCodeApi=function(){return{"
    "postMessage:function(m){window.__posted.push(m)},"
    "getState:function(){return null},setState:function(){}}};</script>"
)

OFFSCREEN_JS = """(w) => {
  const bad = [];
  document.querySelectorAll('button,[data-action],select,textarea,input').forEach(e => {
    const r = e.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) return;          // not rendered
    if (r.x < -0.5 || (r.x + r.width) > w + 0.5) {
      bad.push((e.getAttribute('data-action') || e.tagName)
               + ' "' + (e.textContent || '').trim().slice(0, 16) + '"'
               + ' x=' + r.x.toFixed(0) + ' right=' + (r.x + r.width).toFixed(0));
    }
  });
  return bad;
}"""

EXIT_REACHABLE_JS = """(w) => {
  const e = document.querySelector("[data-action='closeSettings']");
  if (!e) return 'no Close button rendered';
  const r = e.getBoundingClientRect();
  return (r.x >= -0.5 && r.x + r.width <= w + 0.5) ? 'ok' : 'Close is off-screen';
}"""

SETTINGS_PAYLOAD = {
    "providerTypes": [{
        "type": "claude", "label": "Claude",
        "defaultBaseUrl": "https://api.anthropic.com",
        "apiKeyPlaceholder": "sk-ant-api03-...",
        "defaultModels": ["claude-opus-4-6"],
        "requiresApiKey": True, "supportsBaseUrl": True,
    }],
    "providers": [{
        "id": "p1", "type": "claude", "name": "Claude", "enabled": True,
        "models": ["claude-opus-4-6"], "baseUrl": "https://api.anthropic.com",
        "hasKey": True,
    }],
    "activeProviderId": "p1",
    "personaAssignments": [],
}

# Sub-pages that stay inside the sidebar. `personas`/`todos`/`index`/`knowledge`
# hand off to editor panels instead, so they have no header of their own.
INLINE_PAGES = ["providers", "mcp", "skills", "usage"]


def build_harness(target: pathlib.Path) -> pathlib.Path:
    css = (SIDEBAR / "sidebar.css").read_text(encoding="utf-8")
    js = (SIDEBAR / "sidebar.js").read_text(encoding="utf-8")
    target.write_text(
        "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
        + THEME_VARS + css
        + "</style></head><body><div id='app'></div>"
        + STUB_API
        + "<script>" + js + "</script></body></html>",
        encoding="utf-8",
    )
    return target


def post(page, message: dict) -> None:
    page.evaluate("m => window.dispatchEvent(new MessageEvent('message',{data:m}))", message)
    page.wait_for_timeout(120)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("SKIP: playwright not installed (pip install playwright && playwright install chromium)")
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        harness = build_harness(pathlib.Path(tmp) / "sidebar.html")
        failures: list[str] = []

        with sync_playwright() as p:
            browser = p.chromium.launch()
            for width in WIDTHS:
                page = browser.new_page(viewport={"width": width, "height": 800})
                errors: list[str] = []
                page.on("pageerror", lambda e: errors.append(str(e)))
                page.goto(harness.as_uri())
                page.wait_for_timeout(200)
                post(page, {"type": "settings", "data": SETTINGS_PAYLOAD})

                def check(label: str) -> None:
                    for item in page.evaluate(OFFSCREEN_JS, width):
                        failures.append(f"w={width} {label}: {item}")

                check("main")
                post(page, {"type": "openSettings"})
                check("settings-menu")
                exit_state = page.evaluate(EXIT_REACHABLE_JS, width)
                if exit_state != "ok":
                    failures.append(f"w={width} settings-menu: {exit_state}")

                for name in INLINE_PAGES:
                    nav = page.locator(f"[data-action='settingsNav'][data-page='{name}']")
                    if nav.count() == 0:
                        post(page, {"type": "openSettings"})
                        nav = page.locator(f"[data-action='settingsNav'][data-page='{name}']")
                    nav.first.click()
                    page.wait_for_timeout(150)
                    check(name)
                    exit_state = page.evaluate(EXIT_REACHABLE_JS, width)
                    if exit_state != "ok":
                        failures.append(f"w={width} {name}: {exit_state}")
                    page.locator("[data-action='settingsBack']").first.click()
                    page.wait_for_timeout(120)

                for err in errors:
                    failures.append(f"w={width} pageerror: {err}")
                print(f"  w={width:4d} {'ok' if not failures else 'see failures'}")
                page.close()
            browser.close()

    if failures:
        print("\nFAIL - controls unreachable at these widths:")
        for line in failures:
            print("  " + line.encode("ascii", "backslashreplace").decode("ascii"))
        return 1
    print("\nPASS - every control stays inside the viewport at all tested widths")
    return 0


if __name__ == "__main__":
    sys.exit(main())
