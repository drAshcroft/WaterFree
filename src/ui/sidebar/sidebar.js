const vscode = acquireVsCodeApi();
const root = document.getElementById("app");
const savedState = vscode.getState() || {};

const PERSONAS = [
  { id: "architect",        title: "The Architect",         tagline: "Requirements, feasibility, risks, trade-offs" },
  { id: "pattern_expert",   title: "Design Pattern Expert", tagline: "Framework fit, patterns, anti-patterns" },
  { id: "debug_detective",  title: "Debug Detective",       tagline: "Hypothesis-driven root cause analysis" },
  { id: "yolo",             title: "YOLO",                  tagline: "Ship fast, minimal code, no gold-plating" },
  { id: "socratic",         title: "Socratic Coach",        tagline: "Guides with questions instead of answers" },
  { id: "stub_wireframer",  title: "Stub / Wireframer",     tagline: "Compilable skeletons, TODO handoff" },
];

const WIZARDS = [
  { id: "bring_idea_to_life",   icon: "Idea", title: "Bring Idea to Life",      tagline: "From raw idea to working code",               steps: ["Market Research", "Architect Review", "Design Patterns", "Wireframes", "BDD Tests", "Coding Agents", "Review"] },
  { id: "create_application",   icon: "App",  title: "Create Application",       tagline: "Build a full application from scratch",       steps: ["Architect Review", "Design Patterns", "Wireframes", "BDD Tests", "Coding Agents", "Review"] },
  { id: "feature",              icon: "Feat", title: "Feature",                  tagline: "Add a feature to an existing codebase",       steps: ["Architect Review", "BDD Tests", "Coding Agents", "Review"] },
  { id: "refactor",             icon: "Rfct", title: "Refactor",                 tagline: "Improve structure without changing behavior",  steps: ["Architect Review", "Design Patterns", "BDD Tests", "Coding Agents", "Review"] },
  { id: "bug_hunt",             icon: "Bug",  title: "Bug Hunt",                 tagline: "Systematically find and eliminate bugs",      steps: [] },
  { id: "debugging",            icon: "Dbg",  title: "Debugging",                tagline: "Deep dive into a specific issue",             steps: [] },
  { id: "improvement_search",   icon: "Impr", title: "Improvement Search",       tagline: "Find and prioritize improvements",            steps: [] },
  { id: "deploy_package",       icon: "Pkg",  title: "Deploy / Package Helper",  tagline: "Prepare and ship your application",           steps: [] },
  { id: "documentation_genius", icon: "Doc",  title: "Documentation Genius",     tagline: "Generate comprehensive documentation",       steps: [] },
  { id: "clean_code_review",    icon: "Lint", title: "Clean Code Review",        tagline: "Review and enforce code quality standards",   steps: [] },
];

let state = {
  plan: null,
  backlogSummary: { nextTask: null, readyTasks: [], totalReady: 0 },
  busyMessage: null,
  checkingForSession: true,
  debugActive: false,
  debugLocation: "",
  draftGoal: typeof savedState.draftGoal === "string" ? savedState.draftGoal : "",
  draftDebugIntent: typeof savedState.draftDebugIntent === "string" ? savedState.draftDebugIntent : "",
  draftDebugReason: typeof savedState.draftDebugReason === "string" ? savedState.draftDebugReason : "bug investigation",
  selectedPersona: typeof savedState.selectedPersona === "string" ? savedState.selectedPersona : "architect",
  expandedWizard: typeof savedState.expandedWizard === "string" ? savedState.expandedWizard : null,
};

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function persistState() {
  vscode.setState({
    draftGoal: state.draftGoal,
    draftDebugIntent: state.draftDebugIntent,
    draftDebugReason: state.draftDebugReason,
    selectedPersona: state.selectedPersona,
    expandedWizard: state.expandedWizard,
  });
}

