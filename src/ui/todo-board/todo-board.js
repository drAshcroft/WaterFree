const vscode = acquireVsCodeApi();
const savedState = vscode.getState() || {};

const PRIORITIES = ["P0", "P1", "P2", "P3", "spike"];
const STATUSES = ["pending", "annotating", "negotiating", "executing", "complete", "skipped"];
const DEP_TYPES = ["blocks", "informs", "shares-file"];
const DEFAULT_DRAFT = {
  title: "", description: "", rationale: "", phase: "", priority: "P2", status: "pending",
  taskType: "impl", timing: "one_time", ownerType: "unassigned", ownerName: "", ownerAssignedAt: "",
  targetFile: "", targetClass: "", targetMethod: "", targetLine: "", anchorType: "modify",
  blockedReason: "", trigger: "", acceptanceCriteria: "", humanNotes: "", aiNotes: "",
  estimatedMinutes: "", actualMinutes: "", startedAt: "", completedAt: "",
};

const state = {
  board: normalizeBoard({ tasks: [], phases: [] }),
  selectionId: typeof savedState.selectionId === "string" ? savedState.selectionId : null,
  editorMode: savedState.editorMode === "new" ? "new" : "task",
  draftTask: { ...DEFAULT_DRAFT, ...(savedState.draftTask || {}) },
  searchQuery: typeof savedState.searchQuery === "string" ? savedState.searchQuery : "",
  treeMode: savedState.treeMode === "dependencies" ? "dependencies" : "priority",
  collapsedGroups: isRecord(savedState.collapsedGroups) ? savedState.collapsedGroups : {},
};

const summaryEl = document.getElementById("summary");
const treeEl = document.getElementById("tree");
const editorEl = document.getElementById("editor");
const treeCountEl = document.getElementById("tree-count");
const searchInput = document.getElementById("search-input");
const treeModeSelect = document.getElementById("tree-mode");

window.addEventListener("resize", applyViewportMode);

window.addEventListener("message", (event) => {
  const message = event.data || {};
  if (message.type !== "state") { return; }
  state.board = normalizeBoard(message.state || {});
  if (!state.selectionId || !state.board.tasksById[state.selectionId]) {
    state.selectionId = orderedTasks()[0]?.id || null;
  }
  if (!state.board.phaseOptions.includes(state.draftTask.phase || "")) {
    state.draftTask.phase = "";
  }
  persist();
  render();
});

document.addEventListener("click", (event) => {
  if (!(event.target instanceof Element)) { return; }
  const el = event.target.closest("[data-action]");
  if (!el) { return; }
  const action = el.getAttribute("data-action");
  if (!action) { return; }

  if (action === "refresh") { vscode.postMessage({ type: "refresh" }); return; }
  if (action === "startNewTask") {
    state.editorMode = "new";
    state.draftTask = { ...DEFAULT_DRAFT, phase: state.board.phaseOptions[0] || "" };
    persist();
    renderEditor();
    return;
  }
  if (action === "cancelNewTask") { state.editorMode = "task"; persist(); renderEditor(); return; }
  if (action === "toggleGroup") {
    const groupId = el.getAttribute("data-group-id") || "";
    state.collapsedGroups[groupId] = !groupOpen(groupId);
    persist();
    renderTree();
    return;
  }
  if (action === "selectTask") {
    const taskId = el.getAttribute("data-task-id");
    if (taskId && state.board.tasksById[taskId]) {
      state.selectionId = taskId;
      state.editorMode = "task";
      persist();
      renderTree();
      renderEditor();
    }
    return;
  }
  if (action === "openTask") {
    const taskId = el.getAttribute("data-task-id") || state.selectionId;
    const task = taskId ? state.board.tasksById[taskId] : null;
    if (task) {
      vscode.postMessage({ type: "openTask", file: task.targetCoord.file || "", line: task.targetCoord.line ?? undefined });
    }
    return;
  }
  if (action === "createTask") { createTask(); return; }
  if (action === "saveTask") { saveSelectedTask(); return; }
  if (action === "deleteTask") {
    const taskId = el.getAttribute("data-task-id") || state.selectionId;
    const task = taskId ? state.board.tasksById[taskId] : null;
    if (task && window.confirm(`Delete "${task.title || "Untitled task"}"?`)) {
      vscode.postMessage({ type: "deleteTask", taskId });
    }
  }
});

