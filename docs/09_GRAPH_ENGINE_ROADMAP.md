# Subsystem 09 — Graph Engine Roadmap
## WaterFree VS Code Extension

---

## Purpose

This document is the implementation plan for upgrading `backend/graph` into a stronger in-project replacement for the bundled reference system in `example_treesitter/codebase-memory-mcp`.

The goal is not to clone the Go project line-for-line. The goal is to reach feature parity where it matters for WaterFree:
- trustworthy indexing
- durable project persistence
- rich graph queries
- strong architectural summaries
- accurate impact analysis
- a testable path to grow beyond the current Python/TS/JS subset

---

## Baseline

### Current local implementation

The in-project graph stack currently lives in:
- `backend/graph/client.py`
- `backend/graph/engine.py`
- `backend/graph/indexer.py`
- `backend/graph/store.py`

It already provides:
- in-process indexing with SQLite persistence
- per-project graph DB in `.waterfree/graph.db`
- basic nodes for modules, functions, methods, classes
- basic edges for `CALLS`, `DEFINES`, `INHERITS`
- simple implementations of `list_projects`, `get_architecture`, `trace_call_path`, `detect_changes`, `search_graph`, `search_code`, `get_code_snippet`, `query_graph`, and `manage_adr`

### Reference capabilities from `codebase-memory-mcp`

The bundled example adds a much broader surface area:
- 35 languages
- persistent project router
- background auto-sync watcher
- project management tools like `delete_project` and `index_status`
- graph schema inspection via `get_graph_schema`
- richer search filters and pagination
- richer snippet resolution and ambiguity handling
- many more node and edge types:
  - `IMPORTS`
  - `USAGE`
  - `HTTP_CALLS`
  - `ASYNC_CALLS`
  - `HANDLES`
  - `IMPLEMENTS`
  - `OVERRIDE`
  - `FILE_CHANGES_WITH`
  - `CONTAINS_FILE`
  - `CONTAINS_FOLDER`
  - `CONTAINS_PACKAGE`
  - and several semantic edges
- broader architecture views:
  - `packages`
  - `routes`
  - `boundaries`
  - `services`
  - `file_tree`
  - community detection stronger than connected-components

---

## Working Principles

1. Correctness before breadth.
2. Tool parity before language parity.
3. Persisted project discovery before multi-project features.
4. Incremental indexing must be safe before adding more derived edges.
5. Every feature must ship with fixture-based tests and at least one regression case.

---

## Priority Summary

| Priority | Theme | Why it comes first |
|---|---|---|
| P0 | Trustworthy persistence and indexing | If the graph is stale or inconsistent, every higher-level feature lies |
| P1 | Tool parity for discovery and inspection | Needed to make the graph genuinely usable inside WaterFree |
| P2 | Rich graph semantics and architecture views | Raises the graph from basic symbol lookup to architectural reasoning |
| P3 | Cross-service analysis, history coupling, and watcher automation | High value, but depends on stable core model and query behavior |
| P4 | Language expansion and performance hardening | Important, but should sit on top of proven engine behavior |

---

## P0 — Trustworthy Core

### 1. Persistent project router and lifecycle parity

**Features**
- Discover indexed projects from disk after process restart
- Support `list_projects` without requiring prior `index()` in the same process
- Add `delete_project`
- Add `index_status`
- Make project naming stable and collision-resistant

**Implementation areas**
- `backend/graph/engine.py`
- `backend/graph/store.py`
- possibly add `backend/graph/router.py`

**Acceptance criteria**
- Restarting the backend still allows previously indexed projects to appear in `list_projects`
- `delete_project` removes graph data and file hash records cleanly
- `index_status` reports `not_indexed`, `indexing`, or `ready`
- project lookup does not depend on the first open project in memory

**How to test**
- Unit:
  - create two test workspaces, index both, close engine, reopen engine, assert both appear
  - delete one project and assert it is gone while the other remains
- Integration:
  - start backend, index workspace, stop backend, restart backend, call `list_projects`
  - call `index_status` before index, during forced index, and after completion
- Regression:
  - same folder name under different parent paths should not collide

### 2. Incremental indexing correctness

**Features**
- Remove deleted files from nodes, edges, and file hashes
- Rebuild stale edges when imports or symbol names move
- Prevent orphaned nodes and stale hash entries
- Return correct `indexed` and `up_to_date` status

