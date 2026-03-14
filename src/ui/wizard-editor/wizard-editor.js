const vscode = acquireVsCodeApi();

const chunkTitle = document.getElementById("chunk-title");
const guidance = document.getElementById("guidance");
const body = document.getElementById("body");
const clarifications = document.getElementById("clarifications");
const intake = document.getElementById("intake");
const intakeFieldsEl = document.getElementById("intake-fields");
const questionsBlock = document.getElementById("questions-block");
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
const intakeState = new Map();

window.addEventListener("message", (event) => {
  const message = event.data;
  if (!message || message.type !== "state") {
    return;
  }
  const incomingState = isRecord(message.state) ? message.state : {};
  const persistedState = vscode.getState();
  const reuseDraftState = sameWizardContext(incomingState.context, persistedState && persistedState.context);
  currentState = {
    ...incomingState,
    intakeAnswers: {
      ...normalizeAnswerMap(incomingState.intakeAnswers),
      ...normalizeAnswerMap(persistedState && persistedState.intakeAnswers),
    },
    questionAnswers: reuseDraftState ? normalizeAnswerMap(persistedState && persistedState.questionAnswers) : {},
  };
  renderState(currentState);
  persistState();
});

body.addEventListener("input", () => {
  vscode.postMessage({ type: "draftChanged", body: getEditorText() });
  persistState();
});

submit.addEventListener("click", () => {
  if (!currentState) {
    return;
  }
  setProcessing(true);
  if (currentState.hasDraft && currentState.chunkStatus !== "accepted") {
    vscode.postMessage({ type: "acceptChunk", chunkId: currentState.chunkId, body: buildSubmissionBody() });
  } else {
    vscode.postMessage({ type: "generate", body: buildSubmissionBody() });
  }
});

refine.addEventListener("click", () => {
  if (!currentState) {
    return;
  }
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
  currentState = persisted;
  renderState(persisted);
}

function renderState(state) {
  chunkTitle.textContent = state.chunkTitle || "What is the idea?";
  const guidanceText = state.guidance || "";
  guidance.textContent = guidanceText;
  guidance.hidden = !guidanceText;
  setEditorText(state.body || "");

  const hasIntake = renderIntakeForm(
    Array.isArray(state.intakeFields) ? state.intakeFields : [],
    normalizeAnswerMap(state.intakeAnswers),
  );
  const hasQuestions = renderQuestions(
    Array.isArray(state.questions) ? state.questions : [],
    normalizeAnswerMap(state.questionAnswers),
  );
  clarifications.hidden = !hasIntake && !hasQuestions;

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

  const showReview = allAccepted && stageKind === "review";
  const showCoding = allAccepted && stageKind === "coding_agents" && stageStatus !== "accepted";
  const showStageAccept = allAccepted && !showReview && !showCoding && stageStatus !== "accepted";
  const showChunk = !allAccepted || stageStatus === "accepted";

  chunkActions.hidden = !showChunk;
  stageActions.hidden = !showStageAccept;
  codingActions.hidden = !showCoding;
  reviewActions.hidden = !showReview;

  const isInitialIdea = stageKind === "market_research" && state.chunkId === "initial_goal";
  if (state.hasDraft && state.chunkStatus !== "accepted") {
    submit.textContent = "Accept Chunk";
  } else if (isInitialIdea) {
    submit.textContent = "Market Research";
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

  document.querySelectorAll(".clarification-answer, .intake-select").forEach((el) => {
    if ("disabled" in el) {
      el.disabled = on;
    }
  });

  if (on) {
    submit.textContent = "Processing…";
    acceptStageBtn.textContent = "Processing…";
    startCodingBtn.textContent = "Processing…";
    runReviewBtn.textContent = "Processing…";
  }
}

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

    bodyEl.addEventListener("click", () => {
      bodyEl.classList.toggle("expanded");
    });

    acceptedChunksEl.appendChild(section);
  }
}

