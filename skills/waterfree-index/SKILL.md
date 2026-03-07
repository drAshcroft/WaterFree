---
name: waterfree-index
description: Use the WaterFree codebase index MCP tools to inspect architecture, locate symbols, trace callers and callees, and assess change impact before editing code.
---

# WaterFree — Codebase Index (Graph) Tools

You have access to a codebase dependency graph via the `waterfree-index` MCP server.
It indexes Python and TypeScript/JavaScript projects using AST parsing and stores a
symbol graph (nodes = functions/classes/modules, edges = calls/imports/inherits).

## When to Use

Use these tools when you need to:
- Understand an unfamiliar codebase quickly — use `get_architecture`
- Find where a function or class is defined — use `search_code` or `search_graph`
- Understand what calls a function (callers) or what it calls (callees) — use `trace_call_path`
- Assess the blast radius of a change before making it — use `detect_changes`
- Read a symbol's source code without manually navigating the file tree — use `get_code_snippet`
- Check if a project is already indexed — use `index_status`

Always prefer these tools over manual grep or file reads when exploring codebase.

## Setup

The workspace must be indexed before most queries work. If `index_status` returns
`"indexed": false`, call `index_workspace` first (takes ~10-30 seconds for large repos).

## Tools

### Check if indexed
```
index_status(workspace_path="/absolute/path/to/project")
```

### Index a workspace
```
index_workspace(workspace_path="/absolute/path/to/project")
```

### Architecture overview
```
get_architecture(workspace_path="/absolute/path/to/project")
```
Returns high-level modules, layers, and key entry points.

### Search for a symbol
```
search_code(workspace_path="/absolute/path/to/project", query="authenticate_user")
search_graph(workspace_path="/absolute/path/to/project", query="AuthService", node_type="class")
```
`node_type` filter options: `function`, `class`, `module`, `method`

### Get source code for a symbol
```
get_code_snippet(workspace_path="/absolute/path/to/project", qualified_name="auth.service.AuthService.login")
get_code_snippet(workspace_path="...", qualified_name="AuthService.login", scope="neighbors")
get_code_snippet(workspace_path="...", qualified_name="AuthService.login", scope="class")
```
Partial names are auto-resolved.

`scope` controls how much source is returned:
- `"procedure"` — the symbol's own body only (default)
- `"neighbors"` — 20 lines above and below the symbol
- `"class"` — the full enclosing class body (falls back to `procedure` if not inside a class)

The response includes `source_start_line` / `source_end_line` showing the exact window returned.

### Trace call paths
```
trace_call_path(
    workspace_path="/absolute/path/to/project",
    function_name="process_payment",
    direction="both",   # "callers" | "callees" | "both"
    depth=3
)
```

### Detect change impact
```
detect_changes(
    workspace_path="/absolute/path/to/project",
    scope="all",   # "all" = git diff, or comma-separated file paths
    depth=3
)
```

### List all indexed projects
```
list_projects()
```

## Tips

- `search_graph` finds nodes by name in the graph; `search_code` does text search in source files.
- For `get_code_snippet`, try short names first — auto-resolve handles disambiguation.
- `detect_changes` with `scope="all"` reads the current git diff automatically.
- The graph database is at `{workspace}/.waterfree/graph.db`.
