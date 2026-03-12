// @ts-nocheck
/* global acquireVsCodeApi */
const vscode = acquireVsCodeApi();

// ── State ────────────────────────────────────────────────────────────────────
let state = {
  entries: [],
  treeNodes: [],
  treeCurrentPath: "",
  treeStack: [],           // [{path, label}] for breadcrumb back-nav
  totalEntries: 0,
  sources: [],
  selectedEntry: null,
  searchQuery: "",
  mode: "browse",          // "browse" | "search"
  loading: false,
};

// ── DOM Refs ─────────────────────────────────────────────────────────────────
const searchInput  = document.getElementById("search-input");
const searchBtn    = document.getElementById("search-btn");
const statsBar     = document.getElementById("stats-bar");
const treeList     = document.getElementById("tree-list");
const treeRootBtn  = document.getElementById("tree-root-btn");
const resultsHeader = document.getElementById("results-header");
const resultsList  = document.getElementById("results-list");
const detailPanel  = document.getElementById("detail-panel");
const detailContent = document.getElementById("detail-content");
const detailCloseBtn = document.getElementById("detail-close-btn");
const addModal     = document.getElementById("add-modal");
const addForm      = document.getElementById("add-form");
const formError    = document.getElementById("form-error");

// ── Message Handling ─────────────────────────────────────────────────────────
window.addEventListener("message", (event) => {
  const msg = event.data;
  switch (msg.type) {
    case "browseResult":
      state.treeNodes = msg.nodes || [];
      state.treeCurrentPath = msg.path || "";
      state.entries = msg.entries || [];
      state.totalEntries = msg.totalEntries || 0;
      state.loading = false;
      renderTree();
      renderResults();
      renderStats();
      break;
    case "searchResult":
      state.entries = msg.entries || [];
      state.totalEntries = msg.total || 0;
      state.loading = false;
      renderResults();
      renderStats();
      break;
    case "sourcesResult":
      state.sources = msg.repos || [];
      break;
    case "entryAdded":
      hideAddModal();
      addForm.reset();
      loadBrowse(state.treeCurrentPath);
      break;
    case "entryDeleted":
      if (state.selectedEntry && state.selectedEntry.id === msg.id) {
        state.selectedEntry = null;
        detailPanel.classList.add("hidden");
        // Recalculate results panel margin
        resultsPanel.style.marginRight = "0";
      }
      loadBrowse(state.treeCurrentPath);
      break;
    case "error":
      showError(msg.message);
      state.loading = false;
      renderResults();
      break;
  }
});

// ── Communication Helpers ────────────────────────────────────────────────────
function post(type, data) {
  vscode.postMessage({ type, ...data });
}

function loadBrowse(path) {
  state.mode = "browse";
  state.loading = true;
  renderLoadingState();
  post("browse", { path, depth: 1, includeEntries: true, entryLimit: 50 });
}

function doSearch(query) {
  if (!query.trim()) { loadBrowse(state.treeCurrentPath); return; }
  state.mode = "search";
  state.searchQuery = query;
  state.loading = true;
  renderLoadingState();
  post("search", { query: query.trim(), limit: 30 });
}