function formatCoord(coord) {
  const parts = [];
  if (coord && coord.file) {
    const fileParts = String(coord.file).split(/[\\\\/]/);
    parts.push(fileParts[fileParts.length - 1]);
  }
  if (coord && coord.method) {
    parts.push("::" + coord.method);
  } else if (coord && coord.class) {
    parts.push("::" + coord.class);
  }
  if (coord && typeof coord.line === "number") {
    parts.push(":" + coord.line);
  }
  return parts.join("");
}

function statusBadge(status) {
  return '<span class="badge ' + escapeHtml(status) + '">' + escapeHtml(status) + "</span>";
}

function mkButton(label, action, data, primary, disabled) {
  const attrs = Object.entries(data || {})
    .map(function(e) { return "data-" + e[0].replace(/[A-Z]/g, function(m) { return "-" + m.toLowerCase(); }) + '="' + escapeHtml(e[1]) + '"'; })
    .join(" ");
  return '<button type="button" class="' + (primary ? "primary" : "") + '" data-action="' + action + '" ' + attrs + (disabled ? " disabled" : "") + ">" + escapeHtml(label) + "</button>";
}

function renderQuickJobs() {
  const disabled = Boolean(state.busyMessage);
  const personaOptions = PERSONAS.map(function(p) {
    return '<option value="' + escapeHtml(p.id) + '"' + (state.selectedPersona === p.id ? " selected" : "") + ">" + escapeHtml(p.title) + "</option>";
  }).join("");
  return [
    '<section class="card composer">',
    '<div class="card-body">',
    '<div class="title-row">',
    '<p class="eyebrow" style="margin-bottom:0">Quick Jobs</p>',
    disabled && state.busyMessage ? '<span class="badge pending">' + escapeHtml(state.busyMessage) + "</span>" : "",
    "</div>",
    '<textarea id="goal-input" placeholder="Describe what to build or fix..." style="margin-top:8px"' + (disabled ? " disabled" : "") + ">" + escapeHtml(state.draftGoal) + "</textarea>",
    '<label class="field-label">Agent Personality</label>',
    '<select id="persona-select"' + (disabled ? " disabled" : "") + ">",
    personaOptions,
    "</select>",
    '<div class="button-row">',
    '<button type="button" class="primary" data-action="startSession"' + (disabled ? " disabled" : "") + ">Start</button>",
    '<button type="button" data-action="buildKnowledge" title="Extract reusable patterns from this workspace"' + (disabled ? " disabled" : "") + ">Snippetize</button>",
    '<button type="button" data-action="addKnowledgeRepo" title="Snippetize an external git repo or local path"' + (disabled ? " disabled" : "") + ">+ Repo</button>",
    "</div>",
    state.busyMessage ? '<p class="busy">' + escapeHtml(state.busyMessage) + "</p>" : "",
    "</div>",
    "</section>",
  ].join("");
}

function renderWizards() {
  const disabled = Boolean(state.busyMessage);
  const items = WIZARDS.map(function(w) {
    const isExpanded = state.expandedWizard === w.id;
    let bodyHtml = "";
    if (isExpanded) {
      const stepsHtml = w.steps.length > 0
        ? '<div class="wizard-steps">' + w.steps.map(function(s, i) {
            return '<span class="step-pill">' + (i + 1) + ". " + escapeHtml(s) + "</span>";
          }).join("") + "</div>"
        : "";
      bodyHtml = [
        '<div class="wizard-body">',
        stepsHtml,
        '<div class="button-row">',
        '<button type="button" class="primary" data-action="openWizard" data-wizard-id="' + escapeHtml(w.id) + '"' + (disabled ? " disabled" : "") + ">Launch Wizard</button>",
        "</div>",
        "</div>",
      ].join("");
    }
    return [
      '<div class="wizard-item' + (isExpanded ? " expanded" : "") + '">',
      '<div class="wizard-header" data-action="toggleWizard" data-wizard-id="' + escapeHtml(w.id) + '">',
      '<span class="wizard-icon">' + escapeHtml(w.icon) + "</span>",
      '<div class="wizard-info">',
      '<div class="wizard-name">' + escapeHtml(w.title) + "</div>",
      '<div class="wizard-tagline">' + escapeHtml(w.tagline) + "</div>",
      "</div>",
      '<span class="wizard-chevron">' + (isExpanded ? "&#x25B2;" : "&#x25BC;") + "</span>",
      "</div>",
      bodyHtml,
      "</div>",
    ].join("");
  }).join("");

  return [
    '<section class="card wizards-card">',
    '<div class="card-body">',
    '<p class="eyebrow">Wizards</p>',
    '<div class="wizard-list">',
    items,
    "</div>",
    "</div>",
    "</section>",
  ].join("");
}

