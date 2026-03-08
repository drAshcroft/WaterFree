# Subsystem 10 — Pair Snippetize
## WaterFree VS Code Extension

---

## Purpose

Pair Snippetize is a global, cross-project code knowledge base built into WaterFree. It extracts reusable patterns, utilities, conventions, and API usage examples from any codebase using LLM-based analysis — not just structural indexing. The extracted knowledge persists across all workspaces and is automatically surfaced in planning and annotation prompts when relevant.

**Three extraction modes:**

| Mode | Trigger | What it does |
|------|---------|--------------|
| **Workspace** | "Snippetize" button in sidebar | Scans the current workspace for reusable patterns |
| **Repo** | "+ Snippetize Repo" button | Clones and scans an external git repo or local path |
| **Procedure** | Right-click a symbol → "Snippetize Procedure" | Deep-extracts a single function with its full call chain and data structures |

---

## Architecture

```
backend/knowledge/
├── models.py          — KnowledgeEntry and KnowledgeRepo dataclasses
├── store.py           — SQLite store with FTS5 full-text search
├── extractor.py       — Two-pass LLM extraction (triage → describe)
├── git_importer.py    — Git repo cloning and symbol extraction
├── procedure_extractor.py — Deep single-procedure extraction with call chain
└── retriever.py       — Context-time search and prompt injection
```

**Storage:** `~/.waterfree/global/knowledge.db` (global, shared across all workspaces)

---

## Data Model

### KnowledgeEntry

Every extracted snippet is stored as a `KnowledgeEntry`:

```python
@dataclass
class KnowledgeEntry:
    id: str              # UUID
    source_repo: str     # short repo name, e.g. "my-app"
    source_file: str     # relative file path within that repo
    snippet_type: str    # "pattern" | "utility" | "style" | "api_usage" | "convention"
    title: str           # LLM-generated short title (max 10 words)
    description: str     # LLM-generated plain-English summary
    code: str            # raw source code
    tags: list[str]      # LLM-extracted tags, e.g. ["python", "django", "auth"]
    content_hash: str    # SHA-256 of code — used for deduplication
    created_at: str      # ISO-8601 UTC timestamp
    source_repo_url: str # optional git remote URL
```

**Deduplication:** The `content_hash` column has a UNIQUE constraint in SQLite. Inserting an identical code snippet is silently ignored, so running "Snippetize" multiple times on the same workspace is safe.

### KnowledgeRepo

Tracks metadata about each indexed source:

```python
@dataclass
class KnowledgeRepo:
    name: str          # matches source_repo in entries
    local_path: str    # absolute path to local clone or workspace
    remote_url: str    # git remote URL (empty for local workspaces)
    entry_count: int   # live count of entries for this repo
    last_indexed: str  # ISO-8601 UTC timestamp of last index run
```

---

## Storage — `store.py`

### Schema

```sql
-- One row per indexed source
CREATE TABLE knowledge_repos (
    name         TEXT PRIMARY KEY,
    local_path   TEXT NOT NULL,
    remote_url   TEXT NOT NULL DEFAULT '',
    entry_count  INTEGER NOT NULL DEFAULT 0,
    last_indexed TEXT NOT NULL
);

-- Individual extracted snippets
CREATE TABLE knowledge_entries (
    id           TEXT PRIMARY KEY,
    source_repo  TEXT NOT NULL,
    source_file  TEXT NOT NULL,
    snippet_type TEXT NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL,
    code         TEXT NOT NULL,
    tags         TEXT NOT NULL DEFAULT '[]',  -- JSON array
    content_hash TEXT NOT NULL UNIQUE,
    created_at   TEXT NOT NULL,
    source_repo_url TEXT NOT NULL DEFAULT ''
);

-- FTS5 virtual table for BM25 full-text search
CREATE VIRTUAL TABLE knowledge_fts USING fts5(
    title, description, tags, code,
    content='knowledge_entries',
    content_rowid='rowid'
);
```

FTS triggers keep `knowledge_fts` in sync with `knowledge_entries` automatically on insert and delete.

### Key Methods

