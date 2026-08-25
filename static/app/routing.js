// ---------- URL hash deep-linking ----------
//
// The hash encodes the plan + active filters so a view is shareable via URL
// and survives reload / browser back-forward. Defaults are omitted to keep
// URLs short. Unknown values are silently dropped (graceful fallback to
// defaults). localStorage remains a secondary store for when the URL has no
// hash at all (e.g. a fresh tab that previously set filters).

import {
  state,
  STATUS_COLUMNS,
  VALID_SORTS,
  BACKEND_VALUES,
  ESCALATED_VALUES,
  defaultFilters,
  saveFilters,
} from "./state.js";

// Map of hash key -> filter dimension key. Keep the URL form stable.
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

// Build a `{plan, filters}` snapshot from current state, suitable for either
// encoding into the hash or comparing for idempotency. Preserve user order
// for array-valued filters so encode -> parse is a clean round-trip.
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

// Serialize current state to a hash fragment (no leading "#"). Returns ""
// when every value is at its default, keeping a bare URL on default views.
function encodeHashState() {
  const snap = hashStateFrom(state);
  const parts = [];

  if (snap.selectedPlan) {
    parts.push(`plan=${encodeURIComponent(snap.selectedPlan)}`);
  }

  // statuses: only emit if not the default "all". Compare as sets so order in
  // the user-visible list doesn't change the URL form (set comparison is
  // robust to reordering, duplicates already removed).
  const allStatuses = new Set(STATUS_COLUMNS);
  const currentStatuses = new Set(snap.filters.statuses);
  const isAllStatuses = allStatuses.size === currentStatuses.size
    && [...allStatuses].every((s) => currentStatuses.has(s));
  if (currentStatuses.size && !isAllStatuses) {
    parts.push(`status=${snap.filters.statuses.map(encodeURIComponent).join(",")}`);
  }

  if (snap.filters.personas.length) {
    parts.push(`persona=${snap.filters.personas.map(encodeURIComponent).join(",")}`);
  }
  if (snap.filters.risks.length) {
    parts.push(`risk=${snap.filters.risks.map(encodeURIComponent).join(",")}`);
  }
  if (snap.filters.backends.length) {
    parts.push(`backend=${snap.filters.backends.map(encodeURIComponent).join(",")}`);
  }
  if (snap.filters.escalated.length) {
    parts.push(`escalated=${snap.filters.escalated.map(encodeURIComponent).join(",")}`);
  }
  if (snap.filters.sort !== "key") {
    parts.push(`sort=${encodeURIComponent(snap.filters.sort)}`);
  }
  if (snap.filters.search) {
    parts.push(`q=${encodeURIComponent(snap.filters.search)}`);
  }

  return parts.join("&");
}

// Parse a hash fragment (no leading "#") into a partial state overlay.
// Returns a {selectedPlan, filters} object; unknown values are silently
// dropped, malformed input falls back to defaults. Never throws.
function parseHash(raw) {
  const defaults = defaultFilters();
  const out = {
    selectedPlan: null,
    filters: defaults,
  };
  if (!raw) return out;
  // Strip leading "#" defensively; tolerate either form.
  const body = String(raw).replace(/^#/, "");
  if (!body) return out;

  let pairs;
  try {
    pairs = body.split("&").filter(Boolean);
  } catch {
    return out;
  }

  for (const pair of pairs) {
    const eq = pair.indexOf("=");
    if (eq <= 0) continue; // skip empty key or no value
    const key = pair.slice(0, eq).trim().toLowerCase();
    const value = pair.slice(eq + 1);
    if (!Object.prototype.hasOwnProperty.call(HASH_KEYS, key)) continue;
    const dim = HASH_KEYS[key];

    if (dim === "plan") {
      try {
        const plan = decodeURIComponent(value);
        if (plan) out.selectedPlan = plan;
      } catch {
        /* malformed encoding -> ignore */
      }
      continue;
    }

    if (dim === "sort") {
      try {
        const sort = decodeURIComponent(value).trim();
        if (VALID_SORTS.has(sort)) out.filters.sort = sort;
      } catch {
        /* ignore */
      }
      continue;
    }

    if (dim === "search") {
      try {
        const q = decodeURIComponent(value);
        if (q) out.filters.search = q;
      } catch { /* malformed encoding -> ignore */ }
      continue;
    }

    // Array-valued dimensions.
    let items;
    try {
      items = value.split(",").map((v) => {
        try { return decodeURIComponent(v); } catch { return null; }
      }).filter((v) => v !== null && v !== "");
    } catch {
      continue;
    }
    if (dim === "statuses") {
      const valid = new Set(STATUS_COLUMNS);
      out.filters.statuses = items.filter((s) => valid.has(s));
      if (!out.filters.statuses.length) out.filters.statuses = [...STATUS_COLUMNS];
    } else if (dim === "personas" || dim === "risks") {
      out.filters[dim] = items;
    } else if (dim === "backends") {
      const valid = new Set(BACKEND_VALUES);
      out.filters.backends = items.filter((v) => valid.has(v));
    } else if (dim === "escalated") {
      const valid = new Set(ESCALATED_VALUES);
      out.filters.escalated = items.filter((v) => valid.has(v));
    }
  }

  return out;
}

// Re-derive window.location.hash from state. Uses replaceState-like behavior:
// we set the hash via the Location API so a hashchange fires once and a
// back-button can pop to the previous URL. We only rewrite when the parsed
// hash differs from current state, to avoid redundant hashchange loops.
function updateHash() {
  const desired = encodeHashState();
  const current = window.location.hash.replace(/^#/, "");
  if (desired === current) return;
  // Using location.hash assignment is the simplest path; for "no selection"
  // we clear it (hashchange fires once with the empty hash).
  if (desired) {
    window.location.hash = desired;
  } else if (window.location.hash) {
    // Setting to "" removes the fragment entirely.
    window.location.hash = "";
  }
}

function clearHash() {
  if (window.location.hash) window.location.hash = "";
}

// Apply the current window.location.hash onto `state`. Idempotent: callers
// may invoke it on init and again on hashchange without recursion.
//
// Hash-only contract: this function only mutates state when there is a
// non-empty hash. A bare URL is a no-op so loadFilters() (or whatever
// restore path ran before us) keeps the localStorage-derived state.
function applyHashToState() {
  const raw = window.location.hash;
  // Nothing in the URL -> nothing to apply.
  if (!raw) return;
  const parsed = parseHash(raw);
  if (parsed.selectedPlan !== null) {
    state.selectedPlan = parsed.selectedPlan;
  }
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
