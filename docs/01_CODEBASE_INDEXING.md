# Subsystem 01 — Codebase Indexing
## WaterFree VS Code Extension

---

## Purpose

Before any session can begin, both the human and the AI must have a shared, accurate understanding of the codebase. This subsystem builds and maintains that understanding. It is the foundation everything else depends on.

The index must answer three categories of question:
1. **Structural:** What files exist, what do they export, what do they import?
2. **Semantic:** What does this code *do*? What is its intent?
3. **Relational:** What depends on what? If I change X, what else is affected?

---

## Components

### 1. TreeSitterParser.ts

Responsible for parsing every file in the workspace into an AST and extracting structured facts.

**What it extracts per file:**
- All function/method definitions (name, signature, line range, docstring if present)
- All class definitions (name, methods, properties, inheritance)
- All imports/requires (what is imported, from where)
- All exports (what is exposed)
- All call sites (function A calls function B at line N)
- All type annotations (TypeScript, JSDoc, Python type hints)
- Top-level variable declarations
- TODO/FIXME/HACK comments and their line numbers

**Supported languages (v1):**
- TypeScript / JavaScript
- Python
- Rust
- Go
- Ruby
- CSS / SCSS (selector extraction only)

**Tree-sitter grammars to bundle:**
```
tree-sitter-typescript
tree-sitter-javascript
tree-sitter-python
tree-sitter-rust
tree-sitter-go
tree-sitter-ruby
```

**Output schema per file:**
```typescript
interface ParsedFile {
  path: string;                    // relative workspace path
  language: string;
  lastModified: number;            // unix timestamp
  functions: FunctionDef[];
  classes: ClassDef[];
  imports: ImportDef[];
  exports: ExportDef[];
  callSites: CallSite[];
  todos: TodoComment[];
  lineCount: number;
  hash: string;                    // SHA256 of file content, for cache invalidation
}

interface FunctionDef {
  name: string;
  signature: string;               // full signature string
  startLine: number;
  endLine: number;
  docstring?: string;
  isAsync: boolean;
  isExported: boolean;
  parameters: ParameterDef[];
  returnType?: string;
}

interface ImportDef {
  source: string;                  // module path
  specifiers: string[];            // what was imported
  isRelative: boolean;
  resolvedPath?: string;           // absolute path if resolvable
}

interface CallSite {
  callerFunction: string;
  calleeFunction: string;
  calleeModule?: string;           // if imported
  line: number;
}
```

**Performance requirements:**
- Initial full index of 10,000 file repo: under 30 seconds
- Incremental re-index of changed file: under 200ms
- Run in a Worker thread — must not block the extension host

---

### 2. EmbeddingEngine.ts

Generates semantic vector embeddings for each function/class so that the AI can retrieve relevant code by meaning, not just by name.

**Strategy:**
- Embed each function individually (not whole files) to keep vectors focused
- Text to embed: `[filename] [function signature] [docstring] [first 10 lines of body]`
- Store embeddings in `.waterfree/embeddings.bin` (binary format for speed)
- Use cosine similarity for retrieval

**Embedding model options (in priority order):**
1. **Local:** `Xenova/all-MiniLM-L6-v2` via `@xenova/transformers` — runs entirely in Node.js, no API key, 384 dimensions, fast
2. **Remote:** Voyage AI `voyage-code-2` — better quality for code, requires API key, user configurable
3. **Fallback:** Simple TF-IDF if neither is available (lower quality but always works)

**API:**
```typescript
class EmbeddingEngine {
  async embed(text: string): Promise<Float32Array>
  async search(query: string, topK: number): Promise<SearchResult[]>
  async indexFunction(fn: FunctionDef, filePath: string): Promise<void>
  async invalidate(filePath: string): Promise<void>
}

interface SearchResult {
  filePath: string;
  functionName: string;
  score: number;                   // cosine similarity 0-1
  startLine: number;
  snippet: string;                 // first 5 lines
}
```

**When to re-embed:**
- File hash changes (detected by TreeSitterParser)
- Never re-embed unchanged files (embeddings are expensive)

---

### 3. CodeGraph.ts

Builds a directed graph of dependencies across the codebase. This is what powers the side effect detection in Subsystem 05.

**Graph structure:**
- **Nodes:** Each function and class is a node
- **Edges:** 
  - `CALLS` — function A calls function B
  - `IMPORTS` — module A imports from module B  
  - `EXTENDS` — class A extends class B
  - `IMPLEMENTS` — class A implements interface B
  - `USES_TYPE` — function A uses type B in signature

**Storage:** Adjacency list in `.waterfree/graph.json`

