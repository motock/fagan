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
// A 401 from one of our own endpoints means this browser has no valid session:
// either it was never logged in or the session lapsed. When the dashboard has
// login configured (GET /auth/status answers 200 with {"login": true}) send the
// user through login and back to the page they are on. In shared-secret mode
// the probe is a 404 and nothing happens, so the caller sees exactly the error
// it saw before. The probe is a plain fetch on purpose: routing it through
// fetchJson would recurse forever if the probe itself answered 401.
async function _redirectToLoginIfConfigured(res) {
  if (res.status !== 401) return;
  let login = false;
  try {
    const probe = await fetch("/auth/status", {
      headers: { Accept: "application/json" },
    });
    login = probe.ok && (await probe.json())?.login === true;
  } catch {
    // Offline, blocked, or an unparseable body: behave as if login is absent.
    login = false;
  }
  if (login) {
    // A site-relative path, not location.href: the server accepts only a
    // same-origin path and encodes the query so "&" and "#" survive intact.
    window.location.assign(
      "/auth/login?next=" +
        encodeURIComponent(location.pathname + location.search),
    );
  }
}

async function fetchJson(url) {
  const key = window.__PIPELINE_API_KEY__;
  const headers = {};
  if (key) headers["X-Pipeline-Api-Key"] = key;
  const res = await fetch(_scopePlansListUrl(url), { headers });
  if (!res.ok) {
    await _redirectToLoginIfConfigured(res);
    throw new Error(`${url} -> ${res.status}`);
  }
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
  if (!res.ok) {
    await _redirectToLoginIfConfigured(res);
    throw new Error(`${url} -> ${res.status}`);
  }
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

async function fetchIngestablePlans() {
  return fetchJson('/api/ingestable-plans');
}

export { fetchJson, postJson, fetchPlanMetrics, fetchGuardLiveness, fetchHealth, fetchIngestablePlans };