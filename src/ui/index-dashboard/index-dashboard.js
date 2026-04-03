// @ts-check
"use strict";

const vscode = acquireVsCodeApi();

const workspacePathEl = /** @type {HTMLElement} */ (document.getElementById("workspace-path"));
const statsGridEl = /** @type {HTMLElement} */ (document.getElementById("stats-grid"));
const graphSvg = /** @type {SVGSVGElement} */ (document.getElementById("module-graph"));
const graphNoteEl = /** @type {HTMLElement} */ (document.getElementById("graph-note"));
const hotspotsEl = /** @type {HTMLElement} */ (document.getElementById("hotspots-list"));
const entrypointsEl = /** @type {HTMLElement} */ (document.getElementById("entrypoints-list"));
const languagesEl = /** @type {HTMLElement} */ (document.getElementById("languages-list"));
const layersEl = /** @type {HTMLElement} */ (document.getElementById("layers-list"));
const schemaEl = /** @type {HTMLElement} */ (document.getElementById("schema-list"));
const clustersEl = /** @type {HTMLElement} */ (document.getElementById("clusters-list"));
const refreshMetaEl = /** @type {HTMLElement} */ (document.getElementById("refresh-meta"));
const btnRefresh = /** @type {HTMLButtonElement} */ (document.getElementById("btn-refresh"));
const btnReindex = /** @type {HTMLButtonElement} */ (document.getElementById("btn-reindex"));

const PALETTE = [
  "#4f8cff",
  "#22c55e",
  "#f97316",
  "#d946ef",
  "#14b8a6",
  "#f59e0b",
  "#06b6d4",
  "#ec4899",
  "#8b5cf6",
  "#84cc16",
];

/** @type {any} */
let dashboardState = null;

btnRefresh.addEventListener("click", () => {
  vscode.postMessage({ type: "refresh" });
});

btnReindex.addEventListener("click", () => {
  vscode.postMessage({ type: "reindex" });
});

window.addEventListener("message", (/** @type {MessageEvent} */ event) => {
  const message = event.data || {};
  if (message.type === "state") {
    dashboardState = message.state;
    renderDashboard();
  }
});

function renderDashboard() {
  if (!dashboardState) {
    return;
  }

  const status = dashboardState.status || {};
  const schema = dashboardState.schema || {};
  const architecture = dashboardState.architecture || {};
  const moduleGraph = architecture.module_graph || { nodes: [], links: [], groups: [] };
  const languages = Array.isArray(architecture.languages) ? architecture.languages : [];
  const layers = Array.isArray(architecture.layers) ? architecture.layers : [];
  const hotspots = Array.isArray(architecture.hotspots) ? architecture.hotspots : [];
  const entryPoints = Array.isArray(architecture.entry_points) ? architecture.entry_points : [];
  const clusters = Array.isArray(architecture.clusters) ? architecture.clusters : [];

  workspacePathEl.textContent = dashboardState.workspacePath || status.root_path || "";
  graphNoteEl.textContent = `${moduleGraph.visible_modules || moduleGraph.nodes.length || 0} shown of ${moduleGraph.total_modules || moduleGraph.nodes.length || 0} modules`;
  refreshMetaEl.textContent = `Updated ${formatDateTime(dashboardState.updatedAt)} - project ${status.project || schema.project || "not indexed"}`;

  statsGridEl.innerHTML = [
    renderStatCard("Index Status", formatStatus(status.status), status.indexed_at ? `Indexed ${formatDateTime(status.indexed_at)}` : "No index timestamp", status.db_path ? "Graph DB ready" : "Graph DB missing"),
    renderStatCard("Graph Nodes", formatNumber(status.node_count ?? schema.node_count ?? 0), "Functions, methods, classes, and modules", `${sumCounts(schema.node_labels)} catalogued`),
    renderStatCard("Graph Edges", formatNumber(status.edge_count ?? schema.edge_count ?? 0), "Calls, defines, and inheritance links", `${sumCounts(schema.edge_types)} relationships`),
    renderStatCard("Hot Modules", formatNumber(moduleGraph.total_modules || layers.reduce((sum, layer) => sum + Number(layer.file_count || 0), 0)), "Indexed files grouped by top-level area", `${layers.length} layer clusters`),
  ].join("");

  hotspotsEl.innerHTML = hotspots.length
    ? hotspots.slice(0, 10).map((item) => renderInfoCard(
        item.name || item.qualified_name || "Symbol",
        `${item.qualified_name || ""} - ${shortPath(item.file_path || "")}`,
        `${formatNumber(item.in_degree || 0)} inbound calls`,
      )).join("")
    : '<div class="empty-state">No hotspots detected yet.</div>';

  entrypointsEl.innerHTML = entryPoints.length
    ? entryPoints.slice(0, 8).map((item) => renderInfoCard(
        item.name || item.qualified_name || "Entry point",
        `${item.label || "Symbol"} - ${shortPath(item.file_path || "")}`,
        item.qualified_name || "",
      )).join("")
    : '<div class="empty-state">No obvious entry points found.</div>';

  renderLanguageMeters(languages);

  layersEl.innerHTML = layers.length
    ? layers.slice(0, 12).map((layer) => renderInfoCard(
        layer.name || "(root)",
        `${formatNumber(layer.file_count || 0)} files`,
        Array.isArray(layer.modules) ? layer.modules.join(", ") : "",
      )).join("")
    : '<div class="empty-state">No layer information available yet.</div>';

  schemaEl.innerHTML = [
    ...(Array.isArray(schema.node_labels) ? schema.node_labels.slice(0, 6).map((item) => renderSchemaPill(item.count, item.label)) : []),
    ...(Array.isArray(schema.edge_types) ? schema.edge_types.slice(0, 6).map((item) => renderSchemaPill(item.count, item.type)) : []),
  ].join("") || '<div class="empty-state">Schema will appear after indexing.</div>';

  clustersEl.innerHTML = clusters.length
    ? clusters.slice(0, 8).map((cluster) => renderInfoCard(
        `Cluster ${cluster.id}`,
        `${formatNumber(cluster.size || 0)} connected symbols`,
        Array.isArray(cluster.members)
          ? cluster.members.map((member) => member.name || member.qualified_name || "symbol").join(", ")
          : "",
      )).join("")
    : '<div class="empty-state">No multi-node CALLS clusters found.</div>';

  renderModuleGraph(moduleGraph);
}