document.addEventListener("input", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement)) { return; }
  if (target.id === "search-input") {
    state.searchQuery = target.value;
    persist();
    renderSummary();
    renderTree();
    return;
  }
  const draftField = target.getAttribute("data-draft-field");
  if (draftField) { state.draftTask[draftField] = target.value; persist(); }
});

document.addEventListener("change", (event) => {
  const target = event.target;
  if (!(target instanceof HTMLInputElement || target instanceof HTMLTextAreaElement || target instanceof HTMLSelectElement)) { return; }
  if (target.id === "tree-mode") {
    state.treeMode = target.value === "dependencies" ? "dependencies" : "priority";
    persist();
    renderTree();
    return;
  }
  const draftField = target.getAttribute("data-draft-field");
  if (draftField) { state.draftTask[draftField] = target.value; persist(); }
});

function render() {
  applyViewportMode();
  if (searchInput) { searchInput.value = state.searchQuery; }
  if (treeModeSelect) { treeModeSelect.value = state.treeMode; }
  renderSummary();
  renderTree();
  renderEditor();
}

function renderSummary() {
  if (!summaryEl) { return; }
  const all = orderedTasks();
  const visible = filteredTasks();
  const blocked = all.filter((task) => task.dependsOn.length && task.status !== "complete").length;
  const recurring = all.filter((task) => task.timing === "recurring").length;
  const active = all.filter((task) => task.status === "executing").length;
  const tracked = all.filter((task) => task.estimatedMinutes || task.actualMinutes).length;
  summaryEl.innerHTML = [
    summaryCard("Tasks", all.length, `${visible.length} visible`),
    summaryCard("Active", active, "Executing now"),
    summaryCard("Blocked", blocked, "Depends on other work"),
    summaryCard("Recurring", recurring, "Cycles back into backlog"),
    summaryCard("Tracked", tracked, "Has timing data"),
  ].join("");
}

function renderTree() {
  const visible = filteredTasks();
  const total = orderedTasks().length;
  if (treeCountEl) { treeCountEl.textContent = state.searchQuery ? `${visible.length} of ${total} Tasks` : `${visible.length} Tasks`; }
  const groups = state.treeMode === "dependencies" ? dependencyGroups(visible) : priorityGroups(visible);
  treeEl.innerHTML = groups.length ? groups.map(renderGroup).join("") : '<div class="empty-state">No tasks match the current search.</div>';
}

function renderEditor() {
  if (state.editorMode === "new") { editorEl.innerHTML = renderNewEditor(); return; }
  const task = selectedTask();
  if (!task) {
    editorEl.innerHTML = '<section class="editor-card"><div class="editor-card-body"><div class="section-accent section-accent--muted"></div><div class="empty-state">Select a task from the tree or create a new one.</div><div class="button-row"><button type="button" class="primary" data-action="startNewTask">New Task</button></div></div></section>';
    return;
  }
  editorEl.innerHTML = renderTaskEditor(task);
}

function createTask() {
  const title = valueOf("new-title").trim();
  if (!title) { window.alert("Task title is required."); return; }
  vscode.postMessage({ type: "addTask", task: collectForm("new") });
  state.draftTask = { ...DEFAULT_DRAFT, phase: state.board.phaseOptions[0] || "" };
  persist();
  renderEditor();
}

function saveSelectedTask() {
  const task = selectedTask();
  if (!task) { return; }
  const patch = collectForm("detail");
  patch.dependsOn = Array.from(document.querySelectorAll("[data-dependency-checkbox]"))
    .filter((input) => input instanceof HTMLInputElement && input.checked)
    .map((input) => {
      const taskId = input.getAttribute("data-dependency-checkbox") || "";
      const select = document.querySelector(`[data-dependency-type="${cssEscape(taskId)}"]`);
      return { taskId, type: select instanceof HTMLSelectElement ? select.value : "blocks" };
    })
    .filter((dep) => dep.taskId);
  vscode.postMessage({ type: "updateTask", taskId: task.id, patch });
}

