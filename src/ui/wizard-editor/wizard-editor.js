const vscode = acquireVsCodeApi();

const chunkTitle = document.getElementById("chunk-title");
const guidance = document.getElementById("guidance");
const body = document.getElementById("body");
const clarifications = document.getElementById("clarifications");
const questions = document.getElementById("questions");
const submit = document.getElementById("submit");
const refine = document.getElementById("refine");
const acceptedChunksEl = document.getElementById("accepted-chunks");
const separatorLabel = document.getElementById("separator-label");
const stageProgress = document.getElementById("stage-progress");
const stageProgressLabel = document.getElementById("stage-progress-label");
const stageProgressFill = document.getElementById("stage-progress-fill");
const chunkActions = document.getElementById("chunk-actions");
const stageActions = document.getElementById("stage-actions");
const codingActions = document.getElementById("coding-actions");
const reviewActions = document.getElementById("review-actions");
const acceptStageBtn = document.getElementById("accept-stage");
const promoteTodosBtn = document.getElementById("promote-todos");
const promoteTodosCodingBtn = document.getElementById("promote-todos-coding");
const startCodingBtn = document.getElementById("start-coding");
const runReviewBtn = document.getElementById("run-review");

let currentState = null;
const clarificationState = new Map();

window.addEventListener("message", (event) => {
  const message = event.data;
  if (!message || message.type !== "state") {
    return;
  }
  currentState = message.state;
  renderState(currentState);
  vscode.setState(currentState);
});

body.addEventListener("input", () => {
  vscode.postMessage({ type: "draftChanged", body: getEditorText() });
});

submit.addEventListener("click", () => {
  if (!currentState) { return; }
  setProcessing(true);
  if (currentState.hasDraft && currentState.chunkStatus !== "accepted") {
    // Accept the generated draft
    vscode.postMessage({ type: "acceptChunk", chunkId: currentState.chunkId, body: buildSubmissionBody() });
  } else {
    // Generate a draft using the typed text as context
    vscode.postMessage({ type: "generate", body: buildSubmissionBody() });
  }
});

refine.addEventListener("click", () => {
  vscode.postMessage({ type: "refine", body: buildSubmissionBody() });
});

acceptStageBtn.addEventListener("click", () => {
  setProcessing(true);
  vscode.postMessage({ type: "acceptStage" });
});

promoteTodosBtn.addEventListener("click", () => {
  vscode.postMessage({ type: "promoteTodos" });
});

promoteTodosCodingBtn.addEventListener("click", () => {
  vscode.postMessage({ type: "promoteTodos" });
});

startCodingBtn.addEventListener("click", () => {
  setProcessing(true);
  vscode.postMessage({ type: "startCoding" });
});

runReviewBtn.addEventListener("click", () => {
  setProcessing(true);
  vscode.postMessage({ type: "runReview" });
});

const persisted = vscode.getState();
if (persisted) {
  renderState(persisted);
}

// ---------------------------------------------------------------------------
// Render
// ---------------------------------------------------------------------------

function renderState(state) {
  chunkTitle.textContent = state.chunkTitle || "What is the idea?";
  guidance.textContent = state.guidance || "Describe the software idea, problem you want to solve or frustration in plain language.";
  setEditorText(state.body || "");
  renderQuestions(Array.isArray(state.questions) ? state.questions : []);
  renderAcceptedChunks(Array.isArray(state.acceptedChunks) ? state.acceptedChunks : []);
  updateSeparatorLabel(state.chunkStatus);
  renderStageProgress(state);
  renderActionButtons(state);
  setProcessing(false);
}

function renderStageProgress(state) {
  if (!state.stageCount || state.stageCount <= 1) {
    stageProgress.hidden = true;
    return;
  }
  stageProgress.hidden = false;
  const idx = typeof state.stageIndex === "number" ? state.stageIndex : 0;
  const total = state.stageCount;
  stageProgressLabel.textContent = `${state.stageTitle || "Stage"} (${idx + 1} / ${total})`;
  const pct = Math.round(((idx + 1) / total) * 100);
  stageProgressFill.style.width = `${pct}%`;
}

function renderActionButtons(state) {
  const allAccepted = Boolean(state.allChunksAccepted);
  const stageKind = state.stageKind || "";
  const stageStatus = state.stageStatus || "pending";

  // Decide which action group to show
  const showReview = allAccepted && stageKind === "review";
  const showCoding = allAccepted && stageKind === "coding_agents" && stageStatus !== "accepted";
  const showStageAccept = allAccepted && !showReview && !showCoding && stageStatus !== "accepted";
  const showChunk = !allAccepted || stageStatus === "accepted";

  chunkActions.hidden = !showChunk;
  stageActions.hidden = !showStageAccept;
  codingActions.hidden = !showCoding;
  reviewActions.hidden = !showReview;

  // Update the Generate/Accept label dynamically
  if (state.hasDraft && state.chunkStatus !== "accepted") {
    submit.textContent = "Accept Chunk";
  } else {
    submit.textContent = "Generate";
  }
}