function renderIntakeForm(fieldList, savedAnswers) {
  intakeFieldsEl.innerHTML = "";
  intakeState.clear();
  if (fieldList.length === 0) {
    intake.hidden = true;
    return false;
  }

  intake.hidden = false;

  for (const field of fieldList) {
    const wrapper = document.createElement("label");
    wrapper.className = "intake-field";

    const label = document.createElement("span");
    label.className = "intake-label";
    label.textContent = field.label;
    wrapper.appendChild(label);

    const select = document.createElement("select");
    select.className = "intake-select";

    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = field.placeholder || "Choose an option";
    select.appendChild(placeholder);

    const optionLabels = new Map();
    for (const option of Array.isArray(field.options) ? field.options : []) {
      optionLabels.set(option.value, option.label);
      const optionEl = document.createElement("option");
      optionEl.value = option.value;
      optionEl.textContent = option.label;
      select.appendChild(optionEl);
    }

    const savedValue = typeof savedAnswers[field.id] === "string" ? savedAnswers[field.id] : "";
    if (savedValue && optionLabels.has(savedValue)) {
      select.value = savedValue;
    }

    intakeState.set(field.id, {
      prompt: field.label,
      answer: select.value || "",
      answerLabel: optionLabels.get(select.value) || "",
      remember: Boolean(field.remember),
    });

    select.addEventListener("change", () => {
      const state = intakeState.get(field.id);
      if (!state) {
        return;
      }
      state.answer = select.value;
      state.answerLabel = optionLabels.get(select.value) || "";
      intakeState.set(field.id, state);

      if (state.remember) {
        vscode.postMessage({
          type: "rememberIntakeDefaults",
          values: {
            teamSize: intakeState.get("teamSize")?.answer || "",
            skillLevel: intakeState.get("skillLevel")?.answer || "",
          },
        });
      }
      persistState();
    });

    wrapper.appendChild(select);
    intakeFieldsEl.appendChild(wrapper);
  }

  return true;
}

function renderQuestions(questionList, savedAnswers) {
  questions.innerHTML = "";
  clarificationState.clear();
  if (questionList.length === 0) {
    questionsBlock.hidden = true;
    return false;
  }

  questionsBlock.hidden = false;

  questionList.forEach((item, index) => {
    const questionKey = `q${index}`;

    const block = document.createElement("section");
    block.className = "question-block";

    const prompt = document.createElement("p");
    prompt.className = "question";
    prompt.textContent = item;
    block.appendChild(prompt);

    const answerEl = document.createElement("textarea");
    answerEl.className = "clarification-answer";
    answerEl.placeholder = "Your answer…";
    answerEl.rows = 3;
    answerEl.value = typeof savedAnswers[questionKey] === "string" ? savedAnswers[questionKey] : "";

    clarificationState.set(questionKey, { prompt: item, answer: answerEl.value.trim() });

    answerEl.addEventListener("input", () => {
      const state = clarificationState.get(questionKey);
      if (state) {
        state.answer = answerEl.value.trim();
        clarificationState.set(questionKey, state);
      }
      persistState();
    });
    block.appendChild(answerEl);

    questions.appendChild(block);
  });

  return true;
}

function getEditorText() {
  return body.innerText.replace(/\u00A0/g, " ").trim();
}

function setEditorText(value) {
  body.innerText = value || "";
}

function buildSubmissionBody() {
  const base = getEditorText();
  const parts = [];

  if (base) {
    parts.push(base);
  }

  const intakeSummary = buildIntakeSummary();
  if (base && intakeSummary) {
    parts.push(intakeSummary);
  }

  const answers = [];
  for (const state of clarificationState.values()) {
    if (!state.answer) {
      continue;
    }
    answers.push(`${state.prompt}\n${state.answer}`);
  }
  if (answers.length > 0) {
    parts.push(answers.join("\n\n"));
  }

  return parts.join("\n\n").trim();
}

function buildIntakeSummary() {
  const lines = [];
  for (const state of intakeState.values()) {
    if (!state.answer || !state.answerLabel) {
      continue;
    }
    lines.push(`- ${state.prompt}: ${state.answerLabel}`);
  }
  if (lines.length === 0) {
    return "";
  }
  return ["Project profile:", ...lines].join("\n");
}

function persistState() {
  if (!currentState) {
    return;
  }
  vscode.setState({
    ...currentState,
    body: getEditorText(),
    intakeAnswers: serializeAnswerState(intakeState),
    questionAnswers: serializeAnswerState(clarificationState),
  });
}

function serializeAnswerState(source) {
  const result = {};
  for (const [key, value] of source.entries()) {
    if (!value || typeof value.answer !== "string") {
      continue;
    }
    result[key] = value.answer;
  }
  return result;
}

function normalizeAnswerMap(value) {
  if (!isRecord(value)) {
    return {};
  }
  const normalized = {};
  for (const [key, entry] of Object.entries(value)) {
    if (typeof entry === "string") {
      normalized[key] = entry;
    }
  }
  return normalized;
}

function sameWizardContext(left, right) {
  return isRecord(left)
    && isRecord(right)
    && left.runId === right.runId
    && left.stageId === right.stageId
    && left.wizardId === right.wizardId;
}

function isRecord(value) {
  return typeof value === "object" && value !== null;
}