function collectForm(prefix) {
  return {
    title: valueOf(`${prefix}-title`).trim(),
    description: valueOf(`${prefix}-description`).trim(),
    rationale: valueOf(`${prefix}-rationale`).trim(),
    phase: emptyToBlank(valueOf(`${prefix}-phase`)),
    priority: valueOf(`${prefix}-priority`),
    status: valueOf(`${prefix}-status`),
    taskType: valueOf(`${prefix}-task-type`),
    timing: valueOf(`${prefix}-timing`),
    blockedReason: emptyToNull(valueOf(`${prefix}-blocked-reason`)),
    owner: {
      type: valueOf(`${prefix}-owner-type`),
      name: valueOf(`${prefix}-owner-name`).trim(),
      assignedAt: emptyToNull(valueOf(`${prefix}-owner-assigned-at`)),
    },
    targetCoord: {
      file: valueOf(`${prefix}-target-file`).trim(),
      class: emptyToNull(valueOf(`${prefix}-target-class`)),
      method: emptyToNull(valueOf(`${prefix}-target-method`)),
      line: parseNullableInt(valueOf(`${prefix}-target-line`)),
      anchorType: valueOf(`${prefix}-anchor-type`) || "modify",
    },
    estimatedMinutes: parseNullableInt(valueOf(`${prefix}-estimated-minutes`)),
    actualMinutes: parseNullableInt(valueOf(`${prefix}-actual-minutes`)),
    humanNotes: emptyToNull(valueOf(`${prefix}-human-notes`)),
    aiNotes: emptyToNull(valueOf(`${prefix}-ai-notes`)),
    acceptanceCriteria: emptyToNull(valueOf(`${prefix}-acceptance-criteria`)),
    trigger: emptyToNull(valueOf(`${prefix}-trigger`)),
    startedAt: emptyToNull(valueOf(`${prefix}-started-at`)),
    completedAt: emptyToNull(valueOf(`${prefix}-completed-at`)),
  };
}

function priorityGroups(tasks) {
  return PRIORITIES.map((priority) => {
    const items = tasks.filter((task) => task.priority === priority);
    return {
      id: `priority:${priority}`,
      label: priority === "spike" ? "Spikes" : `${priority} Tasks`,
      count: items.length,
      content: items.map((task) => renderTreeTask(task, 0, `${depLabel(task)} . ${labelize(task.status)}`)).join(""),
    };
  }).filter((group) => group.count > 0);
}

function dependencyGroups(tasks) {
  const order = {};
  const visibleIds = new Set(tasks.map((task, index) => { order[task.id] = index; return task.id; }));
  const children = {};
  const parents = {};
  tasks.forEach((task) => { children[task.id] = []; parents[task.id] = 0; });
  tasks.forEach((task) => {
    task.dependsOn.forEach((dep) => {
      if (!visibleIds.has(dep.taskId)) { return; }
      children[dep.taskId].push(task.id);
      parents[task.id] += 1;
    });
  });
  Object.keys(children).forEach((taskId) => children[taskId].sort((a, b) => (order[a] ?? 0) - (order[b] ?? 0)));
  const rendered = new Set();
  const roots = tasks.filter((task) => parents[task.id] === 0);
  const forest = roots.map((task) => renderDepNode(task.id, 0, children, rendered, new Set())).join("");
  const leftovers = tasks.filter((task) => !rendered.has(task.id));
  const groups = [];
  if (forest) { groups.push({ id: "dependencies:forest", label: "Dependency Forest", count: roots.length, content: forest }); }
  if (leftovers.length) {
    groups.push({ id: "dependencies:cycles", label: "Cycles Or Hidden Parents", count: leftovers.length, content: leftovers.map((task) => renderTreeTask(task, 0, depLabel(task))).join("") });
  }
  return groups.length ? groups : [{ id: "dependencies:forest", label: "Dependency Forest", count: 0, content: "" }];
}

