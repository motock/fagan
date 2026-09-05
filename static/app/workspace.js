// static/app/workspace.js
// Module providing workspace selection helpers.
// Conforms to conventions used in static/app/api.js and static/app/comms.js.
// Exported functions: fetchWorkspaces, selectWorkspace, renderWorkspaceList.

import { escapeHtml } from "./render/board.js";

// Fetch the list of workspaces from the backend.
// Returns the array of workspaces on success, or [] on any error.
async function fetchWorkspaces() {
  try {
    const res = await fetch("/api/workspaces");
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
      headers: { "Content-Type": "application/json" },
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
    const res = await fetch("/api/workspace");
    if (!res.ok) return null;
    const body = await res.json();
    return typeof body.active === "string" && body.active.length > 0 ? body.active : null;
  } catch {
    return null;
  }
}

// Render a workspace picker as an HTML string.
// Empty/non-array input yields an explicit empty-state string. Each entry
// carries an escaped data-path attribute and escaped text; the entry whose
// path matches activePath is marked active with a visible "(active)"
// marker; valid:false entries keep the unavailable marking convention.
function renderWorkspacePicker(workspaces, activePath) {
  if (!Array.isArray(workspaces) || workspaces.length === 0) {
    return "<p class=\"empty-state\">No workspaces found.</p>";
  }
  const items = workspaces.map((w) => {
    const path = (w && w.path) ?? "";
    const escapedPath = escapeHtml(path);
    const unavailable = w && w.valid === false;
    const isActive = path === activePath;
    const classes = [unavailable ? "unavailable" : "", isActive ? "active" : ""]
      .filter(Boolean)
      .join(" ");
    const label = unavailable ? " (unavailable)" : "";
    const activeMarker = isActive ? " (active)" : "";
    return `<li class="${classes}" data-path="${escapedPath}">${escapedPath}${label}${activeMarker}</li>`;
  });
  return `<ul>${items.join("")}</ul>`;
}

export {
  fetchWorkspaces,
  selectWorkspace,
  renderWorkspaceList,
  fetchActiveWorkspace,
  renderWorkspacePicker,
};