**Implementation areas**
- `backend/graph/indexer.py`
- `backend/graph/engine.py`
- `backend/graph/store.py`

**Acceptance criteria**
- deleting a file removes all nodes and edges rooted in that file
- renaming a symbol updates edges pointing to the old qualified name
- re-running index with no changes produces no data drift
- index result counts stay stable across repeated no-op runs

**How to test**
- Fixture repo tests:
  - add file -> index -> verify nodes exist
  - delete file -> re-index -> verify nodes and hashes are gone
  - rename function -> re-index -> verify old QN no longer resolves
- Store integrity tests:
  - no edges reference missing node ids
  - no file_hash rows reference deleted files after re-index
- Snapshot tests:
  - store node/edge counts after first index and after no-op index

### 3. Graph schema introspection

**Features**
- Add `get_graph_schema`
- return node label counts
- return edge type counts
- return relationship patterns
- return representative sample names

**Implementation areas**
- `backend/graph/engine.py`
- `backend/graph/store.py`

**Acceptance criteria**
- a client can inspect the graph without guessing supported labels and edge types
- schema output changes predictably as new edge families are added

**How to test**
- Unit:
  - small fixture with known nodes and edges produces exact counts
- Regression:
  - schema handles empty project cleanly

### 4. Search pagination and filter contract

**Features**
- `limit`, `offset`, `total`, `has_more` for `search_graph`
- `limit`, `offset`, `total`, `has_more` for `search_code`
- support `qn_pattern`
- support `file_pattern`
- support relationship-based degree filtering accurately

**Implementation areas**
- `backend/graph/engine.py`
- `backend/graph/store.py`

**Acceptance criteria**
- paging is stable across repeated calls
- `total` reflects full match count, not page size
- `qn_pattern` and `file_pattern` can scope large projects
- degree filters use the requested relationship type

**How to test**
- Unit:
  - seed >20 nodes and verify multiple pages
  - verify `offset=10` starts at the 11th item
- Query behavior:
  - same query with and without `qn_pattern` narrows results
  - relationship `CALLS` inbound with `max_degree=0` finds dead-code candidates

---

## P1 — Usable Tool Parity

### 5. Better `get_code_snippet` resolution

**Features**
- exact QN lookup
- partial suffix lookup
- short-name ambiguity detection
- suggestion payloads instead of plain error strings
- richer metadata:
  - signature
  - docstring
  - decorators
  - caller/callee counts
  - optional neighbor names

**Implementation areas**
- `backend/graph/engine.py`
- `backend/graph/indexer.py`

**Acceptance criteria**
- ambiguous symbols do not silently pick the wrong node
- `auto_resolve` behaves predictably and returns alternatives
- neighbor counts are cheap by default and detailed names are opt-in

**How to test**
- Fixture repo:
  - duplicate function names in different modules
  - assert ambiguous response shape
  - assert partial QN suffix resolves correct node
- Unit:
  - docstring and signature extraction for Python and TS fixtures

### 6. `search_graph` feature parity for discovery

**Features**
- relevance ordering
- exclusion labels
- entry-point exclusion
- sort options
- support for broader node labels beyond current set

**Implementation areas**
- `backend/graph/engine.py`
- `backend/graph/store.py`

**Acceptance criteria**
- exact name matches rank above loose partial matches
- test/helper/entry-point filtering works for dead-code exploration
- sorting is deterministic

**How to test**
- Ranking tests:
  - exact match beats prefix match
  - prefix match beats low-signal contains match
- Filter tests:
  - excluded labels never appear
  - `exclude_entry_points` removes expected nodes

### 7. `query_graph` upgrade from regex interpreter to real query subset

**Features**
- support a defined read-only query subset
- support edge property selection
- support basic `MATCH`, `WHERE`, `RETURN`, `LIMIT`
- support relationship filtering beyond `CALLS`

**Implementation areas**
- `backend/graph/engine.py`
- potentially add `backend/graph/query.py`

**Acceptance criteria**
- clients can query edge properties like confidence or HTTP method
- behavior is documented and testable
- unsupported syntax fails clearly, not silently

**How to test**
- Parser tests for accepted grammar
- Execution tests on a seeded in-memory DB
- Regression tests for invalid syntax and unsupported clauses

### 8. Detect-changes parity

**Features**
- add `scope=branch`
- add `base_branch`
- improve risk classification
- return exact changed symbols, not just file-wide symbol dumps
- include impacted callers consistently

