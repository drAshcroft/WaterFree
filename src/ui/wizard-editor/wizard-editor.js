const vscode = acquireVsCodeApi();

const chunkTitle = document.getElementById("chunk-title");
const guidance = document.getElementById("guidance");
const body = document.getElementById("body");
const clarifications = document.getElementById("clarifications");
const questions = document.getElementById("questions");
const submit = document.getElementById("submit");
const refine = document.getElementById("refine");

let currentState = null;

window.addEventListener("message", (event) => {
  const message = event.data;
  if (!message || message.type !== "state") {
    return;
  }

  currentState = message.state;
  chunkTitle.textContent = currentState.chunkTitle || "What is the idea?";
  guidance.textContent = currentState.guidance || "Describe the software idea, problem you want to solve or frustration in plain language.";
  setEditorText(currentState.body || "");
  renderQuestions(Array.isArray(currentState.questions) ? currentState.questions : []);
  vscode.setState(currentState);
});

body.addEventListener("input", () => {
  vscode.postMessage({ type: "draftChanged", body: getEditorText() });
});

submit.addEventListener("click", () => {
  vscode.postMessage({ type: "submit", body: getEditorText() });
});

refine.addEventListener("click", () => {
  vscode.postMessage({ type: "refine", body: getEditorText() });
});

const persisted = vscode.getState();
if (persisted) {
  chunkTitle.textContent = persisted.chunkTitle || "What is the idea?";
  guidance.textContent = persisted.guidance || "Describe the software idea, problem you want to solve or frustration in plain language.";
  setEditorText(persisted.body || "");
  renderQuestions(Array.isArray(persisted.questions) ? persisted.questions : []);
}

function getEditorText() {
  return body.innerText.replace(/\u00A0/g, " ").trim();
}

function setEditorText(value) {
  body.innerText = value || "";
}

function renderQuestions(questionList) {
  questions.innerHTML = "";
  if (questionList.length === 0) {
    clarifications.hidden = true;
    return;
  }

  clarifications.hidden = false;
  for (const item of questionList) {
    const p = document.createElement("p");
    p.className = "question";
    p.textContent = item;
    questions.appendChild(p);
  }
}
