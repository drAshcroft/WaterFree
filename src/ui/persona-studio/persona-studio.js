const vscode = acquireVsCodeApi();

const STAGE_OPTIONS = [
  { id: "planning", label: "Plans" },
  { id: "annotation", label: "Architect / Design" },
  { id: "execution", label: "Code" },
  { id: "debug", label: "Debug" },
  { id: "question_answer", label: "Q&A / Tutorialize" },
  { id: "ripple_detection", label: "Ripple Detection" },
  { id: "alter_annotation", label: "Alter Annotation" },
  { id: "knowledge", label: "Knowledge / Snippetize" },
];
const PROVIDER_ABBREVIATIONS = {
  claude: "ANT",
  openai: "OAI",
  groq: "GRQ",
  ollama: "OLL",
  huggingface: "HF",
  mock: "MOCK",
};

let baseline = {
  personas: [],
  providers: [],
  customizations: [],
  defaultRoute: null,
};

let draft = structuredClone(baseline);
let selectedPersonaId = "";

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function currentPersona() {
  return draft.personas.find((persona) => persona.id === selectedPersonaId) || draft.personas[0] || null;
}

function currentPersonaCustomization() {
  const persona = currentPersona();
  return persona ? ensureCustomization(persona.id) : { assignments: [] };
}

function ensureCustomization(personaId) {
  let customization = draft.customizations.find((item) => item.personaId === personaId);
  if (!customization) {
    const persona = draft.personas.find((item) => item.id === personaId);
    customization = {
      personaId,
      prompt: persona ? persona.systemFragment || "" : "",
      assignments: [],
    };
    draft.customizations.push(customization);
  }
  if (!Array.isArray(customization.assignments)) {
    customization.assignments = [];
  }
  return customization;
}

function providerModels(providerId) {
  const provider = draft.providers.find((item) => item.id === providerId);
  return provider && Array.isArray(provider.models) ? provider.models : [];
}

function providerRecord(providerId) {
  return draft.providers.find((item) => item.id === providerId) || null;
}

