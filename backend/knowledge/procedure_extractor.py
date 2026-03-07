"""
procedure_extractor.py — Deep single-procedure extraction with call chain assembly.

Given a named function/method, this module:
  1. Resolves it to a qualified name via the graph
  2. Fetches its full source code (root)
  3. Traces the outbound call chain depth-first, fetching each callee's source
  4. Searches for related data structures (classes/types) in the same files
  5. Enforces a token budget — stops adding nodes when the budget is exceeded
     and records truncation warnings rather than silently dropping content
  6. Asks the LLM to write a comprehensive procedure summary
  7. Stores the result as a KnowledgeEntry

Token budget guards
-------------------
  - Each function body is estimated at ~4 chars/token
  - If a single body exceeds SINGLE_BODY_WARN_CHARS it is noted in warnings;
    if it exceeds SINGLE_BODY_MAX_CHARS it is truncated with a marker
  - If the running total exceeds BUDGET_CHARS the assembly stops and the
    number of skipped nodes + the depth at which truncation occurred are
    recorded in the returned warnings list
  - The LLM is told explicitly what was and wasn't included

Returned value
--------------
{
    "entry":      KnowledgeEntry | None,   # None if the symbol wasn't found
    "warnings":   list[str],               # human-readable truncation notices
    "tokenBudgetUsed": int,                # estimated tokens consumed
    "nodesIncluded": int,
    "nodesSkipped": int,
    "depthReached": int,
}
"""

from __future__ import annotations

import json
import logging
import os
from typing import Optional

import anthropic

from backend.knowledge.models import KnowledgeEntry
from backend.knowledge.store import KnowledgeStore

log = logging.getLogger(__name__)

# ── Token budget constants ───────────────────────────────────────────────────
_CHARS_PER_TOKEN = 4
_BUDGET_TOKENS = 6_000          # total assembled context limit
_BUDGET_CHARS = _BUDGET_TOKENS * _CHARS_PER_TOKEN

_SINGLE_BODY_WARN_CHARS = 6_000   # warn but include as-is
_SINGLE_BODY_MAX_CHARS  = 8_000   # hard truncate

_DEFAULT_MAX_DEPTH = 3
_HARD_MAX_DEPTH = 5

# ── LLM schema ───────────────────────────────────────────────────────────────
_SUMMARIZE_SCHEMA = {
    "type": "object",
    "properties": {
        "keep": {
            "type": "boolean",
            "description": "true if this procedure is worth storing as reusable knowledge",
        },
        "snippet_type": {
            "type": "string",
            "enum": ["pattern", "utility", "style", "api_usage", "convention"],
        },
        "title": {
            "type": "string",
            "description": "Short, searchable title — max 10 words",
        },
        "description": {
            "type": "string",
            "description": (
                "4-6 sentence description: what the procedure does, what data flows "
                "through it, its key dependencies, and why/when a developer would "
                "reuse this pattern in another project."
            ),
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
            "description": "4-8 lowercase tags: language, framework, domain, concept, pattern-name",
        },
    },
    "required": ["keep", "snippet_type", "title", "description", "tags"],
}

_SYSTEM_PROMPT = """\
You are a code knowledge curator specialising in deep procedure analysis.
You receive a procedure's source code along with its call chain (fetched from \
a code graph) and any related data structures. Your job is to write a \
comprehensive, searchable summary that captures not just what the procedure \
does but how it does it — the data flow, the key dependencies, and the \
transferable technique.

If the call chain was truncated due to token limits, work with what was \
provided and note the limitation in your description.\
"""


# ── Public entry point ───────────────────────────────────────────────────────

