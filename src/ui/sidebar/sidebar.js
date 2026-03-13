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

const QUICK_JOB_MODES = [
  { id: "plan", label: "Plan", persona: "architect" },
  { id: "debug", label: "Debug", persona: "debug_detective" },
  { id: "yolo", label: "Yolo", persona: "yolo" },
];

const PROVIDER_LABELS = {
  claude: "Claude",
  openai: "OpenAI / Codex",
  groq: "Groq",
  ollama: "Ollama",
  huggingface: "Hugging Face",
  mock: "Mock (no API calls)",
};

const PROVIDER_ICONS = { claude: "ANT", openai: "OAI", groq: "GRQ", ollama: "OLL", huggingface: "HF", mock: "OFF" };

const PROVIDER_MODELS = {
  claude: ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5"],
  openai: ["gpt-4o", "gpt-4o-mini", "o1", "o3-mini"],
  groq: ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"],
  ollama: ["llama3.2", "codestral", "qwen2.5-coder", "mistral"],
  huggingface: [],
  mock: [],
};

const PROVIDER_MODE_LABELS = {
  planning: "Planning",
  execution: "Execution",
  indexing: "Knowledge indexing",
};

const PERSONA_USE_WITH = [
  { id: "all", label: "All sessions" },
  { id: "architect", label: "Architect" },
  { id: "pattern_expert", label: "Design Pattern Expert" },
  { id: "debug_detective", label: "Debug Detective" },
  { id: "yolo", label: "YOLO" },
  { id: "socratic", label: "Socratic Coach" },
  { id: "stub_wireframer", label: "Stub / Wireframer" },
  { id: "indexing", label: "Knowledge indexing" },
];

const MENU_ITEMS = [
  { id: "providers", icon: "API", label: "Providers" },
  { id: "mcp",       icon: "MCP", label: "MCP Servers" },
  { id: "skills",    icon: "SKL", label: "Skills" },
  { id: "personas",  icon: "PSN", label: "Personas" },
  { id: "usage",     icon: "USG", label: "Usage" },
  { id: "todos",     icon: "TDO", label: "Todos" },
  { id: "index",     icon: "IDX", label: "Index" },
  { id: "knowledge", icon: "KNW", label: "Knowledge Explorer" },
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
  selectedQuickMode: typeof savedState.selectedQuickMode === "string"
    ? savedState.selectedQuickMode
    : modeForPersona(typeof savedState.selectedPersona === "string" ? savedState.selectedPersona : "architect"),
  selectedQuickProviderId: typeof savedState.selectedQuickProviderId === "string" ? savedState.selectedQuickProviderId : "",
  selectedQuickModel: typeof savedState.selectedQuickModel === "string" ? savedState.selectedQuickModel : "",
  expandedWizard: typeof savedState.expandedWizard === "string" ? savedState.expandedWizard : null,
  // history dropdown
  historyOpen: false,
  historyItems: [],      // [{id, goalStatement, status, createdAt, persona, file}]
  expandedHistoryDates: {},   // {dateKey: true}
  // settings state (not persisted)
  settingsOpen: false,
  settingsPage: null,   // null = menu, "providers", "mcp", "skills", "personas", "usage", "todos", "index", "knowledge"
  settingsData: { providers: [], activeProviderId: "" },
  providerForm: null,  // {mode:"add"|"edit", id, type, name, apiKey, baseUrl, models, modes, useWith, enabled}
  usageData: null,     // {providers: [], byPersona: [], byStage: []} or null = not loaded yet
  usageLoading: false,
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
    selectedQuickMode: state.selectedQuickMode,
    selectedQuickProviderId: state.selectedQuickProviderId,
    selectedQuickModel: state.selectedQuickModel,
    expandedWizard: state.expandedWizard,
  });
}

function resolveQuickMode(modeId) {
  return QUICK_JOB_MODES.find(function(mode) { return mode.id === modeId; }) || QUICK_JOB_MODES[0];
}

function modeForPersona(persona) {
  const match = QUICK_JOB_MODES.find(function(mode) { return mode.persona === persona; });
  return match ? match.id : QUICK_JOB_MODES[0].id;
}

function listQuickJobProviders() {
  return (state.settingsData.providers || []).filter(function(provider) {
    return provider && provider.enabled;
  });
}

function findQuickProvider(providerId) {
  const providers = listQuickJobProviders();
  return providers.find(function(provider) { return provider.id === providerId; }) || providers[0] || null;
}

