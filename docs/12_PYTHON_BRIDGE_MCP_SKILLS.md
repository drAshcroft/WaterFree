# Subsystem 12 — Python Bridge MCP and Skills Gateway
## WaterFree VS Code Extension

---

## Purpose

`PythonBridge` is currently a thin stdio JSON-RPC transport between the VS Code extension and the Python backend. That is the right transport, but it is too narrow for the next stage of WaterFree.

The bridge needs to become the control plane for:

- runtime selection
- MCP server discovery and connection
- tool execution and policy feedback
- skill discovery and loading
- subagent delegation
- filesystem harness operations
- checkpoint and resume flows

The bridge should remain thin in implementation and rich in contract.

---

## Current State

Today the bridge mainly does three things:

1. starts the backend process
2. sends newline-delimited JSON-RPC requests
3. receives responses and notifications

That is enough for the current single-runtime model, but not enough for multi-provider agents with optional MCP servers and first-class skills.

---

## Design Rule

Do not turn the VS Code extension into the tool orchestrator. The Python backend should own tool loading, MCP lifecycle, skill resolution, and policy enforcement. The bridge should expose those capabilities cleanly to the UI.

---

## Required Capabilities

### 1. Runtime awareness

The UI needs to know what execution backends exist and which one is active.

```typescript
type RuntimeInfo = {
  id: string;
  label: string;
  provider: "anthropic" | "ollama" | "openai" | "deep_agents" | "custom";
  local: boolean;
  supportsTools: boolean;
  supportsSkills: boolean;
  supportsCheckpoints: boolean;
};
```

### 2. MCP awareness

The UI needs to inspect connected MCP servers and the tools they expose.

```typescript
type McpServerInfo = {
  id: string;
  label: string;
  transport: "stdio" | "streamable_http";
  enabled: boolean;
  optional: boolean;
  capabilities: string[];
};

type McpToolInfo = {
  serverId: string;
  name: string;
  title: string;
  readOnly: boolean;
  requiresNetwork: boolean;
  requiresApproval: boolean;
};
```

### 3. Skill awareness

Skills should be visible to the system as first-class runtime inputs.

```typescript
type SkillInfo = {
  id: string;
  title: string;
  description: string;
  path: string;
  appliesTo: string[];
  hasScripts: boolean;
  hasReferences: boolean;
};
```

---

## Proposed JSON-RPC Surface

### Runtime methods

- `listRuntimes() -> { runtimes: RuntimeInfo[] }`
- `getActiveRuntime() -> { runtimeId: string }`
- `setActiveRuntime({ runtimeId }) -> { ok: boolean }`

### MCP methods

- `listMcpServers() -> { servers: McpServerInfo[] }`
- `connectMcpServer({ serverId }) -> { ok: boolean }`
- `disconnectMcpServer({ serverId }) -> { ok: boolean }`
- `listMcpTools({ serverId? }) -> { tools: McpToolInfo[] }`
- `invokeMcpTool({ serverId, toolName, args }) -> { result: unknown }`

### Skill methods

- `listSkills({ persona?, stage? }) -> { skills: SkillInfo[] }`
- `reloadSkills() -> { ok: boolean; count: number }`
- `getSkillDetail({ skillId }) -> { markdown: string; references: string[]; scripts: string[] }`

### Checkpoint methods

- `listCheckpoints({ sessionId }) -> { checkpoints: CheckpointInfo[] }`
- `resumeCheckpoint({ checkpointId, decision }) -> { ok: boolean }`
- `discardCheckpoint({ checkpointId }) -> { ok: boolean }`

### Subagent methods

- `listSubagents() -> { subagents: SubagentInfo[] }`
- `delegateToSubagent({ sessionId, subagentId, taskId, prompt }) -> { checkpointId?: string; result?: object }`

### Filesystem harness methods

- `readWorkspaceFile({ path }) -> { content: string }`
- `applyWorkspacePatch({ edits }) -> { ok: boolean; touchedFiles: string[] }`
- `listWorkspacePaths({ glob, limit }) -> { paths: string[] }`

