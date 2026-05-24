---
name: waterfree-knowledge
description: Use the `waterfree knowledge` CLI to search shared code snippets, patterns, utilities, and conventions before writing new boilerplate or reaching for external docs.
---

# WaterFree — Knowledge / Snippet Store

You have access to a global knowledge store via the `waterfree` CLI. Knowledge
is shared across all workspaces and stored at `~/.waterfree/global/knowledge.db`.

Each invocation is a short shell command — run it through Bash. All commands
emit JSON to stdout.

## When to Use (Read)

- Traverse the store by stable subject/category before searching — `waterfree knowledge browse`
- Find a reusable pattern before writing new code — `waterfree knowledge search`
- Look for prior implementations of a concept across projects — `waterfree knowledge search`
- Check what has already been indexed — `waterfree knowledge list-sources`
- Understand how many snippets are available — `waterfree knowledge stats`

Use the hierarchy first for broad domains and search for precise lookups.
Always consult the knowledge store before writing boilerplate or reaching for
external docs. User preferences and style guides should be pushed to this store.

## When to Add

Use `waterfree knowledge add` when you discover something worth preserving for
future sessions across **any** project. Good candidates:

- **Reusable utility** you just wrote — `--snippet-type utility`
- **Convention** that was agreed upon — `--snippet-type convention`
- **Non-obvious problem you solved** — `--snippet-type pattern`
- **Correct library / API usage** with quirks or gotchas — `--snippet-type api_usage`
- **About-to-write boilerplate that already lives elsewhere** — add the existing version so the next session can find it
- **API preferences** the user tends to reuse
- **Coding styles** the user prefers

**Before adding:** run `waterfree knowledge search` first to avoid duplicates.

### What makes a good entry

| Flag | Guidance |
|------|----------|
| `--title` | Specific and searchable — include the pattern name or key concept |
| `--description` | 2–4 sentences: what it does, why it's useful, when to reach for it |
| `--context` | Caveats, version requirements, related files/symbols, when NOT to use |
| `--snippet-type` | One of `pattern`, `utility`, `style`, `api_usage`, `convention` |
| `--hierarchy-path` | Stable subject taxonomy such as `platform/auth/jwt` or `frontend/forms/validation` |
| `--source-repo` | Actual project name or path — e.g. `WaterFree` or `c:/projects/myapp` |
| `--tag` | Repeatable. 3–6 short tags covering language, framework, domain, key concept |

## When to Delete

Use `waterfree knowledge delete` when:
- An entry is factually incorrect or misleading.
- A pattern was superseded by a better approach and the old entry would cause confusion.
- An entry was added by mistake.

## CLI

The knowledge store is global — no `--workspace` flag.

### Search
```bash
waterfree knowledge search "retry with exponential backoff" --limit 10
waterfree knowledge search "authentication middleware"
waterfree knowledge search "sqlite connection pooling"
```

BM25-ranked full-text search over title, description, tags, and code. Return
fields: `id`, `title`, `description`, `snippet_type`, `code`, `tags`, `context`,
`source_repo`, `source_file`, `source_repo_url`, `created_at`, `hierarchy_path`.

### Browse the taxonomy
```bash
waterfree knowledge browse --path platform/auth --depth 2 --include-entries
waterfree knowledge browse --depth 1
```

Use browse when the question is category-first, exploratory, or the subject
already has an obvious taxonomy. It returns child nodes, subtree counts, and
optionally sample entries from the selected branch.

### Add a snippet

For short snippets, pass code inline:
```bash
waterfree knowledge add \
    --title "Exponential backoff retry decorator" \
    --description "Retries a function up to N times with exponential backoff." \
    --code 'def retry(max_attempts=3): ...' \
    --snippet-type utility \
    --source-repo WaterFree \
    --source-file backend/llm/claude_client.py \
    --hierarchy-path platform/reliability/retries \
    --tag python --tag retry --tag error-handling --tag decorator \
    --context "Requires Python 3.10+. Not suitable for DB transactions."
```

For multi-line code, write to a file and pass `--code-file`, or pipe via stdin
with `--code-file -`:
```bash
cat my_snippet.py | waterfree knowledge add --code-file - \
    --title "..." --description "..." --snippet-type pattern \
    --source-repo WaterFree --tag python
```

Returns JSON with the new entry id. If the code is identical to an existing
entry, `added` is `false` (deduplicated by SHA-256 of code).

### Delete a snippet
```bash
waterfree knowledge delete <entry-id>
```

### List indexed sources
```bash
waterfree knowledge list-sources
```

### Statistics
```bash
waterfree knowledge stats
```

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 2    | Usage / validation error |
| 3    | Not found (entry id) |
| 1    | Internal error |