**Implementation areas**
- `backend/graph/engine.py`
- maybe add `backend/graph/git.py`

**Acceptance criteria**
- `unstaged`, `staged`, `all`, and `branch` behave distinctly
- `base_branch` is honored when provided
- changed symbol detection uses actual file-symbol ranges where possible

**How to test**
- Integration:
  - create temp git repo with staged and unstaged changes
  - compare results across scopes
- Regression:
  - non-git directory returns a safe empty result

---

## P2 — Rich Graph Semantics

### 9. Import and containment graph

**Features**
- add `IMPORTS`
- add `CONTAINS_FILE`
- add `CONTAINS_FOLDER`
- add `CONTAINS_PACKAGE`
- optionally split `DEFINES` and `DEFINES_METHOD`

**Implementation areas**
- `backend/graph/indexer.py`
- `backend/graph/engine.py`
- `backend/graph/store.py`

**Acceptance criteria**
- module/folder/package traversal works without source scanning
- architecture summaries can compute package and file-tree views from graph data

**How to test**
- Fixture tree with nested folders and packages
- assert containment edges and import edges exactly

### 10. Semantic edges inside a codebase

**Features**
- `USAGE`
- `IMPLEMENTS`
- `OVERRIDE`
- `USES_TYPE`
- `READS`
- `WRITES`
- `THROWS` or equivalent exception edges

**Implementation areas**
- `backend/graph/indexer.py`
- possibly add specialized passes under `backend/graph/passes/`

**Acceptance criteria**
- graph can represent callback usage, interface implementation, and common type-based relationships
- `trace_call_path` and `query_graph` can expose these edges where relevant

**How to test**
- Language fixtures:
  - Python inheritance and overrides
  - TS interface implementation
  - variable/property read-write samples
  - typed parameters and returns
- Regression:
  - semantic edge passes do not create duplicate edges on repeated indexing

### 11. Better architecture summaries

**Features**
- add `packages`
- add `routes`
- add `boundaries`
- add `services`
- add `file_tree`
- replace connected-components “clusters” with stronger community detection

**Implementation areas**
- `backend/graph/engine.py`
- `backend/graph/store.py`

**Acceptance criteria**
- `get_architecture(["all"])` returns a meaningful orientation packet
- package and boundary summaries are computed from graph data, not file-path guesses alone
- cluster output remains stable enough for snapshots

**How to test**
- Golden-file tests:
  - known repo fixture produces stable architecture JSON
- Component tests:
  - package detection
  - file tree summarization
  - cluster generation on synthetic graphs

### 12. Route and handler modeling

**Features**
- route nodes
- `HANDLES` edges from handlers to routes
- route metadata: method, path, framework

**Implementation areas**
- `backend/graph/indexer.py`
- maybe `backend/graph/routes.py`

**Acceptance criteria**
- frameworks used by WaterFree-supported languages can surface routes as first-class graph entities
- architecture view can list routes and their handlers

**How to test**
- Fixture apps:
  - Express
  - FastAPI
  - simple Flask or similar if added
- assertions:
  - route node count
  - path and method extraction
  - handler linkage

---

## P3 — Cross-Service and Operational Intelligence

### 13. HTTP and async link discovery

**Features**
- `HTTP_CALLS`
- `ASYNC_CALLS`
- confidence scoring on discovered links
- edge properties like URL path and method

**Implementation areas**
- `backend/graph/indexer.py`
- `backend/graph/engine.py`
- maybe `backend/graph/http_links.py`

**Acceptance criteria**
- call sites can be linked to known route handlers when confidence is sufficient
- query results include edge properties

**How to test**
- Multi-service fixture:
  - one service exposes routes
  - another calls them through a client
- assertions:
  - HTTP edge count
  - URL path/method properties
  - false-positive guardrails

### 14. Git history coupling

**Features**
- `FILE_CHANGES_WITH`
- co-change count
- coupling score

**Implementation areas**
- `backend/graph/engine.py`
- maybe `backend/graph/history.py`

**Acceptance criteria**
- historic co-change edges come from commit history, not current diff only
- edge properties are queryable

**How to test**
- Integration:
  - temporary git repo with scripted commits
  - assert co-change edges after history scan

### 15. Background auto-sync watcher

**Features**
- background polling or FS watcher
- adaptive debounce/backoff
- skip cycles while active indexing is in progress
- mark `index_status` during sync

