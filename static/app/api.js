// HTTP helpers for the dashboard. Wrap the browser fetch API and throw on
// non-OK responses so callers can rely on a resolved promise carrying parsed JSON.

// The server injects the dashboard shared secret into index.html as
// window.__PIPELINE_API_KEY__ (see app/dashboard.py dashboard_index). Every
// request must carry it as the X-Pipeline-Api-Key header. When the global is
// absent (e.g. a stale cached page) the header is simply omitted rather than
// sending the literal string "undefined".
async function fetchJson(url) {
  const key = window.__PIPELINE_API_KEY__;
  const headers = {};
  if (key) headers["X-Pipeline-Api-Key"] = key;
  const res = await fetch(url, { headers });
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

async function postJson(url) {
  const key = window.__PIPELINE_API_KEY__;
  // Merge with the existing Content-Type rather than clobbering it: the
  // server needs it to parse the JSON body.
  const headers = { "Content-Type": "application/json" };
  if (key) headers["X-Pipeline-Api-Key"] = key;
  const res = await fetch(url, { method: "POST", headers });
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

// Maturity panel endpoints (a3-maturity-metrics).
async function fetchPlanMetrics(plan) {
  return fetchJson(`/api/plans/${encodeURIComponent(plan)}/metrics`);
}

async function fetchGuardLiveness() {
  return fetchJson("/api/guard-liveness");
}

// Health endpoint (CFG-B3): reports the canonical plan dir and flags a
// dashboard/scheduler config mismatch. Same fetchJson idiom — auth header
// injected, throws on non-OK so callers can fail soft.
async function fetchHealth() {
  return fetchJson("/api/health");
}

export { fetchJson, postJson, fetchPlanMetrics, fetchGuardLiveness, fetchHealth };