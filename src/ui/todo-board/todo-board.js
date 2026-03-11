const vscode = acquireVsCodeApi();
const savedState = vscode.getState() || {};

const INBOX_PHASE = "__waterfree_inbox__";
const DEFAULT_TASK = {
  title: "",
  description: "",
  phase: INBOX_PHASE,
  priority: "P2",
  status: "pending",
  ownerType: "unassigned",
  ownerName: "",
  taskType: "impl",
  timing: "one_time",
  targetFile: "",
  targetLine: "",
  blockedReason: "",
  humanNotes: "",
  acceptanceCriteria: "",
  trigger: "",
};

const state = {
  board: normalizeBoard({ tasks: [], phases: [] }),
  selectionId: typeof savedState.selectionId === "string" ? savedState.selectionId : null,
  composer: { ...DEFAULT_TASK, ...(savedState.composer || {}) },
  dragTaskId: null,
  dragPhaseKey: null,
};

const overviewEl = document.getElementById("overview");
const boardEl = document.getElementById("board");
const composerEl = document.getElementById("composer");
const detailEl = document.getElementById("detail");

window.addEventListener("message", (event) => {
  const message = event.data || {};
  if (message.type !== "state") {
    return;
  }
  state.board = normalizeBoard(message.state || {});
  if (!state.selectionId || !state.board.tasksById[state.selectionId]) {
    state.selectionId = firstTaskId();
  }
  if (!state.board.phaseOrder.includes(state.composer.phase)) {
    state.composer.phase = firstPhaseKey();
  }
  persist();
  render();
});

document.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const target = event.target.closest("[data-action]");
  if (!target) {
    return;
  }
  const action = target.getAttribute("data-action");
  if (!action) {
    return;
  }

  if (action === "refresh") {
    vscode.postMessage({ type: "refresh" });
    return;
  }
  if (action === "newPhase") {
    const name = window.prompt("New phase name");
    if (name) {
      addPhase(name);
    }
    return;
  }
  if (action === "renamePhase") {
    const phaseKey = target.getAttribute("data-phase-key") || INBOX_PHASE;
    if (phaseKey !== INBOX_PHASE) {
      const current = displayPhaseName(phaseKey);
      const name = window.prompt("Rename phase", current);
      if (name) {
        renamePhase(phaseKey, name);
      }
    }
    return;
  }
  if (action === "selectTask") {
    const taskId = target.getAttribute("data-task-id");
    if (taskId && state.board.tasksById[taskId]) {
      state.selectionId = taskId;
      persist();
      renderBoard();
      renderDetail();
    }
    return;
  }
  if (action === "openTask") {
    const taskId = target.getAttribute("data-task-id");
    const task = taskId ? state.board.tasksById[taskId] : null;
    if (task) {
      vscode.postMessage({
        type: "openTask",
        file: task.targetCoord.file || "",
        line: typeof task.targetCoord.line === "number" ? task.targetCoord.line : undefined,
      });
    }
    return;
  }
  if (action === "saveTask") {
    saveSelectedTask();
    return;
  }
  if (action === "deleteTask") {
    const taskId = target.getAttribute("data-task-id") || state.selectionId;
    const task = taskId ? state.board.tasksById[taskId] : null;
    if (task && window.confirm(`Delete "${task.title || "Untitled task"}"?`)) {
      vscode.postMessage({ type: "deleteTask", taskId });
    }
    return;
  }
  if (action === "createTask") {
    createTask();
  }
});

document.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement)) {
    return;
  }
  if (!target.hasAttribute("data-composer-field")) {
    return;
  }
  const field = target.getAttribute("data-composer-field");
  if (!field) {
    return;
  }
  state.composer[field] = target.value;
  persist();
});

document.addEventListener("dragstart", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const target = event.target.closest("[data-draggable-task], [data-draggable-phase]");
  if (!target || !event.dataTransfer) {
    return;
  }
  if (target.hasAttribute("data-draggable-task")) {
    state.dragTaskId = target.getAttribute("data-task-id");
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", state.dragTaskId || "");
    return;
  }
  state.dragPhaseKey = target.getAttribute("data-phase-key");
  event.dataTransfer.effectAllowed = "move";
  event.dataTransfer.setData("text/plain", state.dragPhaseKey || "");
});