function renderStatCard(label, value, description, chipText) {
  return [
    '<article class="stat-card">',
    '<p class="eyebrow">' + escapeHtml(label) + '</p>',
    '<div class="stat-value">' + escapeHtml(value) + '</div>',
    '<p class="stat-label">' + escapeHtml(description) + '</p>',
    '<span class="stat-chip">' + escapeHtml(chipText) + '</span>',
    '</article>',
  ].join("");
}

function renderInfoCard(title, meta, accent) {
  const safeMeta = meta ? '<div class="item-meta">' + escapeHtml(meta) + '</div>' : "";
  const safeAccent = accent ? '<div class="item-meta">' + escapeHtml(accent) + '</div>' : "";
  return [
    '<div class="item-card">',
    '<div class="item-title">' + escapeHtml(title) + '</div>',
    safeMeta,
    safeAccent,
    '</div>',
  ].join("");
}

function renderSchemaPill(count, label) {
  return [
    '<div class="schema-pill">',
    '<strong>' + escapeHtml(formatNumber(count || 0)) + '</strong>',
    '<span>' + escapeHtml(label || "") + '</span>',
    '</div>',
  ].join("");
}

function renderLanguageMeters(languages) {
  if (!languages.length) {
    languagesEl.innerHTML = '<div class="empty-state">Language stats will appear after indexing.</div>';
    return;
  }

  const total = languages.reduce((sum, item) => sum + Number(item.file_count || 0), 0) || 1;
  languagesEl.innerHTML = languages.map((item, index) => {
    const count = Number(item.file_count || 0);
    const pct = Math.max(6, Math.round((count / total) * 100));
    return [
      '<div class="item-card">',
      '<div class="item-title">' + escapeHtml(item.name || "unknown") + '</div>',
      '<div class="item-meta">' + escapeHtml(formatNumber(count)) + ' files</div>',
      '<div class="meter-track"><div class="meter-fill" style="width:' + pct + '%;background:' + PALETTE[index % PALETTE.length] + '"></div></div>',
      '</div>',
    ].join("");
  }).join("");
}

