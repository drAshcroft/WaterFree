"""
Internal codebase indexer — replaces the external codebase-memory-mcp binary.

3-pass pipeline:
  Pass 1 — Structure: scan files, extract nodes (functions, classes, modules)
  Pass 2 — Imports:   build import maps per file (local alias → resolved QN)
  Pass 3 — Relations: resolve CALLS, IMPORTS, DEFINES, INHERITS edges

Qualified names (QN) scheme:
  {project}.{dot.separated.rel.path.without.ext}.{SymbolName}
  e.g. Paradoxia.src.utils.helpers.MyClass.myMethod
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Tree-sitter setup — same grammars as existing parser.py
# ---------------------------------------------------------------------------

try:
    from tree_sitter import Language, Parser as TSParser, Node
    import tree_sitter_python as _ts_python
    import tree_sitter_typescript as _ts_typescript
    import tree_sitter_javascript as _ts_javascript

    _LANGS: dict[str, Language] = {
        "python":     Language(_ts_python.language()),
        "typescript": Language(_ts_typescript.language_typescript()),
        "tsx":        Language(_ts_typescript.language_tsx()),
        "javascript": Language(_ts_javascript.language()),
    }
    _TS_OK = True
except ImportError:
    _LANGS = {}
    _TS_OK = False
    log.warning("tree-sitter not available — indexer will use regex fallback")

_EXT_LANG: dict[str, str] = {
    ".ts":  "typescript",
    ".tsx": "tsx",
    ".js":  "javascript",
    ".jsx": "javascript",
    ".py":  "python",
}

_EXCLUDE_DIRS = {
    "node_modules", ".git", "dist", "build", "__pycache__",
    ".waterfree", ".waterfree", ".venv", "venv", ".mypy_cache",
    ".pytest_cache", "coverage", ".next", "out", ".turbo",
}

MAX_FILE_BYTES = 500_000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

class RawSymbol:
    __slots__ = (
        "label", "name", "qualified_name", "file_path",
        "start_line", "end_line", "body", "properties",
    )

    def __init__(
        self,
        label: str,
        name: str,
        qualified_name: str,
        file_path: str,
        start_line: int,
        end_line: int,
        body: str = "",
        properties: dict | None = None,
    ):
        self.label = label
        self.name = name
        self.qualified_name = qualified_name
        self.file_path = file_path
        self.start_line = start_line
        self.end_line = end_line
        self.body = body
        self.properties = properties or {}


class ParsedFileResult:
    def __init__(self, path: str, lang: str):
        self.path = path
        self.lang = lang
        self.symbols: list[RawSymbol] = []
        # raw import strings
        self.raw_imports: list[str] = []
        # alias → module_path (relative or absolute)
        self.import_map: dict[str, str] = {}
        self.error: Optional[str] = None
        self.sha256: str = ""

    @property
    def module_qn_prefix(self) -> str:
        """Dot-separated path without extension, used as node QN prefix."""
        return self._mod_prefix

    def set_mod_prefix(self, prefix: str) -> None:
        self._mod_prefix = prefix


# ---------------------------------------------------------------------------
# QN helpers
# ---------------------------------------------------------------------------

def _make_qn(project: str, rel_path: str, *parts: str) -> str:
    """Build a qualified name from project + relative file path + symbol parts."""
    p = Path(rel_path)
    # Remove extension and convert separators
    mod = str(p.with_suffix("")).replace("\\", "/").replace("/", ".")
    # Strip leading dots
    mod = mod.lstrip(".")
    base = f"{project}.{mod}" if project else mod
    if parts:
        return base + "." + ".".join(p for p in parts if p)
    return base


def file_hash(path: str) -> str:
    try:
        data = Path(path).read_bytes()
        return hashlib.sha256(data).hexdigest()[:24]
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# Tree-sitter extraction helpers
# ---------------------------------------------------------------------------

def _node_text(node: "Node", source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _child_text(node: "Node", type_names: tuple, source: bytes) -> str:
    for child in node.children:
        if child.type in type_names:
            return _node_text(child, source)
    return ""


def _body_text(node: "Node", source: bytes, max_lines: int = 60) -> str:
    text = _node_text(node, source)
    lines = text.splitlines()
    return "\n".join(lines[:max_lines])


def _extract_calls_from_body(body: str) -> list[str]:
    """Extract function call names from source body text."""
    # Matches: identifier( or identifier.method(
    return list({m.group(1) for m in re.finditer(r"\b([a-zA-Z_]\w*)\s*\(", body)})


# ---------------------------------------------------------------------------
# Language-specific extraction
# ---------------------------------------------------------------------------

def _extract_python(source: bytes, file_path: str, project: str, rel_path: str) -> ParsedFileResult:
    result = ParsedFileResult(file_path, "python")
    result.set_mod_prefix(_make_qn(project, rel_path))

    if not _TS_OK or "python" not in _LANGS:
        return _fallback_regex(source, file_path, project, rel_path, "python")

    parser = TSParser(_LANGS["python"])
    tree = parser.parse(source)
    root = tree.root_node

    def walk(node: "Node", class_name: str = ""):
        t = node.type

        if t == "import_statement":
            result.raw_imports.append(_node_text(node, source))
            _parse_py_import(node, source, result, project, rel_path)
            return

        if t == "import_from_statement":
            result.raw_imports.append(_node_text(node, source))
            _parse_py_from_import(node, source, result, project, rel_path)
            return

        if t == "class_definition":
            cname = _child_text(node, ("identifier",), source)
            if cname:
                qn = _make_qn(project, rel_path, cname)
                # Base classes
                bases = []
                for child in node.children:
                    if child.type == "argument_list":
                        for arg in child.children:
                            if arg.type == "identifier":
                                bases.append(_node_text(arg, source))
                sym = RawSymbol(
                    label="Class", name=cname, qualified_name=qn,
                    file_path=file_path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    body=_body_text(node, source),
                    properties={"base_classes": bases},
                )
                result.symbols.append(sym)
                for child in node.children:
                    walk(child, class_name=cname)
            return

        if t == "function_definition":
            fname = _child_text(node, ("identifier",), source)
            if fname:
                label = "Method" if class_name else "Function"
                parts = (class_name, fname) if class_name else (fname,)
                qn = _make_qn(project, rel_path, *parts)
                body = _body_text(node, source)
                # Extract decorators from parent (approximate)
                sym = RawSymbol(
                    label=label, name=fname, qualified_name=qn,
                    file_path=file_path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    body=body,
                    properties={"parent_class": class_name} if class_name else {},
                )
                result.symbols.append(sym)
                # Don't recurse into function body for nested function extraction
            return

        for child in node.children:
            walk(child, class_name=class_name)

    walk(root)
    return result


def _parse_py_import(node: "Node", source: bytes, result: ParsedFileResult, project: str, rel_path: str) -> None:
    # import foo, import foo as bar
    for child in node.children:
        if child.type == "aliased_import":
            names = [c for c in child.children if c.type in ("identifier", "dotted_name")]
            if len(names) >= 2:
                alias = _node_text(names[-1], source)
                module = _node_text(names[0], source)
                result.import_map[alias] = module
        elif child.type in ("identifier", "dotted_name"):
            name = _node_text(child, source)
            result.import_map[name] = name


def _parse_py_from_import(node: "Node", source: bytes, result: ParsedFileResult, project: str, rel_path: str) -> None:
    # from module import name1, name2 as alias2
    module = ""
    importing = False
    for child in node.children:
        if child.type == "relative_import":
            module = _node_text(child, source)
        elif child.type in ("identifier", "dotted_name") and not importing:
            module = _node_text(child, source)
        elif child.type == "import":
            importing = True
        elif importing and child.type == "aliased_import":
            names = [c for c in child.children if c.type == "identifier"]
            if len(names) >= 2:
                result.import_map[_node_text(names[-1], source)] = f"{module}.{_node_text(names[0], source)}"
            elif names:
                n = _node_text(names[0], source)
                result.import_map[n] = f"{module}.{n}"
        elif importing and child.type in ("identifier", "dotted_name"):
            n = _node_text(child, source)
            result.import_map[n] = f"{module}.{n}"


def _extract_ts_js(source: bytes, file_path: str, project: str, rel_path: str, lang: str) -> ParsedFileResult:
    result = ParsedFileResult(file_path, lang)
    result.set_mod_prefix(_make_qn(project, rel_path))

    if not _TS_OK or lang not in _LANGS:
        return _fallback_regex(source, file_path, project, rel_path, lang)

    parser = TSParser(_LANGS[lang])
    tree = parser.parse(source)
    root = tree.root_node

    def get_name(node: "Node") -> str:
        return _child_text(node, (
            "identifier", "property_identifier", "shorthand_property_identifier_pattern",
        ), source)

    def walk(node: "Node", class_name: str = ""):
        t = node.type

        if t in ("import_statement", "import_declaration"):
            result.raw_imports.append(_node_text(node, source))
            _parse_ts_import(node, source, result)
            return

        if t in ("class_declaration", "class_definition", "abstract_class_declaration"):
            cname = get_name(node)
            if cname:
                qn = _make_qn(project, rel_path, cname)
                # heritage
                bases = []
                for child in node.children:
                    if child.type == "class_heritage":
                        bases_text = _node_text(child, source)
                        bases = [b.strip() for b in bases_text.replace("extends", "").replace("implements", "").split(",") if b.strip()]
                sym = RawSymbol(
                    label="Class", name=cname, qualified_name=qn,
                    file_path=file_path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    body=_body_text(node, source),
                    properties={"base_classes": bases},
                )
                result.symbols.append(sym)
                for child in node.children:
                    walk(child, class_name=cname)
            return

        if t in ("interface_declaration", "type_alias_declaration"):
            cname = get_name(node)
            if cname:
                qn = _make_qn(project, rel_path, cname)
                sym = RawSymbol(
                    label="Class", name=cname, qualified_name=qn,
                    file_path=file_path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    body=_body_text(node, source),
                    properties={"is_interface": True},
                )
                result.symbols.append(sym)
            return

        if t in ("function_declaration", "function_expression",
                  "generator_function_declaration"):
            fname = get_name(node)
            if fname:
                label = "Method" if class_name else "Function"
                parts = (class_name, fname) if class_name else (fname,)
                qn = _make_qn(project, rel_path, *parts)
                sym = RawSymbol(
                    label=label, name=fname, qualified_name=qn,
                    file_path=file_path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    body=_body_text(node, source),
                    properties={"parent_class": class_name} if class_name else {},
                )
                result.symbols.append(sym)
            return

        if t == "method_definition":
            mname = get_name(node)
            if mname and class_name:
                qn = _make_qn(project, rel_path, class_name, mname)
                sym = RawSymbol(
                    label="Method", name=mname, qualified_name=qn,
                    file_path=file_path,
                    start_line=node.start_point[0] + 1,
                    end_line=node.end_point[0] + 1,
                    body=_body_text(node, source),
                    properties={"parent_class": class_name},
                )
                result.symbols.append(sym)
            return

        if t in ("lexical_declaration", "variable_declaration"):
            # const foo = function(...) or const foo = (...) =>
            for child in node.children:
                if child.type == "variable_declarator":
                    vname = get_name(child)
                    val = None
                    for c2 in child.children:
                        if c2.type in ("arrow_function", "function_expression",
                                        "generator_function"):
                            val = c2
                            break
                    if vname and val and not class_name:
                        qn = _make_qn(project, rel_path, vname)
                        sym = RawSymbol(
                            label="Function", name=vname, qualified_name=qn,
                            file_path=file_path,
                            start_line=val.start_point[0] + 1,
                            end_line=val.end_point[0] + 1,
                            body=_body_text(val, source),
                        )
                        result.symbols.append(sym)

        for child in node.children:
            walk(child, class_name=class_name)

    walk(root)
    return result


def _parse_ts_import(node: "Node", source: bytes, result: ParsedFileResult) -> None:
    """Parse: import { A, B as C } from './mod'"""
    text = _node_text(node, source)
    # Extract the from-source
    from_match = re.search(r'from\s+["\']([^"\']+)["\']', text)
    if not from_match:
        return
    mod_path = from_match.group(1)

    # Named imports: { A, B as C }
    named = re.findall(r'\{\s*([^}]+)\}', text)
    if named:
        for item in named[0].split(","):
            item = item.strip()
            if " as " in item:
                orig, alias = item.split(" as ", 1)
                result.import_map[alias.strip()] = f"{mod_path}.{orig.strip()}"
            elif item and not item.startswith("*"):
                result.import_map[item] = f"{mod_path}.{item}"
        return

    # Default import: import Foo from './mod'
    default_match = re.match(r'import\s+(\w+)\s+from', text)
    if default_match:
        alias = default_match.group(1)
        result.import_map[alias] = mod_path

    # Namespace: import * as Foo from './mod'
    ns_match = re.search(r'import\s+\*\s+as\s+(\w+)\s+from', text)
    if ns_match:
        alias = ns_match.group(1)
        result.import_map[alias] = mod_path


# ---------------------------------------------------------------------------
# Regex fallback (when tree-sitter not available)
# ---------------------------------------------------------------------------

_RE_PY_FUNC  = re.compile(r"^[ \t]*def\s+(\w+)\s*\(", re.MULTILINE)
_RE_PY_CLASS = re.compile(r"^[ \t]*class\s+(\w+)[\s:(]", re.MULTILINE)
_RE_TS_FUNC  = re.compile(
    r"(?:export\s+)?(?:async\s+)?function\s+(\w+)\s*[(<]|"
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s+)?(?:function|\()",
    re.MULTILINE,
)
_RE_TS_CLASS = re.compile(r"(?:export\s+)?(?:abstract\s+)?class\s+(\w+)", re.MULTILINE)
_RE_TS_IFACE = re.compile(r"(?:export\s+)?interface\s+(\w+)", re.MULTILINE)


def _fallback_regex(source: bytes, file_path: str, project: str, rel_path: str, lang: str) -> ParsedFileResult:
    result = ParsedFileResult(file_path, lang)
    result.set_mod_prefix(_make_qn(project, rel_path))
    text = source.decode("utf-8", errors="replace")
    lines = text.splitlines()

    def ln_of(m: re.Match) -> int:
        return text[:m.start()].count("\n") + 1

    def snippet(ln: int) -> str:
        return "\n".join(lines[ln - 1: ln + 5])

    if lang == "python":
        for m in _RE_PY_CLASS.finditer(text):
            name = m.group(1)
            ln = ln_of(m)
            qn = _make_qn(project, rel_path, name)
            result.symbols.append(RawSymbol("Class", name, qn, file_path, ln, ln, snippet(ln)))
        for m in _RE_PY_FUNC.finditer(text):
            name = m.group(1)
            ln = ln_of(m)
            qn = _make_qn(project, rel_path, name)
            result.symbols.append(RawSymbol("Function", name, qn, file_path, ln, ln, snippet(ln)))
    else:
        for m in _RE_TS_CLASS.finditer(text):
            name = m.group(1)
            ln = ln_of(m)
            qn = _make_qn(project, rel_path, name)
            result.symbols.append(RawSymbol("Class", name, qn, file_path, ln, ln, snippet(ln)))
        for m in _RE_TS_IFACE.finditer(text):
            name = m.group(1)
            ln = ln_of(m)
            qn = _make_qn(project, rel_path, name)
            result.symbols.append(RawSymbol("Class", name, qn, file_path, ln, ln, snippet(ln), {"is_interface": True}))
        for m in _RE_TS_FUNC.finditer(text):
            name = m.group(1) or m.group(2)
            if name:
                ln = ln_of(m)
                qn = _make_qn(project, rel_path, name)
                result.symbols.append(RawSymbol("Function", name, qn, file_path, ln, ln, snippet(ln)))
    return result


# ---------------------------------------------------------------------------
# Main parse-file entry point
# ---------------------------------------------------------------------------

def parse_file(file_path: str, project: str, root_path: str) -> Optional[ParsedFileResult]:
    path = Path(file_path)
    ext = path.suffix.lower()
    lang = _EXT_LANG.get(ext)
    if not lang:
        return None

    try:
        stat = path.stat()
        if stat.st_size > MAX_FILE_BYTES:
            return None
        source = path.read_bytes()
    except OSError:
        return None

    try:
        rel = str(path.relative_to(root_path))
    except ValueError:
        rel = file_path

    sha = hashlib.sha256(source).hexdigest()[:24]

    if lang == "python":
        result = _extract_python(source, file_path, project, rel)
    else:
        result = _extract_ts_js(source, file_path, project, rel, lang)

    result.sha256 = sha
    return result


# ---------------------------------------------------------------------------
# Call resolution
# ---------------------------------------------------------------------------

def resolve_calls(
    result: ParsedFileResult,
    all_qns_by_name: dict[str, list[str]],
) -> list[tuple[str, str]]:
    """
    For each symbol in result, find CALLS edges to other symbols.
    Returns list of (source_qn, target_qn).
    """
    edges: list[tuple[str, str]] = []
    for sym in result.symbols:
        if sym.label not in ("Function", "Method"):
            continue
        called_names = _extract_calls_from_body(sym.body)
        for cname in called_names:
            if cname == sym.name:
                continue
            # Try to resolve via import map
            resolved_mod = result.import_map.get(cname)
            candidates: list[str] = []
            if resolved_mod:
                # Look for QNs that end with the resolved name
                for qn in all_qns_by_name.get(cname, []):
                    candidates.append(qn)
            else:
                candidates = all_qns_by_name.get(cname, [])

            for tgt_qn in candidates[:3]:  # cap at 3 to avoid false positives
                if tgt_qn != sym.qualified_name:
                    edges.append((sym.qualified_name, tgt_qn))
    return edges


# ---------------------------------------------------------------------------
# File discovery
# ---------------------------------------------------------------------------

def collect_files(root_path: str) -> list[Path]:
    files = []
    root = Path(root_path)
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _EXCLUDE_DIRS for part in p.parts):
            continue
        if p.suffix.lower() not in _EXT_LANG:
            continue
        files.append(p)
    return files


# ---------------------------------------------------------------------------
# Full indexing pipeline
# ---------------------------------------------------------------------------

class IndexPipeline:
    """
    Orchestrates the 3-pass indexing for a single project.
    Designed to be called from GraphEngine.
    """

    def __init__(self, project: str, root_path: str):
        self.project = project
        self.root_path = root_path

    def run(
        self,
        stored_hashes: dict[str, str],
        progress_cb=None,
    ) -> dict:
        """
        Returns {
            files_indexed: int,
            nodes_written: int,
            changed_files: list[str],
            parsed: list[ParsedFileResult],
            changed_rels: list[str],  # rel paths of changed files
            all_rels: list[str],      # rel paths of all current files
        }
        """
        all_files = collect_files(self.root_path)
        root = Path(self.root_path)
        all_rels: list[str] = []

        # Determine which files changed
        changed: list[Path] = []
        unchanged: list[Path] = []
        for f in all_files:
            try:
                rel = str(f.relative_to(root))
            except ValueError:
                rel = str(f)
            all_rels.append(rel)
            sha = stored_hashes.get(rel)
            if sha is None or sha != file_hash(str(f)):
                changed.append(f)
            else:
                unchanged.append(f)

        if not changed:
            return {
                "files_indexed": 0,
                "nodes_written": 0,
                "changed_files": [],
                "parsed": [],
                "changed_rels": [],
                "new_hashes": {},
                "all_rels": all_rels,
            }

        # Pass 1: parse all changed files
        parsed: list[ParsedFileResult] = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {
                pool.submit(parse_file, str(f), self.project, self.root_path): f
                for f in changed
            }
            done = 0
            total = len(futures)
            for future in as_completed(futures):
                result = future.result()
                if result is not None:
                    parsed.append(result)
                done += 1
                if progress_cb:
                    progress_cb(done, total)

        # Build new hashes map
        new_hashes: dict[str, str] = {}
        for pr in parsed:
            try:
                rel = str(Path(pr.path).relative_to(root))
            except ValueError:
                rel = pr.path
            new_hashes[rel] = pr.sha256

        return {
            "files_indexed": len(parsed),
            "nodes_written": sum(len(p.symbols) for p in parsed),
            "changed_files": [str(f) for f in changed],
            "parsed": parsed,
            "changed_rels": list(new_hashes.keys()),
            "new_hashes": new_hashes,
            "all_rels": all_rels,
        }
