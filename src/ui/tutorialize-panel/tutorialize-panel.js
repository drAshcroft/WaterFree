/* global acquireVsCodeApi */
const vscode = acquireVsCodeApi();

let sessionId = "";
let busy = false;
let selectedAreas = [];

const messagesEl = document.getElementById("messages");
const inputEl = document.getElementById("input");
const sendBtn = document.getElementById("send-btn");
const repoTextEl = document.getElementById("repo-text");
const focusTextEl = document.getElementById("header-sub");

// ── Helpers ──────────────────────────────────────────────────────────────────

function setComposerEnabled(enabled) {
  inputEl.disabled = !enabled;
  sendBtn.disabled = !enabled;
  busy = !enabled;
}

function scrollToBottom() {
  messagesEl.scrollTop = messagesEl.scrollHeight;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function addMessage(role, text) {
  const div = document.createElement("div");
  div.className = "message " + role;
  div.textContent = text;
  messagesEl.appendChild(div);
  scrollToBottom();
  return div;
}

function addErrorMessage(text) {
  const div = document.createElement("div");
  div.className = "message error";
  div.textContent = text;
  messagesEl.appendChild(div);
  scrollToBottom();
}

// Renders the area-selection message with chip buttons + confirm/all action buttons
function addAreaSelectionMessage(areas, overview) {
  const div = document.createElement("div");
  div.className = "message assistant";

  const intro = document.createElement("div");
  intro.textContent = overview
    ? overview + "\n\nI found these key areas. Select the ones you want tutorials for, or choose all:"
    : "I found these key areas. Select the ones you want tutorials for, or choose all:";
  div.appendChild(intro);

  const chipList = document.createElement("div");
  chipList.className = "area-list";

  areas.forEach(function(area) {
    const chip = document.createElement("button");
    chip.className = "area-chip";
    chip.textContent = area.name;
    chip.title = area.description || "";
    chip.dataset.name = area.name;
    chip.addEventListener("click", function() {
      const idx = selectedAreas.indexOf(area.name);
      if (idx === -1) {
        selectedAreas.push(area.name);
        chip.classList.add("selected");
      } else {
        selectedAreas.splice(idx, 1);
        chip.classList.remove("selected");
      }
      confirmBtn.disabled = selectedAreas.length === 0;
    });
    chipList.appendChild(chip);
  });
  div.appendChild(chipList);

  const actionRow = document.createElement("div");
  actionRow.className = "action-row";

  const confirmBtn = document.createElement("button");
  confirmBtn.className = "action-btn primary";
  confirmBtn.textContent = "Generate selected";
  confirmBtn.disabled = true;
  confirmBtn.addEventListener("click", function() {
    if (selectedAreas.length === 0 || busy) return;
    disableAreaSelection(div);
    vscode.postMessage({ type: "send", sessionId, message: "generate:" + selectedAreas.join(",") });
  });

  const allBtn = document.createElement("button");
  allBtn.className = "action-btn";
  allBtn.textContent = "Generate all";
  allBtn.addEventListener("click", function() {
    if (busy) return;
    disableAreaSelection(div);
    vscode.postMessage({ type: "send", sessionId, message: "generate:all" });
  });

  actionRow.appendChild(confirmBtn);
  actionRow.appendChild(allBtn);
  div.appendChild(actionRow);

  messagesEl.appendChild(div);
  scrollToBottom();
}

function disableAreaSelection(containerDiv) {
  containerDiv.querySelectorAll(".area-chip, .action-btn").forEach(function(el) {
    el.disabled = true;
  });
  setComposerEnabled(false);
}

// Update the last progress message (or create one)
let lastProgressEl = null;
function updateProgress(text) {
  if (lastProgressEl && lastProgressEl.className === "message progress") {
    lastProgressEl.textContent = text;
  } else {
    lastProgressEl = addMessage("progress", text);
  }
  scrollToBottom();
}

function clearProgress() {
  if (lastProgressEl) {
    lastProgressEl.remove();
    lastProgressEl = null;
  }
}

// ── Event listeners ───────────────────────────────────────────────────────────

sendBtn.addEventListener("click", function() {
  sendUserMessage();
});

inputEl.addEventListener("keydown", function(e) {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendUserMessage();
  }
});

function sendUserMessage() {
  const text = inputEl.value.trim();
  if (!text || busy || !sessionId) return;
  addMessage("user", text);
  inputEl.value = "";
  setComposerEnabled(false);
  vscode.postMessage({ type: "send", sessionId, message: text });
}

// ── Messages from extension ───────────────────────────────────────────────────

window.addEventListener("message", function(event) {
  const msg = event.data;
  if (!msg || typeof msg.type !== "string") return;

  switch (msg.type) {
    case "init": {
      sessionId = msg.sessionId || "";
      const repoPath = msg.repoPath || "";
      const repoName = repoPath ? repoPath.replace(/\\/g, "/").split("/").filter(Boolean).pop() || repoPath : "current workspace";
      repoTextEl.textContent = repoName;
      const focus = msg.focus || "";
      if (focus) {
        focusTextEl.textContent = "Focus: " + focus;
      } else {
        focusTextEl.textContent = "Deep dive · knowledge base builder";
      }
      setComposerEnabled(false);
      if (focus) {
        addMessage("user", focus);
      }
      break;
    }

    case "progress":
      updateProgress(msg.text || "");
      break;

    case "areaSelection":
      clearProgress();
      selectedAreas = [];
      addAreaSelectionMessage(msg.areas || [], msg.overview || "");
      // composer stays disabled until area selection is done via buttons
      break;

    case "message":
      clearProgress();
      addMessage("assistant", msg.text || "");
      setComposerEnabled(true);
      inputEl.focus();
      break;

    case "done":
      clearProgress();
      if (msg.text) {
        addMessage("assistant", msg.text);
      }
      setComposerEnabled(true);
      inputEl.focus();
      break;

    case "error":
      clearProgress();
      addErrorMessage(msg.message || "An error occurred.");
      setComposerEnabled(true);
      break;
  }
});