function updateSeparatorLabel(status) {
  if (!status || status === "draft") {
    separatorLabel.textContent = "Draft";
  } else if (status === "awaiting_clarification") {
    separatorLabel.textContent = "Needs clarification";
  } else if (status === "resolved") {
    separatorLabel.textContent = "Ready to accept";
  } else if (status === "accepted") {
    separatorLabel.textContent = "Accepted";
  } else {
    separatorLabel.textContent = status;
  }
}

function setProcessing(on) {
  body.setAttribute("aria-readonly", on ? "true" : "false");
  body.contentEditable = on ? "false" : "true";
  submit.disabled = on;
  refine.disabled = on;
  acceptStageBtn.disabled = on;
  startCodingBtn.disabled = on;
  runReviewBtn.disabled = on;
  if (on) {
    submit.textContent = "Processing…";
    acceptStageBtn.textContent = "Processing…";
    startCodingBtn.textContent = "Processing…";
    runReviewBtn.textContent = "Processing…";
  }
}

// ---------------------------------------------------------------------------
// Accepted chunk history
// ---------------------------------------------------------------------------

function renderAcceptedChunks(chunks) {
  acceptedChunksEl.innerHTML = "";
  if (chunks.length === 0) {
    acceptedChunksEl.hidden = true;
    return;
  }
  acceptedChunksEl.hidden = false;

  for (const chunk of chunks) {
    const section = document.createElement("div");
    section.className = "accepted-chunk";

    const header = document.createElement("div");
    header.className = "accepted-chunk-header";

    const titleEl = document.createElement("span");
    titleEl.className = "accepted-chunk-title";
    titleEl.textContent = chunk.title || "Accepted";
    header.appendChild(titleEl);

    const editBtn = document.createElement("button");
    editBtn.type = "button";
    editBtn.className = "accepted-edit-btn";
    editBtn.textContent = "Revise";
    editBtn.addEventListener("click", () => {
      vscode.postMessage({ type: "reopenChunk", chunkId: chunk.id });
    });
    header.appendChild(editBtn);

    section.appendChild(header);

    const bodyEl = document.createElement("div");
    bodyEl.className = "accepted-chunk-body";
    bodyEl.textContent = chunk.body || "";
    section.appendChild(bodyEl);

    // Expand on click if body is long
    bodyEl.addEventListener("click", () => {
      bodyEl.classList.toggle("expanded");
    });

    acceptedChunksEl.appendChild(section);
  }
}

// ---------------------------------------------------------------------------
// Questions / clarifications
// ---------------------------------------------------------------------------

function renderQuestions(questionList) {
  questions.innerHTML = "";
  clarificationState.clear();
  if (questionList.length === 0) {
    clarifications.hidden = true;
    return;
  }

  clarifications.hidden = false;
  for (const [index, item] of questionList.entries()) {
    const questionKey = `q${index}`;
    clarificationState.set(questionKey, { prompt: item, choice: "", note: "" });

    const block = document.createElement("section");
    block.className = "question-block";

    const prompt = document.createElement("p");
    prompt.className = "question";
    prompt.textContent = item;
    block.appendChild(prompt);

    const options = document.createElement("div");
    options.className = "option-row";
    for (const option of ["Yes", "No", "Not sure"]) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "option-chip";
      button.textContent = option;
      button.addEventListener("click", () => {
        const state = clarificationState.get(questionKey);
        if (!state) {
          return;
        }
        state.choice = option;
        clarificationState.set(questionKey, state);
        syncSelectionState(options, option);
      });
      options.appendChild(button);
    }
    block.appendChild(options);

    const note = document.createElement("input");
    note.type = "text";
    note.className = "clarification-note";
    note.placeholder = "Optional detail";
    note.addEventListener("input", () => {
      const state = clarificationState.get(questionKey);
      if (!state) {
        return;
      }
      state.note = note.value.trim();
      clarificationState.set(questionKey, state);
    });
    block.appendChild(note);

    questions.appendChild(block);
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function getEditorText() {
  return body.innerText.replace(/\u00A0/g, " ").trim();
}

function setEditorText(value) {
  body.innerText = value || "";
}

function buildSubmissionBody() {
  const base = getEditorText();
  const answers = [];
  for (const state of clarificationState.values()) {
    if (!state.choice && !state.note) {
      continue;
    }
    let line = `- ${state.prompt}: `;
    if (state.choice) {
      line += state.choice;
    }
    if (state.note) {
      line += state.choice ? ` (${state.note})` : state.note;
    }
    answers.push(line);
  }

  if (answers.length === 0) {
    return base;
  }
  const clarificationsText = ["Clarifications:", ...answers].join("\n");
  return base ? `${base}\n\n${clarificationsText}` : clarificationsText;
}

function syncSelectionState(container, selectedOption) {
  for (const child of container.children) {
    child.classList.toggle("selected", child.textContent === selectedOption);
  }
}