document.addEventListener("dragover", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const dropZone = event.target.closest("[data-drop-phase], [data-drop-before-task-id], [data-drop-phase-card], [data-drop-before-phase-key]");
  if (!dropZone) {
    return;
  }
  event.preventDefault();
  if (event.dataTransfer) {
    event.dataTransfer.dropEffect = "move";
  }
});

document.addEventListener("dragenter", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const dropZone = event.target.closest("[data-drop-phase], [data-drop-before-task-id], [data-drop-phase-card], [data-drop-before-phase-key]");
  if (dropZone) {
    dropZone.classList.add("drag-target");
  }
});

document.addEventListener("dragleave", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const dropZone = event.target.closest(".drag-target");
  if (dropZone && !dropZone.contains(event.relatedTarget)) {
    dropZone.classList.remove("drag-target");
  }
});

document.addEventListener("drop", (event) => {
  if (!(event.target instanceof Element)) {
    return;
  }
  const taskDrop = event.target.closest("[data-drop-phase], [data-drop-before-task-id], [data-drop-phase-card]");
  const phaseDrop = event.target.closest("[data-drop-before-phase-key]");
  clearDragTargets();

  if (state.dragTaskId && taskDrop) {
    event.preventDefault();
    const phaseKey = taskDrop.getAttribute("data-drop-phase")
      || taskDrop.getAttribute("data-drop-phase-card")
      || INBOX_PHASE;
    const beforeTaskId = taskDrop.getAttribute("data-drop-before-task-id");
    moveTask(state.dragTaskId, phaseKey, beforeTaskId || null);
    state.dragTaskId = null;
    return;
  }

  if (state.dragPhaseKey && phaseDrop) {
    event.preventDefault();
    const beforePhaseKey = phaseDrop.getAttribute("data-drop-before-phase-key");
    if (beforePhaseKey) {
      movePhase(state.dragPhaseKey, beforePhaseKey);
    }
  }

  state.dragTaskId = null;
  state.dragPhaseKey = null;
});

function render() {
  renderOverview();
  renderBoard();
  renderComposer();
  renderDetail();
}

function renderOverview() {
  const tasks = orderedTasks();
  const total = tasks.length;
  const complete = tasks.filter((task) => task.status === "complete").length;
  const active = tasks.filter((task) => task.status === "executing").length;
  const blocked = tasks.filter((task) => (task.dependsOn || []).length > 0 && task.status !== "complete").length;
  const progress = total === 0 ? 0 : Math.round((complete / total) * 100);
  const phases = state.board.phaseOrder.filter((phase) => phase !== INBOX_PHASE).length;

  overviewEl.innerHTML = [
    statCard("Tasks", total),
    statCard("Completed", complete),
    statCard("Active", active),
    statCard("Blocked", blocked),
    statCard("Progress", `${progress}%`, `${phases} phase${phases === 1 ? "" : "s"}`),
  ].join("");
}

function renderBoard() {
  const markup = state.board.phaseOrder.map((phaseKey) => renderPhase(phaseKey)).join("");
  boardEl.innerHTML = markup || '<div class="empty-state">No tasks yet. Start by creating one or queueing a `[wf] TODO`.</div>';
}

function renderPhase(phaseKey) {
  const taskIds = state.board.phaseTasks[phaseKey] || [];
  const tasks = taskIds.map((taskId) => state.board.tasksById[taskId]).filter(Boolean);
  const complete = tasks.filter((task) => task.status === "complete").length;
  const progress = tasks.length === 0 ? 0 : Math.round((complete / tasks.length) * 100);

  return [
    `<section class="phase-card" data-drop-phase-card="${escapeHtml(phaseKey)}">`,
    `<div class="phase-header" draggable="true" data-draggable-phase="true" data-phase-key="${escapeHtml(phaseKey)}" data-drop-before-phase-key="${escapeHtml(phaseKey)}">`,
    '<div>',
    '<div class="phase-title-row">',
    `<h2 class="phase-title">${escapeHtml(displayPhaseName(phaseKey))}</h2>`,
    `<span class="meta-pill">${tasks.length} task${tasks.length === 1 ? "" : "s"}</span>`,
    '</div>',
    `<div class="phase-meta"><div class="phase-progress"><div class="phase-progress-bar" style="width:${progress}%"></div></div><span class="muted">${progress}% shipped</span></div>`,
    '</div>',
    '<div class="phase-actions">',
    phaseKey === INBOX_PHASE ? "" : `<button type="button" class="icon-btn" data-action="renamePhase" data-phase-key="${escapeHtml(phaseKey)}">Edit</button>`,
    '</div>',
    '</div>',
    `<div class="phase-body" data-drop-phase="${escapeHtml(phaseKey)}">`,
    tasks.length ? `<div class="task-stack">${tasks.map((task) => renderTask(task, phaseKey)).join("")}</div>` : '<div class="empty-state">Drop tasks here to reshape the flow.</div>',
    '</div>',
    '</section>',
  ].join("");
}

