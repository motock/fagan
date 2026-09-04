// HTTP helpers for the dashboard. Wrap the browser fetch API and throw on
// non-OK responses so callers can rely on a resolved promise carrying parsed JSON.
async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

async function postJson(url) {
  const res = await fetch(url, { method: "POST" });
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

async function fetchPlanMetrics(plan) {
  return fetchJson(`/api/plans/${plan}/metrics`);
}

async function fetchGuardLiveness() {
  return fetchJson(`/api/guard-liveness`);
}

export { fetchJson, postJson, fetchPlanMetrics, fetchGuardLiveness };
