"""Turn a live page into something a small model can read in one glance.

The snapshot lists interactive elements only, each numbered so the model can
say `CLICK 7` instead of guessing a selector. Layout flags come from raw
`getBoundingClientRect()` against the viewport, because Playwright's
`isVisible()` reports an element clipped off the bottom of the screen as
visible (a lesson carried over from the goblinchess harness). Visible text is
trimmed to a budget: instructions matter, walls of copy do not.

Two derived values matter to the driver:

* `digest` -- a stable hash of the interactive structure, used to detect the
  model repeating the same action on the same screen.
* `render_diff()` -- when the structure barely changed, show only what did.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field

MAX_ELEMENTS = 80
MAX_TEXT_CHARS = 1400
MAX_LABEL_CHARS = 70

# Runs inside the page. Returns the raw material; Python does the shaping.
SNAPSHOT_JS = r"""
() => {
  const vw = window.innerWidth, vh = window.innerHeight;
  const SELECTOR = [
    'button', 'a[href]', 'input', 'select', 'textarea', 'summary',
    '[role="button"]', '[role="link"]', '[role="tab"]', '[role="menuitem"]',
    '[role="checkbox"]', '[role="radio"]', '[role="switch"]', '[role="slider"]',
    '[role="option"]', '[role="textbox"]', '[role="combobox"]',
    '[data-testid]', '[tabindex]:not([tabindex="-1"])', '[onclick]', '[contenteditable="true"]',
  ].join(',');
  const dialogSel = 'dialog[open], [role="dialog"], [role="alertdialog"], [aria-modal="true"]';
  const seen = new Set();
  // Nodes that actually received a number this pass. Kept apart from `seen`,
  // which holds every node the selector matched including the ones skipped for
  // being hidden: clearing stale stamps against `seen` left a hidden element
  // holding the number a visible one had just been given, and the click landed
  // on the invisible one. Any app that hides and shows controls hit this.
  const numbered = new Set();
  const nodes = Array.from(document.querySelectorAll(SELECTOR));
  const rows = [];
  const isHidden = (n, cs, r) =>
    cs.display === 'none' || cs.visibility === 'hidden' || cs.opacity === '0' ||
    (r.width === 0 && r.height === 0) || !!n.closest('[hidden], [aria-hidden="true"]');
  const labelOf = (n) => {
    let t = n.getAttribute('aria-label') || '';
    if (!t && n.getAttribute('aria-labelledby')) {
      const el = document.getElementById(n.getAttribute('aria-labelledby'));
      if (el) t = el.innerText || el.textContent || '';
    }
    if (!t && n.labels && n.labels.length) t = n.labels[0].innerText || '';
    if (!t) t = n.innerText || n.value || n.placeholder || n.getAttribute('title') || n.getAttribute('alt') || '';
    if (!t && n.tagName === 'INPUT' && n.type) t = n.type;
    return t.replace(/\s+/g, ' ').trim();
  };
  for (const n of nodes) {
    if (seen.has(n)) continue;
    seen.add(n);
    // Skip wrappers whose only job is to hold a real control.
    if (n.hasAttribute('data-testid') && !n.matches('button, a[href], input, select, textarea, summary, [role], [tabindex], [onclick], [contenteditable="true"]')) {
      const inner = n.querySelector('button, a[href], input, select, textarea, [role="button"]');
      if (inner) continue;
      const cs0 = getComputedStyle(n);
      if (cs0.cursor !== 'pointer') continue;
    }
    const r = n.getBoundingClientRect();
    const cs = getComputedStyle(n);
    if (isHidden(n, cs, r)) continue;
    const flags = [];
    if (r.right <= 0 || r.bottom <= 0 || r.left >= vw || r.top >= vh) flags.push('offscreen');
    else if (r.left < -1 || r.top < -1 || r.right > vw + 1 || r.bottom > vh + 1) flags.push('clipped');
    if (n.disabled || n.getAttribute('aria-disabled') === 'true') flags.push('disabled');
    if (n.closest(dialogSel)) flags.push('in-dialog');
    if (n.getAttribute('aria-pressed') === 'true' || n.getAttribute('aria-checked') === 'true' || n.checked === true) flags.push('on');
    if (n.getAttribute('aria-selected') === 'true' || n.getAttribute('aria-current')) flags.push('selected');
    if (n.getAttribute('aria-expanded') === 'true') flags.push('expanded');
    const role = n.getAttribute('role') || (n.tagName === 'A' ? 'link' : n.tagName === 'INPUT' ? ('input:' + (n.type || 'text')) : n.tagName.toLowerCase());
    rows.push({
      role,
      label: labelOf(n),
      testid: n.getAttribute('data-testid') || '',
      value: (n.tagName === 'INPUT' || n.tagName === 'TEXTAREA' || n.tagName === 'SELECT') ? String(n.value || '') : '',
      rect: [Math.round(r.left), Math.round(r.top), Math.round(r.width), Math.round(r.height)],
      flags,
    });
    n.setAttribute('data-wfqa', String(rows.length));
    numbered.add(n);
  }
  // Clear stale numbering on every node that did NOT get a number this pass.
  for (const stale of document.querySelectorAll('[data-wfqa]')) {
    if (!numbered.has(stale)) stale.removeAttribute('data-wfqa');
  }
  const dialogs = Array.from(document.querySelectorAll(dialogSel))
    .filter(d => { const r = d.getBoundingClientRect(); return r.width > 0 && r.height > 0; })
    .map(d => (d.getAttribute('aria-label') || (d.querySelector('h1,h2,h3,[role="heading"]') || {}).innerText || d.getAttribute('data-testid') || 'dialog').replace(/\s+/g, ' ').trim().slice(0, 80));
  const ae = document.activeElement;
  const canvases = Array.from(document.querySelectorAll('canvas')).filter(c => { const r = c.getBoundingClientRect(); return r.width > 50 && r.height > 50; }).length;
  return {
    url: location.href,
    title: document.title,
    viewport: [vw, vh],
    overflow: [document.documentElement.scrollWidth - vw, document.documentElement.scrollHeight - vh],
    scrollY: Math.round(window.scrollY),
    dialogs,
    focused: ae && ae !== document.body ? (ae.getAttribute('data-wfqa') || null) : null,
    canvases,
    text: (document.body && document.body.innerText || '').replace(/[ \t]+/g, ' ').replace(/\n{2,}/g, '\n').trim(),
    rows,
  };
}
"""


@dataclass(frozen=True)
class Element:
    number: int
    role: str
    label: str
    testid: str = ""
    value: str = ""
    rect: tuple[int, int, int, int] = (0, 0, 0, 0)
    flags: tuple[str, ...] = ()

    @property
    def actionable(self) -> bool:
        return "offscreen" not in self.flags and "disabled" not in self.flags

    def render(self) -> str:
        label = self.label[:MAX_LABEL_CHARS] + ("…" if len(self.label) > MAX_LABEL_CHARS else "")
        bits = [f"[{self.number}]", self.role]
        if label:
            bits.append(f'"{label}"')
        if self.value and self.value != self.label:
            bits.append(f"value={self.value[:40]!r}")
        if self.flags:
            bits.append("(" + ", ".join(self.flags) + ")")
        return " ".join(bits)


@dataclass(frozen=True)
class Snapshot:
    url: str
    title: str
    viewport: tuple[int, int]
    overflow: tuple[int, int]
    scroll_y: int
    dialogs: tuple[str, ...]
    focused: int | None
    canvases: int
    text: str
    elements: tuple[Element, ...]
    truncated_elements: int = 0
    console_errors: int = 0
    digest: str = field(default="", compare=False)

    @property
    def has_interactive(self) -> bool:
        return any(e.actionable for e in self.elements)

    @property
    def is_canvas_only(self) -> bool:
        return self.canvases > 0 and not self.has_interactive

    def element(self, number: int) -> Element | None:
        for e in self.elements:
            if e.number == number:
                return e
        return None

    def claim(self) -> str:
        """One line describing what the DOM says is on screen, for vision to check."""
        parts = [f'page titled "{self.title}"' if self.title else "a page"]
        if self.dialogs:
            parts.append("with a dialog: " + "; ".join(self.dialogs))
        labelled = [e.label for e in self.elements if e.label and e.actionable][:6]
        if labelled:
            parts.append("showing controls " + ", ".join(f'"{x[:30]}"' for x in labelled))
        if self.canvases:
            parts.append(f"and {self.canvases} canvas area(s)")
        return " ".join(parts)

    def render(self, *, text_budget: int = MAX_TEXT_CHARS) -> str:
        lines = [f"URL: {self.url}", f"Title: {self.title or '(none)'}"]
        meta = [f"viewport {self.viewport[0]}x{self.viewport[1]}"]
        if self.overflow[1] > 0:
            meta.append(f"page scrolls {self.overflow[1]}px further down (scrolled {self.scroll_y}px)")
        if self.overflow[0] > 0:
            meta.append(f"page overflows {self.overflow[0]}px horizontally")
        if self.dialogs:
            meta.append("OPEN DIALOG: " + "; ".join(self.dialogs) + " (dialog controls are marked in-dialog; others may be blocked)")
        if self.focused:
            meta.append(f"focus on [{self.focused}]")
        if self.console_errors:
            meta.append(f"{self.console_errors} console error(s) so far")
        lines.append("; ".join(meta))
        lines.append("")
        if self.elements:
            lines.append("Interactive elements:")
            for e in self.elements:
                lines.append("  " + e.render())
            if self.truncated_elements:
                lines.append(f"  … {self.truncated_elements} more not shown; SCROLL to reveal others")
        else:
            lines.append("Interactive elements: none found in the DOM." + (
                " The page draws into a canvas; use LOOK to see it." if self.canvases else ""
            ))
        text = self.text
        if text:
            if len(text) > text_budget:
                text = text[:text_budget].rstrip() + " …"
            lines.append("")
            lines.append("Visible text:")
            lines.append(text)
        return "\n".join(lines)

    def render_diff(self, previous: "Snapshot", *, text_budget: int = MAX_TEXT_CHARS) -> str:
        """Compact render when little changed since `previous`."""
        prev = {(e.role, e.label, e.testid): e for e in previous.elements}
        cur = {(e.role, e.label, e.testid): e for e in self.elements}
        added = [e for k, e in cur.items() if k not in prev]
        removed = [e for k, e in prev.items() if k not in cur]
        flag_changes = [
            (cur[k], prev[k]) for k in cur.keys() & prev.keys() if cur[k].flags != prev[k].flags
        ]
        lines = [f"URL: {self.url}", f"Title: {self.title or '(none)'}"]
        if self.dialogs != previous.dialogs:
            lines.append("Dialogs now: " + ("; ".join(self.dialogs) or "none"))
        if not added and not removed and not flag_changes:
            lines.append("The page did not change. Element numbers are the same as before.")
        if added:
            lines.append("New elements:")
            lines += ["  " + e.render() for e in added]
        if removed:
            lines.append("Gone: " + ", ".join(f'"{e.label[:30]}"' or e.role for e in removed))
        if flag_changes:
            lines.append("Changed state:")
            lines += [f"  {c.render()} (was {', '.join(p.flags) or 'plain'})" for c, p in flag_changes]
        if self.text != previous.text:
            text = self.text
            if len(text) > text_budget:
                text = text[:text_budget].rstrip() + " …"
            lines.append("")
            lines.append("Visible text now:")
            lines.append(text)
        lines.append("")
        lines.append("Numbered elements (unchanged unless listed above):")
        lines += ["  " + e.render() for e in self.elements]
        return "\n".join(lines)


def build_snapshot(raw: dict, *, console_errors: int = 0, max_elements: int = MAX_ELEMENTS) -> Snapshot:
    """Shape the page-side dict into a Snapshot. Pure; unit-testable without a browser."""
    rows = raw.get("rows") or []
    elements: list[Element] = []
    for i, row in enumerate(rows[:max_elements], start=1):
        rect = row.get("rect") or [0, 0, 0, 0]
        elements.append(Element(
            number=i,
            role=str(row.get("role") or "element"),
            label=str(row.get("label") or ""),
            testid=str(row.get("testid") or ""),
            value=str(row.get("value") or ""),
            rect=tuple(int(v) for v in rect[:4]),  # type: ignore[arg-type]
            flags=tuple(str(f) for f in (row.get("flags") or [])),
        ))
    focused_raw = raw.get("focused")
    try:
        focused = int(focused_raw) if focused_raw not in (None, "") else None
    except (TypeError, ValueError):
        focused = None
    viewport = raw.get("viewport") or [0, 0]
    overflow = raw.get("overflow") or [0, 0]
    structure = json.dumps(
        [(e.role, e.label, e.testid, e.flags) for e in elements] + [raw.get("url", ""), list(raw.get("dialogs") or [])],
        sort_keys=True,
    )
    digest = hashlib.sha1(structure.encode("utf-8")).hexdigest()[:12]
    return Snapshot(
        url=str(raw.get("url") or ""),
        title=str(raw.get("title") or ""),
        viewport=(int(viewport[0]), int(viewport[1])),
        overflow=(int(overflow[0]), int(overflow[1])),
        scroll_y=int(raw.get("scrollY") or 0),
        dialogs=tuple(str(d) for d in (raw.get("dialogs") or [])),
        focused=focused,
        canvases=int(raw.get("canvases") or 0),
        text=str(raw.get("text") or ""),
        elements=tuple(elements),
        truncated_elements=max(0, len(rows) - max_elements),
        console_errors=console_errors,
        digest=digest,
    )


def similar(a: Snapshot, b: Snapshot, *, threshold: float = 0.7) -> bool:
    """True when most interactive elements are shared, so a diff render is enough."""
    if a.url != b.url:
        return False
    ka = {(e.role, e.label, e.testid) for e in a.elements}
    kb = {(e.role, e.label, e.testid) for e in b.elements}
    if not ka and not kb:
        return True
    return len(ka & kb) / max(1, len(ka | kb)) >= threshold
