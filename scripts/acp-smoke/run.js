/**
 * Smoke test for the ACP subagent driver, run against a stub ACP agent.
 *
 * Exists because src/acp/ drives external processes over a protocol -- the
 * failure modes (a dead agent, a half-written frame, an unanswered permission
 * request) are exactly the ones that do not show up in a type check. The stub
 * covers them deterministically and without spending provider credit.
 *
 *   npm run test:acp
 *
 * The driver is bundled on the fly with esbuild (already a devDependency), so
 * this needs no test framework and no runtime dependencies.
 */
const fs = require("fs");
const os = require("os");
const path = require("path");

const REPO_ROOT = path.resolve(__dirname, "..", "..");
const STUB = path.join(__dirname, "stub-agent.js");
const BUNDLE = path.join(os.tmpdir(), `acp-smoke-${process.pid}.js`);

// esbuild's Node API rather than the CLI: Node on Windows refuses to spawn
// npx.cmd without a shell, and shelling out here would buy nothing.
require("esbuild").buildSync({
  entryPoints: [path.join(REPO_ROOT, "src", "acp", "AcpSubagentDriver.ts")],
  bundle: true,
  platform: "node",
  format: "cjs",
  outfile: BUNDLE,
});
const { AcpSubagentDriver, assessTurn, isRetryableFailure, buildPrompt } = require(BUNDLE);

let passed = 0;
let failed = 0;
function check(name, condition, detail) {
  if (condition) { passed++; console.log(`  PASS  ${name}`); }
  else { failed++; console.log(`  FAIL  ${name}${detail ? ` -- ${detail}` : ""}`); }
}

/** Minimal in-memory catalog satisfying AcpAgentCatalog. */
function makeCatalog(mode, workspace, extraEnv) {
  const config = { id: "stub", label: "Stub Agent", command: process.execPath, args: [STUB], enabled: true };
  return {
    list: () => [config],
    get: (id) => (id === "stub" ? config : undefined),
    toSpec: (cfg, cwd) => ({
      id: cfg.id, label: cfg.label, command: cfg.command, args: cfg.args,
      env: { STUB_MODE: mode, ...(extraEnv || {}) }, cwd: cwd || workspace,
    }),
  };
}

/** Host mirroring WorkspaceAcpClientHost's boundary rule, without vscode. */
function makeHost(workspace, permissionChoice) {
  const touched = [];
  const inside = (p) => {
    const root = path.resolve(workspace);
    const target = path.resolve(root, p);
    const rel = path.relative(root, target);
    if (rel.startsWith("..") || path.isAbsolute(rel)) {
      throw new Error(`Refusing access outside the workspace: ${p}`);
    }
    return target;
  };
  return {
    touchedFiles: touched,
    async readTextFile({ path: p, limit }) {
      const text = fs.readFileSync(inside(p), "utf-8");
      return limit === undefined ? text : text.split("\n").slice(0, limit).join("\n");
    },
    async writeTextFile({ path: p, content }) {
      const target = inside(p);
      fs.mkdirSync(path.dirname(target), { recursive: true });
      fs.writeFileSync(target, content, "utf-8");
      touched.push(target);
    },
    async requestPermission({ options }) {
      if (permissionChoice === "cancel") { return "cancelled"; }
      const allow = options.find((o) => (o.kind || o.optionId).includes("allow"));
      return allow ? { optionId: allow.optionId } : "cancelled";
    },
  };
}

function makeDriver(mode, workspace, permissionChoice, extraEnv) {
  let host;
  const driver = new AcpSubagentDriver({
    registry: makeCatalog(mode, workspace, extraEnv),
    createHost: () => (host = makeHost(workspace, permissionChoice)),
    log: () => {},
  });
  return { driver, getHost: () => host };
}