function renderTask(task, phaseKey) {
  const deps = (task.dependsOn || []).map((dep) => state.board.tasksById[dep.taskId]?.title).filter(Boolean);
  const ownerName = task.owner && task.owner.name ? task.owner.name : task.owner?.type || "unassigned";
  const depth = taskDepth(task, phaseKey);
  const selected = state.selectionId === task.id ? " selected" : "";

  return [
    `<article class="task-card priority-${escapeHtml(task.priority)}${selected}" data-depth="${depth}" draggable="true" data-draggable-task="true" data-task-id="${escapeHtml(task.id)}" data-drop-phase="${escapeHtml(phaseKey)}" data-drop-before-task-id="${escapeHtml(task.id)}">`,
    `<div class="task-topline"><div><h3 class="task-title">${escapeHtml(task.title || "Untitled task")}</h3><p class="task-description">${escapeHtml(task.description || "No description yet.")}</p></div>${statusPill(task.status)}</div>`,
    '<div class="task-meta-row">',
    `<span class="priority-pill priority-${escapeHtml(task.priority)}">${escapeHtml(task.priority)}</span>`,
    `<span class="task-owner"><strong>${escapeHtml(ownerName)}</strong></span>`,
    '</div>',
    `<div class="task-meta-row"><span class="task-location">${escapeHtml(formatCoord(task.targetCoord))}</span>${deps.length ? `<span class="task-deps"><strong>Depends on</strong> ${escapeHtml(deps.join(", "))}</span>` : ""}</div>`,
    `<div class="button-row"><button type="button" class="ghost-btn" data-action="selectTask" data-task-id="${escapeHtml(task.id)}">Details</button><button type="button" class="ghost-btn" data-action="openTask" data-task-id="${escapeHtml(task.id)}">Open</button></div>`,
    '</article>',
  ].join("");
}

function renderComposer() {
  composerEl.innerHTML = [
    '<div class="detail-header"><div><h2 class="detail-title">Add Task</h2><p class="detail-subtitle">Capture work without leaving the board.</p></div></div>',
    fieldText("Title", "composer-title", state.composer.title, true, "title"),
    fieldTextarea("Description", "composer-description", state.composer.description, "description"),
    '<div class="field-row">',
    fieldSelect("Phase", "composer-phase", phaseOptions(), state.composer.phase, "phase"),
    fieldSelect("Priority", "composer-priority", priorityOptions(), state.composer.priority, "priority"),
    '</div>',
    '<div class="field-row">',
    fieldSelect("Status", "composer-status", statusOptions(), state.composer.status, "status"),
    fieldSelect("Task Type", "composer-task-type", taskTypeOptions(), state.composer.taskType, "taskType"),
    '</div>',
    '<div class="field-row">',
    fieldSelect("Timing", "composer-timing", timingOptions(), state.composer.timing, "timing"),
    '</div>',
    '<div class="button-row"><button type="button" class="primary-btn" data-action="createTask">Create Task</button></div>',
  ].join("");
}