| Method | Description |
|--------|-------------|
| `add_entry(entry) -> bool` | Insert entry; returns `False` silently on duplicate `content_hash` |
| `search(query, limit=10) -> list[KnowledgeEntry]` | BM25-ranked FTS5 search; falls back to LIKE on FTS error |
| `list_repos() -> list[KnowledgeRepo]` | All indexed sources with live entry counts |
| `delete_repo(name) -> int` | Delete all entries for a source; returns count deleted |
| `total_entries() -> int` | Total entries across all sources |

**FTS query escaping:** Each search token is wrapped in double-quotes before being passed to FTS5 (`"token1" OR "token2"`), preventing syntax errors from hyphens, colons, and other special characters.

---

## Extraction — Two-Pass Design (`extractor.py`)

Workspace and repo extraction uses a two-pass LLM pipeline to avoid sending megabytes of code to the API.

### Pass 1 — Triage (cheap, fast)

Sends only symbol names, labels, and first-line signatures in large batches (up to 50 symbols). The LLM identifies which symbols are worth examining further — typically 15–30% of the total.

**Input per symbol:**
```
[index] label `name` — file/path.py
    def first_line_of_signature(arg1, arg2):
```

**LLM output:** A list of 0-based indices to fetch full source for.

### Pass 2 — Describe (full code, selective)

Fetches full source only for symbols selected in Pass 1. The LLM classifies each and writes a title, description, and tags. Snippets the LLM marks as not worth keeping are dropped.

**Constants:**
```python
_TRIAGE_BATCH   = 50   # symbols per triage call
_DESCRIBE_BATCH = 12   # symbols per describe call
_MAX_BODY_CHARS = 900  # truncate very long bodies before sending
_MIN_BODY_CHARS = 30   # skip trivially short snippets
```

### Focus Clause

A user-supplied `focus` string is injected into both system prompts:

- With focus: `"The user specifically wants knowledge about: {focus}"`
- Without focus: `"Apply general judgement — keep broadly reusable content."`

This allows targeted extraction (e.g. "authentication patterns", "error handling") instead of general dumps.

### Snippet Types

| Type | Meaning |
|------|---------|
| `pattern` | A reusable algorithmic or structural pattern |
| `utility` | A helper useful beyond this specific project |
| `style` | A coding convention or style choice |
| `api_usage` | A non-obvious way of using an external API or library |
| `convention` | A project-level naming or organisation convention |

---

## Procedure Extraction — `procedure_extractor.py`

For deep extraction of a single named function or method. Unlike the batch extractor, this traces the full outbound call chain and collects related data structures before asking the LLM for a comprehensive summary.

### Pipeline

```
1. Resolve name → qualified name (via GraphClient)
2. Fetch root procedure source code
3. Trace outbound call chain (GraphClient.trace_call_path)
4. Fetch source for each callee within token budget
5. Find related class/type definitions in the same files
6. Assemble context document
7. LLM summarization (single call)
8. Store as KnowledgeEntry
```

### Token Budget

All assembly is bounded by a character budget (estimated at 4 chars/token):

```python
_BUDGET_TOKENS          = 6_000    # total assembled context limit
_SINGLE_BODY_WARN_CHARS = 6_000    # warn but include as-is
_SINGLE_BODY_MAX_CHARS  = 8_000    # hard truncate with marker
_DEFAULT_MAX_DEPTH      = 3
_HARD_MAX_DEPTH         = 5        # user cannot exceed this
```

**Truncation behaviour:**
- A body between `WARN` and `MAX` chars is included in full but noted in warnings
- A body over `MAX` chars is truncated at `MAX` with `\n... [TRUNCATED — body too large]` appended
- When the running total exceeds `BUDGET_CHARS`, assembly stops and remaining nodes are counted as skipped

### Warnings

The return value always includes a `warnings` list. Warning prefixes:

| Prefix | Meaning |
|--------|---------|
| `LARGE BODY:` | A single function body is large but included |
| `TRUNCATED BODY:` | A body was cut at the hard limit |
| `TRUNCATION:` | The token budget was reached; N nodes were omitted |
| `DEPTH LIMIT:` | Traversal hit max_depth with no truncation; deeper dependencies may exist |
| `CLASSES OMITTED:` | Insufficient budget remaining to include any data structures |
| `CLASS 'X' omitted` | A specific class was excluded due to budget exhaustion |

