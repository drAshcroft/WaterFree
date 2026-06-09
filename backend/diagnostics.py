"""
Runtime dependency self-check for the packaged `waterfree` executable.

Why this exists
---------------
The PyInstaller build (see ``waterfree.spec``) collects third-party packages
through ``collect_all`` and a hidden-imports list. When a package is missing
from the *build* environment, PyInstaller does not fail — it silently omits it,
producing an executable that imports fine but degrades at runtime (the indexer
falls back to regex, graphify dies with ``No module named 'networkx'``, etc.).

``waterfree doctor`` makes that failure loud and catchable. It imports every
dependency the runtime actually needs and, for tree-sitter, builds a real
``Language`` object (so an ABI/binding mismatch is caught, not just a missing
module). It exits non-zero if any REQUIRED dependency is unavailable.

This is invoked:
  * by ``build_installer.ps1`` against the freshly built exe — a degraded
    bundle fails the build instead of shipping, and
  * by the MSI installer helper as a smoke test — a degraded runtime fails the
    install with a clear message instead of silently "working".

Keep the lists below in sync with ``waterfree.spec`` (``_ts_pkgs`` and
``runtime_hiddenimports``) and ``backend/requirements.txt``.
"""

from __future__ import annotations

import importlib
import json
import sys

# Non-grammar packages the runtime cannot function without. These mirror
# backend/requirements.txt plus the providers that ship by default.
_REQUIRED_IMPORTS: tuple[str, ...] = (
    # graphify graph engine
    "networkx",
    "networkx.algorithms",
    "datasketch",
    "rapidfuzz",
    # indexer / gitignore handling
    "pathspec",
    "pathspec.patterns.gitwildmatch",
    # provider + runtime stack
    "pydantic",
    "anthropic",
    "langchain_core",
    "langchain_anthropic",
    "langchain_openai",
    "langchain_ollama",
    "deepagents",
)

# Providers that are wired in the spec but not in requirements.txt — absence is
# acceptable (the user simply hasn't opted into that provider).
_OPTIONAL_IMPORTS: tuple[str, ...] = (
    "langchain_google_genai",
    "langchain_groq",
    "huggingface_hub",
)

# tree-sitter grammar modules that must be bundled (mirrors waterfree.spec
# _ts_pkgs / requirements.txt). A missing grammar silently drops the indexer to
# its regex fallback, so all declared grammars are REQUIRED.
_REQUIRED_GRAMMARS: tuple[str, ...] = (
    "tree_sitter_python",
    "tree_sitter_typescript",
    "tree_sitter_javascript",
    "tree_sitter_c_sharp",
    "tree_sitter_go",
    "tree_sitter_rust",
    "tree_sitter_java",
    "tree_sitter_groovy",
    "tree_sitter_c",
    "tree_sitter_cpp",
    "tree_sitter_ruby",
    "tree_sitter_kotlin",
    "tree_sitter_scala",
    "tree_sitter_php",
    "tree_sitter_swift",
    "tree_sitter_lua",
    "tree_sitter_zig",
    "tree_sitter_powershell",
    "tree_sitter_elixir",
    "tree_sitter_objc",
    "tree_sitter_julia",
    "tree_sitter_bash",
    "tree_sitter_json",
)

# The grammars the built-in indexer loads as Language objects on import. A
# functional build of these is the difference between tree-sitter parsing and
# the regex fallback, so we exercise them harder than a bare import.
#   grammar module -> attribute that returns the grammar pointer
_CORE_LANGUAGE_FACTORIES: tuple[tuple[str, str], ...] = (
    ("tree_sitter_python", "language"),
    ("tree_sitter_typescript", "language_typescript"),
    ("tree_sitter_typescript", "language_tsx"),
    ("tree_sitter_javascript", "language"),
    ("tree_sitter_c_sharp", "language"),
)


def _probe_import(module: str) -> str | None:
    """Return None on success, or a short error string on failure."""
    try:
        importlib.import_module(module)
        return None
    except Exception as exc:  # noqa: BLE001 — report any failure, not just ImportError
        return f"{type(exc).__name__}: {exc}"


def _probe_core_languages() -> dict[str, str]:
    """Build real tree-sitter Language objects; return {label: error} for failures."""
    failures: dict[str, str] = {}
    try:
        from tree_sitter import Language
    except Exception as exc:  # noqa: BLE001
        return {"tree_sitter": f"{type(exc).__name__}: {exc}"}

    for module_name, factory in _CORE_LANGUAGE_FACTORIES:
        label = f"{module_name}.{factory}()"
        try:
            module = importlib.import_module(module_name)
            Language(getattr(module, factory)())
        except Exception as exc:  # noqa: BLE001
            failures[label] = f"{type(exc).__name__}: {exc}"
    return failures


def run_doctor(argv: list[str] | None = None) -> int:
    """
    Probe all runtime dependencies. Print a JSON report to stdout.

    Exit codes:
        0 — all required dependencies present and functional
        4 — one or more required dependencies missing/broken
    """
    json_only = bool(argv) and "--json" in argv

    required_failures: dict[str, str] = {}
    optional_failures: dict[str, str] = {}

    for module in _REQUIRED_IMPORTS:
        err = _probe_import(module)
        if err:
            required_failures[module] = err

    for module in _REQUIRED_GRAMMARS:
        err = _probe_import(module)
        if err:
            required_failures[module] = err

    # Functional tree-sitter check (catches binding/ABI breakage).
    required_failures.update(_probe_core_languages())

    for module in _OPTIONAL_IMPORTS:
        err = _probe_import(module)
        if err:
            optional_failures[module] = err

    ok = not required_failures
    report = {
        "ok": ok,
        "python": sys.version.split()[0],
        "frozen": getattr(sys, "frozen", False),
        "required_checked": len(_REQUIRED_IMPORTS) + len(_REQUIRED_GRAMMARS) + len(_CORE_LANGUAGE_FACTORIES),
        "required_failures": required_failures,
        "optional_failures": optional_failures,
    }

    print(json.dumps(report, indent=2))

    if not json_only and not ok:
        print(
            "\nERROR: this waterfree runtime is missing required dependencies.\n"
            "The build environment did not have every package from "
            "backend/requirements.txt installed when PyInstaller ran.\n"
            "Re-run: pip install -r backend/requirements.txt  (in the build env), "
            "then rebuild the installer.",
            file=sys.stderr,
        )

    return 0 if ok else 4
