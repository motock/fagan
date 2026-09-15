// static/app/patch.js
//
// Worktree-patch review/apply module (WAP-13). Pure, DOM-free, string-template
// — same style as static/app/api.js and static/app/render/markdown.js.
//
// Contract (see tests/unit/test_patch_module.mjs):
//   - renderPatchRecord(record) -> html string. Renders the SERVER-STORED
//     record only (paths, added_lines, status, expires_at and the unified
//     diff); the record object is the ONLY input — chat-reply markdown is
//     never a source, and every piece of record content is HTML-escaped
//     exactly like static/app/render/markdown.js so model-authored diff text
//     can never reach the DOM as markup.
//   - fetchPatchRecord(patchId) -> Promise<parsed JSON>. GET
//     /api/worktree/patch/{patchId} with X-Pipeline-Api-Key (the shared
//     secret, read at call time exactly like api.js) and X-Pipeline-Origin:
//     ui. Non-2xx rejects with an Error whose message carries the status.
//   - applyPatch(patchId, confirmationToken) -> Promise<parsed JSON>. POST
//     /api/worktree/patch/{patchId}/apply with the same two headers plus
//     Content-Type: application/json and a body of EXACTLY
//     {confirmation_token: confirmationToken} — never a diff, never paths.
//
// No module-level mutable state: the shared secret is read inside each
// request function at call time (a top-level read would capture a stale or
// undefined key for every later call).

// Full escape for text that lands in element content or an attribute value.
// Order matters: & first so the entities themselves are not double-escaped.
// Copied verbatim from static/app/render/markdown.js (escapeHtml).
function escapeHtml(value) {
  return String(value)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Classify one unified-diff line so it can carry its per-line class token:
// added -> diff-add, removed -> diff-del, hunk/file header -> diff-hunk,
// everything else (context) -> diff-ctx (dimmed). The `---`/`+++` file
// headers are metadata, not content lines, so they are hunk-classed too.
function diffLineClass(line) {
  if (line.startsWith("@@") || line.startsWith("---") || line.startsWith("+++")) {
    return "diff-hunk";
  }
  if (line.startsWith("+")) return "diff-add";
  if (line.startsWith("-")) return "diff-del";
  return "diff-ctx";
}

// Render the unified diff as one inert <pre><code> block with one element per
// line. EVERY line — including its leading +/-/@@ marker — is escaped, so a
// diff line like `+<img src=x onerror=alert(1)>` is emitted as
// `+&lt;img src=x onerror=alert(1)&gt;` and can never become markup.
function renderDiff(diffText) {
  const text = typeof diffText === "string" ? diffText : "";
  if (!text) return "";
  const lines = text.split("\n").map((line) => {
    const cls = diffLineClass(line);
    return `<span class="diff-line ${cls}">${escapeHtml(line)}</span>`;
  });
  return `<pre class="patch-diff"><code>${lines.join("\n")}</code></pre>`;
}

// Render the stored record: metadata (status, added-lines count, expiry), the
// touched-path list and the diff itself. Unknown record fields (e.g. a
// chat-reply markdown blob, the confirmation token) are never rendered.
function renderPatchRecord(record) {
  const rec = record || {};
  const paths = Array.isArray(rec.paths) ? rec.paths : [];
  const pathItems = paths.map((p) => `<li>${escapeHtml(p)}</li>`).join("");
  const pathsHtml = paths.length ? `<ul class="patch-paths">${pathItems}</ul>` : "";
  const status = escapeHtml(rec.status == null ? "" : rec.status);
  const addedLines = escapeHtml(rec.added_lines == null ? 0 : rec.added_lines);
  const expiresAt = escapeHtml(rec.expires_at == null ? "" : rec.expires_at);
  return [
    '<div class="patch-record">',
    '<div class="patch-meta">',
    `<span class="patch-status">status: ${status}</span>`,
    `<span class="patch-count">added lines: ${addedLines}</span>`,
    `<span class="patch-expiry">expires at: ${expiresAt}</span>`,
    "</div>",
    pathsHtml,
    renderDiff(rec.diff_text),
    "</div>",
  ].join("");
}

// The shared secret is injected into the page as window.__PIPELINE_API_KEY__
// (see app/dashboard.py). Read it at CALL time — never cached at module
// level — and omit the header entirely when it is absent, mirroring
// static/app/api.js rather than sending the literal string "undefined".
function pipelineHeaders(extra) {
  const key =
    typeof window !== "undefined" ? window.__PIPELINE_API_KEY__ : undefined;
  const headers = extra ? { ...extra } : {};
  if (key) headers["X-Pipeline-Api-Key"] = key;
  headers["X-Pipeline-Origin"] = "ui";
  return headers;
}

// Shared non-2xx handling: reject (never resolve) with an Error whose message
// carries the status, and attach .status for programmatic callers.
function rejectStatus(url, status) {
  const err = new Error(`${url} -> ${status}`);
  err.status = status;
  return err;
}

// GET the server-stored patch record. Both pipeline headers are attached;
// resolves with the parsed JSON body.
async function fetchPatchRecord(patchId) {
  const url = `/api/worktree/patch/${patchId}`;
  const res = await fetch(url, {
    method: "GET",
    headers: pipelineHeaders(),
  });
  if (!res.ok) throw rejectStatus(url, res.status);
  return res.json();
}

// Request the apply. The body is EXACTLY {confirmation_token} — the token the
// server issued with the record; never a diff, never paths. The diff itself
// lives server-side and is applied there.
async function applyPatch(patchId, confirmationToken) {
  const url = `/api/worktree/patch/${patchId}/apply`;
  const res = await fetch(url, {
    method: "POST",
    headers: pipelineHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ confirmation_token: confirmationToken }),
  });
  if (!res.ok) throw rejectStatus(url, res.status);
  return res.json();
}

export { renderPatchRecord, fetchPatchRecord, applyPatch };