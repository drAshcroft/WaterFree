# Web Search Integration

## Why web search matters

The Market Research stage mandates live web research — competitive landscapes,
pricing, case studies, existing OSS libraries, ROS package audits, compliance
requirements. Without a real search tool, the persona falls back to asking the
user to run queries manually. With a search tool wired in, it can cite sources
inline and loop through refinements without leaving the editor.

---

## Option A — Native Claude web_search tool

Anthropic exposes a first-party search tool for Claude 3.5+ and Claude 4.x:

```json
{
  "type": "web_search_20250305",
  "name": "web_search"
}
```

**How it works:** The tool is declared in the `tools` array of the Anthropic
API call. When Claude calls it, Anthropic performs the search and returns
results as a `tool_result` block. No external API key is required beyond the
existing Anthropic key.

**Trade-offs:**
- No extra key to manage.
- Billed as additional tokens (results are injected into context).
- Only works with Anthropic-hosted Claude (not Ollama, OpenAI-compatible, etc.).
- Currently requires the tool be declared at the API call level, not via the
  `ToolRegistry` handler path that WaterFree uses for other tools.

**Current status:** Not wired into WaterFree yet. To add it, the
`AnthropicProvider` would need to include `{"type": "web_search_20250305"}` in
the `tools` array it sends to the API, and handle the resulting
`web_search_tool_result` block in the response parser. This is a separate
integration path from the `ToolRegistry` handler system.

**When to prefer this:** If you are on the Anthropic-native provider lane and
do not want to create a search provider account.

---

## Option B — Provider-based search (WaterFree settings)

WaterFree's `ToolRegistry` includes three provider-backed handlers for
`web_search`, `fetch_url`, and `extract_article`. These activate when
`WATERFREE_ENABLE_WEB_TOOLS=1` and a supported provider + key are configured.

### Supported providers

| Provider | Free tier | Strengths | Sign-up |
|----------|-----------|-----------|---------|
| **Brave** | 2 000 queries/month | Fast, clean JSON, domain filters, freshness | api.search.brave.com |
| **Tavily** | 1 000 queries/month | Designed for AI agents, returns content extracts | app.tavily.com |
| **Exa** | Paid (free trial) | Neural/semantic search, good for recent content | exa.ai |

### Configuration — VS Code settings (recommended)

Open **Settings** (`Ctrl+,`) and search for `WaterFree web`:

```
waterfree.webSearch.provider  →  brave | tavily | exa | none (default)
waterfree.webSearch.apiKey    →  your API key
```

WaterFree passes these as `WATERFREE_WEB_SEARCH_PROVIDER` and
`WATERFREE_WEB_SEARCH_API_KEY` when it starts the Python backend.

### Configuration — environment variables

Set these in your shell profile (`.bashrc`, `.zshrc`, `$PROFILE` on Windows):

```bash
export WATERFREE_ENABLE_WEB_TOOLS=1
export WATERFREE_WEB_SEARCH_PROVIDER=brave
export WATERFREE_WEB_SEARCH_API_KEY=your-key-here
```

Environment variables take precedence over VS Code settings for the API key
(the key in `waterfree.webSearch.apiKey` is plaintext in VS Code's JSON; env
vars are preferable for secrets).

### What gets registered

When enabled, three tools appear in the `waterfree-web` server category:

| Tool | Description |
|------|-------------|
| `web_search` | Search with query, optional domain list, optional freshness, optional limit |
| `fetch_url` | Fetch raw content from any URL (returns first 50 000 chars) |
| `extract_article` | Fetch a URL and return plain text, HTML stripped (returns first 20 000 chars) |

All three have `requires_approval: true` and `requires_network: true` in their
policy — the UI will prompt before use if approval gates are enforced.

---

## Option C — External MCP servers

MCP servers run as separate processes and expose tools through the Model
Context Protocol. They are independent of WaterFree's internal `ToolRegistry`
and are visible to any MCP-capable client (Claude Code CLI, Cursor, Zed, etc.)
running in the same environment.

### Available web search MCP servers

| Server | Install | Provider |
|--------|---------|----------|
| `@modelcontextprotocol/server-brave-search` | `npx @modelcontextprotocol/server-brave-search` | Brave |
| `@tavily-ai/tavily-mcp` | `npx @tavily-ai/tavily-mcp` | Tavily |
| `exa-mcp-server` | `npx exa-mcp-server` | Exa |
| `@modelcontextprotocol/server-fetch` | `npx @modelcontextprotocol/server-fetch` | HTTP fetch only, no index |
| `perplexity-mcp` | varies by package | Perplexity |

### Registering with Claude Code

```bash
# Brave example
claude mcp add brave-search \
  -e BRAVE_API_KEY=your-key \
  npx -- @modelcontextprotocol/server-brave-search

# Tavily example
claude mcp add tavily \
  -e TAVILY_API_KEY=your-key \
  npx -- @tavily-ai/tavily-mcp
```

After registration the tools appear in Claude Code's tool list and are
available to any Claude Code session or skill invocation.

### Limitation

MCP servers registered this way are **not** visible inside WaterFree's Python
backend agent loop. They are usable in Claude Code chat and Claude Code skills,
but the Market Research persona running inside the WaterFree deep agents runtime
uses the `ToolRegistry` path (Option B), not the MCP client path.

To make an MCP server visible to WaterFree's backend agent, a `mcp_web.py`
server would need to be added to the WaterFree backend following the existing
`mcp_index.py` / `mcp_todos.py` pattern, and a new method added to `server.py`
to invoke it.

---

## Decision guide

| Situation | Recommended path |
|-----------|-----------------|
| Anthropic-only, no extra accounts | Wait for native tool integration (Option A roadmap) |
| Want Market Research to work now, Brave account | Option B, `waterfree.webSearch.provider = brave` |
| Want Market Research to work now, Tavily account | Option B, `waterfree.webSearch.provider = tavily` |
| Using Claude Code CLI for market research outside WaterFree | Option C, register MCP server with `claude mcp add` |
| Want both WaterFree agent and Claude Code to search | Option B + Option C (both can coexist) |

---

## Adding a new provider

To add a provider (e.g., SerpAPI, Perplexity):

1. Add a handler function in `backend/llm/tools/web_tools.py` following the
   `_brave_handler` or `_tavily_handler` pattern.
2. Add the new provider name to the `if/elif` chain in `web_tool_descriptors`.
3. Add it to the `enum` and `enumDescriptions` in `package.json`
   (`waterfree.webSearch.provider`).

No other files need to change.

---

## Env var reference

| Variable | Values | Effect |
|----------|--------|--------|
| `WATERFREE_ENABLE_WEB_TOOLS` | `1` or `true` | Register web tools in the registry |
| `WATERFREE_WEB_SEARCH_PROVIDER` | `brave` \| `tavily` \| `exa` | Select the search backend |
| `WATERFREE_WEB_SEARCH_API_KEY` | your key | Authenticate with the provider |