function renderDetail() {
  const task = selectedTask();
  if (!task) {
    detailEl.innerHTML = [
      '<div class="detail-header"><div><h2 class="detail-title">Task Details</h2><p class="detail-subtitle">Select a task card to edit its scope and dependencies.</p></div></div>',
      '<div class="empty-state">Choose a task to edit it here.</div>',
    ].join("");
    return;
  }

  const dependencyOptions = orderedTasks()
    .filter((candidate) => candidate.id !== task.id)
    .map((candidate) => {
      const checked = (task.dependsOn || []).some((dep) => dep.taskId === candidate.id) ? " checked" : "";
      return `<label class="dependency-option"><input type="checkbox" data-dependency-id="${escapeHtml(candidate.id)}"${checked}> <span>${escapeHtml(candidate.title || "Untitled task")} <span class="muted">(${escapeHtml(displayPhaseName(normalizePhase(candidate.phase)))})</span></span></label>`;
    })
    .join("");

  detailEl.innerHTML = [
    `<div class="detail-header"><div><h2 class="detail-title">${escapeHtml(task.title || "Untitled task")}</h2><p class="detail-subtitle">${escapeHtml(task.id)}</p></div>${statusPill(task.status)}</div>`,
    fieldText("Title", "detail-title", task.title),
    fieldTextarea("Description", "detail-description", task.description),
    '<div class="field-row">',
    fieldSelect("Phase", "detail-phase", phaseOptions(), normalizePhase(task.phase)),
    fieldSelect("Priority", "detail-priority", priorityOptions(), task.priority),
    '</div>',
    '<div class="field-row">',
    fieldSelect("Status", "detail-status", statusOptions(), task.status),
    fieldSelect("Owner Type", "detail-owner-type", ownerTypeOptions(), task.owner?.type || "unassigned"),
    '</div>',
    '<div class="field-row">',
    fieldText("Owner Name", "detail-owner-name", task.owner?.name || ""),
    fieldSelect("Task Type", "detail-task-type", taskTypeOptions(), task.taskType || "impl"),
    '</div>',
    '<div class="field-row">',
    fieldSelect("Timing", "detail-timing", timingOptions(), task.timing || "one_time"),
    '</div>',
    '<div class="field-row">',
    fieldText("Target File", "detail-target-file", task.targetCoord?.file || ""),
    fieldText("Target Line", "detail-target-line", task.targetCoord?.line ? String(task.targetCoord.line) : ""),
    '</div>',
    fieldText("Trigger", "detail-trigger", task.trigger || ""),
    fieldTextarea("Acceptance Criteria", "detail-acceptance-criteria", task.acceptanceCriteria || ""),
    fieldText("Blocked Reason", "detail-blocked-reason", task.blockedReason || ""),
    fieldTextarea("Human Notes", "detail-human-notes", task.humanNotes || ""),
    '<div class="field-group"><label class="field-label">Dependencies</label>',
    dependencyOptions ? `<div class="dependency-grid">${dependencyOptions}</div>` : '<div class="empty-state">No other tasks available yet.</div>',
    '</div>',
    `<div class="button-row"><button type="button" class="primary-btn" data-action="saveTask">Save Changes</button><button type="button" class="ghost-btn" data-action="openTask" data-task-id="${escapeHtml(task.id)}">Open Target</button><button type="button" class="danger-btn" data-action="deleteTask" data-task-id="${escapeHtml(task.id)}">Delete</button></div>`,
  ].join("");
}

function createTask() {
  const title = valueOf("composer-title").trim();
  if (!title) {
    window.alert("Task title is required.");
    return;
  }
  const phase = valueOf("composer-phase");
  const task = {
    title,
    description: valueOf("composer-description").trim(),
    phase: phase === INBOX_PHASE ? null : phase,
    priority: valueOf("composer-priority"),
    status: valueOf("composer-status"),
    taskType: valueOf("composer-task-type"),
    timing: valueOf("composer-timing"),
    owner: { type: "unassigned", name: "" },
    targetCoord: { file: "", line: null, anchorType: "modify" },
  };
  vscode.postMessage({ type: "addTask", task });
  state.composer = { ...DEFAULT_TASK, phase: firstPhaseKey() };
  persist();
  renderComposer();
}

