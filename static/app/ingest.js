// static/app/ingest.js
//
// Plan-ingest client module (CIH-2). Pure, DOM-free, string-template — same
// style as static/app/patch.js and static/app/api.js.
//
// Contract (see tests/unit/test_ingest_module.mjs):
//   - normalizePlanName(raw) -> string. String(raw == null ? "" : raw).trim();
//     null / undefined / "" / whitespace-only / non-string values all coerce
//     without throwing.
//   - ingestPlan(planName) -> Promise<parsed JSON>. POST
//     /api/plans/{plan_name}/ingest with X-Pipeline-Api-Key (the shared
//     secret, read at call time exactly like patch.js) and X-Pipeline-Origin:
//     ui. The body is EXACTLY JSON.stringify({}) — never paths, never a diff,
//     never overwrite: true (overwrite:false is the server default and this
//     control must never overwrite an already-ingested plan). Non-2xx rejects
//     with an Error carrying .status and the server's `detail` when the body
//     parses as JSON with a string detail.
//   - renderIngestStatusHtml(outcome) -> html string. Renders the outcome
//     object ONLY (plan name + manifest counts on success, status + detail on
//     failure); every interpolated value is HTML-escaped so a plan name or a
//     server detail containing markup can never reach the DOM as markup.
//
// No module-level mutable state: the shared secret is read inside the request
// function at call time (a top-level read would capture a stale or undefined
// key for every later call).

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
// carries the status — plus the server's `detail` when the body parsed as
// JSON with a string detail — and attach .status for programmatic callers.
function rejectStatus(url, status, detail) {
  const message = detail
    ? `${url} -> ${status}: ${detail}`
    : `${url} -> ${status}`;
  const err = new Error(message);
  err.status = status;
  return err;
}

// Coerce any raw plan-name input to a trimmed string without throwing:
// null / undefined become "", a whitespace-only string becomes "", and a
// non-string value is stringified.
function normalizePlanName(raw) {
  return String(raw == null ? "" : raw).trim();
}

// Request the ingest of one plan. An empty name rejects BEFORE any network
// call (it would otherwise POST to /api/plans//ingest). The body is EXACTLY
// {} — the plan name travels in the URL, and overwrite:false is the server
// default, so this control can never overwrite an already-ingested plan.
async function ingestPlan(planName) {
  const name = normalizePlanName(planName);
  if (!name) return Promise.reject(new Error("plan name is required"));
  const url = `/api/plans/${encodeURIComponent(name)}/ingest`;
  const res = await fetch(url, {
    method: "POST",
    headers: pipelineHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({}),
  });
  if (!res.ok) {
    // The body may not be JSON at all (a proxy HTML error page, say); a
    // failed parse must surface as our status Error, never a SyntaxError.
    let detail;
    try {
      const data = await res.json();
      if (data && typeof data.detail === "string") detail = data.detail;
    } catch (parseError) {
      // Non-JSON body: fall back to the plain `<url> -> <status>` message.
    }
    throw rejectStatus(url, res.status, detail);
  }
  return res.json();
}

// Render the ingest outcome: a confirmation naming the plan and the counts
// taken from the server's ingest manifest on success, a failure line carrying
// the status and the server detail otherwise. The outcome object is the ONLY
// input — unknown fields (a diff, the repo root, a chat-markdown blob) are
// never rendered — and a malformed outcome degrades to a neutral line rather
// than throwing.
function renderIngestStatusHtml(outcome) {
  if (outcome && outcome.ok === true) {
    const result = outcome.result || {};
    const epics = Object.keys(result.epics || {}).length;
    const stories = Object.keys(result.stories || {}).length;
    const planName = escapeHtml(outcome.planName == null ? "" : outcome.planName);
    return [
      '<div class="ingest-status ingest-ok">',
      `ingested plan ${planName}: ${epics} epics, ${stories} stories`,
      "</div>",
    ].join("");
  }
  if (outcome && outcome.ok === false) {
    const status = escapeHtml(outcome.status == null ? "" : outcome.status);
    const detail = escapeHtml(outcome.detail == null ? "" : outcome.detail);
    return [
      '<div class="ingest-status ingest-failed">',
      `ingest failed (${status}): ${detail}`,
      "</div>",
    ].join("");
  }
  return '<div class="ingest-status">ingest status unavailable</div>';
}

export { normalizePlanName, ingestPlan, renderIngestStatusHtml };