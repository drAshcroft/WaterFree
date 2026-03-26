"""
Interactive CLI for the WaterFree Tutorializer.

Usage:
  python -m backend.tutorializer <repo_path> [options]

Options:
  --model MODEL     Ollama model to use (prompted if omitted)
  --focus TEXT      What you want to learn — skips the interactive prompt
  --areas A,B,C     Only generate tutorials for these named areas (comma-separated)
  --base URL        Ollama base URL  (default: http://localhost:11434)
  --timeout SECS    Request timeout per LLM call (default: 180)
  --list-models     List available Ollama models and exit
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from backend.knowledge.store import KnowledgeStore
from backend.tutorializer import ollama as _ollama
from backend.tutorializer.generator import TutorialGenerator


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def _choose_model(base: str) -> str:
    """List available Ollama models and let the user pick one interactively."""
    models = _ollama.list_models(base)
    if not models:
        print(
            "\nNo models found in Ollama.\n"
            "Pull a model first, e.g.:  ollama pull llama3.2\n"
            "                           ollama pull mistral\n"
            "                           ollama pull codellama"
        )
        sys.exit(1)

    if len(models) == 1:
        print(f"Using model: {models[0]}")
        return models[0]

    print("\nAvailable Ollama models:")
    for i, m in enumerate(models, 1):
        print(f"  {i}. {m}")

    while True:
        raw = input(f"\nChoose model [1-{len(models)}] (Enter for 1): ").strip()
        if not raw:
            print(f"Using model: {models[0]}")
            return models[0]
        try:
            idx = int(raw) - 1
            if 0 <= idx < len(models):
                print(f"Using model: {models[idx]}")
                return models[idx]
        except ValueError:
            pass
        print(f"  Please enter a number between 1 and {len(models)}.")


def _get_focus(args_focus: str | None) -> str:
    """Return the user's focus — from CLI arg or interactive prompt."""
    if args_focus is not None:
        return args_focus.strip()

    print()
    print("What are you most interested in learning from this repo?")
    print("  Examples: 'authentication and security'")
    print("            'data processing pipeline'")
    print("            'how the API layer is structured'")
    print("  Press Enter for a general overview of all key areas.")
    focus = input("> ").strip()
    return focus


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m backend.tutorializer",
        description="Generate developer tutorials for a repo using a local Ollama model.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "repo",
        nargs="?",
        help="Path to the repository to tutorialise",
    )
    parser.add_argument(
        "--model",
        help="Ollama model name (e.g. llama3.2, mistral, codellama)",
    )
    parser.add_argument(
        "--focus",
        help="What you want to learn — skips the interactive prompt",
    )
    parser.add_argument(
        "--areas",
        help="Comma-separated area names to target (skips Ollama area selection)",
    )
    parser.add_argument(
        "--base",
        default="http://localhost:11434",
        metavar="URL",
        help="Ollama base URL (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=180,
        metavar="SECS",
        help="Per-request timeout in seconds (default: 180)",
    )
    parser.add_argument(
        "--list-models",
        action="store_true",
        help="List available Ollama models and exit",
    )
    args = parser.parse_args()

    # --list-models
    if args.list_models:
        try:
            models = _ollama.list_models(args.base)
        except _ollama.OllamaError as exc:
            print(f"Error: {exc}")
            sys.exit(1)
        if models:
            print("Available Ollama models:")
            for m in models:
                print(f"  {m}")
        else:
            print("No models available.  Run: ollama pull <model>")
        return

    # Require repo path
    if not args.repo:
        parser.print_help()
        sys.exit(1)

    repo_path = Path(args.repo).resolve()
    if not repo_path.is_dir():
        print(f"Error: '{repo_path}' is not a directory.")
        sys.exit(1)

    print("\n=== WaterFree Tutorializer ===")
    print(f"Repo : {repo_path}")

    # Connect to Ollama and pick a model
    try:
        model = args.model or _choose_model(args.base)
    except _ollama.OllamaError as exc:
        print(f"\nOllama error: {exc}")
        sys.exit(1)

    # Gather user focus
    focus = _get_focus(args.focus)
    if focus:
        print(f"\nFocus : {focus}")
    else:
        print("\nNo specific focus — generating a general overview.")

    # Parse --areas override
    areas_override: list[str] | None = None
    if args.areas:
        areas_override = [a.strip() for a in args.areas.split(",") if a.strip()]
        print(f"Areas : {', '.join(areas_override)}")

    print(f"Model : {model}")
    print()

    # Run the pipeline
    store = KnowledgeStore()
    generator = TutorialGenerator(
        repo_path=repo_path,
        model=model,
        store=store,
        progress_cb=lambda msg: print(f"  {msg}"),
        ollama_base=args.base,
        ollama_timeout=args.timeout,
    )

    try:
        count = generator.run(focus=focus, areas_override=areas_override)
    except _ollama.OllamaError as exc:
        print(f"\nOllama error: {exc}")
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nAborted.")
        sys.exit(0)

    repo_name_slug = repo_path.name
    print(f"\n=== Done — {count} tutorial(s) added to the WaterFree knowledge base ===")
    if count > 0:
        print()
        print("To read the tutorials:")
        print(f"  search_knowledge('tutorial {repo_name_slug}')")
        print(f"  browse_knowledge_index('tutorial/{repo_name_slug}')")
        if focus:
            from backend.tutorializer.generator import _slugify
            print(f"  browse_knowledge_index('tutorial/{repo_name_slug}/{_slugify(focus)}')")


if __name__ == "__main__":
    main()
