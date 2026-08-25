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
// Expose the value lists on `window` so the test harness (and any other
// out-of-realm consumer) can reference them via dom.window.BACKEND_VALUES
// rather than relying on the test running inside the same script realm.
window.BACKEND_VALUES = BACKEND_VALUES;
window.ESCALATED_VALUES = ESCALATED_VALUES;

function defaultFilters() {
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

// `state` is intentionally attached to `window` so deep-link helpers
// (applyHashToState / updateHash / clearHash) can mutate it from any caller,
// and so test harnesses can inspect it via dom.window.state.
window.state = {
  selectedPlan: null,
  pollHandle: null,
  refreshIndicatorTimer: null,
  filters: defaultFilters(),
  showArchived: false,
  commsActive: true,
  configActive: false,
};

// Local alias keeps the rest of the file terse.
const FILTERS_KEY = "pipeline-dashboard-filters";
const state = window.state;

function loadFilters() {
  try {
    const stored = JSON.parse(localStorage.getItem(FILTERS_KEY) || "{}");
    state.filters = { ...defaultFilters(), ...stored };
  } catch {
    state.filters = defaultFilters();
  }
}

function saveFilters() {
  try {
    localStorage.setItem(FILTERS_KEY, JSON.stringify(state.filters));
  } catch {
    /* localStorage unavailable; filters simply won't persist */
  }
}

// Toggle a value in one of the array-valued filter dimensions, then persist.
function toggleFilter(dimension, value) {
  const list = state.filters[dimension];
  const idx = list.indexOf(value);
  if (idx === -1) list.push(value);
  else list.splice(idx, 1);
  saveFilters();
}

export {
  state,
  defaultFilters,
  loadFilters,
  saveFilters,
  toggleFilter,
  STATUS_COLUMNS,
  SORT_OPTIONS,
  VALID_SORTS,
  BACKEND_VALUES,
  ESCALATED_VALUES,
};