// ── Render Helpers ───────────────────────────────────────────────────────────
function escHtml(str) {
  return String(str ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function renderLoadingState() {
  resultsList.innerHTML = '<div class="loading">Loading…</div>';
}

function renderStats() {
  const parts = [];
  if (state.mode === "search") {
    parts.push(`${state.entries.length} result${state.entries.length !== 1 ? "s" : ""} for "${escHtml(state.searchQuery)}"`);
  } else {
    parts.push(`${state.totalEntries} total entries`);
    if (state.sources.length) {
      parts.push(`${state.sources.length} source${state.sources.length !== 1 ? "s" : ""}`);
    }
  }
  statsBar.textContent = parts.join("  ·  ");
}

// ── Tree ─────────────────────────────────────────────────────────────────────
function renderTree() {
  if (!state.treeNodes.length) {
    treeList.innerHTML = '<div class="empty-state" style="padding:16px;font-size:12px;">No categories yet</div>';
    return;
  }

  treeList.innerHTML = state.treeNodes.map((node) => {
    const label = node.label || node.path.split("/").pop() || node.path;
    return `<div class="tree-node" data-path="${escHtml(node.path)}" title="${escHtml(node.path)}">
      <span class="tree-node-icon">&#x25B6;</span>
      <span class="tree-node-label">${escHtml(label)}</span>
      <span class="tree-node-count">${node.total_entry_count ?? node.entry_count ?? 0}</span>
    </div>`;
  }).join("");
}

// ── Results List ─────────────────────────────────────────────────────────────
function renderResults() {
  // Header
  if (state.mode === "browse") {
    const pathLabel = state.treeCurrentPath || "Root";
    resultsHeader.innerHTML = `<strong>${escHtml(pathLabel)}</strong>`;
  } else {
    resultsHeader.innerHTML = `Search: <strong>${escHtml(state.searchQuery)}</strong>`;
  }

  if (state.loading) { renderLoadingState(); return; }

  if (!state.entries.length) {
    resultsList.innerHTML = '<div class="empty-state">No entries found.<br><small>Try a different search or browse a category.</small></div>';
    return;
  }

  resultsList.innerHTML = state.entries.map((e) => {
    const tags = (e.tags || []).slice(0, 4).map(t => `<span class="tag">${escHtml(t)}</span>`).join("");
    const selected = state.selectedEntry && state.selectedEntry.id === e.id ? " selected" : "";
    return `<div class="result-card${selected}" data-id="${escHtml(e.id)}">
      <div class="result-title">${escHtml(e.title)}</div>
      <div class="result-desc">${escHtml(e.description)}</div>
      <div class="result-meta">
        <span class="snippet-type">${escHtml(e.snippetType || e.snippet_type || "")}</span>
        ${tags}
        <span class="source-repo">${escHtml(e.sourceRepo || e.source_repo || "")}</span>
      </div>
    </div>`;
  }).join("");
}

// ── Detail Panel ─────────────────────────────────────────────────────────────
function showDetail(entry) {
  state.selectedEntry = entry;
  renderResults(); // update selected highlight

  const tags = (entry.tags || []).map(t => `<span class="tag">${escHtml(t)}</span>`).join(" ");
  const hier = entry.hierarchyPath || entry.hierarchy_path || "";
  const src = [entry.sourceRepo || entry.source_repo, entry.sourceFile || entry.source_file].filter(Boolean).join(" › ");

  detailContent.innerHTML = `
    <div class="detail-title">${escHtml(entry.title)}</div>
    <div class="detail-desc">${escHtml(entry.description)}</div>
    <div class="detail-meta">
      <span class="snippet-type">${escHtml(entry.snippetType || entry.snippet_type || "")}</span>
      ${tags}
    </div>
    ${hier ? `<div class="detail-path">&#x1F4C1; ${escHtml(hier)}</div>` : ""}
    ${src ? `<div class="detail-path">&#x1F4C4; ${escHtml(src)}</div>` : ""}
    <div class="detail-label">Code</div>
    <pre class="detail-code">${escHtml(entry.code || "")}</pre>
    ${entry.context ? `<div class="detail-label">Context</div><div class="detail-context">${escHtml(entry.context)}</div>` : ""}
    <div class="detail-actions">
      <button class="danger-btn" data-action="deleteEntry" data-id="${escHtml(entry.id)}">Delete</button>
    </div>`;

  detailPanel.classList.remove("hidden");
}

// ── Modal ─────────────────────────────────────────────────────────────────────
function showAddModal() {
  formError.classList.add("hidden");
  addModal.classList.remove("hidden");
  addForm.querySelector('[name="title"]').focus();
}

function hideAddModal() {
  addModal.classList.add("hidden");
}

function showError(msg) {
  formError.textContent = msg;
  formError.classList.remove("hidden");
}

// ── Event Delegation ─────────────────────────────────────────────────────────
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action],[data-path],[data-id]");
  if (!el) return;

  // Tree node click → drill down
  if (el.classList.contains("tree-node") && el.dataset.path !== undefined) {
    const newPath = el.dataset.path;
    state.treeStack.push({ path: state.treeCurrentPath, label: state.treeCurrentPath || "Root" });
    loadBrowse(newPath);
    return;
  }

  const action = el.dataset.action;

  if (action === "addEntry") { showAddModal(); return; }

  if (action === "refresh") { loadBrowse(state.treeCurrentPath); post("loadSources", {}); return; }

  if (action === "deleteEntry") {
    const id = el.dataset.id;
    if (id && confirm("Delete this knowledge entry?")) {
      post("deleteEntry", { id });
    }
    return;
  }

  // Result card click → show detail
  if (el.classList.contains("result-card") && el.dataset.id) {
    const entry = state.entries.find(e => e.id === el.dataset.id);
    if (entry) showDetail(entry);
    return;
  }
});

// Tree root button
treeRootBtn.addEventListener("click", () => {
  state.treeStack = [];
  loadBrowse("");
});

// Detail close
detailCloseBtn.addEventListener("click", () => {
  state.selectedEntry = null;
  detailPanel.classList.add("hidden");
  renderResults();
});

// Modal close/cancel
document.getElementById("modal-close-btn").addEventListener("click", hideAddModal);
document.getElementById("modal-cancel-btn").addEventListener("click", hideAddModal);
addModal.addEventListener("click", (e) => { if (e.target === addModal) hideAddModal(); });

// Search
searchBtn.addEventListener("click", () => doSearch(searchInput.value));
searchInput.addEventListener("keydown", (e) => { if (e.key === "Enter") doSearch(searchInput.value); });
searchInput.addEventListener("input", () => {
  if (!searchInput.value.trim()) { loadBrowse(state.treeCurrentPath); }
});

// Add form submit
addForm.addEventListener("submit", (e) => {
  e.preventDefault();
  const data = Object.fromEntries(new FormData(addForm));
  if (!data.title || !data.description || !data.code || !data.source_repo) {
    showError("Please fill in all required fields.");
    return;
  }
  const tags = (data.tags || "").split(",").map(t => t.trim()).filter(Boolean);
  post("addEntry", {
    title: data.title,
    description: data.description,
    code: data.code,
    snippet_type: data.snippet_type,
    source_repo: data.source_repo,
    source_file: data.source_file || "",
    tags,
    hierarchy_path: data.hierarchy_path || "",
    context: data.context || "",
  });
  formError.classList.add("hidden");
});

// ── Initial Load ─────────────────────────────────────────────────────────────
loadBrowse("");
post("loadSources", {});
