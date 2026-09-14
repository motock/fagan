// HTTP helpers for the dashboard. Wrap the browser fetch API and throw on
// non-OK responses so callers can rely on a resolved promise carrying parsed JSON.

import { state } from "./state.js";

// Repo-scoped plan visibility (030576c6): the plan-list request is scoped to
// the active repository unless the "Show plans from all repositories" opt-out
// is checked or no workspace is selected at all. An absent repo param is the
// backend's all-plans signal, so a fresh session keeps show-everything.
// Composed from state on EVERY call (never cached) and applied ONLY to the
// plans-list endpoint — never to /api/plans/<name> detail fetches.
function _scopePlansListUrl(url) {
  if (url !== "/api/plans" && !url.startsWith("/api/plans?")) return url;
  const params = new URLSearchParams(
    url.includes("?") ? url.slice(url.indexOf("?") + 1) : "",
  );
  if (
    !state.showAllRepos &&
    typeof state.selectedWorkspace === "string" &&
    state.selectedWorkspace
  ) {
    params.set("repo", state.selectedWorkspace);
  }
  const query = params.toString();
  return query ? `/api/plans?${query}` : "/api/plans";
}

// The server injects the dashboard shared secret into index.html as
// window.__PIPELINE_API_KEY__ (see app/dashboard.py dashboard_index). Every
// request must carry it as the X-Pipeline-Api-Key header. When the global is
// absent (e.g. a stale cached page) the header is simply omitted rather than
// sending the literal string "undefined".
async function fetchJson(url) {
  const key = window.__PIPELINE_API_KEY__;
  const headers = {};
  if (key) headers["X-Pipeline-Api-Key"] = key;
  const res = await fetch(_scopePlansListUrl(url), { headers });
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  const body = await res.json();
  // Plans-list responses normalize to { plans: [...] } at this boundary so a
  // body lacking the array (e.g. a narrow test stub) renders as an empty
  // sidebar instead of crashing the refresh chain.
  if (url === "/api/plans" || url.startsWith("/api/plans?")) {
    if (!Array.isArray(body.plans)) body.plans = [];
  }
  return body;
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