function renderDebugPanel() {
  if (!state.debugActive) { return ""; }
  const loc = state.debugLocation
    ? '<p class="location" style="margin-bottom:8px">' + escapeHtml(state.debugLocation) + "</p>"
    : "";
  const stopReasons = ["bug investigation", "exception", "understanding flow", "data inspection", "other"];
  const options = stopReasons.map(function(r) {
    return '<option value="' + escapeHtml(r) + '"' + (state.draftDebugReason === r ? " selected" : "") + ">" + escapeHtml(r) + "</option>";
  }).join("");
  return [
    '<section class="card" style="border-top:3px solid var(--warning)">',
    '<div class="card-body">',
    '<p class="eyebrow">Debug Investigation</p>',
    loc,
    '<label class="field-label">What do you want to investigate?</label>',
    '<textarea id="debug-intent-input" placeholder="e.g. Why is user.balance going negative?" style="min-height:60px">' + escapeHtml(state.draftDebugIntent) + "</textarea>",
    '<label class="field-label">Why did you stop here?</label>',
    '<select id="debug-reason-select">',
    options,
    "</select>",
    '<div class="button-row">',
    '<button type="button" class="primary" data-action="pushDebugToAgent">Push to Agent</button>',
    "</div>",
    '<p class="hint">Snapshot written to <code>.waterfree/debug/snapshot.json</code></p>',
    "</div>",
    "</section>",
  ].join("");
}

function renderPlan(plan) {
  if (!plan) {
    const status = state.checkingForSession
      ? '<p class="hint">Checking for an existing session...</p>'
      : '<p class="empty">No active session yet. Use Quick Jobs or a Wizard above to start.</p>';
    return [
      '<section class="card">',
      '<div class="card-body">',
      '<p class="eyebrow">Current Session</p>',
      status,
      "</div>",
      "</section>",
    ].join("");
  }

  const tasks = (plan.tasks || []).map(function(task) {
    const annotations = (task.annotations || []).map(function(annotation) {
      return renderAnnotation(task, annotation, Boolean(state.busyMessage));
    }).join("");
    return [
      '<div class="task">',
      '<div class="title-row">',
      "<div>",
      '<div class="task-title">[' + escapeHtml(task.priority) + "] " + escapeHtml(task.title) + "</div>",
      '<div class="location">' + escapeHtml(formatCoord(task.targetCoord || {})) + "</div>",
      "</div>",
      statusBadge(task.status),
      "</div>",
      '<p class="task-description">' + escapeHtml(task.description || task.title) + "</p>",
      task.blockedReason ? '<p class="busy">Blocked: ' + escapeHtml(task.blockedReason) + "</p>" : "",
      '<div class="button-row">',
      mkButton("Open", "openTask", { taskId: task.id }, false, Boolean(state.busyMessage)),
      task.annotations && task.annotations.length === 0
        ? mkButton("Generate Intent", "generateAnnotation", { taskId: task.id }, false, Boolean(state.busyMessage))
        : "",
      task.status !== "complete" && task.status !== "skipped"
        ? mkButton("Skip", "skipTask", { taskId: task.id }, false, Boolean(state.busyMessage))
        : "",
      "</div>",
      annotations,
      "</div>",
    ].join("");
  }).join("");

  return [
    '<section class="card">',
    '<div class="card-body">',
    '<div class="title-row">',
    "<div>",
    '<p class="eyebrow">Current Session</p>',
    '<h2 class="goal">' + escapeHtml(plan.goalStatement) + "</h2>",
    "</div>",
    statusBadge(plan.status || "active"),
    "</div>",
    '<div class="task-list">' + (tasks || '<p class="empty">Planning has not produced tasks yet.</p>') + "</div>",
    "</div>",
    "</section>",
  ].join("");
}