function renderDepNode(taskId, depth, children, rendered, ancestry) {
  if (rendered.has(taskId)) { return ""; }
  if (ancestry.has(taskId)) { return ""; }
  const task = state.board.tasksById[taskId];
  if (!task) { return ""; }
  rendered.add(taskId);
  const next = new Set(ancestry);
  next.add(taskId);
  const childIds = children[taskId] || [];
  const extra = childIds.length ? `${childIds.length} child${childIds.length === 1 ? "" : "ren"}` : depLabel(task);
  return renderTreeTask(task, depth, extra) + childIds.map((childId) => renderDepNode(childId, depth + 1, children, rendered, next)).join("");
}

function renderGroup(group) {
  const open = state.searchQuery ? true : groupOpen(group.id);
  return [
    '<section class="tree-group">',
    `<button type="button" class="tree-group-header" data-action="toggleGroup" data-group-id="${esc(group.id)}">`,
    '<div class="tree-group-title">',
    `<span class="tree-group-arrow">${open ? "v" : ">"}</span>`,
    `<span class="tree-group-name">${esc(group.label)}</span>`,
    '</div>',
    `<span class="tree-group-count">${group.count}</span>`,
    '</button>',
    open ? `<div class="tree-group-body">${group.content || '<div class="tree-empty">No tasks here.</div>'}</div>` : "",
    '</section>',
  ].join("");
}

function renderTreeTask(task, depth, extra) {
  const meta = [displayPhase(task.phase), labelize(task.taskType), compactCoord(task.targetCoord)];
  if (task.estimatedMinutes) { meta.push(`${task.estimatedMinutes}m est`); }
  if (extra) { meta.unshift(extra); }
  return [
    `<div class="tree-task depth-${Math.min(depth, 4)}">`,
    `<button type="button" class="tree-task-row${state.selectionId === task.id && state.editorMode === "task" ? " selected" : ""}" data-action="selectTask" data-task-id="${esc(task.id)}">`,
    '<div class="tree-task-main">',
    '<div class="tree-task-title-row">',
    `<span class="tree-task-title">${esc(task.title || "Untitled task")}</span>`,
    `<span class="tree-task-meta">${esc(meta.filter(Boolean).join(" . "))}</span>`,
    '</div>',
    '</div>',
    '<div class="tree-task-badges">',
    `<span class="tree-priority priority-${esc(task.priority)}">${esc(task.priority)}</span>`,
    `<span class="tree-status-dot ${esc(task.status)}" title="${esc(labelize(task.status))}"></span>`,
    '</div>',
    '</button>',
    '</div>',
  ].join("");
}

function renderTaskEditor(task) {
  const form = taskForm(task);
  const annoCount = task.annotations.length;
  return [
    '<section class="editor-card"><div class="editor-card-body">',
    '<div class="section-accent section-accent--muted"></div>',
    '<div class="editor-header"><div>',
    '<p class="eyebrow">Editor</p>',
    `<h2 class="editor-title">${esc(task.title || "Untitled task")}</h2>`,
    `<p class="editor-subtitle mono">${esc(task.id)}</p>`,
    '</div>',
    `<span class="badge status-badge ${esc(task.status)}">${esc(labelize(task.status))}</span>`,
    '</div>',
    '<div class="editor-meta">',
    `<span class="badge priority-badge priority-${esc(task.priority)}">${esc(task.priority)}</span>`,
    `<span class="badge">${esc(labelize(task.timing))}</span>`,
    `<span class="badge">${task.dependsOn.length} dep${task.dependsOn.length === 1 ? "" : "s"}</span>`,
    `<span class="badge">${annoCount} annotation${annoCount === 1 ? "" : "s"}</span>`,
    '</div>',
    '<div class="editor-sections">',
    section("Core", true, workFields("detail", form)),
    section("Routing And Ownership", false, routingFields("detail", form) + dependencyFields(task)),
    section("Timing And Notes", false, timingNotesFields("detail", form)),
    '</div>',
    '<div class="button-row"><button type="button" class="primary" data-action="saveTask">Save Changes</button>',
    `<button type="button" data-action="openTask" data-task-id="${esc(task.id)}">Open Target</button>`,
    `<button type="button" class="danger-btn" data-action="deleteTask" data-task-id="${esc(task.id)}">Delete</button></div>`,
    '</div></section>',
  ].join("");
}