function saveSelectedTask() {
  const task = selectedTask();
  if (!task) {
    return;
  }
  const dependsOn = Array.from(detailEl.querySelectorAll("[data-dependency-id]:checked"))
    .map((input) => input.getAttribute("data-dependency-id"))
    .filter(Boolean)
    .map((taskId) => {
      const existing = (task.dependsOn || []).find((dep) => dep.taskId === taskId);
      return existing || { taskId, type: "blocks" };
    });
  const phase = valueOf("detail-phase");
  const patch = {
    title: valueOf("detail-title").trim(),
    description: valueOf("detail-description").trim(),
    phase: phase === INBOX_PHASE ? "" : phase,
    priority: valueOf("detail-priority"),
    status: valueOf("detail-status"),
    taskType: valueOf("detail-task-type"),
    timing: valueOf("detail-timing"),
    trigger: valueOf("detail-trigger").trim() || null,
    acceptanceCriteria: valueOf("detail-acceptance-criteria").trim() || null,
    blockedReason: valueOf("detail-blocked-reason").trim(),
    humanNotes: valueOf("detail-human-notes").trim(),
    owner: {
      type: valueOf("detail-owner-type"),
      name: valueOf("detail-owner-name").trim(),
    },
    targetCoord: {
      ...(task.targetCoord || { anchorType: "modify" }),
      file: valueOf("detail-target-file").trim(),
      line: parsePositiveInt(valueOf("detail-target-line")),
    },
    dependsOn,
  };
  vscode.postMessage({ type: "updateTask", taskId: task.id, patch });
}

function addPhase(name) {
  const phaseName = String(name || "").trim();
  if (!phaseName) {
    return;
  }
  if (state.board.phaseOrder.some((phaseKey) => displayPhaseName(phaseKey).toLowerCase() === phaseName.toLowerCase())) {
    return;
  }
  state.board.phaseOrder.push(phaseName);
  state.board.phaseTasks[phaseName] = state.board.phaseTasks[phaseName] || [];
  persist();
  render();
  saveBoard();
}

function renamePhase(phaseKey, nextName) {
  const phaseName = String(nextName || "").trim();
  if (!phaseName || phaseName === phaseKey) {
    return;
  }
  if (state.board.phaseOrder.includes(phaseName)) {
    return;
  }
  state.board.phaseOrder = state.board.phaseOrder.map((candidate) => candidate === phaseKey ? phaseName : candidate);
  state.board.phaseTasks[phaseName] = state.board.phaseTasks[phaseKey] || [];
  delete state.board.phaseTasks[phaseKey];
  (state.board.phaseTasks[phaseName] || []).forEach((taskId) => {
    if (state.board.tasksById[taskId]) {
      state.board.tasksById[taskId].phase = phaseName;
    }
  });
  if (state.composer.phase === phaseKey) {
    state.composer.phase = phaseName;
  }
  persist();
  render();
  saveBoard();
}

function moveTask(taskId, targetPhaseKey, beforeTaskId) {
  const task = state.board.tasksById[taskId];
  if (!task) {
    return;
  }
  if (beforeTaskId && beforeTaskId === taskId) {
    return;
  }
  const currentPhaseKey = normalizePhase(task.phase);
  state.board.phaseTasks[currentPhaseKey] = (state.board.phaseTasks[currentPhaseKey] || []).filter((id) => id !== taskId);
  const nextIds = [...(state.board.phaseTasks[targetPhaseKey] || [])].filter((id) => id !== taskId);
  const insertIndex = beforeTaskId ? nextIds.indexOf(beforeTaskId) : -1;
  if (insertIndex >= 0) {
    nextIds.splice(insertIndex, 0, taskId);
  } else {
    nextIds.push(taskId);
  }
  state.board.phaseTasks[targetPhaseKey] = nextIds;
  task.phase = targetPhaseKey === INBOX_PHASE ? null : targetPhaseKey;
  if (!state.board.phaseOrder.includes(targetPhaseKey)) {
    state.board.phaseOrder.push(targetPhaseKey);
  }
  persist();
  renderBoard();
  renderDetail();
  saveBoard();
}

function movePhase(phaseKey, beforePhaseKey) {
  if (!phaseKey || !beforePhaseKey || phaseKey === beforePhaseKey) {
    return;
  }
  const remaining = state.board.phaseOrder.filter((candidate) => candidate !== phaseKey);
  const index = remaining.indexOf(beforePhaseKey);
  remaining.splice(index >= 0 ? index : remaining.length, 0, phaseKey);
  state.board.phaseOrder = remaining;
  persist();
  renderBoard();
  saveBoard();
}

function saveBoard() {
  const tasks = [];
  for (const phaseKey of state.board.phaseOrder) {
    for (const taskId of state.board.phaseTasks[phaseKey] || []) {
      tasks.push({
        id: taskId,
        phase: phaseKey === INBOX_PHASE ? null : phaseKey,
      });
    }
  }
  vscode.postMessage({
    type: "saveBoard",
    tasks,
    phases: state.board.phaseOrder.filter((phaseKey) => phaseKey !== INBOX_PHASE),
  });
}