function renderAnnotation(task, annotation, disabled) {
  const detail = annotation.detail
    ? '<p class="annotation-detail">' + escapeHtml(annotation.detail) + "</p>"
    : "";
  const pendingActions = annotation.status === "pending"
    ? [
        mkButton("Approve", "approveAnnotation", { annotationId: annotation.id }, true, disabled),
        mkButton("Alter", "alterAnnotation", { taskId: task.id, annotationId: annotation.id }, false, disabled),
        mkButton("Redirect", "redirectTask", { taskId: task.id }, false, disabled),
      ].join("")
    : "";
  return [
    '<div class="annotation">',
    '<div class="title-row">',
    '<div class="annotation-summary">' + escapeHtml(annotation.summary) + "</div>",
    statusBadge(annotation.status),
    "</div>",
    detail,
    '<div class="button-row">',
    mkButton("Review", "showAnnotation", { taskId: task.id, annotationId: annotation.id }, false, disabled),
    pendingActions,
    "</div>",
    "</div>",
  ].join("");
}

function renderBacklog(backlogSummary) {
  if (!state.plan || !backlogSummary || backlogSummary.totalReady <= 0) { return ""; }
  const nextTask = backlogSummary.nextTask;
  const remaining = (backlogSummary.readyTasks || []).filter(function(task) {
    return !nextTask || task.id !== nextTask.id;
  });
  const nextMarkup = nextTask
    ? [
        '<div class="annotation" style="margin-top:0">',
        '<p class="eyebrow">What Next</p>',
        '<div class="title-row">',
        '<div class="annotation-summary">[' + escapeHtml(nextTask.priority) + "] " + escapeHtml(nextTask.title) + "</div>",
        statusBadge(nextTask.status || "pending"),
        "</div>",
        '<p class="location">' + escapeHtml(formatCoord(nextTask.targetCoord || {})) + "</p>",
        nextTask.blockedReason ? '<p class="busy">Blocked: ' + escapeHtml(nextTask.blockedReason) + "</p>" : "",
        "</div>",
      ].join("")
    : '<p class="empty">No ready backlog task.</p>';
  const restMarkup = remaining.length > 0
    ? remaining.map(function(task) {
        return [
          '<div class="task">',
          '<div class="title-row">',
          '<div class="task-title">[' + escapeHtml(task.priority) + "] " + escapeHtml(task.title) + "</div>",
          statusBadge(task.status || "pending"),
          "</div>",
          '<p class="location">' + escapeHtml(formatCoord(task.targetCoord || {})) + "</p>",
          task.phase ? '<p class="task-description">Phase: ' + escapeHtml(task.phase) + "</p>" : "",
          "</div>",
        ].join("");
      }).join("")
    : '<p class="empty">No additional ready backlog tasks.</p>';
  return [
    '<section class="card">',
    '<div class="card-body">',
    '<div class="title-row">',
    "<div>",
    '<p class="eyebrow">Backlog Handoff</p>',
    '<h3>' + escapeHtml(String(backlogSummary.totalReady)) + " ready task(s)</h3>",
    "</div>",
    "</div>",
    nextMarkup,
    '<div class="task-list" style="margin-top:10px">' + restMarkup + "</div>",
    "</div>",
    "</section>",
  ].join("");
}

function render() {
  root.innerHTML = [
    '<div class="stack">',
    renderQuickJobs(),
    renderWizards(),
    renderDebugPanel(),
    renderPlan(state.plan),
    renderBacklog(state.backlogSummary),
    "</div>",
  ].join("");

  const goalInput = document.getElementById("goal-input");
  if (goalInput) {
    goalInput.addEventListener("input", function(e) { state.draftGoal = e.target.value; persistState(); });
  }
  const personaSelect = document.getElementById("persona-select");
  if (personaSelect) {
    personaSelect.addEventListener("change", function(e) { state.selectedPersona = e.target.value; persistState(); });
  }
  const debugIntentInput = document.getElementById("debug-intent-input");
  if (debugIntentInput) {
    debugIntentInput.addEventListener("input", function(e) { state.draftDebugIntent = e.target.value; persistState(); });
  }
  const debugReasonSelect = document.getElementById("debug-reason-select");
  if (debugReasonSelect) {
    debugReasonSelect.addEventListener("change", function(e) { state.draftDebugReason = e.target.value; persistState(); });
  }
}

