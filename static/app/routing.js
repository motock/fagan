// ---------- URL hash deep-linking ----------
import {
  state,
  STATUS_COLUMNS,
  VALID_SORTS,
  BACKEND_VALUES,
  ESCALATED_VALUES,
  defaultFilters,
  saveFilters,
  loadFilters,
} from "./state.js";

const HASH_KEYS = {
  plan: "plan",
  status: "statuses",
  persona: "personas",
  risk: "risks",
  backend: "backends",
  escalated: "escalated",
  sort: "sort",
  q: "search",
};

function hashStateFrom(s) {
  return {
    selectedPlan: s.selectedPlan || null,
    filters: {
      statuses: [...s.filters.statuses],
      personas: [...s.filters.personas],
      risks: [...s.filters.risks],
      backends: [...s.filters.backends],
      escalated: [...s.filters.escalated],
      sort: s.filters.sort,
      search: s.filters.search,
    },
  };
}

function encodeHashState() {
  const snap = hashStateFrom(state);
  const parts = [];
  if (snap.selectedPlan) parts.push(`plan=${encodeURIComponent(snap.selectedPlan)}`);
  const allStatuses = new Set(STATUS_COLUMNS);
  const currentStatuses = new Set(snap.filters.statuses);
  const isAllStatuses = allStatuses.size === currentStatuses.size
    && [...allStatuses].every((s) => currentStatuses.has(s));
  if (currentStatuses.size && !isAllStatuses) {
    parts.push(`status=${snap.filters.statuses.map(encodeURIComponent).join(",")}`);
  }
  if (snap.filters.personas.length) parts.push(`persona=${snap.filters.personas.map(encodeURIComponent).join(",")}`);
  if (snap.filters.risks.length) parts.push(`risk=${snap.filters.risks.map(encodeURIComponent).join(",")}`);
  if (snap.filters.backends.length) parts.push(`backend=${snap.filters.backends.map(encodeURIComponent).join(",")}`);
  if (snap.filters.escalated.length) parts.push(`escalated=${snap.filters.escalated.map(encodeURIComponent).join(",")}`);
  if (snap.filters.sort !== "key") parts.push(`sort=${encodeURIComponent(snap.filters.sort)}`);
  if (snap.filters.search) parts.push(`q=${encodeURIComponent(snap.filters.search)}`);
  return parts.join("&");
}

function parseHash(raw) {
  const defaults = defaultFilters();
  const out = { selectedPlan: null, filters: defaults };
  if (!raw) return out;
  const body = String(raw).replace(/^#/, "");
  if (!body) return out;
  let pairs;
  try { pairs = body.split("&").filter(Boolean); } catch { return out; }
  for (const pair of pairs) {
    const eq = pair.indexOf("=");
    if (eq <= 0) continue;
    const key = pair.slice(0, eq).trim().toLowerCase();
    const value = pair.slice(eq + 1);
    if (!Object.prototype.hasOwnProperty.call(HASH_KEYS, key)) continue;
    const dim = HASH_KEYS[key];
    if (dim === "plan") { try { const plan = decodeURIComponent(value); if (plan) out.selectedPlan = plan; } catch {} continue; }
    if (dim === "sort") { try { const sort = decodeURIComponent(value).trim(); if (VALID_SORTS.has(sort)) out.filters.sort = sort; } catch {} continue; }
    if (dim === "search") { try { const q = decodeURIComponent(value); if (q) out.filters.search = q; } catch {} continue; }
    let items;
    try { items = value.split(",").map((v) => { try { return decodeURIComponent(v); } catch { return null; } }).filter((v) => v !== null && v !== ""); } catch { continue; }
    if (dim === "statuses") { const valid = new Set(STATUS_COLUMNS); out.filters.statuses = items.filter((s) => valid.has(s)); if (!out.filters.statuses.length) out.filters.statuses = [...STATUS_COLUMNS]; }
    else if (dim === "personas" || dim === "risks") { out.filters[dim] = items; }
    else if (dim === "backends") { const valid = new Set(BACKEND_VALUES); out.filters.backends = items.filter((v) => valid.has(v)); }
    else if (dim === "escalated") { const valid = new Set(ESCALATED_VALUES); out.filters.escalated = items.filter((v) => valid.has(v)); }
  }
  return out;
}

function updateHash() {
  const desired = encodeHashState();
  const current = window.location.hash.replace(/^#/, "");
  if (desired === current) return;
  if (desired) window.location.hash = desired;
  else if (window.location.hash) window.location.hash = "";
}

function clearHash() { if (window.location.hash) window.location.hash = ""; }

function applyHashToState() {
  const raw = window.location.hash;
  // Nothing in the URL -> reset plan selection and restore filters from
  // localStorage (defaults when nothing is stored).
  if (!raw) {
    state.selectedPlan = null;
    loadFilters();
    return;
  }
  const parsed = parseHash(raw);
  if (parsed.selectedPlan !== null) state.selectedPlan = parsed.selectedPlan;
  state.filters = parsed.filters;
  saveFilters();
}

export {
  HASH_KEYS,
  hashStateFrom,
  encodeHashState,
  parseHash,
  updateHash,
  clearHash,
  applyHashToState,
};