### Return Value

```python
{
    "entry":           KnowledgeEntry | None,  # None if symbol not found or not kept
    "warnings":        list[str],
    "tokenBudgetUsed": int,       # estimated tokens consumed by assembled context
    "nodesIncluded":   int,       # call chain callees successfully included
    "nodesSkipped":    int,       # callees omitted due to budget
    "depthReached":    int,       # deepest depth reached in trace
    "kept":            bool,      # whether LLM judged it worth storing
    "stored":          bool,      # False if entry already existed (dedup)
}
```

---

## Git Repo Ingestion — `git_importer.py`

Accepts a local directory path or a git remote URL:

```
import_repo(source, store, focus="", progress_cb=None) -> dict
```

**For remote URLs:** Clones to `~/.waterfree/global/repos/<repo-name>/` using `git clone --depth=1`. On subsequent calls, performs `git pull --ff-only` instead of re-cloning.

**For local paths:** Uses the path directly; no cloning.

After obtaining the local path, it calls the graph indexer (`collect_files` + `parse_file` from `backend.graph.indexer`) to extract AST symbols, then passes them to `KnowledgeExtractor` for two-pass LLM extraction.

---

## Context Injection — `retriever.py`

At context-build time, `retriever.search_for_context(query)` is called from `context_builder.py` and appended to planning and annotation prompts.

**Caps to prevent context window overflow:**
```python
_MAX_ENTRIES    = 8     # maximum entries to include
_MAX_CODE_CHARS = 400   # truncate long code previews
_MAX_TOTAL_CHARS = 2000 # stop adding entries beyond this total
```

The retriever **does not create** the database file if it does not exist — if no knowledge has been indexed, the function returns an empty string and the prompt is unmodified.

**Injected section format:**
```
GLOBAL KNOWLEDGE BASE (relevant patterns from other projects):

[PATTERN: JWT Token Validation with Expiry Check]
Source: auth-service / src/tokens.py
Validates a JWT token and raises specific exceptions for expiry vs. malformed tokens,
enabling callers to distinguish between "refresh" and "reject" scenarios.
```python
def validate_token(token: str) -> dict:
    ...
```
```

---

## RPC API (`backend/server.py`)

Six JSON-RPC methods are registered under the `knowledge` feature group:

### `buildKnowledge`

Extract knowledge from the current workspace.

**Request params:**
```json
{
    "workspacePath": "/path/to/workspace",
    "focus": "authentication patterns"   // optional
}
```

**Response:**
```json
{
    "added": 12,
    "symbolsScanned": 87,
    "repo": "my-app",
    "message": "Added 12 new snippets from workspace 'my-app'."
}
```

Sends `indexProgress` notifications (`{ done, total, phase: "knowledge" }`) during extraction.

---

### `addKnowledgeRepo`

Clone and extract knowledge from a remote git repo or local path.

**Request params:**
```json
{
    "source": "https://github.com/org/repo.git",
    "focus": "React hooks patterns"   // optional
}
```

**Response:**
```json
{
    "name": "repo",
    "symbolsScanned": 340,
    "added": 28,
    "localPath": "/home/user/.waterfree/global/repos/repo",
    "error": "..."   // only present on failure
}
```

---

### `extractProcedure`

Deep-extract a single named function or method with call chain analysis.

**Request params:**
```json
{
    "name": "validate_token",
    "workspacePath": "/path/to/workspace",
    "focus": "how it handles token expiry",   // optional
    "maxDepth": 3                             // 1-5, default 3
}
```

**Response:**
```json
{
    "entry": { ... },          // KnowledgeEntry dict, or null if not kept
    "warnings": ["..."],
    "tokenBudgetUsed": 1240,
    "nodesIncluded": 4,
    "nodesSkipped": 1,
    "depthReached": 3,
    "kept": true,
    "stored": true
}
```

---

### `searchKnowledge`

Query the global knowledge base directly.

**Request params:**
```json
{
    "query": "retry logic exponential backoff",
    "limit": 10   // optional, default 10
}
```

