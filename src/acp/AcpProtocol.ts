/**
 * Agent Client Protocol (ACP) wire types — JSON-RPC 2.0 over newline-delimited
 * JSON on the agent process's stdin/stdout.
 *
 * WaterFree is the ACP **client**: it spawns agent processes and drives them.
 * The agent calls back into us for file access and permission, which is why
 * this integration fits WaterFree at all — every write a subagent wants to make
 * arrives as a request we can gate, rather than a mutation we discover later.
 *
 * Hand-written rather than taken from `@zed-industries/agent-client-protocol`
 * because this extension ships with zero runtime dependencies, and the surface
 * we need is small. Shapes verified against hermes-agent 0.20.5.
 *
 * Spec: https://agentclientprotocol.com/protocol/overview
 */

/** MAJOR version of the protocol we implement. */
export const ACP_PROTOCOL_VERSION = 1;

// ── JSON-RPC envelopes ─────────────────────────────────────────────────────

export interface JsonRpcRequest {
  jsonrpc: "2.0";
  id: number | string;
  method: string;
  params?: unknown;
}

export interface JsonRpcNotification {
  jsonrpc: "2.0";
  method: string;
  params?: unknown;
}

export interface JsonRpcError {
  code: number;
  message: string;
  data?: unknown;
}

export interface JsonRpcResponse {
  jsonrpc: "2.0";
  id: number | string;
  result?: unknown;
  error?: JsonRpcError;
}

export type JsonRpcMessage = JsonRpcRequest | JsonRpcNotification | JsonRpcResponse;

/** JSON-RPC reserved codes we emit when serving agent-initiated requests. */
export const JSON_RPC_METHOD_NOT_FOUND = -32601;
export const JSON_RPC_INTERNAL_ERROR = -32603;

// ── initialize ─────────────────────────────────────────────────────────────

export interface ClientCapabilities {
  /** Omitting a flag means "we will reject that call", so advertise honestly. */
  fs?: { readTextFile?: boolean; writeTextFile?: boolean };
  terminal?: boolean;
}

export interface InitializeParams {
  protocolVersion: number;
  clientCapabilities: ClientCapabilities;
  clientInfo?: { name: string; title?: string; version?: string };
}

/**
 * An auth method the agent offers. `type: "terminal"` means the method is not
 * something we can satisfy over the wire — it needs a human in a terminal
 * running the agent's own setup flow (Hermes advertises `hermes --setup`).
 */
export interface AuthMethod {
  id: string;
  name?: string;
  description?: string;
  type?: string;
  args?: string[];
}

export interface AgentCapabilities {
  loadSession?: boolean;
  promptCapabilities?: { image?: boolean; audio?: boolean; embeddedContext?: boolean };
  /** Presence of a key means supported; the value is an options object. */
  sessionCapabilities?: { fork?: unknown; list?: unknown; resume?: unknown };
  mcpCapabilities?: { http?: boolean; sse?: boolean };
}

export interface InitializeResult {
  protocolVersion: number;
  agentCapabilities?: AgentCapabilities;
  agentInfo?: { name?: string; title?: string; version?: string };
  authMethods?: AuthMethod[];
}

// ── sessions ───────────────────────────────────────────────────────────────

export interface SessionNewParams {
  /** Absolute path. Scopes the agent's view of the project. */
  cwd: string;
  mcpServers: unknown[];
}

export interface ModelInfo {
  modelId: string;
  name?: string;
  description?: string;
}

/**
 * Agents may advertise a model roster on session creation. Hermes does, which
 * is what makes per-subagent model pinning possible from WaterFree rather than
 * only from the agent's own config file.
 */
export interface SessionModels {
  availableModels?: ModelInfo[];
  currentModelId?: string;
}

export interface SessionNewResult {
  sessionId?: string;
  models?: SessionModels;
  /** Vendor extension bag; Hermes puts session provenance here. */
  _meta?: Record<string, unknown>;
}

export interface SessionSetModelParams {
  sessionId: string;
  modelId: string;
}

// ── prompt turn ────────────────────────────────────────────────────────────

export interface TextContentBlock {
  type: "text";
  text: string;
}

export interface ResourceContentBlock {
  type: "resource";
  resource: { uri: string; mimeType?: string; text?: string };
}

export type ContentBlock = TextContentBlock | ResourceContentBlock;

export interface SessionPromptParams {
  sessionId: string;
  prompt: ContentBlock[];
}

