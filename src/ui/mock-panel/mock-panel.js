// @ts-check
"use strict";

/**
 * WaterFree Mock Panel webview script.
 *
 * Receives messages from MockPanel.ts:
 *   { type: "capture", capture: { id, stage, persona, system, user } }
 *   { type: "queueSize", count: number }
 *   { type: "cleared" }
 *
 * Sends messages back:
 *   { type: "submit",  captureId: string, response: string }
 *   { type: "discard", captureId: string }
 */

const vscode = acquireVsCodeApi();

const emptyState   = /** @type {HTMLElement} */ (document.getElementById("empty-state"));
const captureView  = /** @type {HTMLElement} */ (document.getElementById("capture-view"));
const captureMeta  = /** @type {HTMLElement} */ (document.getElementById("capture-meta"));
const badgeStage   = /** @type {HTMLElement} */ (document.getElementById("badge-stage"));
const captureIdEl  = /** @type {HTMLElement} */ (document.getElementById("capture-id"));
const systemEl     = /** @type {HTMLElement} */ (document.getElementById("system-prompt"));
const userEl       = /** @type {HTMLElement} */ (document.getElementById("user-prompt"));
const responseEl   = /** @type {HTMLTextAreaElement} */ (document.getElementById("response-input"));
const btnSubmit    = /** @type {HTMLButtonElement} */ (document.getElementById("btn-submit"));
const btnDiscard   = /** @type {HTMLButtonElement} */ (document.getElementById("btn-discard"));
const queueBar     = /** @type {HTMLElement} */ (document.getElementById("queue-bar"));
const queueCountEl = /** @type {HTMLElement} */ (document.getElementById("queue-count"));

/** @type {string | null} */
let currentCaptureId = null;

// ---- Message handling ----

window.addEventListener("message", (/** @type {MessageEvent} */ event) => {
  const message = event.data;
  if (!message || typeof message.type !== "string") return;

  switch (message.type) {
    case "capture":
      showCapture(message.capture);
      break;
    case "queueSize":
      updateQueue(message.count);
      break;
    case "cleared":
      showEmpty();
      break;
  }
});

// ---- Button handlers ----

btnSubmit.addEventListener("click", () => {
  if (!currentCaptureId) return;
  const response = responseEl.value.trim();
  if (!response) {
    responseEl.focus();
    return;
  }
  vscode.postMessage({ type: "submit", captureId: currentCaptureId, response });
  showEmpty();
});

btnDiscard.addEventListener("click", () => {
  if (!currentCaptureId) return;
  vscode.postMessage({ type: "discard", captureId: currentCaptureId });
  showEmpty();
});

// Ctrl+Enter / Cmd+Enter to submit
responseEl.addEventListener("keydown", (/** @type {KeyboardEvent} */ e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
    btnSubmit.click();
  }
});

// ---- View helpers ----

/**
 * @param {{ id: string; stage: string; persona: string; system: string; user: string }} capture
 */
function showCapture(capture) {
  currentCaptureId = capture.id;

  badgeStage.textContent  = capture.stage || "?";
  captureIdEl.textContent = `#${capture.id}`;

  systemEl.textContent = capture.system || "(empty)";
  userEl.textContent   = capture.user   || "(empty)";
  responseEl.value     = "";

  emptyState.hidden  = true;
  captureView.hidden = false;
  captureMeta.hidden = false;

  responseEl.focus();
}

function showEmpty() {
  currentCaptureId = null;
  emptyState.hidden  = false;
  captureView.hidden = true;
  captureMeta.hidden = true;
}

/** @param {number} count */
function updateQueue(count) {
  if (count > 0) {
    queueBar.hidden          = false;
    queueCountEl.textContent = String(count) + " more";
  } else {
    queueBar.hidden = true;
  }
}
