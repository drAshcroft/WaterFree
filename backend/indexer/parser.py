"""
Tree-sitter based source file parser.
Extracts symbols (functions, classes, imports) from source files.
Uses tree-sitter-languages for pre-built grammars — no WASM required.
"""

from __future__ import annotations
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

try:
    from tree_sitter import Language, Parser as TSParser
    import tree_sitter_python as _ts_python
    import tree_sitter_typescript as _ts_typescript
    import tree_sitter_javascript as _ts_javascript

    _LANGUAGE_OBJECTS: dict[str, Language] = {
        "python": Language(_ts_python.language()),
        "typescript": Language(_ts_typescript.language_typescript()),
        "tsx": Language(_ts_typescript.language_tsx()),
        "javascript": Language(_ts_javascript.language()),
    }
    TREE_SITTER_AVAILABLE = True
except ImportError:
    TREE_SITTER_AVAILABLE = False
    _LANGUAGE_OBJECTS = {}

log = logging.getLogger(__name__)

# Map file extension → tree-sitter language name
EXTENSION_LANGUAGE: dict[str, str] = {
    ".ts": "typescript",
    ".tsx": "tsx",
    ".js": "javascript",
    ".jsx": "javascript",
    ".py": "python",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".cs": "c_sharp",
    ".java": "java",
    ".cpp": "cpp",
    ".c": "c",
}

SUPPORTED_EXTENSIONS = set(EXTENSION_LANGUAGE.keys())


@dataclass
class Symbol:
    name: str
    kind: str           # "function", "class", "method", "import"
    file: str
    line: int           # 1-based
    end_line: int
    body_snippet: str   # first ~3 lines of body for context
    docstring: str = ""
    parent: Optional[str] = None  # for methods: the containing class name


@dataclass
class ParsedFile:
    path: str
    language: str
    symbols: list[Symbol] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def function_names(self) -> list[str]:
        return [s.name for s in self.symbols if s.kind in ("function", "method")]

    def class_names(self) -> list[str]:
        return [s.name for s in self.symbols if s.kind == "class"]


def language_for_file(path: str) -> Optional[str]:
    ext = Path(path).suffix.lower()
    return EXTENSION_LANGUAGE.get(ext)


def parse_file(path: str) -> ParsedFile:
    """Parse a single source file. Returns ParsedFile (with error set if parsing failed)."""
    lang_name = language_for_file(path)
    if not lang_name:
        return ParsedFile(path=path, language="unknown", error="unsupported extension")

    if not TREE_SITTER_AVAILABLE:
        return _fallback_parse(path, lang_name)

    try:
        source = Path(path).read_bytes()
    except OSError as e:
        return ParsedFile(path=path, language=lang_name, error=str(e))

    lang_obj = _LANGUAGE_OBJECTS.get(lang_name)
    if not lang_obj:
        return _fallback_parse(path, lang_name)

    try:
        parser = TSParser(lang_obj)
        tree = parser.parse(source)
        source_str = source.decode("utf-8", errors="replace")
        symbols, imports = _extract_symbols(tree.root_node, source_str, path, lang_name)
        return ParsedFile(path=path, language=lang_name, symbols=symbols, imports=imports)
    except Exception as e:
        log.warning("tree-sitter parse failed for %s: %s", path, e)
        return ParsedFile(path=path, language=lang_name, error=str(e))


# ---------------------------------------------------------------------------
# Symbol extraction helpers
# ---------------------------------------------------------------------------