The extension does not need every one of these on day one, but the bridge contract should be designed with them in mind now.

---

## Backend Responsibilities

The Python backend should own:

- runtime registry
- provider selection
- MCP client lifecycle
- skill loading and progressive disclosure
- tool metadata and approval policy
- checkpoint persistence
- audit logging for tool calls

The TypeScript extension should own:

- presentation
- user intent capture
- editor diagnostics
- file open/navigation
- checkpoint approval UI

---

## MCP Tool Categories

### Built-in local MCPs

- index / graph
- todos / backlog
- knowledge store
- debug snapshot inspection

### Optional external MCPs

- web search
- document retrieval
- issue tracker integration
- deployment / observability tools

### Policy

External MCPs should be optional, visible, and revocable. Networked tools should advertise that they are networked and should be eligible for approval gates.

---

## Web Search and Retrieval

Web search should not be baked into the core bridge as a hidden behavior. It should appear as an optional MCP capability.

### Suggested split

- `web_search(query, freshness, domains, limit)`
- `fetch_url(url, mode)`
- `extract_article(url)`

### Why split it

- search and fetch have different failure modes
- policies differ for each
- caching is easier
- attribution is easier

### Bridge behavior

The bridge should be able to report:

- whether web search is available
- which MCP server provides it
- whether a request used network access
- whether the result was cached

---

## Ollama Lane

Ollama should be treated as a local provider for high-volume, low-cost workloads.

### Good first workloads

- snippet triage
- procedure summarization drafts
- tag generation
- local embeddings
- reranking
- repo-scale knowledge extraction

### Bad early workloads

- high-stakes architectural synthesis
- difficult multi-hop planning with weak local models
- final user-facing explanations that need strong judgment

### Bridge requirement

The bridge should surface local provider availability and model names so the UI and backend can route work intelligently.

---

## Skills and MCP Together

Skills should influence tool usage, not just prompt text.

### Example

The `waterfree-debug` skill should tell the runtime:

- prefer `mcp_debug` tools first
- inspect execution context before variables
- avoid broad variable dumps

The bridge does not need to understand that logic in detail, but it needs to carry enough metadata for the backend to make the right call.

---

## Filesystem Harness

If WaterFree adopts filesystem-capable agents, the bridge contract must make approval and scope visible.

### Required metadata

- read vs write
- touched paths
- creates vs modifies vs deletes
- workspace-local vs external path
- approval required

### Rule

No hidden file writes behind a generic `executeTask` call once filesystem-capable subagents exist.

---

## Checkpoint Protocol

Checkpointing is how WaterFree preserves collaboration while gaining more powerful agent tooling.

### Minimum checkpoint payload

```typescript
type CheckpointInfo = {
  id: string;
  sessionId: string;
  reason: string;
  createdAt: string;
  runtimeId: string;
  subagentId?: string;
  requiresApproval: boolean;
  summary: string;
  touchedFiles: string[];
  toolCalls: Array<{ serverId: string; toolName: string }>;
};
```

### Expected UI behavior

- show the summary
- show tool calls
- show touched files
- allow approve, alter, or reject
- allow resume after editor restart

---

## Rollout Plan

### Phase 1

- document the bridge contract
- add runtime and skill listing methods

### Phase 2

- add MCP server inventory methods
- expose tool metadata and approval flags

### Phase 3

- add checkpoint listing and resume
- connect the sidebar to checkpoint state

### Phase 4

- add subagent delegation
- add filesystem harness methods

### Phase 5

- add optional web-search and retrieval MCP support
- route knowledge extraction through Ollama where appropriate

---

## Bottom Line

`PythonBridge` should stay simple as transport and become stronger as a contract.

That means:

1. Python owns runtime, tools, skills, and checkpoint policy
2. the bridge exposes those capabilities cleanly to the extension
3. optional MCPs stay optional
4. Ollama is added as a local workload lane
5. WaterFree keeps human approval visible even as agent capability grows