**Implementation areas**
- likely new `backend/graph/watcher.py`
- `backend/server.py`

**Acceptance criteria**
- index refreshes without manual reindex after file changes
- watcher does not corrupt active requests
- watcher failures degrade gracefully and log clearly

**How to test**
- Integration:
  - modify file, wait, confirm graph updates
  - modify many files rapidly, verify coalescing
- Concurrency:
  - query during sync should still work or fail cleanly

---

## P4 — Language Expansion and Hardening

### 16. Expand language support

**Priority order**
1. Python
2. Typescript
3. C#
4. Ruby
5. Java
6. SQL/YAML/HCL/TOML/Dockerfile for infra awareness

**Implementation areas**
- `backend/graph/indexer.py`
- likely new language-specific extraction modules

**Acceptance criteria**
- each newly supported language has fixture coverage for:
  - function extraction
  - class/type extraction where applicable
  - imports/includes
  - basic call resolution

**How to test**
- One small fixture repo per language
- One regression repo with mixed languages

### 17. Performance and storage tuning

**Features**
- larger worker pools where safe
- batched inserts
- query indexes tuned for search patterns
- optional WAL and checkpoint strategy review
- benchmark harness

**Acceptance criteria**
- no major slowdown as edge families grow
- no repeated full-index work on no-op runs

**How to test**
- Benchmarks:
  - cold full index
  - warm incremental no-op
  - single-file change
  - multi-file burst
- Compare baseline before and after each optimization

---

## Cross-Cutting Test Strategy

### Test layers

1. Unit tests
   - parser helpers
   - store queries
   - ranking/pagination logic
   - architecture summarizers

2. Fixture-based indexing tests
   - small repositories checked into `tests/fixtures/graph/`
   - deterministic node/edge assertions

3. Integration tests
   - backend server request flow
   - git-aware features
   - watcher behavior

4. Golden/snapshot tests
   - `get_architecture(["all"])`
   - `get_graph_schema`
   - key `search_graph` and `trace_call_path` outputs

5. Performance tests
   - cold full index
   - warm no-op index
   - incremental changed-file index

### Recommended fixture repos

- `basic_py_ts/`
  - Python + TS mixed project
  - imports, inheritance, duplicate symbol names
- `web_routes/`
  - REST handlers and outbound HTTP client calls
- `interfaces/`
  - interface/override examples
- `git_history/`
  - scripted commit history for coupling tests
- `dead_code/`
  - entry points, helpers, unreachable code

### Integrity checks to run after every index

- every edge source and target node exists
- every node belongs to a real project
- every file-hash row maps to an existing file or is removed on reindex
- no duplicate edges for the same `(source, target, type)`
- no stale nodes remain after file deletion

---

## Recommended Delivery Order

### Milestone 1 — Make the graph trustworthy
- persistent project discovery
- `delete_project`
- `index_status`
- deleted-file cleanup
- `get_graph_schema`
- pagination contract

### Milestone 2 — Make the graph usable
- stronger `search_graph`
- stronger `get_code_snippet`
- stronger `query_graph`
- improved `detect_changes`

### Milestone 3 — Make the graph architecturally informative
- imports and containment edges
- packages, boundaries, file tree
- better clusters
- route and handler modeling

### Milestone 4 — Make the graph semantically powerful
- `USAGE`
- `IMPLEMENTS`
- `OVERRIDE`
- `USES_TYPE`
- read/write and exception edges

### Milestone 5 — Make the graph operationally intelligent
- `HTTP_CALLS`
- `ASYNC_CALLS`
- `FILE_CHANGES_WITH`
- watcher-based auto-sync

### Milestone 6 — Scale breadth and speed
- new languages
- benchmarks
- storage and query optimization

---

## Definition of Done

A roadmap item is only complete when all of the following are true:
- feature behavior is implemented in `backend/graph`
- backend surface is documented if request/response shape changed
- at least one fixture-based test exists
- at least one regression case exists for the failure mode that motivated the work
- logging is present at failure points
- repeated indexing of the same fixture is deterministic

---

## Immediate Next Step

Start with Milestone 1. It gives the highest confidence return for the rest of the roadmap, and it fixes the current risks that make the graph hard to trust:
- process-local project visibility
- stale graph data after file deletion
- missing schema visibility
- incomplete paging/filter contracts
