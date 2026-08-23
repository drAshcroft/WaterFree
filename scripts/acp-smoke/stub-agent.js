#!/usr/bin/env node
/**
 * Stub ACP agent used by scripts/acp-smoke/run.js.
 *
 * Exercises the full client surface: during a prompt turn it reads a file,
 * asks permission, writes a file, streams chunks, and reports usage. Behaviour
 * is switched by env so one stub covers the success and failure paths.
 *
 *   STUB_MODE=ok            normal turn
 *   STUB_MODE=upstream_fail end_turn whose content is a provider error
 *   STUB_MODE=no_session_id return the id under _meta (the Hermes shape)
 *   STUB_MODE=crash         exit non-zero right after spawn
 *   STUB_MODE=bad_version   claim ACP v99
 *   STUB_MODE=noisy         emit junk on stdout before valid frames
 *   STUB_MODE=flaky         empty-content failure until STUB_FLAKY_FILE exists
 *   STUB_MODE=broke         end_turn whose content is an exhausted-account error
 *   STUB_MODE=echo          stream back the prompt text it received, nothing else
 *
 * `flaky` coordinates across processes through a marker file because each
 * delegation attempt is a fresh process: in-process state cannot express
 * "fail the first attempt, succeed the second".
 */
const readline = require("readline");

const MODE = process.env.STUB_MODE || "ok";
const SESSION_ID = "stub-session-1";

if (MODE === "crash") {
  process.stderr.write("stub: fatal: could not load model backend\n");
  process.exit(3);
}

function send(msg) {
  process.stdout.write(JSON.stringify(msg) + "\n");
}
function respond(id, result) {
  send({ jsonrpc: "2.0", id, result });
}
function update(update) {
  send({ jsonrpc: "2.0", method: "session/update", params: { sessionId: SESSION_ID, update } });
}

let nextId = 1000;
const pendingClientCalls = new Map();
function callClient(method, params) {
  return new Promise((resolve) => {
    const id = nextId++;
    pendingClientCalls.set(id, resolve);
    send({ jsonrpc: "2.0", id, method, params });
  });
}

const rl = readline.createInterface({ input: process.stdin });

