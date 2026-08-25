// Shared dashboard state and state-init helpers.
//
// Extracted verbatim from static/app.js. Importers read `state` (and the
// filter helpers) from this module instead of `window.state`. The exported
// `state` binding is the single shared object — never reassign it; mutate it
// in place so every importer sees the same reference.

const STATUS_COLUMNS = [
  "todo", "in_progress", "tests_passed", "pr_open", "done",
  "changes_requested", "interrupted", "parked", "failed",
];

const SORT_OPTIONS = [
  ["key", "Key"],
  ["risk", "Risk"],
  ["activity", "Activity"],
];

const VALID_SORTS = new Set(SORT_OPTIONS.map(([v]) => v));

// Backend filter chip values. Stories whose `backend` field is missing are
// treated as "local" (the default semantic — the orchestrator hasn't picked
// anything else yet). Keeps the filter and the badge logic consistent: a
// story without a backend never shows a "claude" badge and matches the
// "local" chip, so neither surface ever leaks "undefined" to the user.
const BACKEND_VALUES = ["local", "claude"];
const ESCALATED_VALUES = ["yes", "no"];

export function defaultFilters() {
  return {
    statuses: [...STATUS_COLUMNS], // enabled statuses; default = all
    personas: [], // [] = no persona filter (show all)
    risks: [], // [] = no risk filter (show all)
    backends: [], // [] = no backend filter (show all); "local" | "claude"
    escalated: [], // [] = no escalation filter (show all); "yes" | "no"
    sort: "key", // "key" | "risk" | "activity"
    search: "", // free-text search term; "" = match all
  };
}

// `state` is the single shared dashboard state object. It was historically
// attached to `window` so deep-link helpers could mutate it from any caller
// and test harnesses could inspect it via dom.window.state; importers now
// read it directly from this module. Mutate in place — never reassign the
// exported binding.
export const state = {
  selectedPlan: null,
  pollHandle: null,
  refreshIndicatorTimer: null,
  filters: defaultFilters(),
  showArchived: false,
  commsActive: true,
  configActive: false,
};

const FILTERS_KEY = "pipeline-dashboard-filters";

export function loadFilters() {
  try {
    const stored = JSON.parse(localStorage.getItem(FILTERS_KEY) || "{}");
    state.filters = { ...defaultFilters(), ...stored };
  } catch {
    state.filters = defaultFilters();
  }
}

export function saveFilters() {
  try {
    localStorage.setItem(FILTERS_KEY, JSON.stringify(state.filters));
  } catch {
    /* localStorage unavailable; filters simply won't persist */
  }
}

// Toggle a value in one of the array-valued filter dimensions, then persist.
export function toggleFilter(dimension, value) {
  const list = state.filters[dimension];
  const idx = list.indexOf(value);
  if (idx === -1) list.push(value);
  else list.splice(idx, 1);
  saveFilters();
}