function renderModuleGraph(moduleGraph) {
  const nodes = Array.isArray(moduleGraph.nodes) ? moduleGraph.nodes.map((node) => ({
    id: String(node.id || ""),
    path: String(node.path || node.id || ""),
    label: String(node.label || node.id || ""),
    group: String(node.group || "(root)"),
    groupIndex: Number(node.group_index || 0),
    symbolCount: Number(node.symbol_count || 0),
    callIn: Number(node.call_in || 0),
    callOut: Number(node.call_out || 0),
    entryPoint: Boolean(node.entry_point),
    radius: Number(node.radius || 12),
    x: 600,
    y: 360,
    vx: 0,
    vy: 0,
  })).filter((node) => node.id) : [];

  const nodeMap = new Map(nodes.map((node) => [node.id, node]));
  const links = Array.isArray(moduleGraph.links) ? moduleGraph.links.map((link) => ({
    sourceId: String(link.source || ""),
    targetId: String(link.target || ""),
    weight: Math.max(1, Number(link.weight || 1)),
  })).filter((link) => nodeMap.has(link.sourceId) && nodeMap.has(link.targetId)) : [];

  graphSvg.innerHTML = "";

  if (!nodes.length) {
    graphSvg.innerHTML = '<text x="50%" y="50%" text-anchor="middle" class="cluster-label">No module graph yet. Rebuild the index first.</text>';
    return;
  }

  const width = 1200;
  const height = 720;
  const groups = Array.isArray(moduleGraph.groups) && moduleGraph.groups.length
    ? moduleGraph.groups.map(String)
    : Array.from(new Set(nodes.map((node) => node.group)));
  const anchors = groups.map((groupName, index) => {
    const angle = groups.length === 1 ? 0 : (Math.PI * 2 * index) / groups.length - Math.PI / 2;
    const radius = groups.length === 1 ? 0 : 230;
    return {
      groupName,
      x: width / 2 + Math.cos(angle) * radius,
      y: height / 2 + Math.sin(angle) * radius,
      color: PALETTE[index % PALETTE.length],
    };
  });

  nodes.forEach((node, index) => {
    const anchor = anchors[node.groupIndex] || anchors[0] || { x: width / 2, y: height / 2 };
    const jitter = (hashString(node.id) % 17) - 8;
    node.x = clamp(anchor.x + ((index % 5) - 2) * 22 + jitter, 60, width - 60);
    node.y = clamp(anchor.y + (Math.floor(index / 5) % 5 - 2) * 22 - jitter, 60, height - 60);
    node.vx = 0;
    node.vy = 0;
  });

  simulateLayout(nodes, links, nodeMap, anchors, width, height);

  const linkLayer = svgEl("g", { class: "graph-links" });
  links
    .sort((a, b) => a.weight - b.weight)
    .forEach((link) => {
      const source = nodeMap.get(link.sourceId);
      const target = nodeMap.get(link.targetId);
      if (!source || !target) {
        return;
      }
      const line = svgEl("line", {
        x1: String(source.x),
        y1: String(source.y),
        x2: String(target.x),
        y2: String(target.y),
        stroke: colorForGroup(source.groupIndex),
        "stroke-width": String(Math.min(7, 1 + Math.sqrt(link.weight) * 1.6)),
        opacity: String(Math.min(0.75, 0.15 + Math.sqrt(link.weight) * 0.1)),
        class: "graph-link",
      });
      const title = svgEl("title");
      title.textContent = `${source.path} -> ${target.path} (${link.weight})`;
      line.appendChild(title);
      linkLayer.appendChild(line);
    });
  graphSvg.appendChild(linkLayer);

  const clusterLayer = svgEl("g", { class: "cluster-layer" });
  anchors.forEach((anchor, index) => {
    const memberCount = nodes.filter((node) => node.groupIndex === index).length;
    const haloRadius = 90 + Math.min(140, memberCount * 8);
    const halo = svgEl("circle", {
      cx: String(anchor.x),
      cy: String(anchor.y),
      r: String(haloRadius),
      fill: anchor.color,
      opacity: "0.07",
      stroke: anchor.color,
      "stroke-width": "1",
      "stroke-dasharray": "6 8",
      "stroke-opacity": "0.25",
    });
    const label = svgEl("text", {
      x: String(anchor.x),
      y: String(anchor.y - haloRadius - 14),
      "text-anchor": "middle",
      class: "cluster-label",
    });
    label.textContent = anchor.groupName;
    clusterLayer.appendChild(halo);
    clusterLayer.appendChild(label);
  });
  graphSvg.appendChild(clusterLayer);

  const nodeLayer = svgEl("g", { class: "graph-nodes" });
  nodes.forEach((node) => {
    const group = svgEl("g", {});
    const circle = svgEl("circle", {
      cx: String(node.x),
      cy: String(node.y),
      r: String(clamp(node.radius, 8, 24)),
      fill: colorForGroup(node.groupIndex),
      opacity: node.entryPoint ? "0.98" : "0.9",
    });
    const ring = svgEl("circle", {
      cx: String(node.x),
      cy: String(node.y),
      r: String(clamp(node.radius, 8, 24) + 2.5),
      class: "graph-node-ring",
      opacity: node.entryPoint ? "0.95" : "0.55",
    });
    const label = svgEl("text", {
      x: String(node.x),
      y: String(node.y + node.radius + 16),
      "text-anchor": "middle",
      class: "graph-node-label",
    });
    label.textContent = trimLabel(node.label, 18);
    const meta = svgEl("text", {
      x: String(node.x),
      y: String(node.y + node.radius + 30),
      "text-anchor": "middle",
      class: "graph-node-meta",
    });
    meta.textContent = `${node.symbolCount} symbols`;
    const title = svgEl("title");
    title.textContent = `${node.path}\n${node.symbolCount} symbols\n${node.callIn} inbound / ${node.callOut} outbound calls`;
    group.appendChild(ring);
    group.appendChild(circle);
    group.appendChild(label);
    group.appendChild(meta);
    group.appendChild(title);
    nodeLayer.appendChild(group);
  });
  graphSvg.appendChild(nodeLayer);
}

