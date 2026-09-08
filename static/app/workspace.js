// static/app/workspace.js
// Module providing workspace selection helpers.
// Conforms to conventions used in static/app/api.js and static/app/comms.js.
// Exported functions: fetchWorkspaces, selectWorkspace, renderWorkspaceList.

import { escapeHtml } from "./render/board.js";

// Fetch the list of workspaces from the backend.
// Returns the array of workspaces on success, or [] on any error.
async function fetchWorkspaces() {
  try {
    const headers = {};
    const key = window.__PIPELINE_API_KEY__;
    if (key) headers["X-Pipeline-Api-Key"] = key;
    const res = await fetch("/api/workspaces", { headers });
    if (!res.ok) return [];
    const body = await res.json();
    const arr = Array.isArray(body.workspaces) ? body.workspaces : [];
    return arr;
  } catch {
    return [];
  }
}

// Select a workspace by POSTing to /api/workspace.
// Returns the parsed JSON on success, or an object { error: "..." } on failure.
async function selectWorkspace(path, create) {
  try {
    const res = await fetch("/api/workspace", {
      method: "POST",
      headers: (() => {
        const h = { "Content-Type": "application/json" };
        const key = window.__PIPELINE_API_KEY__;
        if (key) h["X-Pipeline-Api-Key"] = key;
        return h;
      })(),
      body: JSON.stringify({ path, create }),
    });
    if (res.ok) {
      return await res.json();
    }
    try {
      const errBody = await res.json();
      if (errBody && typeof errBody.detail === "string") {
        return { error: errBody.detail };
      }
    } catch {
      // ignore JSON parse errors
    }
    return { error: "unknown error" };
  } catch {
    return { error: "unknown error" };
  }
}

// Render a list of workspaces as an HTML string.
// Empty array yields an explicit empty-state string.
// Each entry includes the escaped path; if valid===false, mark as unavailable.
function renderWorkspaceList(workspaces) {
  if (!Array.isArray(workspaces) || workspaces.length === 0) {
    return "<p class=\"empty-state\">No workspaces found.</p>";
  }
  const items = workspaces.map((w) => {
    const escaped = escapeHtml((w && w.path) ?? "");
    const unavailable = w && w.valid === false;
    const cls = unavailable ? "unavailable" : "";
    const label = unavailable ? " (unavailable)" : "";
    return `<li class="${cls}">${escaped}${label}</li>`;
  });
  return `<ul>${items.join("")}</ul>`;
}

// Fetch the currently active workspace path from the backend.
// Returns the active path string on success, or null on any error,
// non-2xx response, or malformed/absent body (mirrors fetchWorkspaces'
// never-throw convention).
async function fetchActiveWorkspace() {
  try {
    const headers = {};
    const key = window.__PIPELINE_API_KEY__;
    if (key) headers["X-Pipeline-Api-Key"] = key;
    const res = await fetch("/api/workspace", { headers });
    if (!res.ok) return null;
    const body = await res.json();
    return typeof body.active === "string" && body.active.length > 0 ? body.active : null;
  } catch {
    return null;
  }
}

// Render a workspace picker as an HTML string.
// Empty/non-array input yields an explicit empty-state string. The entry
// whose path matches activePath (if any) is pulled out into a "Current
// workspace" block with a visible "(active)" marker; the rest render as a
// plain clickable list carrying an escaped data-path attribute and escaped
// text. valid:false entries are collapsed behind a <details> disclosure
// (labelled with their count) instead of interleaved with live entries -
// a real workspace list commonly accumulates dozens of stale sandbox paths
// that would otherwise bury the handful of usable ones - and drop their
// data-path attribute entirely so a dead entry can't be clicked into a
// doomed selectWorkspace() call.
function renderWorkspacePicker(workspaces, activePath) {
  if (!Array.isArray(workspaces) || workspaces.length === 0) {
    return "<p class=\"empty-state\">No workspaces found.</p>";
  }
  const active = workspaces.find((w) => w && w.path === activePath) || null;
  const available = workspaces.filter((w) => w && w.path !== activePath && w.valid !== false);
  const unavailable = workspaces.filter((w) => w && w.valid === false && w.path !== activePath);

  const parts = [];

  if (active) {
    const escapedPath = escapeHtml(active.path);
    parts.push(
      `<div class="workspace-current">` +
        `<div class="workspace-current-head">` +
          `<span class="workspace-current-label">Current workspace</span>` +
          `<span class="workspace-tag workspace-tag-active">(active)</span>` +
        `</div>` +
        `<div class="workspace-current-path" title="${escapedPath}">${escapedPath}</div>` +
      `</div>`,
    );
  }

  if (available.length > 0) {
    const rows = available
      .map((w) => {
        const escapedPath = escapeHtml((w && w.path) ?? "");
        return `<li class="workspace-row" data-path="${escapedPath}" title="${escapedPath}">` +
          `<span class="workspace-path">${escapedPath}</span></li>`;
      })
      .join("");
    parts.push(`<ul class="workspace-list">${rows}</ul>`);
  } else if (!active) {
    parts.push("<p class=\"empty-state\">No available workspaces.</p>");
  }

  if (unavailable.length > 0) {
    const rows = unavailable
      .map((w) => {
        const escapedPath = escapeHtml((w && w.path) ?? "");
        return `<li class="workspace-row unavailable" title="${escapedPath}">` +
          `<span class="workspace-path">${escapedPath}</span>` +
          `<span class="workspace-tag">unavailable</span></li>`;
      })
      .join("");
    parts.push(
      `<details class="workspace-unavailable">` +
        `<summary>${unavailable.length} unavailable</summary>` +
        `<ul class="workspace-list">${rows}</ul>` +
      `</details>`,
    );
  }

  return parts.join("");
}

export {
  fetchWorkspaces,
  selectWorkspace,
  renderWorkspaceList,
  fetchActiveWorkspace,
  renderWorkspacePicker,
};
