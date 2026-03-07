---
name: waterfree-knowledge
description: Use the WaterFree knowledge MCP tools to search shared code snippets, patterns, utilities, and conventions before writing new boilerplate or reaching for external docs.
---

# PairProgram — Knowledge / Snippet Store Tools

You have access to a global cross-project knowledge store via the `pairprogram-knowledge`
MCP server. It contains code snippets, patterns, utilities, and conventions extracted
from indexed repositories using LLM classification.

Knowledge is shared across all workspaces and stored at `~/.waterfree/global/knowledge.db`.

## When to Use

Use these tools when you need to:
- Find a reusable pattern before writing new code — use `search_knowledge`
- Look for prior implementations of a concept across projects — use `search_knowledge`
- Check what has already been indexed — use `list_knowledge_sources`
- Understand how many snippets are available — use `knowledge_stats`

Always search the knowledge store before writing boilerplate or reaching for external docs.

## Snippet Types

The store categorises entries into:
- `pattern` — design patterns and architectural patterns
- `utility` — helper functions and utility classes
- `style` — code style and formatting conventions
- `api_usage` — example usages of libraries/frameworks
- `convention` — naming, structure, and project conventions

## Tools

### Search for snippets
```
search_knowledge(query="retry with exponential backoff", limit=10)
search_knowledge(query="authentication middleware")
search_knowledge(query="sqlite connection pooling")
search_knowledge(query="dataclass serialisation")
```

The search uses BM25-ranked full-text search over title, description, tags, and code.
Return fields: `id`, `title`, `description`, `snippet_type`, `code`, `tags`,
`source_repo`, `source_file`, `source_repo_url`, `created_at`.

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

## Tips

- Search with specific terms ("singleton pattern", "JWT decode") or broad concepts ("caching").
- Use `tags` in the returned entries to discover related searches.
- If the store is empty, knowledge must be built first using the `buildKnowledge` command
  in the PairProgram VS Code extension, or by running the knowledge extractor directly.
