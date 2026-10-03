"""Large-tier vision supplies labelled candidate points for canvas controls."""
from dataclasses import dataclass, field
import re

from backend.llm import ollama_client
from backend.vision.models import ensure_available, get_purpose, resolve_model


@dataclass
class CanvasRegions:
    description: str = ""
    regions: list[dict] = field(default_factory=list)
    model: str = ""
    error: str = ""

    def render(self):
        lines = [self.description or self.error or "No candidate controls were identified."]
        lines += [f"CLICK AT {r['x']:g} {r['y']:g} — {r['label']}" for r in self.regions]
        return "\n".join(lines)


def parse_regions(answer: str, viewport: tuple[int, int], *, model="") -> CanvasRegions:
    regions = []
    description = []
    for line in answer.splitlines():
        # Models sometimes put several valid REGION clauses in one paragraph.
        # Extract only the coordinates explicitly reported in those clauses.
        matches = list(re.finditer(
            r'\bREGION\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(.+?)'
            r'(?=\s*[,;]?\s*REGION\s+\d|\s*$)', line, re.I))
        # Some vision models return labelled centers despite the requested
        # grammar. Preserve those observed coordinates rather than guessing.
        if not matches:
            center = re.match(r'^\s*\d+[.)]\s*(.+?):\s*Center(?:ed)?(?:\s+at)?(?:\s+approximately)?\s*\(?(\d+(?:\.\d+)?)[,\s]+(\d+(?:\.\d+)?)', line, re.I)
            if center:
                line = f'REGION {center[2]} {center[3]} {center[1]}'
                matches = list(re.finditer(r'REGION\s+(\d+(?:\.\d+)?)\s+(\d+(?:\.\d+)?)\s+(.+)', line))
        for match in matches:
            x, y = float(match[1]), float(match[2])
            label = match[3].strip().rstrip(',;').strip().strip('"')[:100]
            if 0 <= x < viewport[0] and 0 <= y < viewport[1] and label and len(regions) < 8:
                regions.append({"x": x, "y": y, "label": label})
        if matches and matches[0].start() > 0 and len(description) < 4:
            prefix = line[:matches[0].start()].strip().strip('-*')
            if prefix:
                description.append(prefix[:200])
        elif not matches and line.strip() and len(description) < 4:
            description.append(line.strip()[:200])
    return CanvasRegions(" ".join(description), regions, model)


def describe_regions(screenshot_b64: str, viewport: tuple[int, int], *, base="", goal="") -> CanvasRegions:
    if not screenshot_b64:
        return CanvasRegions(error="Screenshot unavailable; cannot identify canvas controls.")
    try:
        model_id = resolve_model(get_purpose("ui"), tier="large")
        ensure_available(model_id, base=base)
        answer = ollama_client.chat(model=model_id, images=[screenshot_b64],
            messages=[{"role": "system", "content":
                "Inspect the screenshot, not HTML. Briefly describe the screen and identify up to eight visible clickable controls. "
                "Report each control's center in viewport pixels using exactly: REGION x y label. "
                "Do not invent invisible controls or include normalized coordinates."},
                {"role": "user", "content": f"Viewport: {viewport[0]}x{viewport[1]}. Goal: {goal}. Describe the screen in one sentence, then list visible controls using ONLY REGION x y label lines. Example format (not actual coordinates): REGION 120 240 Start button. Do not number the list. Coordinates must be centers in viewport pixels."}],
            timeout=180, keep_alive="0", options={"temperature": 0, "num_predict": 400, "num_ctx": 4096},
            **({"base": base} if base else {}))
        return parse_regions(answer, viewport, model=model_id)
    except Exception as exc:
        return CanvasRegions(error=f"Canvas vision unavailable: {exc}")