root.addEventListener("click", function(event) {
  const el = event.target.closest("[data-action]");
  if (!el) { return; }
  const action = el.getAttribute("data-action");
  if (!action) { return; }

  if (action === "toggleWizard") {
    const wizardId = el.getAttribute("data-wizard-id");
    if (wizardId) {
      if (state.expandedWizard === wizardId) {
        state.expandedWizard = null;
      } else {
        state.expandedWizard = wizardId;
      }
      persistState();
      render();
    }
    return;
  }

  if (action === "startSession") {
    const goal = state.draftGoal.trim();
    if (goal && !state.busyMessage) {
      vscode.postMessage({ type: "startSession", goal, persona: state.selectedPersona });
    }
    return;
  }

  if (action === "openWizard") {
    if (!state.busyMessage) {
      const wizardId = el.getAttribute("data-wizard-id");
      if (wizardId) {
        vscode.postMessage({ type: "openWizard", wizardId, persona: state.selectedPersona });
      }
    }
    return;
  }

  if (action === "buildKnowledge") {
    if (!state.busyMessage) {
      const goal = state.draftGoal.trim();
      const match = goal.match(/^Extract and explain (.+?) for snippetize\\.\\s*Add context:\\s*(.*)$/s);
      if (match) {
        vscode.postMessage({ type: "snippetizeSymbol", symbol: match[1].trim(), context: match[2].trim() });
      } else {
        vscode.postMessage({ type: "buildKnowledge" });
      }
    }
    return;
  }

  if (action === "addKnowledgeRepo") {
    if (!state.busyMessage) { vscode.postMessage({ type: "addKnowledgeRepo" }); }
    return;
  }

  if (action === "pushDebugToAgent") {
    if (!state.busyMessage) {
      vscode.postMessage({ type: "pushDebugToAgent", intent: state.draftDebugIntent, stopReason: state.draftDebugReason });
    }
    return;
  }

  if (state.busyMessage) { return; }

  const taskId = el.getAttribute("data-task-id") || undefined;
  const annotationId = el.getAttribute("data-annotation-id") || undefined;

  switch (action) {
    case "openTask":
    case "generateAnnotation":
    case "skipTask":
      if (taskId) { vscode.postMessage({ type: action, taskId }); }
      return;
    case "showAnnotation":
      if (taskId) { vscode.postMessage({ type: "showAnnotation", taskId, annotationId }); }
      return;
    case "approveAnnotation":
      if (annotationId) { vscode.postMessage({ type: "approveAnnotation", annotationId }); }
      return;
    case "alterAnnotation":
      if (taskId && annotationId) {
        const feedback = window.prompt("What should be different?");
        if (feedback && feedback.trim()) {
          vscode.postMessage({ type: "alterAnnotation", taskId, annotationId, feedback });
        }
      }
      return;
    case "redirectTask":
      if (taskId) {
        const instruction = window.prompt("Give a new direction for this task:");
        if (instruction && instruction.trim()) {
          vscode.postMessage({ type: "redirectTask", taskId, instruction });
        }
      }
      return;
  }
});

window.addEventListener("message", function(event) {
  const message = event.data || {};
  if (message.type === "state") {
    state = { ...state, ...message.state };
    render();
  } else if (message.type === "clearComposer") {
    state.draftGoal = "";
    persistState();
    render();
  } else if (message.type === "prefillSnippetize") {
    const sym = message.symbol || "";
    state.draftGoal = "Extract and explain " + sym + " for snippetize. Add context: ";
    persistState();
    render();
    const input = document.getElementById("goal-input");
    if (input) {
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    }
  }
});

render();