def _extract_symbols(
    root_node, source: str, file_path: str, lang: str
) -> tuple[list[Symbol], list[str]]:
    symbols: list[Symbol] = []
    imports: list[str] = []
    lines = source.splitlines()

    def node_text(node) -> str:
        return source[node.start_byte:node.end_byte]

    def snippet(node, max_lines: int = 3) -> str:
        start = node.start_point[0]
        end = min(start + max_lines, len(lines))
        return "\n".join(lines[start:end])

    def walk(node, parent_class: Optional[str] = None):
        t = node.type

        # --- Functions / methods ---
        if t in ("function_declaration", "function_definition",
                  "method_definition", "arrow_function",
                  "function_item",  # Rust
                  "func_literal",   # Go
                  ):
            name = _child_text(node, ("identifier", "property_identifier", "name"), source)
            if name:
                kind = "method" if parent_class else "function"
                sym = Symbol(
                    name=name,
                    kind=kind,
                    file=file_path,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    body_snippet=snippet(node),
                    parent=parent_class,
                )
                symbols.append(sym)
                # Don't recurse into function bodies for top-level scan
                return

        # --- Classes ---
        if t in ("class_declaration", "class_definition",
                  "interface_declaration", "struct_item", "impl_item"):
            name = _child_text(node, ("identifier", "type_identifier", "name"), source)
            if name:
                sym = Symbol(
                    name=name,
                    kind="class",
                    file=file_path,
                    line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    body_snippet=snippet(node),
                )
                symbols.append(sym)
                for child in node.children:
                    walk(child, parent_class=name)
                return

        # --- Imports ---
        if t in ("import_statement", "import_from_statement",
                  "use_declaration",  # Rust
                  "import_declaration",  # Go
                  ):
            imports.append(node_text(node).strip())

        for child in node.children:
            walk(child, parent_class=parent_class)

    walk(root_node)
    return symbols, imports


def _child_text(node, type_names: tuple, source: str) -> Optional[str]:
    """Return text of the first child whose type is in type_names."""
    for child in node.children:
        if child.type in type_names:
            return source[child.start_byte:child.end_byte].strip()
    return None


# ---------------------------------------------------------------------------
# Fallback: regex-based extraction when tree-sitter isn't available
# ---------------------------------------------------------------------------

import re

_PYTHON_FUNC = re.compile(r"^(\s*)def\s+(\w+)\s*\(", re.MULTILINE)
_PYTHON_CLASS = re.compile(r"^(\s*)class\s+(\w+)[\s:(]", re.MULTILINE)
_TS_FUNC = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[\(<]|"
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?\(",
    re.MULTILINE,
)
_TS_CLASS = re.compile(r"(?:export\s+)?class\s+(\w+)", re.MULTILINE)
_IMPORT_TS = re.compile(r"^import\s+.+?from\s+['\"].+?['\"]", re.MULTILINE)
_IMPORT_PY = re.compile(r"^(?:import|from)\s+\S+", re.MULTILINE)


def _fallback_parse(path: str, lang: str) -> ParsedFile:
    try:
        source = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return ParsedFile(path=path, language=lang, error=str(e))

    symbols: list[Symbol] = []
    imports: list[str] = []
    lines = source.splitlines()

    def line_of(match) -> int:
        return source[: match.start()].count("\n") + 1

    if lang == "python":
        for m in _PYTHON_CLASS.finditer(source):
            ln = line_of(m)
            symbols.append(Symbol(
                name=m.group(2), kind="class", file=path,
                line=ln, end_line=ln,
                body_snippet="\n".join(lines[ln - 1: ln + 2]),
            ))
        for m in _PYTHON_FUNC.finditer(source):
            ln = line_of(m)
            symbols.append(Symbol(
                name=m.group(2), kind="function", file=path,
                line=ln, end_line=ln,
                body_snippet="\n".join(lines[ln - 1: ln + 2]),
            ))
        imports = _IMPORT_PY.findall(source)
    else:
        for m in _TS_CLASS.finditer(source):
            ln = line_of(m)
            symbols.append(Symbol(
                name=m.group(1), kind="class", file=path,
                line=ln, end_line=ln,
                body_snippet="\n".join(lines[ln - 1: ln + 2]),
            ))
        for m in _TS_FUNC.finditer(source):
            name = m.group(1) or m.group(2)
            if name:
                ln = line_of(m)
                symbols.append(Symbol(
                    name=name, kind="function", file=path,
                    line=ln, end_line=ln,
                    body_snippet="\n".join(lines[ln - 1: ln + 2]),
                ))
        imports = _IMPORT_TS.findall(source)

    return ParsedFile(path=path, language=lang, symbols=symbols, imports=imports)