**Key queries the graph must support:**
```typescript
class CodeGraph {
  // What does this function directly call?
  getDirectDependencies(functionName: string, filePath: string): GraphNode[]
  
  // What calls this function? (reverse lookup)
  getCallers(functionName: string, filePath: string): GraphNode[]
  
  // Full downstream impact — everything affected if this changes
  getImpactRadius(functionName: string, filePath: string, maxDepth?: number): GraphNode[]
  
  // Everything needed to understand this function
  getUpstreamContext(functionName: string, filePath: string): GraphNode[]
  
  // Is there a path between two nodes?
  pathExists(from: GraphNode, to: GraphNode): boolean
  
  // Find all functions that touch a given file
  getFunctionsInFile(filePath: string): GraphNode[]
}
```

---

## IndexManager.ts — Orchestrator

Coordinates the three components above. This is what the rest of the extension talks to.

```typescript
class IndexManager {
  // Run full index on workspace open
  async initialIndex(workspaceRoot: string): Promise<void>
  
  // Incremental update when a file changes
  async updateFile(filePath: string): Promise<void>
  
  // Delete a file from the index
  async removeFile(filePath: string): Promise<void>
  
  // Get a human-readable summary of the index (sent to AI in planning prompt)
  getIndexSummary(): IndexSummary
  
  // Get focused context for a specific task
  getTaskContext(taskDescription: string, targetFile: string): TaskContext
  
  // Get everything relevant to a function
  getFunctionContext(functionName: string, filePath: string): FunctionContext
}

interface IndexSummary {
  fileCount: number;
  languageBreakdown: Record<string, number>;
  topLevelModules: ModuleSummary[];  // high level structure
  entryPoints: string[];             // files with no importers
  totalFunctions: number;
  totalClasses: number;
  existingTodos: TodoComment[];      // all existing TODOs across codebase
}

interface TaskContext {
  relevantFiles: RelevantFile[];     // top K files from embedding search
  dependencyChain: GraphNode[];      // nodes this task will likely touch
  styleExamples: string[];           // similar existing code patterns
}
```

---

## Index Persistence

**Location:** `.waterfree/` in workspace root (add to `.gitignore`)

**Files:**
```
.waterfree/
├── index.json          # ParsedFile[] for all files
├── graph.json          # CodeGraph adjacency list
├── embeddings.bin      # binary embedding vectors
└── index.meta.json     # timestamps, version, file hashes
```

**Cache invalidation:**
- On extension startup, compare file hashes against `index.meta.json`
- Only re-parse and re-embed files whose hash has changed
- If `index.meta.json` is missing or corrupted, full re-index

---

## What Gets Sent to the AI

The full raw index is never sent — it would overflow the context window. Instead, `ContextBuilder.ts` (Subsystem 06) assembles a focused context per turn:

**For planning phase:**
```
INDEX SUMMARY:
- 47 files, TypeScript (38), CSS (9)
- Entry points: src/index.ts, src/cli.ts
- Key modules: AuthService, UserRepository, EmailQueue, PaymentGateway
- Existing TODOs: 12 (see attached list)

ARCHITECTURE NOTES:
[top 5 most-connected nodes and their roles]
```

**For per-task annotation phase:**
```
TARGET FUNCTION: processPayment() in src/payment/PaymentGateway.ts (L142-L198)

DIRECT DEPENDENCIES:
- validateCard() — src/payment/CardValidator.ts:L34
- createCharge() — src/payment/StripeClient.ts:L88  
- logTransaction() — src/logging/Logger.ts:L12

CALLERS OF THIS FUNCTION:
- CheckoutController.handleSubmit() — src/controllers/Checkout.ts:L67
- RetryQueue.retryFailed() — src/queues/RetryQueue.ts:L203

SIMILAR PATTERNS IN CODEBASE:
[2-3 examples of similar payment/async patterns from codebase]
```

This focused context is what gives the AI the "obsessive colleague who knows everything" quality.

---

## Configuration (package.json contributes.configuration)

```json
{
  "waterfree.indexing.excludePatterns": {
    "type": "array",
    "default": ["**/node_modules/**", "**/dist/**", "**/.git/**"],
    "description": "Glob patterns to exclude from indexing"
  },
  "waterfree.indexing.embeddingProvider": {
    "type": "string",
    "enum": ["local", "voyage", "tfidf"],
    "default": "local"
  },
  "waterfree.indexing.voyageApiKey": {
    "type": "string",
    "default": ""
  },
  "waterfree.indexing.maxFileSizeKb": {
    "type": "number",
    "default": 500,
    "description": "Files larger than this are indexed for structure only, not embedded"
  }
}
```

---

## Error Handling

- If Tree-sitter fails to parse a file (syntax error), log the error, skip the file, continue
- If embedding fails for a function, fall back to TF-IDF for that function only
- If the graph becomes inconsistent (deleted file still referenced), run a graph integrity check and prune dead nodes
- Never crash the extension due to indexing failures — index is best-effort