**Response:**
```json
{
    "entries": [ { ... } ],   // array of KnowledgeEntry dicts
    "count": 3,
    "total": 156
}
```

---

### `listKnowledgeSources`

List all indexed repos/sources.

**Request params:** `{}`

**Response:**
```json
{
    "repos": [
        {
            "name": "my-app",
            "localPath": "/path/to/workspace",
            "remoteUrl": "",
            "entryCount": 12,
            "lastIndexed": "2026-03-06T10:00:00+00:00"
        }
    ],
    "totalEntries": 156
}
```

---

### `removeKnowledgeSource`

Delete all knowledge entries from a named source.

**Request params:**
```json
{ "name": "my-app" }
```

**Response:**
```json
{ "name": "my-app", "deleted": 12 }
```

---

## VS Code UI

### Sidebar Buttons

Both buttons appear in the composer section of the Plan Sidebar:

- **"🧠 Snippetize"** (`data-action="buildKnowledge"`) — triggers workspace extraction, with an optional focus input.
- **"+ Snippetize Repo"** (`data-action="addKnowledgeRepo"`) — prompts for a git URL or local path, then an optional focus.

### Right-Click → Snippetize Procedure

The `waterfree.extractProcedure` command appears in the editor context menu. When invoked:

1. The word under the cursor (or selected text) is read from the active editor.
2. The sidebar textarea is pre-filled with:
   ```
   Extract and explain <symbol> for snippetize. Add context:
   ```
3. The sidebar panel is focused and the cursor is placed at the end of the textarea.
4. The user adds context (or leaves it blank) and clicks **"🧠 Snippetize"**.
5. The sidebar detects the template pattern and fires `extractProcedure` instead of the normal workspace scan.

This keeps the entire flow in a single input surface — no sequential popup dialogs.

### Commands

| Command ID | Title | Notes |
|------------|-------|-------|
| `waterfree.buildKnowledge` | Pair Snippetize: Snippetize Workspace | Also accessible via sidebar button |
| `waterfree.addKnowledgeRepo` | Pair Snippetize: Snippetize Repo | Also accessible via sidebar button |
| `waterfree.extractProcedure` | Pair Snippetize: Snippetize Procedure | Editor context menu; right-click a symbol |

---

## Key Design Decisions

### Why SQLite FTS5 instead of embeddings?

Embeddings require either a local model (slower startup, larger binary) or a remote API (cost, latency, privacy concern). FTS5 with BM25 is built into SQLite, has zero dependencies, and — because the LLM writes natural-language descriptions at index time — keyword search works well even for code-specific concepts. Embeddings can be added later as an optional enhancement without changing the store schema.

### Why two-pass extraction?

Sending full source for every symbol in a large repo would use thousands of tokens just to identify the 20% that are actually reusable. Pass 1 costs roughly 10–20× less than sending everything to Pass 2. For a 500-symbol repo, this typically saves 80–100K input tokens per extraction run.

### Why is the retriever capped so aggressively?

Planning and annotation prompts already include the workspace graph summary, file context, and session history. The KB section must not overflow the remaining context budget. The 8-entry / 2000-character cap leaves room for everything else while still surfacing the most relevant patterns.

### Why does `prefillSnippetize` use the textarea instead of an input box?

The previous flow showed three sequential `showInputBox` prompts (name → focus → depth), which felt disjointed. The sidebar textarea is always visible and the pre-filled template makes the intent obvious without extra prompts. The user can edit the symbol name directly in the textarea if the auto-detected word is wrong.

---

## Error Handling

| Situation | Behaviour |
|-----------|-----------|
| Symbol not in graph index | Returns `kept: false`, warning: "Symbol not found — try indexing workspace first." |
| Git clone failure | Returns `{ error: "git clone failed: ..." }` in response |
| LLM API error during extraction | Logs warning, skips batch; other batches continue |
| FTS5 query syntax error | Falls back to `LIKE`-based search automatically |
| Duplicate snippet (same content hash) | `add_entry` returns `False`; no error raised; count not incremented |
| Token budget exhausted mid-chain | Records truncation warnings; LLM works with what was assembled |
| KB does not exist yet | `retriever.search_for_context` returns `""` without creating the DB |
