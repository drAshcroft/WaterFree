const vscode = acquireVsCodeApi();

let baseline = { personas: [] };
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

function renderTool(tool) {
  const mode = tool.readOnly ? "RO" : "RW";
  return [
    '<div class="tool-chip" title="' + escapeHtml((tool.title || tool.name || "Tool") + ' · ' + (tool.category || "tool")) + '">',
    '<span class="tool-chip-title">' + escapeHtml(tool.title || tool.name || tool.category || "Tool") + '</span>',
    '<span class="tool-chip-meta">' + escapeHtml(tool.category || "tool") + '</span>',
    '<span class="tool-chip-mode">' + escapeHtml(mode) + '</span>',
    '</div>',
  ].join('');
}

function renderSummaryList(values, emptyLabel) {
  if (!Array.isArray(values) || values.length === 0) {
    return '<div class="empty">' + escapeHtml(emptyLabel) + '</div>';
  }
  return '<div class="model-pill-row">' + values.map((value) => {
    return '<span class="model-pill is-summary"><span class="model-pill-provider">CFG</span><span class="model-pill-model">' + escapeHtml(value) + '</span></span>';
  }).join('') + '</div>';
}

function renderTierSummary(preferredModelTiers) {
  const entries = preferredModelTiers && typeof preferredModelTiers === 'object'
    ? Object.entries(preferredModelTiers)
    : [];
  if (entries.length === 0) {
    return '<div class="empty">No preferred model tiers configured.</div>';
  }
  return '<div class="tool-grid">' + entries.map(([stage, tiers]) => {
    const label = Array.isArray(tiers) ? tiers.join(', ') : '';
    return [
      '<div class="tool-chip">',
      '<span class="tool-chip-title">' + escapeHtml(stage) + '</span>',
      '<span class="tool-chip-meta">tiers</span>',
      '<span class="tool-chip-mode">' + escapeHtml(label || 'none') + '</span>',
      '</div>',
    ].join('');
  }).join('') + '</div>';
}

function render() {
  const persona = currentPersona();
  const listHtml = draft.personas.map((item) => {
    return [
      '<div class="persona-item' + (item.id === selectedPersonaId ? ' active' : '') + '" data-action="selectPersona" data-persona-id="' + escapeHtml(item.id) + '">',
      '<div class="persona-item-title">' + escapeHtml(item.name) + '</div>',
      '<div class="persona-item-tagline">' + escapeHtml(item.tagline || '') + '</div>',
      '</div>',
    ].join('');
  }).join('');

  const toolHtml = persona && Array.isArray(persona.tools) && persona.tools.length > 0
    ? '<div class="tool-grid">' + persona.tools.map(renderTool).join('') + '</div>'
    : '<div class="empty">No WaterFree tools are exposed for this persona.</div>';

  const editorHtml = !persona
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
      '<h2>SKILL.md</h2>',
      '<p class="hint">This is the global prompt source for the persona. WaterFree reads the <code>## System</code> and <code>## Stage: ...</code> sections from this file.</p>',
      '<textarea id="skill-markdown-input" rows="18">' + escapeHtml(persona.skillMarkdown || '') + '</textarea>',
      '</section>',
      '<section class="card">',
      '<h2>waterfree.persona.json</h2>',
      '<p class="hint">Edit WaterFree-only metadata here: tool categories, preferred skills, model tiers, and subagent settings.</p>',
      '<textarea id="metadata-json-input" rows="16">' + escapeHtml(persona.metadataJson || '') + '</textarea>',
      '</section>',
      '<section class="card">',
      '<h2>WaterFree Tools</h2>',
      '<p class="hint">These tools are currently derived from the persona metadata and workspace tool registry.</p>',
      toolHtml,
      '</section>',
      '<section class="card">',
      '<h2>Preferred Skills</h2>',
      renderSummaryList(persona.preferredSkillIds || [], 'No preferred skills configured.'),
      '</section>',
      '<section class="card">',
      '<h2>Tool Categories</h2>',
      renderSummaryList(persona.toolCategories || [], 'No tool categories configured.'),
      '</section>',
      '<section class="card">',
      '<h2>Preferred Model Tiers</h2>',
      renderTierSummary(persona.preferredModelTiers),
      '</section>',
      '<section class="card">',
      '<h2>Subagent</h2>',
      persona.subagent && persona.subagent.enabled
        ? '<div class="info-banner">Enabled for ' + escapeHtml(persona.subagent.promptStage || 'PLANNING') + ': ' + escapeHtml(persona.subagent.description || '') + '</div>'
        : '<div class="empty">Subagent disabled.</div>',
      '</section>',
    ].join('');

  document.getElementById('app').innerHTML = [
    '<div class="studio">',
    '<aside class="persona-list">',
    '<h1>Persona Studio</h1>',
    '<p>Edit the global AppData persona catalog. Changes apply across workspaces after the backend reloads.</p>',
    listHtml,
    '</aside>',
    '<main class="editor">',
    editorHtml,
    '</main>',
    '</div>',
  ].join('');

  const skillInput = document.getElementById('skill-markdown-input');
  if (skillInput && persona) {
    skillInput.addEventListener('input', function(event) {
      persona.skillMarkdown = event.target.value;
    });
  }

  const metadataInput = document.getElementById('metadata-json-input');
  if (metadataInput && persona) {
    metadataInput.addEventListener('input', function(event) {
      persona.metadataJson = event.target.value;
    });
  }
}

document.addEventListener('click', function(event) {
  const target = event.target.closest('[data-action]');
  if (!target) {
    return;
  }
  const action = target.getAttribute('data-action');

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

  if (action === 'save') {
    vscode.postMessage({
      type: 'save',
      personas: draft.personas.map((persona) => ({
        personaId: persona.id,
        skillMarkdown: persona.skillMarkdown || '',
        metadataJson: persona.metadataJson || '',
      })),
    });
    baseline = structuredClone(draft);
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