function renderNewEditor() {
  const form = { ...DEFAULT_DRAFT, ...state.draftTask };
  return [
    '<section class="editor-card"><div class="editor-card-body">',
    '<div class="section-accent"></div>',
    '<div class="editor-header"><div><p class="eyebrow">Create</p><h2 class="editor-title">New Task</h2><p class="editor-subtitle">Add work without opening every detail until it matters.</p></div>',
    '<button type="button" data-action="cancelNewTask">Close</button></div>',
    '<div class="editor-sections">',
    section("Core", true, workFields("new", form, true)),
    section("Routing", true, routingFields("new", form, true)),
    section("Timing And Notes", false, timingNotesFields("new", form, true)),
    '</div>',
    '<div class="button-row"><button type="button" class="primary" data-action="createTask">Create Task</button><button type="button" data-action="cancelNewTask">Cancel</button></div>',
    '</div></section>',
  ].join("");
}

function workFields(prefix, form, draft) {
  return [
    fieldText("Title", `${prefix}-title`, form.title, draft ? "title" : null, "Required"),
    fieldTextarea("Description", `${prefix}-description`, form.description, draft ? "description" : null),
    fieldTextarea("Rationale", `${prefix}-rationale`, form.rationale, draft ? "rationale" : null),
    fieldTextarea("Acceptance Criteria", `${prefix}-acceptance-criteria`, form.acceptanceCriteria, draft ? "acceptanceCriteria" : null),
  ].join("");
}

function schedulingFields(prefix, form, draft) {
  return [
    '<p class="section-hint">Keep the tree compact. Put the scheduling detail here.</p>',
    '<div class="field-row">',
    fieldSelect("Priority", `${prefix}-priority`, PRIORITIES.map((value) => ({ value, label: value === "spike" ? "Spike" : value })), form.priority, draft ? "priority" : null),
    fieldSelect("Status", `${prefix}-status`, STATUSES.map((value) => ({ value, label: labelize(value) })), form.status, draft ? "status" : null),
    '</div>',
    '<div class="field-row">',
    fieldSelect("Task Type", `${prefix}-task-type`, taskTypeOptions(), form.taskType, draft ? "taskType" : null),
    fieldSelect("Timing", `${prefix}-timing`, [{ value: "one_time", label: "One Time" }, { value: "recurring", label: "Recurring" }], form.timing, draft ? "timing" : null),
    '</div>',
    '<div class="field-row">',
    fieldSelect("Phase", `${prefix}-phase`, phaseOptions(), form.phase || "", draft ? "phase" : null),
    fieldText("Trigger", `${prefix}-trigger`, form.trigger, draft ? "trigger" : null),
    '</div>',
    '<div class="field-row">',
    fieldNumber("Estimated Minutes", `${prefix}-estimated-minutes`, form.estimatedMinutes, draft ? "estimatedMinutes" : null),
    fieldNumber("Actual Minutes", `${prefix}-actual-minutes`, form.actualMinutes, draft ? "actualMinutes" : null),
    '</div>',
    fieldText("Blocked Reason", `${prefix}-blocked-reason`, form.blockedReason, draft ? "blockedReason" : null),
  ].join("");
}