function normalizeBoard(raw) {
  const tasks = Array.isArray(raw.tasks) ? raw.tasks.map((task) => normalizeTask(task)) : [];
  const phaseOrder = [];
  const phaseTasks = {};
  const tasksById = {};
  const phases = Array.isArray(raw.phases) ? raw.phases.map((phase) => String(phase || "").trim()).filter(Boolean) : [];
  const hasInbox = tasks.some((task) => normalizePhase(task.phase) === INBOX_PHASE);

  if (hasInbox) {
    phaseOrder.push(INBOX_PHASE);
    phaseTasks[INBOX_PHASE] = [];
  }
  for (const phase of phases) {
    if (!phaseTasks[phase]) {
      phaseOrder.push(phase);
      phaseTasks[phase] = [];
    }
  }
  for (const task of tasks) {
    tasksById[task.id] = task;
    const phaseKey = normalizePhase(task.phase);
    if (!phaseTasks[phaseKey]) {
      phaseOrder.push(phaseKey);
      phaseTasks[phaseKey] = [];
    }
    phaseTasks[phaseKey].push(task.id);
  }
  if (phaseOrder.length === 0) {
    phaseOrder.push(INBOX_PHASE);
    phaseTasks[INBOX_PHASE] = [];
  }
  return {
    updatedAt: raw.updatedAt || "",
    path: raw.path || "",
    tasksById,
    phaseOrder,
    phaseTasks,
  };
}

function normalizeTask(raw) {
  return {
    id: String(raw.id || ""),
    title: String(raw.title || ""),
    description: String(raw.description || ""),
    rationale: raw.rationale || "",
    targetCoord: {
      file: String(raw.targetCoord?.file || ""),
      class: raw.targetCoord?.class || "",
      method: raw.targetCoord?.method || "",
      line: typeof raw.targetCoord?.line === "number" ? raw.targetCoord.line : null,
      anchorType: raw.targetCoord?.anchorType || "modify",
    },
    contextCoords: Array.isArray(raw.contextCoords) ? raw.contextCoords : [],
    priority: String(raw.priority || "P2"),
    phase: raw.phase || null,
    dependsOn: Array.isArray(raw.dependsOn) ? raw.dependsOn : [],
    blockedReason: raw.blockedReason || "",
    owner: raw.owner || { type: "unassigned", name: "" },
    taskType: raw.taskType || "impl",
    timing: raw.timing || "one_time",
    estimatedMinutes: raw.estimatedMinutes,
    actualMinutes: raw.actualMinutes,
    status: raw.status || "pending",
    humanNotes: raw.humanNotes || "",
    aiNotes: raw.aiNotes || "",
    annotations: Array.isArray(raw.annotations) ? raw.annotations : [],
    acceptanceCriteria: raw.acceptanceCriteria || "",
    trigger: raw.trigger || "",
  };
}

function orderedTasks() {
  const items = [];
  for (const phaseKey of state.board.phaseOrder) {
    for (const taskId of state.board.phaseTasks[phaseKey] || []) {
      const task = state.board.tasksById[taskId];
      if (task) {
        items.push(task);
      }
    }
  }
  return items;
}

function selectedTask() {
  return state.selectionId ? state.board.tasksById[state.selectionId] : null;
}

function firstTaskId() {
  return orderedTasks()[0]?.id || null;
}

function firstPhaseKey() {
  return state.board.phaseOrder[0] || INBOX_PHASE;
}

function normalizePhase(phase) {
  const value = String(phase || "").trim();
  return value || INBOX_PHASE;
}

function displayPhaseName(phaseKey) {
  return phaseKey === INBOX_PHASE ? "Inbox" : phaseKey;
}

function formatCoord(coord) {
  if (!coord) {
    return "No target";
  }
  const file = String(coord.file || "").trim();
  const fileName = file ? file.split(/[\\/]/).pop() : "No target";
  const symbol = coord.method || coord.class || "";
  const line = typeof coord.line === "number" ? `:${coord.line}` : "";
  return `${fileName}${symbol ? `::${symbol}` : ""}${line}`;
}

