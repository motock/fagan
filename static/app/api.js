// HTTP helpers for the dashboard.
//
// Extracted verbatim from static/app.js. api.js depends on nothing (no state,
// no routing) — it is a foundational layer.

export async function fetchJson(url) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}

export async function postJson(url) {
  const res = await fetch(url, { method: "POST" });
  if (!res.ok) throw new Error(`${url} -> ${res.status}`);
  return res.json();
}
