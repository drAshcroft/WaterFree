---
name: waterfree-knowledge
description: Use the WaterFree knowledge MCP tools to search shared code snippets, patterns, utilities, and conventions before writing new boilerplate or reaching for external docs.
---

# WaterFree — Knowledge / Snippet Store Tools

You have access to a global cross-project knowledge store via the `pairprogram-knowledge`
MCP server. It contains code snippets, patterns, utilities, and conventions extracted
from indexed repositories using LLM classification, and entries added directly by agents.

Knowledge is shared across all workspaces and stored at `~/.waterfree/global/knowledge.db`.

## When to Use (Read)

Use these tools when you need to:
- Find a reusable pattern before writing new code — use `search_knowledge`
- Look for prior implementations of a concept across projects — use `search_knowledge`
- Check what has already been indexed — use `list_knowledge_sources`
- Understand how many snippets are available — use `knowledge_stats`

Always search the knowledge store before writing boilerplate or reaching for external docs.

## When to Add

Use `add_knowledge` when you discover something worth preserving for future sessions
across **any** project. Good candidates:

- **You just wrote a reusable utility** — a helper, decorator, or class that could be
  useful in a different project (save as `utility`)
- **You established a convention** — a naming rule, file layout pattern, or team
  agreement that was agreed upon during the session (save as `convention`)
- **You solved a non-obvious problem** — the solution involved research, trial-and-error,
  or tricky framework behaviour worth remembering (save as `pattern`)
- **You worked out correct library/API usage** — especially for third-party APIs with
  quirks, gotchas, or non-obvious initialization (save as `api_usage`)
- **You're about to write boilerplate that already lives elsewhere** — if you can't
  find it in the store but just wrote it, add it so the next session can

**Before adding:** run `search_knowledge` first to avoid duplicates.

### What makes a good entry

| Field | Guidance |
|-------|----------|
| `title` | Specific and searchable — include the pattern name or key concept |
| `description` | 2–4 sentences: what it does, why it's useful, when to reach for it |
| `context` | Caveats, version requirements, related files/symbols, when NOT to use |
| `snippet_type` | Pick the closest: `pattern`, `utility`, `style`, `api_usage`, `convention` |
| `source_repo` | Always the actual project name or path — e.g. `"WaterFree"` or `"c:/projects/myapp"` |
| `tags` | 3–6 short tags covering language, framework, domain, and key concept |

## When to Delete

Use `delete_knowledge` when:
- An entry is factually incorrect or misleading
- A pattern was superseded by a better approach and the old entry would cause confusion
- An entry was added by mistake

---

## Snippet Types

- `pattern` — design patterns and architectural patterns
- `utility` — helper functions and utility classes
- `style` — code style and formatting conventions
- `api_usage` — example usages of libraries/frameworks
- `convention` — naming, structure, and project conventions

---

## Tools

### Search for snippets
```
search_knowledge(query="retry with exponential backoff", limit=10)
search_knowledge(query="authentication middleware")
search_knowledge(query="sqlite connection pooling")
search_knowledge(query="dataclass serialisation")
```

The search uses BM25-ranked full-text search over title, description, tags, and code.
Return fields: `id`, `title`, `description`, `snippet_type`, `code`, `tags`, `context`,
`source_repo`, `source_file`, `source_repo_url`, `created_at`.

### Add a snippet
```
add_knowledge(
    title="Exponential backoff retry decorator",
    description="Retries a function up to N times with exponential backoff. Useful for
                 wrapping flaky network calls or external API requests.",
    code="def retry(max_attempts=3, ...):\n    ...",
    snippet_type="utility",
    source_repo="WaterFree",
    source_file="backend/llm/claude_client.py",
    tags=["python", "retry", "error-handling", "decorator"],
    context="Requires Python 3.10+. Not suitable for DB transactions — use explicit
             savepoints instead. See also: circuit_breaker pattern.",
    source_repo_url="https://github.com/org/waterfree.git",
)
```

Returns `{ "id": "<uuid>", "added": true, "message": "..." }`.
If the code content is identical to an existing entry, `added` is `false` (deduplicated
by SHA-256 hash of code).

### Delete a snippet
```
delete_knowledge(entry_id="<uuid>")
```

Returns `{ "deleted": true, "message": "..." }`.
The `id` is available in the output of `add_knowledge` and `search_knowledge`.

### List all indexed sources
```
list_knowledge_sources()
```
Returns each source with name, path or URL, entry count, and last-indexed date.

### Check store statistics
```
knowledge_stats()
```
Returns total entry count and number of indexed sources.

---

## Tips

- Search with specific terms ("singleton pattern", "JWT decode") or broad concepts ("caching").
- Use `tags` in the returned entries to discover related searches.
- Use the `context` field to explain why a snippet has constraints — future readers
  won't have the original conversation to refer to.
- If the store is empty, knowledge must be built first using the `buildKnowledge` command
  in the WaterFree VS Code extension, or by running the knowledge extractor directly.