rl.on("line", async (line) => {
  const trimmed = line.trim();
  if (!trimmed) { return; }
  let msg;
  try { msg = JSON.parse(trimmed); } catch { return; }

  // A response to one of our client-directed calls.
  if (msg.id !== undefined && msg.method === undefined) {
    const resolve = pendingClientCalls.get(msg.id);
    if (resolve) {
      pendingClientCalls.delete(msg.id);
      resolve(msg.error ? { __error: msg.error } : msg.result);
    }
    return;
  }

  switch (msg.method) {
    case "initialize":
      if (MODE === "noisy") {
        process.stdout.write("starting up, not json at all\n");
      }
      respond(msg.id, {
        protocolVersion: MODE === "bad_version" ? 99 : 1,
        agentCapabilities: { loadSession: true, sessionCapabilities: { fork: {}, list: {}, resume: {} } },
        agentInfo: { name: "stub-agent", version: "0.0.1" },
        authMethods: [],
      });
      return;

    case "session/new": {
      const models = {
        availableModels: [{ modelId: "stub:small", name: "Stub Small" }, { modelId: "stub:big", name: "Stub Big" }],
        currentModelId: "stub:small",
      };
      if (MODE === "no_session_id") {
        // Hermes shape: id only under _meta.
        respond(msg.id, { models, _meta: { hermes: { sessionProvenance: { acpSessionId: SESSION_ID } } } });
      } else {
        respond(msg.id, { sessionId: SESSION_ID, models });
      }
      return;
    }

    case "session/set_model":
      process.stderr.write(`stub: model set to ${msg.params.modelId}\n`);
      respond(msg.id, {});
      return;

    case "session/prompt": {
      if (MODE === "echo") {
        const blocks = Array.isArray(msg.params.prompt) ? msg.params.prompt : [];
        const received = blocks.map((b) => (b && b.type === "text" ? b.text : "")).join("");
        update({ sessionUpdate: "agent_message_chunk", content: { type: "text", text: received } });
        respond(msg.id, { stopReason: "end_turn" });
        return;
      }

      if (MODE === "flaky") {
        const marker = process.env.STUB_FLAKY_FILE;
        const fs = require("fs");
        if (marker && !fs.existsSync(marker)) {
          // First attempt: fail transiently and arm the marker so the retry wins.
          fs.writeFileSync(marker, "attempted\n");
          update({ sessionUpdate: "agent_message_chunk", content: { type: "text",
            text: "⚠️ No reply: the model returned empty content after retries and any fallback providers." } });
          respond(msg.id, { stopReason: "end_turn" });
          return;
        }
        update({ sessionUpdate: "agent_message_chunk", content: { type: "text", text: "recovered on retry" } });
        respond(msg.id, { stopReason: "end_turn" });
        return;
      }

      if (MODE === "broke") {
        update({ sessionUpdate: "agent_message_chunk", content: { type: "text",
          text: "Billing or credits exhausted: HTTP 402: This request requires more credits." } });
        respond(msg.id, { stopReason: "end_turn" });
        return;
      }

      if (MODE === "upstream_fail") {
        update({ sessionUpdate: "agent_message_chunk", content: { type: "text",
          text: "Billing or credits exhausted: HTTP 402: This request requires more credits." } });
        respond(msg.id, { stopReason: "end_turn" });
        return;
      }

      update({ sessionUpdate: "usage_update", used: 120, size: 1048576 });

      // 1. read a file through the client
      const read = await callClient("fs/read_text_file", {
        sessionId: SESSION_ID, path: "README.md", limit: 2,
      });
      update({ sessionUpdate: "tool_call", toolCallId: "tc-1", title: "Read README.md", kind: "read", status: "pending" });
      update({ sessionUpdate: "tool_call_update", toolCallId: "tc-1", status: "completed" });

      // 2. try to escape the workspace — the client must refuse
      const escaped = await callClient("fs/read_text_file", {
        sessionId: SESSION_ID, path: "../../../../Windows/System32/drivers/etc/hosts",
      });

      // 3. ask permission, then write
      const permission = await callClient("session/request_permission", {
        sessionId: SESSION_ID,
        toolCall: { toolCallId: "tc-2", title: "write stub-output.txt", kind: "edit" },
        options: [
          { optionId: "reject_once", name: "Reject", kind: "reject_once" },
          { optionId: "allow_once", name: "Allow once", kind: "allow_once" },
        ],
      });
      update({ sessionUpdate: "tool_call", toolCallId: "tc-2", title: "write stub-output.txt", kind: "edit", status: "pending" });

      const allowed = permission && permission.outcome && permission.outcome.outcome === "selected";
      if (allowed) {
        await callClient("fs/write_text_file", {
          sessionId: SESSION_ID, path: "stub-output.txt", content: "written by stub agent\n",
        });
        update({ sessionUpdate: "tool_call_update", toolCallId: "tc-2", status: "completed" });
      } else {
        update({ sessionUpdate: "tool_call_update", toolCallId: "tc-2", status: "cancelled" });
      }

      // 4. write a file with our "own tools", bypassing client fs entirely --
      // the Hermes pattern the workspace change tracker exists to catch.
      require("fs").writeFileSync(
        require("path").join(process.cwd(), "local-side-effect.txt"),
        "written directly by the agent process\n",
      );

      update({ sessionUpdate: "agent_message_chunk", content: { type: "text", text: "Read " } });
      update({ sessionUpdate: "agent_message_chunk", content: { type: "text",
        text: `${(read && read.content ? read.content.split("\n").length : 0)} lines; ` } });
      update({ sessionUpdate: "agent_message_chunk", content: { type: "text",
        text: `escape_refused=${escaped && escaped.__error ? "yes" : "no"}; ` } });
      update({ sessionUpdate: "agent_message_chunk", content: { type: "text",
        text: `wrote=${allowed ? "yes" : "no"}` } });
      update({ sessionUpdate: "usage_update", used: 480, size: 1048576 });
      respond(msg.id, { stopReason: "end_turn" });
      return;
    }

    case "session/cancel":
      process.exit(0);
      return;

    default:
      if (msg.id !== undefined) {
        send({ jsonrpc: "2.0", id: msg.id, error: { code: -32601, message: `no such method ${msg.method}` } });
      }
  }
});