function ownershipFields(prefix, form, draft) {
  return [
    '<div class="field-row">',
    fieldSelect("Owner Type", `${prefix}-owner-type`, [{ value: "unassigned", label: "Unassigned" }, { value: "human", label: "Human" }, { value: "agent", label: "Agent" }], form.ownerType, draft ? "ownerType" : null),
    fieldText("Owner Name", `${prefix}-owner-name`, form.ownerName, draft ? "ownerName" : null),
    '</div>',
    '<div class="field-row">',
    fieldText("Owner Assigned At", `${prefix}-owner-assigned-at`, form.ownerAssignedAt, draft ? "ownerAssignedAt" : null, "ISO-8601"),
    fieldSelect("Anchor Type", `${prefix}-anchor-type`, anchorTypeOptions(), form.anchorType, draft ? "anchorType" : null),
    '</div>',
    '<div class="field-row">',
    fieldText("Target File", `${prefix}-target-file`, form.targetFile, draft ? "targetFile" : null),
    fieldNumber("Target Line", `${prefix}-target-line`, form.targetLine, draft ? "targetLine" : null),
    '</div>',
    '<div class="field-row">',
    fieldText("Target Class", `${prefix}-target-class`, form.targetClass, draft ? "targetClass" : null),
    fieldText("Target Method", `${prefix}-target-method`, form.targetMethod, draft ? "targetMethod" : null),
    '</div>',
  ].join("");
}

function dependencyFields(task) {
  const rows = orderedTasks()
    .filter((candidate) => candidate.id !== task.id)
    .map((candidate) => {
      const existing = task.dependsOn.find((dep) => dep.taskId === candidate.id);
      return [
        '<div class="dependency-row">',
        `<input type="checkbox" data-dependency-checkbox="${esc(candidate.id)}"${existing ? " checked" : ""}>`,
        '<div class="dependency-copy">',
        `<div class="dependency-title">${esc(candidate.title || "Untitled task")}</div>`,
        `<div class="dependency-meta">${esc([candidate.priority, displayPhase(candidate.phase), compactCoord(candidate.targetCoord)].filter(Boolean).join(" . "))}</div>`,
        '</div>',
        `<select data-dependency-type="${esc(candidate.id)}">`,
        DEP_TYPES.map((type) => `<option value="${esc(type)}"${(existing?.type || "blocks") === type ? " selected" : ""}>${esc(labelize(type))}</option>`).join(""),
        '</select></div>',
      ].join("");
    }).join("");
  return `<p class="section-hint">Dependencies</p>${rows || '<div class="empty-state">No other tasks available to link.</div>'}`;
}

function notesFields(prefix, form, draft) {
  return [
    '<div class="field-row">',
    fieldText("Started At", `${prefix}-started-at`, form.startedAt, draft ? "startedAt" : null, "ISO-8601"),
    fieldText("Completed At", `${prefix}-completed-at`, form.completedAt, draft ? "completedAt" : null, "ISO-8601"),
    '</div>',
    fieldTextarea("Human Notes", `${prefix}-human-notes`, form.humanNotes, draft ? "humanNotes" : null),
    fieldTextarea("AI Notes", `${prefix}-ai-notes`, form.aiNotes, draft ? "aiNotes" : null),
  ].join("");
}

function routingFields(prefix, form, draft) {
  return schedulingFields(prefix, form, draft) + ownershipFields(prefix, form, draft);
}

function timingNotesFields(prefix, form, draft) {
  return notesFields(prefix, form, draft);
}

function section(title, open, body) {
  return `<details class="editor-section"${open ? " open" : ""}><summary>${esc(title)}</summary><div class="editor-section-body">${body}</div></details>`;
}

function summaryCard(label, value, hint) {
  return `<article class="summary-card"><span class="summary-label">${esc(label)}</span><span class="summary-value">${esc(String(value))}</span><span class="summary-hint">${esc(hint || "")}</span></article>`;
}

function fieldText(label, id, value, draftField, placeholder) {
  return `<div class="field-group"><label class="field-label" for="${esc(id)}">${esc(label)}</label><input id="${esc(id)}" type="text" value="${esc(value || "")}"${placeholder ? ` placeholder="${esc(placeholder)}"` : ""}${draftField ? ` data-draft-field="${esc(draftField)}"` : ""}></div>`;
}

function fieldNumber(label, id, value, draftField) {
  return `<div class="field-group"><label class="field-label" for="${esc(id)}">${esc(label)}</label><input id="${esc(id)}" type="number" value="${esc(value || "")}"${draftField ? ` data-draft-field="${esc(draftField)}"` : ""}></div>`;
}

