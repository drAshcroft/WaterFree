---
name: waterfree-index
description: Use the `waterfree index` CLI to inspect architecture, locate symbols, trace callers and callees, find god nodes / surprising couplings / import cycles, query the graph, and assess change impact before editing code.
---

# WaterFree — Codebase Index (Graph)

You have access to a codebase dependency graph via the `waterfree` CLI. It is
powered by the built-in **graphify** engine, which extracts symbols across 40+
languages via tree-sitter and stores a symbol graph (nodes =
functions/classes/modules, edges = calls/imports/inherits/defines).

Each invocation is a short shell command — run it through Bash. All commands
emit JSON to stdout.

## When to Use

- Understand an unfamiliar codebase quickly — `waterfree index architecture`
- Find where a function or class is defined — `waterfree index search-code` or `search-graph`
- Understand what calls a function (callers) or what it calls (callees) — `waterfree index trace`
- Find the core abstractions / refactor risks (most-connected symbols) — `waterfree index god-nodes`
- Spot non-obvious coupling that crosses layers or languages — `waterfree index surprising`
- Detect circular import dependencies — `waterfree index import-cycles`
- See module clusters / communities — `waterfree index clusters`
- Run a targeted graph query — `waterfree index query`
- Inspect the graph shape (labels, edge types, patterns) — `waterfree index schema`
- Assess the blast radius of a change before making it — `waterfree index detect-changes`
- Read a symbol's source code without manually navigating the file tree — `waterfree index get-snippet`
- Check if a project is already indexed — `waterfree index status`

Always prefer these tools over manual grep or file reads when exploring a codebase.

## Setup

The workspace must be indexed before most queries work. If `status` returns
`"indexed": false`, run `waterfree index build` first (takes ~10–30s for large repos).

## CLI

All commands except `list-projects` accept `--workspace <path>` (defaults to CWD).

### Check if indexed
```bash
waterfree index status --workspace /abs/path/to/project
```

### Index a workspace
```bash
waterfree index build --workspace .
```

### Architecture overview
```bash
waterfree index architecture --workspace .
```
Returns the full picture: `languages`, `entry_points`, `hotspots`, `layers`,
`clusters`, `module_graph`, `god_nodes`, `surprising_connections`,
`import_cycles`, and `adr`. This is a large payload — to pull just what you
need, use `--aspect` with a comma-separated subset:
```bash
waterfree index architecture --workspace . --aspect languages,layers
waterfree index architecture --workspace . --aspect god_nodes,import_cycles
```

### God nodes (core abstractions / refactor risks)
```bash
waterfree index god-nodes --workspace . --limit 12
```
The most-connected symbols (high in+out degree). These are the load-bearing
parts of the architecture — touch them carefully. Each entry has
`qualified_name`, `degree`, `file_path`, and `label`.

### Surprising connections
```bash
waterfree index surprising --workspace . --limit 8
```
Edges that cross community / layer / language boundaries — non-obvious coupling
worth a second look. Each entry has `source`, `target`, `relation`, `reasons`,
and a `score`.

### Import cycles
```bash
waterfree index import-cycles --workspace .
```
Circular import dependencies at the file level. Each entry is a
`{cycle: [files...], length, why}` record (shortest cycles first).

### Module clusters
```bash
waterfree index clusters --workspace .
```
Connected-component clusters of symbols — a quick map of cohesive subsystems.

### Graph query (pseudo-Cypher)
```bash
waterfree index query "MATCH (n:Function)" --workspace .
waterfree index query "MATCH (n:Class) WHERE name =~ 'Auth'" --workspace .
waterfree index query "MATCH (n:Function) CALLS ->" --workspace .
```
A lightweight pseudo-Cypher interpreter over the symbol graph. It supports a
small, fixed subset:
- `(n:Label)` — filter by node label (`Function`, `Method`, `Class`, `Module`)
- `name =~ 'regex'` or `name = 'exact'` — filter by symbol name
- include `CALLS ->` in the query to expand each matched node's outbound
  `CALLS` edges (returns `source`/`target` rows)

For richer relationship traversal use `trace` (callers/callees) rather than
`query`.

### Graph schema
```bash
waterfree index schema --workspace .
```
Node-label counts, edge-type counts, common relationship patterns, and sample
nodes — useful before writing a `query`.

### Search for a symbol
```bash
waterfree index search-code "authenticate_user" --workspace .
waterfree index search-graph "AuthService" --workspace . --node-type class
```
`--node-type` filter options: `function`, `class`, `module`, `method`.

### Get source code for a symbol
```bash
waterfree index get-snippet "auth.service.AuthService.login" --workspace .
waterfree index get-snippet "AuthService.login" --workspace . --scope neighbors
waterfree index get-snippet "AuthService.login" --workspace . --scope class
```
Partial names are auto-resolved.

`--scope` controls how much source is returned:
- `procedure` — the symbol's own body only (default)
- `neighbors` — 20 lines above and below the symbol
- `class` — the full enclosing class body (falls back to `procedure` if not inside a class)

The response includes `source_start_line` / `source_end_line` showing the exact window returned.

### Trace call paths
```bash
waterfree index trace "process_payment" --workspace . \
    --direction both --depth 3
```
`--direction` is `callers`, `callees`, or `both`.

### Detect change impact
```bash
waterfree index detect-changes --workspace . --scope all --depth 3
waterfree index detect-changes --workspace . --scope src/api/auth.py,src/api/users.py
```
`--scope all` reads the current git diff automatically.

### List all indexed projects
```bash
waterfree index list-projects
```

## Tips

- `search-graph` finds nodes by name in the graph; `search-code` does text search in source files.
- For `get-snippet`, try short names first — auto-resolve handles disambiguation.
- The graph database is at `{workspace}/.waterfree/graph.db`.

## Exit codes

| Code | Meaning |
|------|---------|
| 0    | Success |
| 2    | Usage / validation error |
| 4    | Workspace not indexed (run `waterfree index build`) |
| 1    | Internal error |
