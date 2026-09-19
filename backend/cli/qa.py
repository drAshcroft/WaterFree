"""`waterfree qa ...` — drive a running web app the way a careless user would."""

from __future__ import annotations

import re
import sys
from argparse import Namespace, _SubParsersAction
from datetime import date
from pathlib import Path

from backend.cli._common import (
    EXIT_DEP_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    add_workspace_arg,
    emit_error,
    emit_json,
    resolve_workspace,
)
from backend.qa import hints as hints_mod
from backend.qa import models as driver_models
from backend.qa import personas as personas_mod
from backend.qa.findings import slug_from_url
from backend.qa.prompts import RunBrief

# Only http(s), and only a URL we were given whole. Never assembled from parts:
# building a URL out of shell arguments is how a QA run ends up hammering
# something that was not the app under test.
_URL_RE = re.compile(r"^https?://[^\s]+$", re.I)

DEFAULT_OUT_REL = "qa-runs"
REPORT_OUT_REL = "qa-reports"


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("qa", help="Exercise a running web app with a local model")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_run = actions.add_parser("run", help="Run one or more personas against a URL")
    p_run.add_argument("url", help="The running app, e.g. http://localhost:5173")
    p_run.add_argument("--goal", default="", help="What the tester should try to achieve.")
    p_run.add_argument(
        "--hint", action="append", default=[],
        help=("Repeatable. Free text, or `genre:<name>` to pull a matching "
              "playbook from the knowledge base."),
    )
    p_run.add_argument(
        "--persona", action="append", default=[],
        help="Repeatable. Defaults to first-timer. See `waterfree qa personas`.",
    )
    p_run.add_argument("--steps", type=int, default=None, help="Step cap per persona.")
    p_run.add_argument("--minutes", type=int, default=None, help="Wall-clock cap per persona.")
    p_run.add_argument("--viewport", default="", help="Override the persona's viewport, e.g. 1440x900.")
    p_run.add_argument("--headed", action="store_true", help="Show the browser window.")
    p_run.add_argument("--out", default="", help=f"Run directory root. Default: <workspace>/{DEFAULT_OUT_REL}")
    p_run.add_argument("--model", default="", help="Force a specific Ollama driver model.")
    p_run.add_argument(
        "--vision-every", type=int, default=None,
        help="Check the screenshot against the DOM every N steps. 0 disables.",
    )
    p_run.add_argument("--no-vision", action="store_true", help="Disable vision checks entirely.")
    add_workspace_arg(p_run)

    p_personas = actions.add_parser("personas", help="List built-in and workspace personas")
    add_workspace_arg(p_personas)

    p_verify = actions.add_parser("verify", help="Replay each finding's steps")
    p_verify.add_argument("run_dir", help="A run directory, or a parent holding several.")
    p_verify.add_argument("--headed", action="store_true", help="Show the browser window.")
    add_workspace_arg(p_verify)

    p_report = actions.add_parser("report", help="Consolidated markdown across runs")
    p_report.add_argument("run_dirs", nargs="+", help="Run directories, or parents holding them.")
    p_report.add_argument("--out", default="", help="Write the markdown here instead of stdout.")
    add_workspace_arg(p_report)

    p_doctor = actions.add_parser("doctor", help="Check Playwright, browsers and models")
    add_workspace_arg(p_doctor)

    p.set_defaults(_runner=run)


def run(args: Namespace) -> int:
    action = args.action
    workspace = resolve_workspace(args)

    if action == "personas":
        return _personas(workspace)
    if action == "doctor":
        return _doctor(workspace)
    if action == "report":
        return _report(args, workspace)
    if action == "verify":
        return _verify(args)
    if action == "run":
        return _run(args, workspace)
    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)


# ---------------------------------------------------------------------------
# personas / doctor
# ---------------------------------------------------------------------------


def _personas(workspace: str) -> int:
    registry = personas_mod.registry(workspace)
    emit_json([registry[name].as_dict() for name in sorted(registry)])
    return EXIT_OK