function fieldTextarea(label, id, value, draftField) {
  return `<div class="field-group"><label class="field-label" for="${esc(id)}">${esc(label)}</label><textarea id="${esc(id)}"${draftField ? ` data-draft-field="${esc(draftField)}"` : ""}>${esc(value || "")}</textarea></div>`;
}

function fieldSelect(label, id, options, selected, draftField) {
  const markup = options.map((option) => `<option value="${esc(option.value)}"${option.value === selected ? " selected" : ""}>${esc(option.label)}</option>`).join("");
  return `<div class="field-group"><label class="field-label" for="${esc(id)}">${esc(label)}</label><select id="${esc(id)}"${draftField ? ` data-draft-field="${esc(draftField)}"` : ""}>${markup}</select></div>`;
}

function taskTypeOptions() {
  return [
    { value: "impl", label: "Implementation" }, { value: "test", label: "Test" }, { value: "spike", label: "Spike" },
    { value: "review", label: "Review" }, { value: "refactor", label: "Refactor" }, { value: "protocol", label: "Protocol" },
    { value: "bug_fix", label: "Bug Fix" }, { value: "feature", label: "Feature" }, { value: "task", label: "Task" },
  ];
}

function anchorTypeOptions() {
  return [
    { value: "modify", label: "Modify" }, { value: "create-at", label: "Create At" },
    { value: "delete", label: "Delete" }, { value: "read-only-context", label: "Read Only Context" },
  ];
}

function phaseOptions() {
  return [{ value: "", label: "Inbox" }, ...state.board.phaseOptions.map((phase) => ({ value: phase, label: phase }))];
}

function normalizeBoard(raw) {
  const tasks = Array.isArray(raw.tasks) ? raw.tasks.map(normalizeTask) : [];
  const tasksById = {};
  const orderedIds = [];
  const phaseOptions = [];
  const pushPhase = (value) => {
    const phase = emptyToNull(value);
    if (phase && !phaseOptions.includes(phase)) { phaseOptions.push(phase); }
  };
  (Array.isArray(raw.phases) ? raw.phases : []).forEach(pushPhase);
  tasks.forEach((task) => {
    tasksById[task.id] = task;
    orderedIds.push(task.id);
    pushPhase(task.phase);
  });
  return { tasksById, orderedIds, phaseOptions, updatedAt: String(raw.updatedAt || ""), path: String(raw.path || "") };
}

function normalizeTask(raw) {
  const owner = isRecord(raw.owner) ? raw.owner : {};
  const coord = isRecord(raw.targetCoord) ? raw.targetCoord : {};
  return {
    id: String(raw.id || ""),
    title: String(raw.title || ""),
    description: String(raw.description || ""),
    rationale: String(raw.rationale || ""),
    phase: emptyToNull(raw.phase),
    priority: String(raw.priority || "P2"),
    status: String(raw.status || "pending"),
    taskType: String(raw.taskType || "impl"),
    timing: String(raw.timing || "one_time"),
    blockedReason: String(raw.blockedReason || ""),
    owner: { type: String(owner.type || "unassigned"), name: String(owner.name || ""), assignedAt: String(owner.assignedAt || "") },
    targetCoord: {
      file: String(coord.file || ""),
      class: String(coord.class || ""),
      method: String(coord.method || ""),
      line: typeof coord.line === "number" ? coord.line : null,
      anchorType: String(coord.anchorType || "modify"),
    },
    dependsOn: Array.isArray(raw.dependsOn) ? raw.dependsOn.map((dep) => ({ taskId: String(dep?.taskId || ""), type: String(dep?.type || "blocks") })) : [],
    estimatedMinutes: typeof raw.estimatedMinutes === "number" ? raw.estimatedMinutes : null,
    actualMinutes: typeof raw.actualMinutes === "number" ? raw.actualMinutes : null,
    humanNotes: String(raw.humanNotes || ""),
    aiNotes: String(raw.aiNotes || ""),
    acceptanceCriteria: String(raw.acceptanceCriteria || ""),
    trigger: String(raw.trigger || ""),
    startedAt: String(raw.startedAt || ""),
    completedAt: String(raw.completedAt || ""),
    annotations: Array.isArray(raw.annotations) ? raw.annotations : [],
  };
}