function simulateLayout(nodes, links, nodeMap, anchors, width, height) {
  const iterations = 180;
  for (let step = 0; step < iterations; step += 1) {
    const alpha = 1 - step / iterations;

    links.forEach((link) => {
      const source = nodeMap.get(link.sourceId);
      const target = nodeMap.get(link.targetId);
      if (!source || !target) {
        return;
      }
      const dx = target.x - source.x;
      const dy = target.y - source.y;
      const distance = Math.max(1, Math.sqrt(dx * dx + dy * dy));
      const desired = 86 + Math.min(90, (source.radius + target.radius) * 2.2);
      const force = (distance - desired) * 0.012 * alpha;
      const fx = (dx / distance) * force;
      const fy = (dy / distance) * force;
      source.vx += fx;
      source.vy += fy;
      target.vx -= fx;
      target.vy -= fy;
    });

    for (let i = 0; i < nodes.length; i += 1) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j += 1) {
        const b = nodes[j];
        const dx = b.x - a.x;
        const dy = b.y - a.y;
        const distanceSq = Math.max(16, dx * dx + dy * dy);
        const distance = Math.sqrt(distanceSq);
        const minDistance = a.radius + b.radius + 24;
        if (distance > minDistance * 2.4) {
          continue;
        }
        const force = ((minDistance * minDistance) / distanceSq) * 0.42 * alpha;
        const fx = (dx / distance) * force;
        const fy = (dy / distance) * force;
        a.vx -= fx;
        a.vy -= fy;
        b.vx += fx;
        b.vy += fy;
      }
    }

    nodes.forEach((node) => {
      const anchor = anchors[node.groupIndex] || anchors[0];
      if (anchor) {
        node.vx += (anchor.x - node.x) * 0.01 * alpha;
        node.vy += (anchor.y - node.y) * 0.01 * alpha;
      }
      node.vx += (width / 2 - node.x) * 0.0015 * alpha;
      node.vy += (height / 2 - node.y) * 0.0015 * alpha;
      node.vx *= 0.85;
      node.vy *= 0.85;
      node.x = clamp(node.x + node.vx, 40, width - 40);
      node.y = clamp(node.y + node.vy, 40, height - 40);
    });
  }
}

function formatStatus(value) {
  const text = String(value || "not indexed").replace(/_/g, " ");
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function formatNumber(value) {
  return Number(value || 0).toLocaleString();
}

function formatDateTime(value) {
  if (!value) {
    return "unknown";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString();
}

function shortPath(filePath) {
  const text = String(filePath || "");
  const parts = text.split(/[\\/]/).filter(Boolean);
  return parts.slice(-3).join("/");
}

function trimLabel(value, maxLen) {
  const text = String(value || "");
  return text.length > maxLen ? text.slice(0, maxLen - 1) + "..." : text;
}

function sumCounts(items) {
  return Array.isArray(items)
    ? items.reduce((sum, item) => sum + Number(item.count || 0), 0)
    : 0;
}

function colorForGroup(groupIndex) {
  return PALETTE[Math.abs(Number(groupIndex || 0)) % PALETTE.length];
}

function hashString(value) {
  const text = String(value || "");
  let hash = 0;
  for (let i = 0; i < text.length; i += 1) {
    hash = ((hash << 5) - hash + text.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

function clamp(value, min, max) {
  return Math.max(min, Math.min(max, value));
}

function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function svgEl(name, attrs = {}) {
  const el = document.createElementNS("http://www.w3.org/2000/svg", name);
  Object.keys(attrs).forEach((key) => {
    el.setAttribute(key, String(attrs[key]));
  });
  return el;
}

renderDashboard();
