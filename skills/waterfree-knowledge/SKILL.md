---
name: waterfree-knowledge
description: Use the `waterfree knowledge` CLI to search shared code snippets, patterns, utilities, and conventions before writing new boilerplate or reaching for external docs.
---

# WaterFree — Knowledge / Snippet Store

You have access to a global knowledge store via the `waterfree` CLI. Knowledge
is shared across all workspaces and stored at `~/.waterfree/global/knowledge.db`.

Each invocation is a short shell command — run it in whatever shell you have
(Bash or PowerShell). `waterfree` is on PATH, so the command text is identical
in both; every example below is a single line so nothing depends on
shell-specific line continuations. All commands emit JSON to stdout.

## When to Use (Read)

- Traverse the store by stable subject/category before searching — `waterfree knowledge browse`
- Find a reusable pattern before writing new code — `waterfree knowledge search`
- Look for prior implementations of a concept across projects — `waterfree knowledge search`
- Read one entry back by id — `waterfree knowledge get <id>`
- Check what has already been indexed — `waterfree knowledge list-sources`
- Understand how many snippets are available — `waterfree knowledge stats`

Use the hierarchy first for broad domains and search for precise lookups.
Always consult the knowledge store before writing boilerplate or reaching for
external docs. User preferences and style guides should be pushed to this store.

Search is **global across every source repo**: a query from one project can
return another project's entry first. Pass distinctive terms, and check
`source_repo` on each hit.

## When to Add

Use `waterfree knowledge add` when you discover something worth preserving for
future sessions across **any** project. Good candidates:

- **Reusable utility** you just wrote — `--snippet-type utility`
- **Convention** that was agreed upon — `--snippet-type convention`
- **Non-obvious problem you solved** — `--snippet-type pattern`
- **Correct library / API usage** with quirks or gotchas — `--snippet-type api_usage`
- **A lesson, decision or gate policy with no code** — omit `--code` entirely
  (see "Prose-only entries" below)
- **About-to-write boilerplate that already lives elsewhere** — add the existing version so the next session can find it
- **API preferences** the user tends to reuse
- **Coding styles** the user prefers

**Before adding:** run `waterfree knowledge search` first to avoid duplicates.
`add` also runs a quality gate and reports `warnings` (never blocks): a
`near_duplicate` when an existing entry contains every word of the new title
(revise it with `update <id>` instead), `no_hierarchy` when no
`--hierarchy-path` was given, and `reads_like_a_trace` when the description is
a log of what you did rather than an instruction. Write entries as
"do X when Y, because Z"; that is what transfers to the next session.
`--no-check` skips the gate for bulk imports.

### What makes a good entry

| Flag | Guidance |
|------|----------|
| `--title` | Specific and searchable — include the pattern name or key concept |
| `--description` | 2–4 sentences: what it does, why it's useful, when to reach for it |
| `--context` | Caveats, version requirements, related files/symbols, when NOT to use |
| `--snippet-type` | One of `pattern`, `utility`, `style`, `api_usage`, `convention`. Required when code is given; defaults to `convention` for a prose-only entry |
| `--hierarchy-path` | Stable subject taxonomy such as `platform/auth/jwt` or `frontend/forms/validation` |
| `--source-repo` | Actual project name or path — e.g. `WaterFree` or `c:/projects/myapp` |
| `--tag` | Repeatable. 3–6 short tags covering language, framework, domain, key concept |

## When to Update

Use `waterfree knowledge update <id>` when an entry is right but incomplete or
slightly stale: a recipe changed, a caveat was discovered, a better tag set
exists. The id is kept (so anything citing it still resolves), `revision` is
bumped and `updated_at` stamped; only the fields you pass change. Prefer this
over add-then-delete, which changes the id and leaves two copies if either
half fails.

## Reminder hooks (opt-in, Claude Code and Codex)

Three small hooks keep the store in the loop without an agent having to
remember it: `hook-session` (SessionStart: one orientation line), `hook-context`
(UserPromptSubmit: up to three rows when an entry matches every key term of
the prompt) and `hook-stop` (Stop: when the final message describes several
attempts, a workaround or a root cause, ask once to file the insight). See
`hooks/README.md`; install with `install_claude.ps1 -InstallHooks` or
`install_codex.ps1 -InstallHooks` after the backend on PATH is current.

## Consolidation (run monthly, or when search feels noisy)

```bash
waterfree knowledge consolidate                 # report: duplicate groups with merge proposals, relative dates, stale entries
waterfree knowledge consolidate --no-llm        # same without a local model (no merge proposals)
waterfree knowledge consolidate --apply         # perform the merges and date rewrites
waterfree knowledge consolidate --window 60 --min-age 14 --threshold 0.4 --report-file report.json
```

An append-only store decays into a diary: the same lesson filed three times,
"yesterday" that no longer means anything, entries nobody retrieves. The pass
reports four things and applies two of them on `--apply`:

- **duplicate groups** (titles overlapping ≥ threshold, or title+description
  within one hierarchy): a local Ollama model (stage `knowledge_consolidate`,
  default `freehuntx/qwen3-coder:14b`) proposes one merged entry per group;
  `--apply` updates the **oldest id** with it and deletes the rest, so citations
  keep resolving. Groups larger than `--max-group-size` (default 5) are
  reported, never merged: that many "duplicates" is a template.
