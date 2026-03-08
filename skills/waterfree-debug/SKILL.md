---
name: waterfree-debug
description: Use the WaterFree live debug MCP tools to inspect a paused VS Code debugging snapshot, starting from status and execution context before drilling into variables.
---

# WaterFree — Live Debug MCP Tools

Provides lazy, context-safe access to live debugger state captured from the VS Code debugger.

## How it works

1. User sets a breakpoint and pauses execution in VS Code
2. User opens the WaterFree sidebar -> fills "What do you want to investigate?" + stop reason -> clicks **Push to Agent**
3. Extension writes `.waterfree/debug/snapshot.json` with the full debug state + intent
4. Agent queries the snapshot progressively using the tools below

## Tools

### `mcp__waterfree-debug__debug_status`

Check if a snapshot exists before doing anything else.

```
debug_status(workspace_path: str)
-> {active, capturedAt, stale, intent, stopReason, location:{file,line,qualifiedName}, scopeNames}
```

### `mcp__waterfree-debug__get_execution_context`

Get the full execution context: user's investigation goal, where execution stopped (with qualified class.method name), call stack hierarchy, and a +/-5 line code snippet.

```
get_execution_context(workspace_path: str)
-> {intent, stopReason, capturedAt, location:{file,line,qualifiedName},
   callStack:[{frame,file,line}], codeSnippet, exceptionMessage}
```

### `mcp__waterfree-debug__list_variables`

List variable names and types only — **no values**. Use this first to see what's available without blowing context.

```
list_variables(workspace_path: str, scope: str = "")
-> {scopes: {scopeName: [{name, type, isExpandable}]}}
```

`isExpandable: true` means it's a complex object/array — use `get_variable_schema` next.

### `mcp__waterfree-debug__get_variable_schema`

Get structure/shape of a variable without loading all values.

```
get_variable_schema(workspace_path: str, var_name: str, scope: str = "locals")
-> {name, type, structure}
```

Structure by type:
- **array/list/tuple** -> `{kind:"array", length, elementType}`
- **dict/object** -> `{kind:"object", keys:[...]}`
- **DataFrame/ndarray** -> `{kind:"table", shape}`
- **primitive** -> `{kind:"primitive", type, preview}`

### `mcp__waterfree-debug__get_variable_value`

Fetch actual variable values. Type-aware and chunked to keep responses small.

```
get_variable_value(workspace_path: str, var_name: str, scope: str = "locals",
                   path: str = "", start: int = 0, end: int = 50)
```

Returns:
- **arrays** -> `{type:"array", total, items:[...sliced], range}`
- **objects** -> `{type:"object", keyCount, keys:[...], preview:{first 10}}`
- **tables** -> `{type:"table", rawPreview}`
- **primitives** -> `{type:"primitive", value}`

Navigate nested structures with `path`: `"users[0].address.city"`

Paginate arrays with `start`/`end`: e.g. `start=50, end=100` for the next page.

## Recommended query pattern

```
1. debug_status(workspace_path)          # Confirm snapshot exists + get intent
2. get_execution_context(workspace_path) # Understand WHERE and WHY
3. list_variables(workspace_path)        # Survey available variables
4. get_variable_schema(workspace_path, "interestingVar")  # Understand shape
5. get_variable_value(workspace_path, "interestingVar", start=0, end=20)  # Get data
```

Never request all variables at once — always start with `list_variables` and drill down.

## workspace_path

Always pass the absolute path to the project root, e.g. `c:\Projects\MyApp`.
The snapshot is stored at `{workspace_path}.waterfree\debug\snapshot.json`.