function providerAbbreviation(providerId) {
  const provider = providerRecord(providerId);
  if (!provider) {
    return "DEF";
  }
  if (PROVIDER_ABBREVIATIONS[provider.type]) {
    return PROVIDER_ABBREVIATIONS[provider.type];
  }
  const initials = String(provider.name || provider.id || "DEF")
    .split(/[\s_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase())
    .join("");
  return initials.slice(0, 4) || "DEF";
}

function defaultRouteLabel() {
  if (!draft.defaultRoute) {
    return "No workspace default provider is available.";
  }
  const model = draft.defaultRoute.model ? ` / ${draft.defaultRoute.model}` : " / provider default";
  return `${draft.defaultRoute.providerName}${model}`;
}

function renderModelPill(providerId, model, options = {}) {
  const provider = providerRecord(providerId);
  const providerLabel = provider ? provider.name : (draft.defaultRoute?.providerName || "Workspace default");
  const providerCode = provider ? providerAbbreviation(providerId) : "WRK";
  const modelLabel = model || "provider default";
  const classes = ["model-pill"];
  if (options.className) {
    classes.push(options.className);
  }
  if (!provider) {
    classes.push("is-default");
  }
  const actionAttrs = options.action
    ? ' data-action="' + escapeHtml(options.action) + '"'
      + (typeof options.personaId === "string" ? ' data-persona-id="' + escapeHtml(options.personaId) + '"' : "")
      + (typeof options.index === "number" ? ' data-index="' + options.index + '"' : "")
    : "";
  return [
    '<button type="button" class="' + classes.join(" ") + '"' + actionAttrs + ' title="' + escapeHtml(providerLabel + " / " + modelLabel) + '">',
    '<span class="model-pill-provider">' + escapeHtml(providerCode) + '</span>',
    '<span class="model-pill-model">' + escapeHtml(modelLabel) + '</span>',
    '</button>',
  ].join("");
}

function renderPersonaModelPills(personaId, assignments) {
  const pills = assignments.length > 0
    ? assignments.map((assignment) => renderModelPill(assignment.providerId, assignment.model, {
      action: "selectPersona",
      personaId,
    })).join("")
    : renderModelPill("", draft.defaultRoute?.model || "", {
      action: "selectPersona",
      personaId,
      className: "is-fallback",
    });
  return '<div class="persona-model-pills">' + pills + '</div>';
}

function renderTool(tool) {
  const mode = tool.readOnly ? "RO" : "RW";
  return [
    '<div class="tool-chip" title="' + escapeHtml((tool.title || tool.name || "Tool") + ' · ' + (tool.category || "tool")) + '">',
    '<span class="tool-chip-title">' + escapeHtml(tool.title || tool.name || tool.category || "Tool") + '</span>',
    '<span class="tool-chip-meta">' + escapeHtml(tool.category || "tool") + '</span>',
    '<span class="tool-chip-mode">' + escapeHtml(mode) + '</span>',
    '</div>',
  ].join("");
}

function renderRoute(assignment, index) {
  const providerOptions = draft.providers.length > 0
    ? draft.providers.map((provider) => {
      return '<option value="' + escapeHtml(provider.id) + '"' + (provider.id === assignment.providerId ? " selected" : "") + ">"
        + escapeHtml(provider.name + (provider.enabled ? "" : " (Disabled)")) + "</option>";
    }).join("")
    : '<option value="">No providers</option>';
  const knownModels = providerModels(assignment.providerId);
  const datalistId = "route-models-" + index;
  const stageOptions = STAGE_OPTIONS.map((stage) => {
    const checked = (assignment.stages || []).includes(stage.id) ? " checked" : "";
    return '<label><input type="checkbox" data-route-stage="' + index + '" data-stage-id="' + escapeHtml(stage.id) + '"' + checked + '> '
      + escapeHtml(stage.label) + '</label>';
  }).join("");
  return [
    '<div class="route-card" id="model-card-' + index + '">',
    '<div class="route-card-header">',
    renderModelPill(assignment.providerId, assignment.model),
    '<div class="button-row">',
    '<button type="button" data-action="moveRouteUp" data-index="' + index + '"' + (index === 0 ? ' disabled' : '') + '>Up</button>',
    '<button type="button" data-action="moveRouteDown" data-index="' + index + '"' + (index === currentPersonaCustomization().assignments.length - 1 ? ' disabled' : '') + '>Down</button>',
    '<button type="button" class="danger" data-action="removeRoute" data-index="' + index + '">Remove</button>',
    '</div>',
    '</div>',
    '<div class="field-grid">',
    '<div class="field">',
    '<label>Provider</label>',
    '<select data-route-provider="' + index + '">' + providerOptions + '</select>',
    '</div>',
    '<div class="field">',
    '<label>Model</label>',
    '<input type="text" data-route-model="' + index + '" value="' + escapeHtml(assignment.model || "") + '" list="' + datalistId + '" placeholder="Provider default">',
    knownModels.length > 0
      ? '<datalist id="' + datalistId + '">' + knownModels.map((model) => '<option value="' + escapeHtml(model) + '"></option>').join("") + '</datalist>'
      : '',
    '</div>',
    '</div>',
    '<div class="field" style="margin-top:12px">',
    '<label>Stages</label>',
    '<div class="stage-grid">' + stageOptions + '</div>',
    '</div>',
    '</div>',
  ].join("");
}

function render() {
  const persona = currentPersona();
  const customization = persona ? ensureCustomization(persona.id) : null;
  const listHtml = draft.personas.map((item) => {
    const itemCustomization = ensureCustomization(item.id);
    return [
      '<div class="persona-item' + (item.id === selectedPersonaId ? ' active' : '') + '" data-action="selectPersona" data-persona-id="' + escapeHtml(item.id) + '">',
      '<div class="persona-item-title">' + escapeHtml(item.name) + '</div>',
      '<div class="persona-item-tagline">' + escapeHtml(item.tagline || '') + '</div>',
      renderPersonaModelPills(item.id, itemCustomization.assignments),
      '</div>',
    ].join('');
  }).join('');

  const toolHtml = persona && Array.isArray(persona.tools) && persona.tools.length > 0
    ? '<div class="tool-grid">' + persona.tools.map(renderTool).join('') + '</div>'
    : '<div class="empty">No persona-specific tools are exposed for this persona.</div>';

  const modelSummaryHtml = customization && customization.assignments.length > 0
    ? '<div class="model-pill-row">' + customization.assignments.map((assignment, index) => renderModelPill(
      assignment.providerId,
      assignment.model,
      { action: "focusModel", index, className: "is-summary" },
    )).join('') + '</div>'
    : '<div class="model-pill-row">' + renderModelPill("", draft.defaultRoute?.model || "", { className: "is-fallback" }) + '</div>';

  const routingHtml = customization && customization.assignments.length > 0
    ? customization.assignments.map((assignment, index) => renderRoute(assignment, index)).join('')
    : [
      '<div class="empty">No explicit models configured yet.</div>',
      '<div class="info-banner">Using the workspace default when nothing is assigned: ' + escapeHtml(defaultRouteLabel()) + '</div>',
    ].join('');

  const editorHtml = !persona || !customization
    ? '<div class="empty">No persona selected.</div>'
    : [
      '<div class="editor-header">',
      '<div>',
      '<h2 class="editor-title">' + escapeHtml(persona.name) + '</h2>',
      '<p class="editor-tagline">' + escapeHtml(persona.tagline || '') + '</p>',
      '</div>',
      '<div class="button-row">',
      '<button type="button" data-action="revert">Revert</button>',
      '<button type="button" class="primary" data-action="save">Save</button>',
      '</div>',
      '</div>',
      '<section class="card">',
      '<h2>Persona Prompt</h2>',
      '<p class="hint">Edit the base persona prompt text that is prepended before stage-specific instructions.</p>',
      '<textarea id="prompt-input" rows="10">' + escapeHtml(customization.prompt || '') + '</textarea>',
      '</section>',
      '<section class="card">',
      '<h2>Workspace Tools</h2>',
      '<p class="hint">These are the tools this persona can use inside WaterFree.</p>',
      toolHtml,
      '</section>',
      '<section class="card">',
      '<div class="editor-header">',
      '<div>',
      '<h2>Persona Models</h2>',
      '<p class="hint">Assign one or more provider/model combinations. Higher entries are tried first. Leave this empty to use the workspace default provider/model.</p>',
      '</div>',
      '<div class="button-row">',
      '<button type="button" data-action="addRoute"' + (draft.providers.length === 0 ? ' disabled' : '') + '>+ Add Model</button>',
      '</div>',
      '</div>',
      modelSummaryHtml,
      routingHtml,
      '</section>',
    ].join('');

  document.getElementById('app').innerHTML = [
    '<div class="studio">',
    '<aside class="persona-list">',
    '<h1>Persona Studio</h1>',
    '<p>Edit prompt text, review each persona\'s WaterFree tools, and assign provider/model combinations for cost, capability, or specialization.</p>',
    listHtml,
    '</aside>',
    '<main class="editor">',
    editorHtml,
    '</main>',
    '</div>',
  ].join('');

  const promptInput = document.getElementById('prompt-input');
  if (promptInput && customization) {
    promptInput.addEventListener('input', function(event) {
      customization.prompt = event.target.value;
    });
  }

  document.querySelectorAll('[data-route-provider]').forEach((select) => {
    select.addEventListener('change', (event) => {
      const index = Number(event.target.getAttribute('data-route-provider'));
      const item = customization.assignments[index];
      item.providerId = event.target.value;
      const models = providerModels(item.providerId);
      if (models.length > 0 && !models.includes(item.model)) {
        item.model = models[0];
      }
      render();
    });
  });

  document.querySelectorAll('[data-route-model]').forEach((input) => {
    input.addEventListener('input', (event) => {
      const index = Number(event.target.getAttribute('data-route-model'));
      customization.assignments[index].model = event.target.value;
    });
  });

  document.querySelectorAll('[data-route-stage]').forEach((input) => {
    input.addEventListener('change', (event) => {
      const index = Number(event.target.getAttribute('data-route-stage'));
      const stageId = event.target.getAttribute('data-stage-id');
      const current = customization.assignments[index].stages || [];
      if (!stageId) {
        return;
      }
      if (event.target.checked) {
        if (!current.includes(stageId)) {
          current.push(stageId);
        }
        customization.assignments[index].stages = current;
      } else {
        customization.assignments[index].stages = current.filter((stage) => stage !== stageId);
      }
    });
  });
}

document.addEventListener('click', function(event) {
  const target = event.target.closest('[data-action]');
  if (!target) {
    return;
  }
  const action = target.getAttribute('data-action');
  const customization = currentPersonaCustomization();

  if (action === 'selectPersona') {
    const personaId = target.getAttribute('data-persona-id');
    if (personaId) {
      selectedPersonaId = personaId;
      render();
    }
    return;
  }

  if (action === 'revert') {
    draft = structuredClone(baseline);
    selectedPersonaId = draft.personas.find((persona) => persona.id === selectedPersonaId)?.id || draft.personas[0]?.id || '';
    render();
    return;
  }

  if (action === 'focusModel') {
    const index = Number(target.getAttribute('data-index'));
    if (!Number.isNaN(index)) {
      document.getElementById('model-card-' + index)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    return;
  }

  if (action === 'save') {
    vscode.postMessage({ type: 'save', customizations: draft.customizations });
    baseline = structuredClone(draft);
    render();
    return;
  }

  if (action === 'addRoute') {
    const providerId = draft.defaultRoute?.providerId || draft.providers[0]?.id || '';
    const models = providerModels(providerId);
    customization.assignments.push({
      providerId,
      model: draft.defaultRoute?.model || models[0] || '',
      stages: STAGE_OPTIONS.map((stage) => stage.id),
    });
    render();
    return;
  }

  if (action === 'removeRoute') {
    const index = Number(target.getAttribute('data-index'));
    if (!Number.isNaN(index)) {
      customization.assignments.splice(index, 1);
      render();
    }
    return;
  }

  if (action === 'moveRouteUp' || action === 'moveRouteDown') {
    const index = Number(target.getAttribute('data-index'));
    if (Number.isNaN(index)) {
      return;
    }
    const delta = action === 'moveRouteUp' ? -1 : 1;
    const nextIndex = index + delta;
    if (nextIndex < 0 || nextIndex >= customization.assignments.length) {
      return;
    }
    const temp = customization.assignments[index];
    customization.assignments[index] = customization.assignments[nextIndex];
    customization.assignments[nextIndex] = temp;
    render();
  }
});

window.addEventListener('message', function(event) {
  const message = event.data || {};
  if (message.type === 'state' && message.state) {
    const previous = selectedPersonaId;
    baseline = structuredClone(message.state);
    draft = structuredClone(message.state);
    selectedPersonaId = draft.personas.find((persona) => persona.id === previous)?.id || draft.personas[0]?.id || '';
    render();
  }
});