async function main() {
  const workspace = fs.mkdtempSync(path.join(os.tmpdir(), "acp-ws-"));
  fs.writeFileSync(path.join(workspace, "README.md"), "line one\nline two\nline three\n", "utf-8");

  console.log("\n[1] happy path: full client surface");
  {
    const { driver } = makeDriver("ok", workspace);
    const r = await driver.delegate({ agentId: "stub", prompt: "do the thing", workspacePath: workspace, model: "stub:big" });
    check("ok=true", r.ok === true, r.failureReason);
    check("stopReason=end_turn", r.stopReason === "end_turn", r.stopReason);
    check("streamed text assembled in order", r.text.startsWith("Read 2 lines;"), JSON.stringify(r.text));
    check("fs read honoured limit", r.text.includes("Read 2 lines"), r.text);
    check("workspace escape refused", r.text.includes("escape_refused=yes"), r.text);
    check("permission granted -> file written", r.text.includes("wrote=yes"), r.text);
    check("host-reported write in touchedFiles", r.touchedFiles.some((f) => f.endsWith("stub-output.txt")), JSON.stringify(r.touchedFiles));
    check("agent-local write caught by change tracker", r.touchedFiles.some((f) => f.endsWith("local-side-effect.txt")), JSON.stringify(r.touchedFiles));
    check("client write not double-counted", r.touchedFiles.filter((f) => f.toLowerCase().endsWith("stub-output.txt")).length === 1, JSON.stringify(r.touchedFiles));
    check("written file exists on disk", fs.existsSync(path.join(workspace, "stub-output.txt")));
    check("tool calls merged by id", r.toolCalls.length === 2, JSON.stringify(r.toolCalls));
    check("tool_call_update merged onto tool_call", r.toolCalls.find((t) => t.toolCallId === "tc-1")?.status === "completed", JSON.stringify(r.toolCalls));
    check("model pin applied", r.modelId === "stub:big", r.modelId);
    check("availableModels surfaced", (r.availableModels || []).length === 2);
    check("usage captured (last wins)", r.usage && r.usage.used === 480, JSON.stringify(r.usage));
    check("no processes left active", driver.activeDelegations.length === 0);
  }

  console.log("\n[2] permission denied");
  {
    fs.rmSync(path.join(workspace, "stub-output.txt"), { force: true });
    const { driver } = makeDriver("ok", workspace, "cancel");
    const r = await driver.delegate({ agentId: "stub", prompt: "x", workspacePath: workspace });
    check("agent told write was refused", r.text.includes("wrote=no"), r.text);
    check("nothing written", !fs.existsSync(path.join(workspace, "stub-output.txt")));
    check("refused write absent from touchedFiles", !r.touchedFiles.some((f) => f.endsWith("stub-output.txt")), JSON.stringify(r.touchedFiles));
    check("agent-local write still tracked", r.touchedFiles.some((f) => f.endsWith("local-side-effect.txt")), JSON.stringify(r.touchedFiles));
  }

  console.log("\n[3] upstream failure reported as end_turn");
  {
    const { driver } = makeDriver("upstream_fail", workspace);
    const r = await driver.delegate({ agentId: "stub", prompt: "x", workspacePath: workspace });
    check("stopReason still end_turn", r.stopReason === "end_turn", r.stopReason);
    check("ok=false despite clean stopReason", r.ok === false);
    check("failureReason quotes the provider", (r.failureReason || "").includes("402"), r.failureReason);
  }

  console.log("\n[4] Hermes-shaped session id (only under _meta)");
  {
    const { driver } = makeDriver("no_session_id", workspace);
    const r = await driver.delegate({ agentId: "stub", prompt: "x", workspacePath: workspace });
    check("sessionId recovered from _meta", r.sessionId === "stub-session-1", r.sessionId);
    check("turn still succeeded", r.ok === true, r.failureReason);
  }

  console.log("\n[5] agent crashes on startup");
  {
    const { driver } = makeDriver("crash", workspace);
    const r = await driver.delegate({ agentId: "stub", prompt: "x", workspacePath: workspace });
    check("ok=false", r.ok === false);
    check("stopReason=error", r.stopReason === "error", r.stopReason);
    check("stderr surfaced in failureReason", (r.failureReason || "").includes("model backend"), r.failureReason);
    check("no processes left active", driver.activeDelegations.length === 0);
  }

  console.log("\n[6] protocol major-version mismatch");
  {
    const { driver } = makeDriver("bad_version", workspace);
    const r = await driver.delegate({ agentId: "stub", prompt: "x", workspacePath: workspace });
    check("ok=false", r.ok === false);
    check("names both versions", (r.failureReason || "").includes("v99"), r.failureReason);
  }

  console.log("\n[7] junk on stdout does not break the session");
  {
    const { driver } = makeDriver("noisy", workspace);
    const r = await driver.delegate({ agentId: "stub", prompt: "x", workspacePath: workspace });
    check("survived non-JSON stdout line", r.ok === true, r.failureReason);
  }

  console.log("\n[8] unknown agent id");
  {
    const { driver } = makeDriver("ok", workspace);
    let threw = false;
    try { await driver.delegate({ agentId: "nope", prompt: "x", workspacePath: workspace }); }
    catch (e) { threw = /No ACP agent configured/.test(e.message); }
    check("throws a clear error", threw);
  }

  console.log("\n[9] retry policy");
  {
    // A transient failure recovers on a second attempt.
    const marker = path.join(workspace, "flaky-marker.txt");
    fs.rmSync(marker, { force: true });
    const { driver } = makeDriver("flaky", workspace, undefined, { STUB_FLAKY_FILE: marker });
    const r = await driver.delegate({ agentId: "stub", prompt: "x", workspacePath: workspace, maxAttempts: 3 });
    check("transient failure retried to success", r.ok === true, r.failureReason);
    check("attempts counted", r.attempts === 2, String(r.attempts));
    check("retry text replaces failed attempt's", r.text.includes("recovered on retry"), r.text);

    // ...but not when retries are not asked for.
    fs.rmSync(marker, { force: true });
    const single = makeDriver("flaky", workspace, undefined, { STUB_FLAKY_FILE: marker });
    const r2 = await single.driver.delegate({ agentId: "stub", prompt: "x", workspacePath: workspace });
    check("no retry by default", r2.ok === false && r2.attempts === 1, `ok=${r2.ok} attempts=${r2.attempts}`);

    // A terminal failure must not burn attempts.
    const broke = makeDriver("broke", workspace);
    const r3 = await broke.driver.delegate({ agentId: "stub", prompt: "x", workspacePath: workspace, maxAttempts: 5 });
    check("exhausted account not retried", r3.attempts === 1, String(r3.attempts));
    check("exhausted account still reported", r3.ok === false && (r3.failureReason || "").includes("402"), r3.failureReason);
  }

  console.log("\n[10] text deliverable mode");
  {
    const { driver } = makeDriver("echo", workspace);
    const asText = await driver.delegate({
      agentId: "stub", prompt: "Write the brief.", workspacePath: workspace, deliverable: "text",
    });
    check("no-tools instruction reaches the agent", /Do not use any tools/.test(asText.text), asText.text.slice(0, 120));
    check("turn named as the deliverable", /reply in this turn IS the deliverable/.test(asText.text));
    check("original prompt preserved after preamble", asText.text.trimEnd().endsWith("Write the brief."), asText.text.slice(-60));

    const asFiles = await driver.delegate({
      agentId: "stub", prompt: "Write the brief.", workspacePath: workspace,
    });
    check("files mode leaves prompt untouched", asFiles.text === "Write the brief.", asFiles.text);
    check("files mode is the default", (await driver.delegate({
      agentId: "stub", prompt: "x", workspacePath: workspace, deliverable: undefined,
    })).text === "x");
  }

  console.log("\n[11] buildPrompt unit checks");
  {
    check("text mode prepends once", (buildPrompt("body", "text").match(/Do not use any tools/g) || []).length === 1);
    check("files mode is identity", buildPrompt("body", "files") === "body");
    check("undefined is identity", buildPrompt("body", undefined) === "body");
    check("empty prompt still gets guidance", buildPrompt("", "text").includes("Do not use any tools"));
  }

  console.log("\n[12] isRetryableFailure unit checks");
  {
    check("empty content is retryable", isRetryableFailure("The agent reported an upstream failure: returned empty content"));
    check("rate limit is retryable", isRetryableFailure("Rate limited"));
    check("http 503 is retryable", isRetryableFailure("HTTP 503 bad gateway"));
    check("no output is retryable", isRetryableFailure("The agent produced no output."));
    check("http 402 is NOT retryable", !isRetryableFailure("Billing or credits exhausted: HTTP 402"));
    check("credits exhausted is NOT retryable", !isRetryableFailure("credits exhausted, upgrade your account"));
    check("refusal is NOT retryable", !isRetryableFailure("The agent refused the request."));
    check("cancellation is NOT retryable", !isRetryableFailure("The delegation was cancelled."));
    check("invalid key is NOT retryable", !isRetryableFailure("invalid api key"));
    check("undefined is NOT retryable", !isRetryableFailure(undefined));
    // 402 text also matches no retryable pattern; the guard matters when a
    // provider mixes both, e.g. a 402 body that also says "rate limit".
    check("terminal wins over retryable", !isRetryableFailure("rate limited: HTTP 402 credits exhausted"));
  }

  console.log("\n[13] assessTurn unit checks");
  {
    check("empty output is a failure", assessTurn("end_turn", "   ") !== undefined);
    check("refusal is a failure", assessTurn("refusal", "no") !== undefined);
    check("max_tokens is a failure", assessTurn("max_tokens", "partial") !== undefined);
    check("rate limit text is a failure", assessTurn("end_turn", "Rate limited, try later") !== undefined);
    check("empty-content apology is a failure", assessTurn("end_turn",
      "⚠️ No reply: the model returned empty content after retries and any fallback providers.") !== undefined);
    check("ordinary prose passes", assessTurn("end_turn", "I updated the parser.") === undefined);
    check("prose mentioning http 200 passes", assessTurn("end_turn", "The endpoint returned HTTP 200.") === undefined);
  }

  console.log(`\n==== ${passed} passed, ${failed} failed ====`);
  // Best-effort: on Windows a just-exited agent can still hold its cwd handle,
  // and a cleanup EPERM must not turn an otherwise green run red.
  for (const target of [workspace, BUNDLE]) {
    try {
      fs.rmSync(target, { recursive: true, force: true });
    } catch {
      /* transient handle; the OS reclaims temp */
    }
  }
  process.exit(failed === 0 ? 0 : 1);
}

main().catch((err) => { console.error("harness error:", err); process.exit(1); });