function statCard(label, value, hint) {
  return [
    '<article class="stat-card">',
    `<span class="stat-label">${escapeHtml(label)}</span>`,
    `<span class="stat-value">${escapeHtml(String(value))}</span>`,
    hint ? `<span class="muted">${escapeHtml(hint)}</span>` : "",
    '</article>',
  ].join("");
}

function statusPill(status) {
  return `<span class="status-pill status-${escapeHtml(status)}">${escapeHtml(status)}</span>`;
}

function taskDepth(task, phaseKey) {
  const phaseTaskIds = new Set(state.board.phaseTasks[phaseKey] || []);
  let depth = 0;
  for (const dep of task.dependsOn || []) {
    if (phaseTaskIds.has(dep.taskId)) {
      depth += 1;
    }
  }
  return Math.min(depth, 2);
}

function phaseOptions() {
  const options = state.board.phaseOrder.map((phaseKey) => ({
    value: phaseKey,
    label: displayPhaseName(phaseKey),
  }));
  if (!options.some((option) => option.value === INBOX_PHASE)) {
    options.unshift({ value: INBOX_PHASE, label: "Inbox" });
  }
  return options;
}

function priorityOptions() {
  return ["P0", "P1", "P2", "P3", "spike"].map((value) => ({ value, label: value }));
}

function statusOptions() {
  return ["pending", "annotating", "negotiating", "executing", "complete", "skipped"].map((value) => ({ value, label: value }));
}

function ownerTypeOptions() {
  return ["unassigned", "human", "agent"].map((value) => ({ value, label: value }));
}

function taskTypeOptions() {
  return [
    { value: "impl",     label: "Implementation" },
    { value: "test",     label: "Test" },
    { value: "spike",    label: "Spike" },
    { value: "review",   label: "Review" },
    { value: "refactor", label: "Refactor" },
    { value: "protocol", label: "Protocol" },
    { value: "bug_fix",  label: "Bug Fix" },
    { value: "feature",  label: "Feature" },
    { value: "task",     label: "Task" },
  ];
}

function timingOptions() {
  return [
    { value: "one_time",   label: "One and Done" },
    { value: "recurring",  label: "Recurring" },
  ];
}

function fieldText(label, id, value, autofocus, composerField) {
  return [
    '<div class="field-group">',
    `<label class="field-label" for="${escapeHtml(id)}">${escapeHtml(label)}</label>`,
    `<input id="${escapeHtml(id)}" type="text" value="${escapeHtml(value || "")}"${autofocus ? " autofocus" : ""}${composerField ? ` data-composer-field="${escapeHtml(composerField)}"` : ""}>`,
    '</div>',
  ].join("");
}

function fieldTextarea(label, id, value, composerField) {
  return [
    '<div class="field-group">',
    `<label class="field-label" for="${escapeHtml(id)}">${escapeHtml(label)}</label>`,
    `<textarea id="${escapeHtml(id)}"${composerField ? ` data-composer-field="${escapeHtml(composerField)}"` : ""}>${escapeHtml(value || "")}</textarea>`,
    '</div>',
  ].join("");
}

function fieldSelect(label, id, options, selected, composerField) {
  const markup = options.map((option) => (
    `<option value="${escapeHtml(option.value)}"${option.value === selected ? " selected" : ""}>${escapeHtml(option.label)}</option>`
  )).join("");
  return [
    '<div class="field-group">',
    `<label class="field-label" for="${escapeHtml(id)}">${escapeHtml(label)}</label>`,
    `<select id="${escapeHtml(id)}"${composerField ? ` data-composer-field="${escapeHtml(composerField)}"` : ""}>${markup}</select>`,
    '</div>',
  ].join("");
}

function parsePositiveInt(value) {
  const parsed = Number.parseInt(String(value || "").trim(), 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
}

function valueOf(id) {
  const element = document.getElementById(id);
  if (!element) {
    return "";
  }
  if (element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement) {
    return element.value;
  }
  return "";
}

function clearDragTargets() {
  document.querySelectorAll(".drag-target").forEach((element) => {
    element.classList.remove("drag-target");
  });
}

function persist() {
  vscode.setState({
    selectionId: state.selectionId,
    composer: state.composer,
  });
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

render();