/**
 * Why a turn ended. Note `end_turn` means "the agent stopped talking", NOT
 * "the work succeeded" — an upstream billing or provider failure can arrive as
 * ordinary message content under `end_turn`. Callers must inspect the turn's
 * text, not just this value.
 */
export type StopReason =
  | "end_turn"
  | "max_tokens"
  | "max_turn_requests"
  | "refusal"
  | "cancelled";

export interface SessionPromptResult {
  stopReason: StopReason;
}

export interface SessionCancelParams {
  sessionId: string;
}

// ── session/update notifications ───────────────────────────────────────────

export interface AgentMessageChunkUpdate {
  sessionUpdate: "agent_message_chunk";
  messageId?: string;
  content: ContentBlock;
}

export interface AgentThoughtChunkUpdate {
  sessionUpdate: "agent_thought_chunk";
  content: ContentBlock;
}

export interface ToolCallUpdate {
  sessionUpdate: "tool_call";
  toolCallId: string;
  title?: string;
  kind?: string;
  status?: string;
}

export interface ToolCallProgressUpdate {
  sessionUpdate: "tool_call_update";
  toolCallId: string;
  status?: "pending" | "in_progress" | "completed" | "cancelled" | "failed";
  content?: unknown[];
}

export interface PlanUpdate {
  sessionUpdate: "plan";
  entries: Array<{
    content: string;
    priority?: "high" | "medium" | "low";
    status?: "pending" | "in_progress" | "completed";
  }>;
}

/** Token accounting for the session's context window. */
export interface UsageUpdate {
  sessionUpdate: "usage_update";
  used?: number;
  size?: number;
  cost?: { amount: number; currency: string };
}

export interface UnknownUpdate {
  sessionUpdate: string;
  [key: string]: unknown;
}

export type SessionUpdate =
  | AgentMessageChunkUpdate
  | AgentThoughtChunkUpdate
  | ToolCallUpdate
  | ToolCallProgressUpdate
  | PlanUpdate
  | UsageUpdate
  | UnknownUpdate;

export interface SessionUpdateParams {
  sessionId: string;
  update: SessionUpdate;
}

// ── client-served methods (agent → us) ─────────────────────────────────────

export interface FsReadTextFileParams {
  sessionId: string;
  /** Absolute path, per spec. */
  path: string;
  /** 1-based start line. */
  line?: number;
  limit?: number;
}

export interface FsReadTextFileResult {
  content: string;
}

export interface FsWriteTextFileParams {
  sessionId: string;
  path: string;
  content: string;
}

export interface PermissionOption {
  optionId: string;
  name?: string;
  /** Conventionally allow_once / allow_always / reject_once / reject_always. */
  kind?: string;
}

export interface RequestPermissionParams {
  sessionId: string;
  toolCall?: { toolCallId?: string; title?: string; kind?: string };
  options: PermissionOption[];
}

/**
 * Note the nesting: the result is `{ outcome: { outcome, optionId } }`, not a
 * flat object. Getting this wrong leaves the agent waiting forever.
 */
export interface RequestPermissionResult {
  outcome: { outcome: "selected"; optionId: string } | { outcome: "cancelled" };
}

// ── method name constants ──────────────────────────────────────────────────

export const AcpMethod = {
  initialize: "initialize",
  sessionNew: "session/new",
  sessionPrompt: "session/prompt",
  sessionCancel: "session/cancel",
  sessionSetModel: "session/set_model",
  sessionUpdate: "session/update",
  fsReadTextFile: "fs/read_text_file",
  fsWriteTextFile: "fs/write_text_file",
  requestPermission: "session/request_permission",
} as const;

// ── narrowing helpers ──────────────────────────────────────────────────────

export function isJsonRpcResponse(msg: JsonRpcMessage): msg is JsonRpcResponse {
  return "id" in msg && !("method" in msg);
}

export function isJsonRpcRequest(msg: JsonRpcMessage): msg is JsonRpcRequest {
  return "id" in msg && "method" in msg;
}

export function isJsonRpcNotification(msg: JsonRpcMessage): msg is JsonRpcNotification {
  return !("id" in msg) && "method" in msg;
}

/** Flatten a content block to plain text; non-text blocks contribute nothing. */
export function contentBlockText(block: ContentBlock | undefined): string {
  if (!block) {
    return "";
  }
  if (block.type === "text") {
    return block.text ?? "";
  }
  if (block.type === "resource") {
    return block.resource?.text ?? "";
  }
  return "";
}