def _doctor(workspace: str) -> int:
    """Report what is present without installing or downloading anything."""
    report: dict = {"ok": True, "checks": [], "fixes": []}

    def check(name: str, ok: bool, detail: str, fix: str = "") -> None:
        report["checks"].append({"name": name, "ok": ok, "detail": detail})
        if not ok:
            report["ok"] = False
            if fix:
                report["fixes"].append(fix)

    try:
        import playwright  # noqa: F401, PLC0415

        check("playwright", True, "the python package is importable")
        playwright_ok = True
    except ImportError as exc:
        check("playwright", False, str(exc), "pip install playwright")
        playwright_ok = False

    if playwright_ok:
        try:
            from playwright.sync_api import sync_playwright  # noqa: PLC0415

            with sync_playwright() as pw:
                browser = pw.chromium.launch(headless=True, args=["--mute-audio"])
                version = browser.version
                browser.close()
            check("chromium", True, f"launches ({version})")
        except Exception as exc:
            check("chromium", False, _first_line(exc), "python -m playwright install chromium")

    for entry in driver_models.installed_report():
        check(
            f"driver model ({entry['tier']})",
            bool(entry["downloaded"]),
            f"{entry['model']}: {'present' if entry['downloaded'] else 'not pulled'}",
            f"ollama pull {entry['model']}",
        )

    try:
        from backend.vision.models import installed_report as vision_report  # noqa: PLC0415

        for entry in vision_report():
            if entry["tier"] != "large":
                continue
            # `installed`, not `downloaded`: the vision report and the driver
            # report spell this differently, and reading the wrong key here
            # reported a present model as missing.
            present = bool(entry["installed"])
            check(
                "vision model (large)",
                present,
                f"{entry['model']}: {'present' if present else 'not pulled'}",
                f"ollama pull {entry['model']} (or run with --no-vision)",
            )
    except Exception as exc:  # noqa: BLE001 - doctor never fails on a check
        check("vision model (large)", False, _first_line(exc), "")

    personas = personas_mod.registry(workspace)
    check("personas", bool(personas), f"{len(personas)} available: {', '.join(sorted(personas))}")

    emit_json(report)
    return EXIT_OK if report["ok"] else EXIT_DEP_MISSING


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _run(args: Namespace, workspace: str) -> int:
    url = (args.url or "").strip()
    if not _URL_RE.match(url):
        return emit_error(
            f"'{url}' is not an http(s) URL. Start the dev server yourself and "
            "pass the address it printed, e.g. http://localhost:5173",
            exit_code=EXIT_USAGE,
        )

    goal = (args.goal or "").strip()
    if not goal:
        return emit_error(
            "--goal is required: the tester needs something to try. "
            'For example --goal "start a new game and finish the first turn"',
            exit_code=EXIT_USAGE,
        )

    try:
        chosen = personas_mod.resolve(args.persona, workspace)
    except personas_mod.PersonaNotFound as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)

    viewport = _parse_viewport(args.viewport)
    if args.viewport and viewport is None:
        return emit_error(f"--viewport '{args.viewport}' should look like 1440x900", exit_code=EXIT_USAGE)

    # Imported here, not at module scope: `qa personas` and `qa report` must
    # work without Playwright installed.
    from backend.qa.browser import BrowserUnavailable  # noqa: PLC0415
    from backend.qa.driver import Caps, run_persona  # noqa: PLC0415

    plain_hints, genres = hints_mod.split_hints(args.hint or [])
    brief = RunBrief(
        goal=goal,
        hints=plain_hints,
        qa_md=hints_mod.load_qa_md(workspace),
        playbook=hints_mod.load_playbooks(genres),
    )

    caps = Caps()
    if args.steps is not None:
        caps.steps = max(1, args.steps)
    if args.minutes is not None:
        caps.minutes = max(1, args.minutes)
    if args.no_vision:
        caps.vision_every = 0
    elif args.vision_every is not None:
        caps.vision_every = max(0, args.vision_every)

    model = (args.model or "").strip()
    if model:
        resolved = model
    else:
        resolved = driver_models.resolve_model(tier=driver_models.DEFAULT)
    try:
        driver_models.ensure_available(resolved)
    except driver_models.DriverModelMissing as exc:
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)

    root = Path(args.out) if args.out else Path(workspace) / DEFAULT_OUT_REL
    sweep_dir = root / f"{slug_from_url(url)}-{date.today().isoformat()}"

    results: list[dict] = []
    for index, persona in enumerate(chosen, start=1):
        if viewport:
            persona = _with_viewport(persona, viewport)
        run_dir = sweep_dir / f"{persona.name}-{index}"
        sys.stderr.write(f"qa: {persona.name} -> {run_dir}\n")
        try:
            outcome = run_persona(
                url, persona, brief, run_dir,
                caps=caps, model=resolved, headed=args.headed,
            )
        except BrowserUnavailable as exc:
            return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
        results.append({
            "persona": persona.name,
            "dir": str(run_dir),
            **outcome.as_dict(),
        })

    emit_json({
        "url": url,
        "goal": goal,
        "sweepDir": str(sweep_dir),
        "model": resolved,
        "runs": results,
        "findings": _severity_totals(results),
    })
    # A finding is not a failure of the command: the run did what it was asked.
    # Exit codes stay for "the harness could not run".
    return EXIT_OK


