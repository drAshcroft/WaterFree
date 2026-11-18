"""`waterfree vision ...` — local vision understanding of images and screenshots."""

from __future__ import annotations

from argparse import ArgumentParser, Namespace, _SubParsersAction

from backend.cli._common import (
    EXIT_DEP_MISSING,
    EXIT_OK,
    EXIT_USAGE,
    add_workspace_arg,
    emit_error,
    emit_json,
)
from backend.llm import ollama_client
from backend.vision.analyze import DEFAULT_TIMEOUT_SECONDS, VisionError, analyze
from backend.vision.models import (
    DEFAULT_PURPOSE,
    PURPOSES,
    REGISTRY,
    TIERS,
    VisionModelMissing,
    installed_report,
)


def register(sub: _SubParsersAction) -> None:
    p = sub.add_parser("vision", help="Understand images with a local vision model")
    actions = p.add_subparsers(dest="action", metavar="<action>")
    actions.required = True

    p_look = actions.add_parser("look", help="Analyze one or more images")
    p_look.add_argument("images", nargs="+", help="Image file path(s)")
    _add_analysis_args(p_look)
    add_workspace_arg(p_look)

    p_web = actions.add_parser("web", help="Capture a URL with Playwright and analyze it locally")
    p_web.add_argument("url", help="HTTP(S) website URL")
    _add_analysis_args(p_web)
    p_web.add_argument("--mobile", action="store_true", help="Also capture and analyze a 390px mobile viewport")
    p_web.add_argument("--ready-selector", default="", help="Wait until this CSS selector is visible (useful for SPAs)")
    p_web.add_argument("--wait-ms", type=int, default=750, help="Settle time after DOM/selector readiness")
    p_web.add_argument(
        "--timeout-ms", type=int, default=30000,
        help="Page-load limit in milliseconds (the model call is bounded by --timeout).",
    )
    p_web.add_argument("--output-dir", default="")
    p_web.add_argument("--full-page", action="store_true", help="Analyze the full-page capture instead of the viewport")
    p_web.add_argument("--capture-only", action="store_true", help="Save screenshots without invoking a vision model")
    add_workspace_arg(p_web)

    p_models = actions.add_parser("models", help="Show the vision tiers and what is downloaded")
    add_workspace_arg(p_models)

    p_purposes = actions.add_parser("purposes", help="List the available purposes")
    add_workspace_arg(p_purposes)

    p_pull = actions.add_parser("pull", help="Download a vision model")
    p_pull.add_argument(
        "--tier",
        choices=sorted(TIERS),
        default="",
        help="Which tier to download. Omit when using --model.",
    )
    p_pull.add_argument("--model", default="", help="Download a specific Ollama model id.")
    add_workspace_arg(p_pull)

    p.set_defaults(_runner=run)


def _add_analysis_args(parser: ArgumentParser) -> None:
    """Flags shared by every action that runs the vision model (look, web)."""
    parser.add_argument(
        "--purpose",
        choices=sorted(PURPOSES),
        default=DEFAULT_PURPOSE,
        help=(
            "What you want out of the image. Selects the model tier and the "
            "framing of the question. Default: %(default)s."
        ),
    )
    parser.add_argument(
        "-q", "--question",
        default="",
        help="Ask something specific instead of the purpose's default question.",
    )
    parser.add_argument("--model", default="", help="Force a specific Ollama vision model.")
    parser.add_argument(
        "--tier",
        choices=sorted(TIERS),
        default="",
        help="Force a model tier, overriding the purpose's choice.",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        metavar="SECONDS",
        help=(
            "Seconds to wait for the model on each attempt (default %(default)g, "
            "or $WATERFREE_VISION_TIMEOUT). Raise it on a busy host."
        ),
    )
    parser.add_argument(
        "--labels",
        default=None,
        metavar="A,B,C",
        help=(
            "Comma-separated labels for --purpose classify (2-20). 'none' and "
            "'uncertain' are always added; the order is shuffled per call."
        ),
    )


def run(args: Namespace) -> int:
    action = args.action

    if action == "models":
        report = installed_report()
        payload: dict = {"tiers": report}
        if any(row["installed"] is None for row in report):
            payload["warning"] = (
                "Ollama did not say which models are downloaded (not running, or too "
                "busy to answer), so `installed` is unknown. Check with `ollama list`."
            )
        emit_json(payload)
        return EXIT_OK

    if action == "purposes":
        emit_json({
            "default": DEFAULT_PURPOSE,
            "purposes": [
                {
                    "purpose": p.key,
                    "tier": p.tier,
                    "model": REGISTRY[p.tier].model_id,
                    "summary": p.summary,
                }
                for p in sorted(PURPOSES.values(), key=lambda x: (x.tier, x.key))
            ],
        })
        return EXIT_OK

    if action == "pull":
        return _pull(args)

    if action == "look":
        return _look(args)
    if action == "web":
        return _web(args)

    return emit_error(f"unknown action: {action}", exit_code=EXIT_USAGE)


def _web(args: Namespace) -> int:
    from backend.vision.web import analyze_web
    try:
        result = analyze_web(
            args.url, purpose=args.purpose, question=args.question, model=args.model, tier=args.tier,
            workspace_path=args.workspace, output_dir=args.output_dir, mobile=args.mobile,
            ready_selector=args.ready_selector, wait_ms=args.wait_ms, timeout_ms=args.timeout_ms,
            full_page=args.full_page, capture_only=args.capture_only, timeout=args.timeout,
            labels=args.labels,
        )
    except (VisionModelMissing, VisionError, ollama_client.OllamaError) as exc:
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)
    emit_json(result)
    return EXIT_OK


def _look(args: Namespace) -> int:
    try:
        result = analyze(
            list(args.images),
            purpose=args.purpose,
            question=args.question,
            model=args.model,
            tier=args.tier,
            timeout=args.timeout,
            labels=args.labels,
        )
    except (VisionModelMissing, ollama_client.OllamaError) as exc:
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except VisionError as exc:
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)
    except ValueError as exc:
        return emit_error(str(exc), exit_code=EXIT_USAGE)

    emit_json(result)
    return EXIT_OK


def _pull(args: Namespace) -> int:
    model_id = args.model.strip()
    if not model_id:
        tier = args.tier.strip()
        if not tier:
            return emit_error(
                "Supply --tier or --model. See `waterfree vision models`.",
                exit_code=EXIT_USAGE,
            )
        model_id = REGISTRY[tier].model_id

    if ollama_client.has_model(model_id):
        emit_json({"model": model_id, "pulled": False, "message": "Already downloaded."})
        return EXIT_OK

    try:
        ollama_client.pull(model_id)
    except ollama_client.OllamaError as exc:
        return emit_error(str(exc), exit_code=EXIT_DEP_MISSING)

    emit_json({"model": model_id, "pulled": True})
    return EXIT_OK