function taskForm(task) {
  return {
    title: task.title || "", description: task.description || "", rationale: task.rationale || "", phase: task.phase || "",
    priority: task.priority || "P2", status: task.status || "pending", taskType: task.taskType || "impl", timing: task.timing || "one_time",
    ownerType: task.owner?.type || "unassigned", ownerName: task.owner?.name || "", ownerAssignedAt: task.owner?.assignedAt || "",
    targetFile: task.targetCoord?.file || "", targetClass: task.targetCoord?.class || "", targetMethod: task.targetCoord?.method || "",
    targetLine: task.targetCoord?.line == null ? "" : String(task.targetCoord.line), anchorType: task.targetCoord?.anchorType || "modify",
    blockedReason: task.blockedReason || "", trigger: task.trigger || "", acceptanceCriteria: task.acceptanceCriteria || "",
    humanNotes: task.humanNotes || "", aiNotes: task.aiNotes || "",
    estimatedMinutes: task.estimatedMinutes == null ? "" : String(task.estimatedMinutes),
    actualMinutes: task.actualMinutes == null ? "" : String(task.actualMinutes),
    startedAt: task.startedAt || "", completedAt: task.completedAt || "",
  };
}

function filteredTasks() {
  const query = state.searchQuery.trim().toLowerCase();
  if (!query) { return orderedTasks(); }
  return orderedTasks().filter((task) => [
    task.id, task.title, task.description, task.rationale, task.phase, task.blockedReason, task.owner.name, task.owner.type,
    task.taskType, task.timing, task.humanNotes, task.aiNotes, task.trigger, task.acceptanceCriteria,
    task.targetCoord.file, task.targetCoord.class, task.targetCoord.method,
  ].filter(Boolean).join("\n").toLowerCase().includes(query));
}

function orderedTasks() {
  return state.board.orderedIds.map((taskId) => state.board.tasksById[taskId]).filter(Boolean);
}

function selectedTask() {
  return state.selectionId ? state.board.tasksById[state.selectionId] : null;
}

function groupOpen(groupId) {
  if (state.collapsedGroups[groupId] != null) { return !state.collapsedGroups[groupId]; }
  return groupId === "priority:P0" || groupId === "priority:P1" || groupId === "dependencies:forest";
}

function displayPhase(phase) {
  return emptyToNull(phase) || "Inbox";
}

function compactCoord(coord) {
  if (!coord || !coord.file) { return "No target"; }
  return String(coord.file).split(/[\\/]/).pop() || "No target";
}

function depLabel(task) {
  return task.dependsOn.length ? `${task.dependsOn.length} dep${task.dependsOn.length === 1 ? "" : "s"}` : "Ready";
}

function labelize(value) {
  return String(value || "").replace(/[_-]+/g, " ").replace(/\b\w/g, (match) => match.toUpperCase());
}

function parseNullableInt(value) {
  const text = String(value || "").trim();
  if (!text) { return null; }
  const parsed = Number.parseInt(text, 10);
  return Number.isFinite(parsed) ? parsed : null;
}

function emptyToNull(value) {
  const text = String(value || "").trim();
  return text || null;
}

function emptyToBlank(value) {
  return emptyToNull(value) || "";
}

function valueOf(id) {
  const element = document.getElementById(id);
  return element instanceof HTMLInputElement || element instanceof HTMLTextAreaElement || element instanceof HTMLSelectElement ? element.value : "";
}

function persist() {
  vscode.setState({
    selectionId: state.selectionId,
    editorMode: state.editorMode,
    draftTask: state.draftTask,
    searchQuery: state.searchQuery,
    treeMode: state.treeMode,
    collapsedGroups: state.collapsedGroups,
  });
}

function applyViewportMode() {
  document.body.classList.toggle("compact-mode", window.innerWidth <= 900);
}

function esc(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function cssEscape(value) {
  return String(value).replace(/["\\]/g, "\\$&");
}

function isRecord(value) {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

render();