def _severity_totals(results: list[dict]) -> dict:
    totals: dict = {}
    for item in results:
        for finding in item.get("findings", []):
            severity = finding.get("severity", "note")
            totals[severity] = totals.get(severity, 0) + 1
    return totals


# ---------------------------------------------------------------------------
# verify / report
# ---------------------------------------------------------------------------


def _verify(args: Namespace) -> int:
    from backend.qa.browser import BrowserUnavailable  # noqa: PLC0415
    from backend.qa.report import find_run_dirs  # noqa: PLC0415
    from backend.qa.verify import apply_statuses, summarize, verify_run  # noqa: PLC0415

    run_dirs = find_run_dirs([args.run_dir])
    if not run_dirs:
        return emit_error(f"no run directory with a run.json under {args.run_dir}", exit_code=EXIT_USAGE)

    payload = []
    for run_dir in run_dirs:
        try:
            results = verify_run(run_dir, headed=args.headed)
        except BrowserUnavailable as exc:
            return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
        if results:
            apply_statuses(run_dir, results)
        payload.append({
            "dir": str(run_dir),
            "summary": summarize(results),
            "results": [r.as_dict() for r in results],
        })

    emit_json({"runs": payload})
    return EXIT_OK


def _report(args: Namespace, workspace: str) -> int:
    from backend.qa.report import find_run_dirs, render  # noqa: PLC0415

    run_dirs = find_run_dirs(args.run_dirs)
    if not run_dirs:
        return emit_error(
            f"no run directories with a run.json under: {', '.join(args.run_dirs)}",
            exit_code=EXIT_USAGE,
        )

    markdown = render(run_dirs)
    if args.out:
        out_path = Path(args.out)
    else:
        out_path = Path(workspace) / REPORT_OUT_REL / f"{run_dirs[0].parent.name}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(markdown, encoding="utf-8")

    emit_json({"report": str(out_path), "runs": [str(p) for p in run_dirs]})
    return EXIT_OK


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _parse_viewport(raw: str) -> tuple[int, int] | None:
    if not raw.strip():
        return None
    match = re.match(r"^\s*(\d+)\s*[xX]\s*(\d+)\s*$", raw)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _with_viewport(persona, viewport: tuple[int, int]):
    from dataclasses import replace  # noqa: PLC0415

    return replace(persona, viewport=viewport)


def _first_line(exc: Exception) -> str:
    for line in str(exc).splitlines():
        if line.strip():
            return line.strip()[:200]
    return str(exc)[:200]