- **relative dates** ("yesterday", "last week", "two days ago"): rewritten to
  absolute dates anchored on the entry's creation date; applied on `--apply`.
- **never retrieved**: entries older than `--min-age` (30 days) with no search,
  browse, get or prompt-injection hit in the usage log for `--window` (90 days).
  Listed for a human; never deleted automatically.
- **no hierarchy path**: entries that browse cannot place.

Asset-catalog rows (`assets/*`) are skipped unless `--include-assets`.
Review the report before `--apply`; a bad merge proposal can be fixed with
`knowledge update <surviving-id>`.

## When to Delete

Use `waterfree knowledge delete` when:
- An entry is factually incorrect or misleading and cannot be repaired with `update`.
- A pattern was superseded by a better approach and the old entry would cause confusion.
- An entry was added by mistake.

## CLI

The knowledge store is global. `--workspace` is accepted as a compatibility flag
for agent muscle memory, but it does not change the global store location.

### Search
```bash
waterfree knowledge search "retry with exponential backoff" --limit 10
waterfree knowledge search "authentication middleware"
waterfree knowledge search "sqlite connection pooling"
```

Full-text search over title, description, tags, and code. Entries containing
**every** query word rank first; any-word matches follow, ordered by how many
of your words their title/description/tags contain. Entries from the current
workspace's own repo come before other projects' within each tier (pass
`--workspace` so the preference is right); `--repo NAME` restricts to one repo.

Every entry has a **scope**: `global` (any project may see it; the default),
`project` (only searches run from its own source repo see it; use for lessons
that only make sense inside one codebase) or `assets` (the owned-asset
catalog; automatic for `assets/*` paths). Search's default scope is global +
the current workspace's own project entries. Pass `--scope all` to see
everything, or `global` / `project` / `assets` for exactly one. Set it on
`add --scope project` or change it with `update <id> --scope ...`.

Returns `{"entries": [...], "total": N, "preferred_repo": "...", "scope": "...", "hint"?: "..."}`.
Default rows are summaries: `id`, `title`, `description` (clipped), `snippet_type`,
`source_repo`, `source_file`, `tags`, `hierarchy_path`, `code_chars`,
`context_chars`, `revision`. Add `--full` to include `code` and `context`, or
call `get <id>` for the one entry you chose — that keeps a search at a few
hundred bytes per row instead of several KB.

### Get one entry
```bash
waterfree knowledge get <entry-id>
```

Returns the full entry as JSON (same shape as a search hit). Exit code 3 when
the id is unknown.

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
waterfree knowledge add --title "Exponential backoff retry decorator" --description "Retries a function up to N times with exponential backoff." --code 'def retry(max_attempts=3): ...' --snippet-type utility --source-repo WaterFree --source-file backend/llm/claude_client.py --hierarchy-path platform/reliability/retries --tag python --tag retry --tag error-handling --tag decorator --context "Requires Python 3.10+. Not suitable for DB transactions."
```

For multi-line code, write it to a file and pass `--code-file <path>`:
```bash
waterfree knowledge add --code-file my_snippet.py --title "..." --description "..." --snippet-type pattern --source-repo WaterFree --tag python
```

You can also stream code via stdin with `--code-file -` and your shell's reader:
`cat my_snippet.py | waterfree knowledge add --code-file - …` in Bash, or
`Get-Content my_snippet.py | waterfree knowledge add --code-file - …` in PowerShell.

Returns JSON with the new entry id. If the code is identical to an existing
entry, `added` is `false` (deduplicated by SHA-256 of code).

### Prose-only entries (lessons, decisions, conventions)

Leave out `--code` and `--code-file` altogether. Put the lesson in
`--description` (what / why) and `--context` (caveats, when not to apply).
`--snippet-type` defaults to `convention`; the stored `code` is empty rather
than filler.

```bash
waterfree knowledge add --title "Seed-if-empty ships new bundled content to nobody" --description "A seed-on-first-run gate keyed on an empty directory means resources bundled later never reach existing installs. Track what has been offered in a marker file instead." --context "Applies to personas, skills, templates, any bundled resource." --source-repo WaterFree --hierarchy-path platform/persistence/seeding --tag seeding --tag migration
```

Prose entries are deduplicated on title + description + context, so two
different lessons never collide.

### Update a snippet
```bash
waterfree knowledge update <entry-id> --description "..." --context "..."
waterfree knowledge update <entry-id> --code-file revised.py
waterfree knowledge update <entry-id> --tag python --tag retry   # replaces the whole tag list
waterfree knowledge update <entry-id> --title "..." --hierarchy-path platform/auth/jwt
```

Accepts any of `--title`, `--description`, `--code` / `--code-file`,
`--snippet-type`, `--tag` (repeatable; replaces the list), `--context`,
`--source-repo`, `--source-file`, `--source-repo-url`, `--hierarchy-path`.
Returns the revised entry plus `updated` (the field names changed) and
`revision`. Exit code 3 for an unknown id; exit code 2 when no field was passed
or when the revised content would duplicate another entry (the entry is left
unchanged).

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