function normalizeQuickJobSelection() {
  const providers = listQuickJobProviders();
  const activeProviderId = state.settingsData.activeProviderId || "";
  const fallbackProvider = providers.find(function(provider) { return provider.id === activeProviderId; }) || providers[0] || null;
  const provider = findQuickProvider(state.selectedQuickProviderId) || fallbackProvider;

  state.selectedQuickMode = resolveQuickMode(state.selectedQuickMode).id;
  state.selectedPersona = resolveQuickMode(state.selectedQuickMode).persona;
  state.selectedQuickProviderId = provider ? provider.id : "";

  const models = provider && Array.isArray(provider.models) ? provider.models : [];
  if (!models.includes(state.selectedQuickModel)) {
    state.selectedQuickModel = models[0] || "";
  }
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

function formatLabel(value) {
  return String(value || "")
    .replace(/[_-]+/g, " ")
    .replace(/\b\w/g, function(match) { return match.toUpperCase(); });
}

function statusBadge(status) {
  return '<span class="badge ' + escapeHtml(status) + '">' + escapeHtml(formatLabel(status)) + "</span>";
}

function mkButton(label, action, data, primary, disabled) {
  const attrs = Object.entries(data || {})
    .map(function(e) { return "data-" + e[0].replace(/[A-Z]/g, function(m) { return "-" + m.toLowerCase(); }) + '="' + escapeHtml(e[1]) + '"'; })
    .join(" ");
  return '<button type="button" class="' + (primary ? "primary" : "") + '" data-action="' + action + '" ' + attrs + (disabled ? " disabled" : "") + ">" + escapeHtml(label) + "</button>";
}

function renderQuickJobs() {
  const disabled = Boolean(state.busyMessage);
  normalizeQuickJobSelection();
  const modeOptions = QUICK_JOB_MODES.map(function(mode) {
    return '<option value="' + escapeHtml(mode.id) + '"' + (state.selectedQuickMode === mode.id ? " selected" : "") + ">" + escapeHtml(mode.label) + "</option>";
  }).join("");
  const providers = listQuickJobProviders();
  const selectedProvider = findQuickProvider(state.selectedQuickProviderId);
  const providerOptions = providers.length > 0
    ? providers.map(function(provider) {
        const label = provider.name || PROVIDER_LABELS[provider.type] || provider.id;
        return '<option value="' + escapeHtml(provider.id) + '"' + (state.selectedQuickProviderId === provider.id ? " selected" : "") + ">" + escapeHtml(label) + "</option>";
      }).join("")
    : '<option value="">Workspace default</option>';
  const modelOptions = selectedProvider && selectedProvider.models && selectedProvider.models.length > 0
    ? selectedProvider.models.map(function(model) {
        return '<option value="' + escapeHtml(model) + '"' + (state.selectedQuickModel === model ? " selected" : "") + ">" + escapeHtml(model) + "</option>";
      }).join("")
    : '<option value="">Provider default</option>';
  return [
    '<section class="card composer">',
    '<div class="card-body">',
    '<div class="section-accent" aria-hidden="true"></div>',
    '<div class="title-row">',
    '<p class="eyebrow" style="margin-bottom:0">Quick Jobs</p>',
    disabled && state.busyMessage ? '<span class="badge pending">' + escapeHtml(state.busyMessage) + "</span>" : "",
    "</div>",
    '<textarea id="goal-input" class="composer-prompt"' + (disabled ? " disabled" : "") + ' placeholder="Describe what to build or fix...">' + escapeHtml(state.draftGoal) + "</textarea>",
    '<div class="quick-job-controls">',
    '<select id="quick-mode-select" aria-label="Quick job mode" title="Mode"' + (disabled ? " disabled" : "") + ">",
    modeOptions,
    "</select>",
    '<select id="quick-provider-select" aria-label="Quick job provider" title="Provider"' + (disabled ? " disabled" : "") + ">",
    providerOptions,
    "</select>",
    '<select id="quick-model-select" aria-label="Quick job model" title="Model"' + (disabled ? " disabled" : "") + ">",
    modelOptions,
    "</select>",
    "</div>",
    '<div class="button-row button-row--compact">',
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
    const collapsedHint = w.steps.length > 0 ? String(w.steps.length) + " steps" : "Run";
    let bodyHtml = "";
    if (isExpanded) {
      const stepsHtml = w.steps.length > 0
        ? '<p class="wizard-steps"><span class="wizard-steps-label">Flow:</span> ' + w.steps.map(function(s) {
            return escapeHtml(s);
          }).join(" · ") + "</p>"
        : '<p class="wizard-steps">Focused run for this workflow.</p>';
      bodyHtml = [
        '<div class="wizard-body">',
        stepsHtml,
        '<div class="button-row button-row--compact">',
        '<button type="button" class="primary" data-action="openWizard" data-wizard-id="' + escapeHtml(w.id) + '"' + (disabled ? " disabled" : "") + ">Launch Wizard</button>",
        "</div>",
        "</div>",
      ].join("");
    }
    return [
      '<div class="wizard-item' + (isExpanded ? " expanded" : "") + '">',
      '<div class="wizard-header" data-action="toggleWizard" data-wizard-id="' + escapeHtml(w.id) + '">',
      '<div class="wizard-info">',
      '<div class="wizard-title-row">',
      '<div class="wizard-name">' + escapeHtml(w.title) + "</div>",
      '<span class="wizard-icon">' + escapeHtml(w.icon) + "</span>",
      "</div>",
      '<div class="wizard-tagline">' + escapeHtml(w.tagline) + "</div>",
      "</div>",
      '<span class="wizard-chevron">' + escapeHtml(isExpanded ? "Hide" : collapsedHint) + "</span>",
      "</div>",
      bodyHtml,
      "</div>",
    ].join("");
  }).join("");

  return [
    '<section class="card wizards-card">',
    '<div class="card-body">',
    '<div class="section-accent section-accent--muted" aria-hidden="true"></div>',
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
    '<section class="card card--warning">',
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

function renderHeader() {
  const gearActive = state.settingsOpen ? ' gear-btn--active' : '';
  const histActive = state.historyOpen ? ' history-btn--active' : '';
  return [
    '<div class="sidebar-header">',
    '<button type="button" class="history-btn' + histActive + '" data-action="toggleHistory" title="Session History">',
    '&#x25A4;',
    '</button>',
    '<button type="button" class="gear-btn' + gearActive + '" data-action="openSettings" title="Settings">',
    '⚙',
    '</button>',
    '</div>',
  ].join("");
}

function renderHistoryDropdown() {
  if (!state.historyOpen) { return ""; }

  if (state.historyItems.length === 0) {
    return [
      '<div class="history-dropdown">',
      '<p class="empty" style="padding:10px 12px;margin:0">No archived sessions yet.</p>',
      '</div>',
    ].join("");
  }

  // Group sessions by date (YYYY-MM-DD from createdAt)
  var groups = {};
  var groupOrder = [];
  state.historyItems.forEach(function(s) {
    var dateKey = (s.createdAt || "").slice(0, 10) || "Unknown";
    if (!groups[dateKey]) {
      groups[dateKey] = [];
      groupOrder.push(dateKey);
    }
    groups[dateKey].push(s);
  });
  // Reverse so most recent date is first
  groupOrder.reverse();

  var groupsHtml = groupOrder.map(function(dateKey, i) {
    var isExpanded = state.expandedHistoryDates[dateKey] !== false && i === 0
      ? true
      : Boolean(state.expandedHistoryDates[dateKey]);
    var sessions = groups[dateKey];
    var sessionsHtml = isExpanded ? sessions.map(function(s) {
      var goal = s.goalStatement || "(no goal)";
      var persona = s.persona ? ' <span class="history-persona">' + escapeHtml(formatLabel(s.persona)) + '</span>' : '';
      return [
        '<div class="history-session" data-action="restoreSession" data-file="' + escapeHtml(s.file || "") + '">',
        '<div class="history-session-goal">' + escapeHtml(goal) + '</div>',
        '<div class="history-session-meta">',
        statusBadge(s.status || "complete"),
        persona,
        '</div>',
        '</div>',
      ].join("");
    }).join("") : "";

    return [
      '<div class="history-group">',
      '<div class="history-group-header" data-action="toggleHistoryDate" data-date="' + escapeHtml(dateKey) + '">',
      '<span class="history-date">' + escapeHtml(dateKey) + '</span>',
      '<span class="history-group-count">' + sessions.length + '</span>',
      '<span class="history-chevron">' + (isExpanded ? '&#x25B4;' : '&#x25BE;') + '</span>',
      '</div>',
      sessionsHtml,
      '</div>',
    ].join("");
  }).join("");

  return [
    '<div class="history-dropdown">',
    groupsHtml,
    '</div>',
  ].join("");
}

/* ── Settings Panel ──────────────────────────────────────────── */

function renderSettingsPanel() {
  const isMenu      = !state.settingsPage;
  const isProvForm  = Boolean(state.providerForm);
  const menuItem    = MENU_ITEMS.find(function(m) { return m.id === state.settingsPage; });

  let title = "Settings";
  if (isProvForm) { title = state.providerForm.mode === "add" ? "Add Provider" : "Edit Provider"; }
  else if (menuItem) { title = menuItem.label; }

  const backAction  = isMenu ? "closeSettings" : isProvForm ? "cancelProvider" : "settingsBack";
  const backBtn = isMenu
    ? ""
    : '<button type="button" class="settings-nav-btn" data-action="' + backAction + '">Back</button>';

  let bodyHtml;
  if (isProvForm)                              { bodyHtml = renderProviderForm(); }
  else if (state.settingsPage === "providers") { bodyHtml = renderProvidersPage(); }
  else if (state.settingsPage === "usage")     { bodyHtml = renderUsagePage(); }
  else if (state.settingsPage)                 { bodyHtml = renderSettingsStub(state.settingsPage); }
  else                                         { bodyHtml = renderSettingsMenu(); }

  return [
    '<section class="card settings-panel">',
    '<div class="card-body settings-panel-body">',
    '<div class="section-accent section-accent--muted" aria-hidden="true"></div>',
    '<div class="settings-dialog-header">',
    backBtn,
    '<span class="settings-title">' + escapeHtml(title) + '</span>',
    '<button type="button" class="settings-nav-btn" data-action="closeSettings">Close</button>',
    '</div>',
    '<div class="settings-body">',
    bodyHtml,
    '</div>',
    '</div>',
    '</section>',
  ].join("");
}

function renderSettingsMenu() {
  // Compute total calls from usage data for the badge
  var totalUsageCalls = 0;
  if (state.usageData && state.usageData.providers) {
    state.usageData.providers.forEach(function(p) { totalUsageCalls += p.callCount || 0; });
  }

  const items = MENU_ITEMS.map(function(m) {
    var badgeVal = 0;
    if (m.id === "providers") { badgeVal = state.settingsData.providers.length; }
    else if (m.id === "usage") { badgeVal = totalUsageCalls; }
    const badge = badgeVal > 0 ? ' <span class="menu-count">' + badgeVal + '</span>' : '';
    return [
      '<div class="menu-item" data-action="settingsNav" data-page="' + m.id + '">',
      '<span class="menu-icon">' + m.icon + '</span>',
      '<span class="menu-label">' + escapeHtml(m.label) + badge + '</span>',
      '<span class="menu-arrow">&#x203A;</span>',
      '</div>',
    ].join("");
  }).join("");
  return '<div class="settings-menu">' + items + '</div>';
}

function renderProvidersPage() {
  const providers = state.settingsData.providers;
  const cards = providers.length === 0
    ? '<p class="empty" style="margin-bottom:10px">No providers configured.</p>'
    : providers.map(function(p) {
        const icon = PROVIDER_ICONS[p.type] || "???";
        const label = PROVIDER_LABELS[p.type] || p.type;
        const enabledLabel = p.enabled ? "Enabled" : "Disabled";
        const credHtml = p.type === "mock"
          ? '<span class="key-badge mock">Mock</span>'
          : p.hasKey
            ? '<span class="key-badge set">' + escapeHtml(p.maskedKey) + '</span>'
            : '<span class="key-badge unset">No key</span>';
        const modePills = (p.modes || []).map(function(m) {
          return '<span class="mode-pill">' + escapeHtml(PROVIDER_MODE_LABELS[m] || formatLabel(m)) + '</span>';
        }).join("");
        const enabledClass = p.enabled ? " provider-card--on" : " provider-card--off";
        return [
          '<div class="provider-card' + enabledClass + '">',
          '<div class="provider-card-header">',
          '<span class="provider-icon">' + icon + '</span>',
          '<div class="provider-card-info">',
          '<span class="provider-name">' + escapeHtml(p.name) + '</span>',
          '<span class="provider-type">' + escapeHtml(label) + ' · ' + enabledLabel + '</span>',
          '</div>',
          '<button type="button" class="toggle-btn' + (p.enabled ? ' toggle-btn--on' : '') + '" data-action="toggleProvider" data-provider-id="' + escapeHtml(p.id) + '" title="' + (p.enabled ? "Disable" : "Enable") + '">',
          p.enabled ? 'Disable' : 'Enable',
          '</button>',
          '</div>',
          '<div class="provider-card-meta">',
          credHtml,
          modePills,
          '</div>',
          '<div class="button-row" style="margin-top:8px">',
          '<button type="button" data-action="editProvider" data-provider-id="' + escapeHtml(p.id) + '">Edit</button>',
          '<button type="button" class="danger-btn" data-action="removeProvider" data-provider-id="' + escapeHtml(p.id) + '">Remove</button>',
          '</div>',
          '</div>',
        ].join("");
      }).join("");

  return [
    cards,
    '<button type="button" class="primary" data-action="showAddProvider" style="width:100%;margin-top:8px">+ Add Provider</button>',
  ].join("");
}

function renderProviderForm() {
  const f = state.providerForm;
  const typeOptions = Object.keys(PROVIDER_LABELS).map(function(t) {
    return '<option value="' + t + '"' + (f.type === t ? " selected" : "") + ">" + escapeHtml(PROVIDER_LABELS[t]) + "</option>";
  }).join("");

  const useWithOptions = PERSONA_USE_WITH.map(function(u) {
    return '<option value="' + u.id + '"' + (f.useWith === u.id ? " selected" : "") + ">" + escapeHtml(u.label) + "</option>";
  }).join("");

  const needsKey = f.type !== "mock" && f.type !== "ollama";
  const needsUrl = f.type === "ollama";
  const knownModels = PROVIDER_MODELS[f.type] || [];
  const hasFreeTextModels = f.type === "ollama" || f.type === "huggingface";

  let modelsHtml = "";
  if (knownModels.length > 0) {
    modelsHtml = '<div class="checkbox-group">' + knownModels.map(function(m) {
      const checked = f.models.includes(m) ? " checked" : "";
      return '<label><input type="checkbox" data-model-check="' + escapeHtml(m) + '"' + checked + '> ' + escapeHtml(m) + '</label>';
    }).join("") + '</div>';
  } else if (hasFreeTextModels) {
    modelsHtml = '<input type="text" id="pf-models-text" class="key-input" placeholder="model1, model2" value="' + escapeHtml((f.models || []).join(", ")) + '">';
  }

  return [
    '<div class="provider-form">',
    '<div class="field-group">',
    '<label class="field-label" for="pf-type">Provider</label>',
    '<select id="pf-type">' + typeOptions + '</select>',
    '</div>',
    '<div class="field-group">',
    '<label class="field-label" for="pf-name">Name</label>',
    '<input type="text" id="pf-name" class="key-input" value="' + escapeHtml(f.name) + '" placeholder="e.g. Claude Primary">',
    '</div>',
    needsKey ? [
      '<div class="field-group">',
      '<label class="field-label" for="pf-key">API Key' + (f.mode === "edit" ? " (leave blank to keep existing)" : "") + '</label>',
      '<input type="password" id="pf-key" class="key-input" placeholder="sk-ant-..." autocomplete="off">',
      '</div>',
    ].join("") : "",
    needsUrl ? [
      '<div class="field-group">',
      '<label class="field-label" for="pf-url">Base URL</label>',
      '<input type="text" id="pf-url" class="key-input" value="' + escapeHtml(f.baseUrl || "http://localhost:11434") + '">',
      '</div>',
    ].join("") : "",
    f.type !== "mock" && modelsHtml ? [
      '<div class="field-group">',
      '<label class="field-label">Models</label>',
      modelsHtml,
      '</div>',
    ].join("") : "",
    '<div class="field-group">',
    '<label class="field-label">Use for</label>',
    '<div class="checkbox-group">',
    ['planning', 'execution', 'indexing'].map(function(mode) {
      const checked = (f.modes || []).includes(mode) ? " checked" : "";
      return '<label><input type="checkbox" data-mode-check="' + mode + '"' + checked + '> '
        + escapeHtml(PROVIDER_MODE_LABELS[mode] || formatLabel(mode)) + '</label>';
    }).join(""),
    '</div>',
    '</div>',
    '<div class="field-group">',
    '<label class="field-label" for="pf-usewith">Use with</label>',
    '<select id="pf-usewith">' + useWithOptions + '</select>',
    '</div>',
    '<div class="button-row" style="margin-top:12px">',
    '<button type="button" class="primary" data-action="submitProvider">Save</button>',
    '<button type="button" data-action="cancelProvider">Cancel</button>',
    '</div>',
    '</div>',
  ].join("");
}

function fmtTokens(n) {
  if (typeof n !== "number") { return "0"; }
  if (n >= 1000000) { return (n / 1000000).toFixed(1) + "M"; }
  if (n >= 1000) { return (n / 1000).toFixed(1) + "K"; }
  return String(n);
}

function renderUsageTokenRow(label, inputTokens, outputTokens, cacheReadTokens, cacheCreationTokens, callCount) {
  const processed = (inputTokens || 0) + (cacheReadTokens || 0) + (cacheCreationTokens || 0);
  const cacheHitRate = processed > 0 ? Math.round((cacheReadTokens || 0) / processed * 100) : 0;
  const hasCaching = (cacheReadTokens || 0) + (cacheCreationTokens || 0) > 0;
  return [
    '<div class="usage-row">',
    '<div class="usage-row-label">' + escapeHtml(formatLabel(label)) + '</div>',
    '<div class="usage-row-stats">',
    '<span class="usage-stat" title="Input tokens">&#x2191; ' + fmtTokens(inputTokens) + '</span>',
    '<span class="usage-stat" title="Output tokens">&#x2193; ' + fmtTokens(outputTokens) + '</span>',
    hasCaching ? '<span class="usage-stat usage-stat--cache" title="Cache hit rate">' + cacheHitRate + '% hit</span>' : '',
    '<span class="usage-stat usage-stat--calls" title="API calls">' + (callCount || 0) + ' calls</span>',
    '</div>',
    '</div>',
  ].join("");
}

function renderUsagePage() {
  if (state.usageLoading) {
    return [
      '<div class="usage-loading">',
      '<p class="hint">Loading usage data...</p>',
      '</div>',
    ].join("");
  }

  const data = state.usageData;
  if (!data) {
    return [
      '<p class="hint" style="margin-bottom:12px">No usage data loaded yet.</p>',
      '<button type="button" class="primary" data-action="refreshUsage" style="width:100%">Load Usage Stats</button>',
    ].join("");
  }

  const providers = data.providers || [];
  const byPersona = data.byPersona || [];
  const byStage = data.byStage || [];

  // Compute totals across all providers
  let totalIn = 0, totalOut = 0, totalCacheRead = 0, totalCacheCreate = 0, totalCalls = 0;
  providers.forEach(function(p) {
    totalIn += p.totalInputTokens || 0;
    totalOut += p.totalOutputTokens || 0;
    totalCacheRead += p.totalCacheReadTokens || 0;
    totalCacheCreate += p.totalCacheCreationTokens || 0;
    totalCalls += p.callCount || 0;
  });
  const totalProcessed = totalIn + totalCacheRead + totalCacheCreate;
  const totalCacheHitRate = totalProcessed > 0 ? Math.round(totalCacheRead / totalProcessed * 100) : 0;

  const summaryHtml = [
    '<div class="usage-summary">',
    '<div class="usage-summary-grid">',
    '<div class="usage-summary-cell"><div class="usage-summary-value">' + fmtTokens(totalIn + totalCacheCreate + totalCacheRead) + '</div><div class="usage-summary-label">Total Input</div></div>',
    '<div class="usage-summary-cell"><div class="usage-summary-value">' + fmtTokens(totalOut) + '</div><div class="usage-summary-label">Total Output</div></div>',
    '<div class="usage-summary-cell"><div class="usage-summary-value">' + totalCacheHitRate + '%</div><div class="usage-summary-label">Cache Hit Rate</div></div>',
    '<div class="usage-summary-cell"><div class="usage-summary-value">' + totalCalls + '</div><div class="usage-summary-label">API Calls</div></div>',
    '</div>',
    '</div>',
  ].join("");

  const providersHtml = providers.length === 0
    ? '<p class="empty">No provider data recorded yet.</p>'
    : providers.map(function(p) {
        return renderUsageTokenRow(
          p.provider + (p.model ? ' (' + p.model + ')' : ''),
          p.totalInputTokens, p.totalOutputTokens,
          p.totalCacheReadTokens, p.totalCacheCreationTokens,
          p.callCount
        );
      }).join("");

  const personaHtml = byPersona.length === 0
    ? '<p class="empty">No persona data recorded yet.</p>'
    : byPersona.map(function(p) {
        return renderUsageTokenRow(p.label, p.totalInputTokens, p.totalOutputTokens, p.totalCacheReadTokens, p.totalCacheCreationTokens, p.callCount);
      }).join("");

  const stageOrder = ["planning", "annotation", "execution", "debug", "question_answer", "indexing"];
  const sortedStages = byStage.slice().sort(function(a, b) {
    var ai = stageOrder.indexOf(a.label);
    var bi = stageOrder.indexOf(b.label);
    if (ai === -1) { ai = 99; }
    if (bi === -1) { bi = 99; }
    return ai - bi;
  });
  const stageHtml = sortedStages.length === 0
    ? '<p class="empty">No stage data recorded yet.</p>'
    : sortedStages.map(function(s) {
        return renderUsageTokenRow(s.label, s.totalInputTokens, s.totalOutputTokens, s.totalCacheReadTokens, s.totalCacheCreationTokens, s.callCount);
      }).join("");

  return [
    summaryHtml,
    '<div class="usage-section">',
    '<div class="usage-section-title">By Provider</div>',
    providersHtml,
    '</div>',
    '<div class="usage-section">',
    '<div class="usage-section-title">By Persona</div>',
    personaHtml,
    '</div>',
    '<div class="usage-section">',
    '<div class="usage-section-title">By Stage / Mode</div>',
    stageHtml,
    '</div>',
    '<button type="button" data-action="refreshUsage" style="width:100%;margin-top:12px">Refresh</button>',
  ].join("");
}

function renderSettingsStub(page) {
  const item = MENU_ITEMS.find(function(m) { return m.id === page; });
  const label = item ? item.label : page;
  return '<p class="empty" style="padding:8px 0">' + escapeHtml(label) + ' configuration coming soon.</p>';
}

function wireSettingsForms() {
  // Provider type select — triggers re-render to show correct fields
  const typeSelect = document.getElementById("pf-type");
  if (typeSelect) {
    typeSelect.addEventListener("change", function(e) {
      const t = e.target.value;
      const autoNames = Object.values(PROVIDER_LABELS);
      if (!state.providerForm.name || autoNames.includes(state.providerForm.name)) {
        state.providerForm.name = PROVIDER_LABELS[t] || t;
      }
      state.providerForm.type = t;
      state.providerForm.models = [];
      render();
    });
  }
  const nameInput = document.getElementById("pf-name");
  if (nameInput) { nameInput.addEventListener("input", function(e) { state.providerForm.name = e.target.value; }); }
  const keyInput = document.getElementById("pf-key");
  if (keyInput) { keyInput.addEventListener("input", function(e) { state.providerForm.apiKey = e.target.value; }); }
  const urlInput = document.getElementById("pf-url");
  if (urlInput) { urlInput.addEventListener("input", function(e) { state.providerForm.baseUrl = e.target.value; }); }
  const modelsText = document.getElementById("pf-models-text");
  if (modelsText) {
    modelsText.addEventListener("input", function(e) {
      state.providerForm.models = e.target.value.split(",").map(function(s) { return s.trim(); }).filter(Boolean);
    });
  }
  document.querySelectorAll("[data-model-check]").forEach(function(cb) {
    cb.addEventListener("change", function(e) {
      const model = e.target.getAttribute("data-model-check");
      if (e.target.checked) { if (!state.providerForm.models.includes(model)) { state.providerForm.models.push(model); } }
      else { state.providerForm.models = state.providerForm.models.filter(function(m) { return m !== model; }); }
    });
  });
  document.querySelectorAll("[data-mode-check]").forEach(function(cb) {
    cb.addEventListener("change", function(e) {
      const mode = e.target.getAttribute("data-mode-check");
      if (e.target.checked) { if (!state.providerForm.modes.includes(mode)) { state.providerForm.modes.push(mode); } }
      else { state.providerForm.modes = state.providerForm.modes.filter(function(m) { return m !== mode; }); }
    });
  });
  const useWithSelect = document.getElementById("pf-usewith");
  if (useWithSelect) { useWithSelect.addEventListener("change", function(e) { state.providerForm.useWith = e.target.value; }); }
}

function render() {
  const contentHtml = state.settingsOpen
    ? renderSettingsPanel()
    : [
        renderQuickJobs(),
        renderWizards(),
        renderDebugPanel(),
        renderPlan(state.plan),
        renderBacklog(state.backlogSummary),
      ].join("");

  root.innerHTML = [
    renderHeader(),
    renderHistoryDropdown(),
    '<div class="stack">',
    contentHtml,
    "</div>",
  ].join("");

  const goalInput = document.getElementById("goal-input");
  if (goalInput) {
    goalInput.addEventListener("input", function(e) { state.draftGoal = e.target.value; persistState(); });
  }
  const quickModeSelect = document.getElementById("quick-mode-select");
  if (quickModeSelect) {
    quickModeSelect.addEventListener("change", function(e) {
      state.selectedQuickMode = e.target.value;
      state.selectedPersona = resolveQuickMode(state.selectedQuickMode).persona;
      persistState();
    });
  }
  const quickProviderSelect = document.getElementById("quick-provider-select");
  if (quickProviderSelect) {
    quickProviderSelect.addEventListener("change", function(e) {
      state.selectedQuickProviderId = e.target.value;
      normalizeQuickJobSelection();
      persistState();
      render();
    });
  }
  const quickModelSelect = document.getElementById("quick-model-select");
  if (quickModelSelect) {
    quickModelSelect.addEventListener("change", function(e) {
      state.selectedQuickModel = e.target.value;
      persistState();
    });
  }
  const debugIntentInput = document.getElementById("debug-intent-input");
  if (debugIntentInput) {
    debugIntentInput.addEventListener("input", function(e) { state.draftDebugIntent = e.target.value; persistState(); });
  }
  const debugReasonSelect = document.getElementById("debug-reason-select");
  if (debugReasonSelect) {
    debugReasonSelect.addEventListener("change", function(e) { state.draftDebugReason = e.target.value; persistState(); });
  }
  if (state.settingsOpen && state.providerForm) {
    wireSettingsForms();
  }
}

root.addEventListener("click", function(event) {
  const el = event.target.closest("[data-action]");
  if (!el) { return; }
  const action = el.getAttribute("data-action");
  if (!action) { return; }

  if (action === "toggleHistory") {
    state.historyOpen = !state.historyOpen;
    if (state.historyOpen) {
      vscode.postMessage({ type: "requestHistory" });
    }
    render();
    return;
  }

  if (action === "toggleHistoryDate") {
    var dateKey = el.getAttribute("data-date");
    if (dateKey) {
      state.expandedHistoryDates[dateKey] = !state.expandedHistoryDates[dateKey];
      render();
    }
    return;
  }

  if (action === "restoreSession") {
    var file = el.getAttribute("data-file");
    if (file) {
      state.historyOpen = false;
      vscode.postMessage({ type: "restoreSession", file: file });
      render();
    }
    return;
  }

  if (action === "openSettings") {
    state.settingsOpen = true;
    state.settingsPage = null;
    state.providerForm = null;
    vscode.postMessage({ type: "requestSettings" });
    render();
    return;
  }

  if (action === "closeSettings") {
    state.settingsOpen = false;
    state.settingsPage = null;
    state.providerForm = null;
    render();
    return;
  }

  if (action === "settingsBack") {
    if (state.providerForm) {
      state.providerForm = null;
    } else {
      state.settingsPage = null;
    }
    render();
    return;
  }

  if (action === "settingsNav") {
    const page = el.getAttribute("data-page");
    if (page === "todos") {
      state.settingsOpen = false;
      state.settingsPage = null;
      state.providerForm = null;
      vscode.postMessage({ type: "openTodoBoard" });
      render();
      return;
    }
    if (page === "knowledge") {
      state.settingsOpen = false;
      state.settingsPage = null;
      state.providerForm = null;
      vscode.postMessage({ type: "openKnowledge" });
      render();
      return;
    }
    state.settingsPage = page;
    state.providerForm = null;
    if (page === "usage" && !state.usageData) {
      state.usageLoading = true;
      vscode.postMessage({ type: "requestUsageStats" });
    }
    render();
    return;
  }

  if (action === "refreshUsage") {
    state.usageLoading = true;
    state.usageData = null;
    vscode.postMessage({ type: "requestUsageStats" });
    render();
    return;
  }

  if (action === "showAddProvider") {
    state.providerForm = {
      mode: "add", id: null,
      type: "claude",
      name: PROVIDER_LABELS["claude"],
      apiKey: "", baseUrl: "",
      models: ["claude-opus-4-6", "claude-sonnet-4-6"],
      modes: ["planning", "execution"],
      useWith: "all",
      enabled: true,
    };
    render();
    return;
  }

  if (action === "editProvider") {
    const providerId = el.getAttribute("data-provider-id");
    const provider = state.settingsData.providers.find(function(p) { return p.id === providerId; });
    if (provider) {
      state.providerForm = {
        mode: "edit",
        id: provider.id,
        type: provider.type,
        name: provider.name,
        apiKey: "",
        baseUrl: provider.baseUrl || "",
        models: (provider.models || []).slice(),
        modes: (provider.modes || []).slice(),
        useWith: provider.useWith || "all",
        enabled: provider.enabled,
      };
      render();
    }
    return;
  }

  if (action === "cancelProvider") {
    state.providerForm = null;
    render();
    return;
  }

  if (action === "submitProvider") {
    const f = state.providerForm;
    if (!f) { return; }
    const msg = {
      providerType: f.type,
      name: f.name || PROVIDER_LABELS[f.type] || f.type,
      apiKey: f.apiKey || "",
      baseUrl: f.baseUrl || "",
      models: f.models || [],
      modes: f.modes || [],
      useWith: f.useWith || "all",
      enabled: f.enabled !== false,
    };
    if (f.mode === "add") {
      vscode.postMessage(Object.assign({ type: "addProvider" }, msg));
    } else {
      vscode.postMessage(Object.assign({ type: "updateProvider", id: f.id }, msg));
    }
    state.providerForm = null;
    render();
    return;
  }

  if (action === "removeProvider") {
    const providerId = el.getAttribute("data-provider-id");
    if (providerId) { vscode.postMessage({ type: "removeProvider", id: providerId }); }
    return;
  }

  if (action === "toggleProvider") {
    const providerId = el.getAttribute("data-provider-id");
    if (providerId) { vscode.postMessage({ type: "toggleProvider", id: providerId }); }
    return;
  }

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
      normalizeQuickJobSelection();
      vscode.postMessage({
        type: "startSession",
        goal,
        persona: resolveQuickMode(state.selectedQuickMode).persona,
        runtimeSelection: {
          providerId: state.selectedQuickProviderId,
          model: state.selectedQuickModel,
        },
      });
    }
    return;
  }

  if (action === "openWizard") {
    if (!state.busyMessage) {
      const wizardId = el.getAttribute("data-wizard-id");
      if (wizardId) {
        vscode.postMessage({ type: "openWizard", wizardId, goal: state.draftGoal.trim(), persona: state.selectedPersona });
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
  if (message.type === "history") {
    state.historyItems = Array.isArray(message.sessions) ? message.sessions : [];
    // Auto-expand the most recent date group
    state.expandedHistoryDates = {};
    if (state.historyItems.length > 0) {
      var newest = state.historyItems[state.historyItems.length - 1];
      var newestDate = (newest.createdAt || "").slice(0, 10) || "Unknown";
      state.expandedHistoryDates[newestDate] = true;
    }
    if (state.historyOpen) { render(); }
    return;
  }
  if (message.type === "settings") {
    state.settingsData = Object.assign({ providers: [], activeProviderId: "" }, message.data || {});
    normalizeQuickJobSelection();
    persistState();
    render();
    return;
  }
  if (message.type === "usageStats") {
    state.usageData = message.data || { providers: [], byPersona: [], byStage: [] };
    state.usageLoading = false;
    if (state.settingsOpen && state.settingsPage === "usage") { render(); }
    return;
  }
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
vscode.postMessage({ type: "requestSettings" });