def extract_procedure(
    graph,                    # GraphClient — passed in from server to avoid circular import
    store: KnowledgeStore,
    name: str,
    source_repo: str,
    focus: str = "",
    max_depth: int = _DEFAULT_MAX_DEPTH,
) -> dict:
    """
    Deep-extract a single named procedure and store the result.

    Parameters
    ----------
    graph       : GraphClient instance (already connected to the workspace)
    store       : KnowledgeStore to persist the entry
    name        : function/method name (qualified or short)
    source_repo : project name shown in stored entries
    focus       : optional user hint about what aspect to emphasise
    max_depth   : maximum call chain depth (capped at HARD_MAX_DEPTH)

    Returns
    -------
    dict with keys: entry, warnings, tokenBudgetUsed, nodesIncluded,
                    nodesSkipped, depthReached
    """
    max_depth = min(max_depth, _HARD_MAX_DEPTH)
    warnings: list[str] = []

    # ── 1. Resolve to qualified name ────────────────────────────────────────
    qname = _resolve_qname(graph, name)
    if not qname:
        return _not_found(name, warnings)

    # ── 2. Fetch root source ────────────────────────────────────────────────
    root_snippet = _safe_get_snippet(graph, qname)
    root_body = root_snippet.get("source") or root_snippet.get("snippet") or ""
    root_file = root_snippet.get("file_path", "")
    root_sig  = root_snippet.get("signature", "")

    if not root_body:
        warnings.append(f"Could not retrieve source for '{name}' (qname={qname}).")
        return _not_found(name, warnings)

    root_body, root_truncated = _guard_body(root_body, name, warnings)
    budget_remaining = _BUDGET_CHARS - len(root_body)

    # ── 3. Trace outbound call chain ────────────────────────────────────────
    chain_nodes: list[dict] = []   # {name, qname, file, body, depth}
    skipped = 0
    depth_reached = 0

    try:
        trace = graph.trace_call_path(
            name, direction="outbound", depth=max_depth, risk_labels=False
        )
        raw_nodes = trace.get("nodes", [])
    except Exception as exc:
        warnings.append(f"Call chain trace failed: {exc}")
        raw_nodes = []

    # raw_nodes includes the root itself; skip it
    seen_qnames: set[str] = {qname}

    for node in raw_nodes:
        node_name  = node.get("name") or node.get("qualified_name", "")
        node_qname = node.get("qualified_name") or node.get("name", "")
        node_depth = node.get("depth", 1)

        if node_qname in seen_qnames or node_name == name:
            continue
        seen_qnames.add(node_qname)
        depth_reached = max(depth_reached, node_depth)

        if budget_remaining <= 0:
            skipped += 1
            continue

        snip = _safe_get_snippet(graph, node_qname)
        body = snip.get("source") or snip.get("snippet") or ""
        if not body:
            continue

        body, _ = _guard_body(body, node_name, warnings)

        if len(body) > budget_remaining:
            skipped += 1
            if skipped == 1:
                warnings.append(
                    f"Token budget reached at depth {node_depth} — "
                    f"some call chain nodes were omitted (first omitted: '{node_name}')."
                )
            continue

        budget_remaining -= len(body)
        chain_nodes.append({
            "name":  node_name,
            "qname": node_qname,
            "file":  node.get("file_path", ""),
            "body":  body,
            "depth": node_depth,
        })

    if skipped > 0:
        warnings.append(
            f"TRUNCATION: {skipped} call chain node(s) omitted. "
            f"Re-run with a smaller max_depth or narrower focus to see them."
        )
    if depth_reached >= max_depth and skipped == 0:
        warnings.append(
            f"DEPTH LIMIT: call chain traversal stopped at depth {max_depth}. "
            f"There may be deeper dependencies not included here."
        )

    # ── 4. Related data structures ──────────────────────────────────────────
    class_blocks = _find_related_classes(
        graph, chain_nodes + [{"file": root_file}], budget_remaining, warnings
    )
    chars_used = (_BUDGET_CHARS - budget_remaining) + sum(len(c["body"]) for c in class_blocks)

    # ── 5. Assemble context ─────────────────────────────────────────────────
    context = _assemble_context(
        name=name,
        source_repo=source_repo,
        root_body=root_body,
        root_file=root_file,
        root_sig=root_sig,
        chain_nodes=chain_nodes,
        class_blocks=class_blocks,
        skipped=skipped,
        depth_reached=depth_reached,
        max_depth=max_depth,
        warnings=warnings,
        focus=focus,
    )

    # ── 6. LLM summarization ────────────────────────────────────────────────
    llm_result = _call_llm(context, focus)
    if not llm_result.get("keep"):
        log.info("extract_procedure: LLM marked '%s' as not worth keeping", name)
        return {
            "entry": None,
            "warnings": warnings,
            "tokenBudgetUsed": chars_used // _CHARS_PER_TOKEN,
            "nodesIncluded": len(chain_nodes),
            "nodesSkipped": skipped,
            "depthReached": depth_reached,
            "kept": False,
        }

    # ── 7. Build and store entry ────────────────────────────────────────────
    # The stored code is the root procedure only — the description captures the chain.
    entry = KnowledgeEntry.create(
        source_repo=source_repo,
        source_file=root_file,
        snippet_type=llm_result.get("snippet_type", "pattern"),
        title=llm_result.get("title", name),
        description=llm_result.get("description", ""),
        code=root_body,
        tags=llm_result.get("tags", []),
    )

    stored = store.add_entry(entry)
    if not stored:
        warnings.append("Entry already exists in knowledge base (duplicate content hash).")

    return {
        "entry": entry.to_dict() if stored else None,
        "warnings": warnings,
        "tokenBudgetUsed": chars_used // _CHARS_PER_TOKEN,
        "nodesIncluded": len(chain_nodes),
        "nodesSkipped": skipped,
        "depthReached": depth_reached,
        "kept": True,
        "stored": stored,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _resolve_qname(graph, name: str) -> Optional[str]:
    """Try qualified name first, then short name resolution."""
    # If it looks like a qualified name already, use it directly
    if "." in name:
        snip = _safe_get_snippet(graph, name)
        if snip.get("source") or snip.get("snippet"):
            return name
    try:
        return graph.find_qualified_name(name)
    except Exception as exc:
        log.debug("_resolve_qname failed: %s", exc)
        return None


def _safe_get_snippet(graph, qname: str) -> dict:
    try:
        return graph.get_code_snippet(qname, auto_resolve=True)
    except Exception:
        return {}


def _guard_body(body: str, name: str, warnings: list[str]) -> tuple[str, bool]:
    """
    Truncate body if it exceeds limits. Returns (body, was_truncated).
    Adds to warnings if the body was large enough to warn about.
    """
    if len(body) > _SINGLE_BODY_WARN_CHARS and len(body) <= _SINGLE_BODY_MAX_CHARS:
        warnings.append(
            f"LARGE BODY: '{name}' is {len(body)} chars (~{len(body) // _CHARS_PER_TOKEN} tokens) "
            f"— included in full but may dominate the context."
        )
        return body, False

    if len(body) > _SINGLE_BODY_MAX_CHARS:
        truncated = body[:_SINGLE_BODY_MAX_CHARS] + "\n... [TRUNCATED — body too large]"
        warnings.append(
            f"TRUNCATED BODY: '{name}' was {len(body)} chars; "
            f"truncated to {_SINGLE_BODY_MAX_CHARS} chars (~{_SINGLE_BODY_MAX_CHARS // _CHARS_PER_TOKEN} tokens)."
        )
        return truncated, True

    return body, False


def _find_related_classes(
    graph,
    nodes: list[dict],
    budget_remaining: int,
    warnings: list[str],
) -> list[dict]:
    """
    Find class definitions that appear in the same files as the call chain.
    Returns a list of {name, file, body} dicts that fit within the remaining budget.
    """
    if budget_remaining <= 500 * _CHARS_PER_TOKEN:  # less than 500 tokens left
        warnings.append(
            "CLASSES OMITTED: insufficient token budget remaining to include "
            "related data structures. Consider using a smaller max_depth."
        )
        return []

    files: set[str] = {n["file"] for n in nodes if n.get("file")}
    classes: list[dict] = []
    used = 0

    for file_path in list(files)[:5]:  # cap at 5 files to avoid explosion
        try:
            result = graph.search_graph(
                file_pattern=file_path,
                label=["class"],
                max_results=10,
            )
            for node in result.get("nodes", []):
                node_qname = node.get("qualified_name") or node.get("name", "")
                snip = _safe_get_snippet(graph, node_qname)
                body = snip.get("source") or snip.get("snippet") or ""
                if not body or len(body) > 3000:
                    continue  # skip classes with huge bodies
                if used + len(body) > budget_remaining:
                    warnings.append(
                        f"CLASS '{node.get('name')}' omitted — token budget exhausted."
                    )
                    continue
                used += len(body)
                classes.append({
                    "name":  node.get("name", ""),
                    "file":  file_path,
                    "body":  body,
                })
        except Exception as exc:
            log.debug("_find_related_classes: search failed for %s: %s", file_path, exc)

    return classes


def _assemble_context(
    *,
    name: str,
    source_repo: str,
    root_body: str,
    root_file: str,
    root_sig: str,
    chain_nodes: list[dict],
    class_blocks: list[dict],
    skipped: int,
    depth_reached: int,
    max_depth: int,
    warnings: list[str],
    focus: str,
) -> str:
    parts: list[str] = []

    if focus:
        parts.append(f"EXTRACTION FOCUS: {focus}\n")

    parts.append(f"=== ROOT PROCEDURE: {name} ===")
    parts.append(f"File: {root_file}")
    if root_sig:
        parts.append(f"Signature: {root_sig}")
    parts.append(f"```\n{root_body}\n```")

    if chain_nodes:
        included_count = len(chain_nodes)
        total_note = f" ({included_count} included" + (f", {skipped} omitted)" if skipped else ")")
        parts.append(f"\n=== CALL CHAIN (depth {depth_reached}/{max_depth}){total_note} ===")
        for node in chain_nodes:
            parts.append(
                f"\n[depth {node['depth']}] {node['name']} — {node['file']}\n"
                f"```\n{node['body']}\n```"
            )
    else:
        parts.append("\n=== CALL CHAIN: (none resolved or budget exhausted) ===")

    if class_blocks:
        parts.append("\n=== RELATED DATA STRUCTURES ===")
        for cls in class_blocks:
            parts.append(f"\n{cls['name']} — {cls['file']}\n```\n{cls['body']}\n```")

    if warnings:
        parts.append("\n=== ASSEMBLY NOTES ===")
        for w in warnings:
            parts.append(f"- {w}")

    return "\n".join(parts)


def _call_llm(context: str, focus: str) -> dict:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    client = anthropic.Anthropic(api_key=api_key)

    focus_line = f"\nUser focus: {focus}" if focus else ""
    user_msg = (
        f"Analyse this procedure and its call chain, then describe it as a knowledge entry.{focus_line}\n\n"
        f"{context}"
    )

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        temperature=0.1,
        system=_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_msg}],
        tools=[
            {
                "name": "submit_procedure_knowledge",
                "description": "Submit a knowledge entry for this procedure",
                "input_schema": _SUMMARIZE_SCHEMA,
            }
        ],
        tool_choice={"type": "tool", "name": "submit_procedure_knowledge"},
    )

    for block in response.content:
        if block.type == "tool_use" and block.name == "submit_procedure_knowledge":
            inp = block.input
            if isinstance(inp, str):
                return json.loads(inp)
            return inp if isinstance(inp, dict) else {}
    return {}


def _not_found(name: str, warnings: list[str]) -> dict:
    warnings.append(f"Symbol '{name}' not found in the graph index. "
                    "Try indexing the workspace first.")
    return {
        "entry": None,
        "warnings": warnings,
        "tokenBudgetUsed": 0,
        "nodesIncluded": 0,
        "nodesSkipped": 0,
        "depthReached": 0,
        "kept": False,
    }
