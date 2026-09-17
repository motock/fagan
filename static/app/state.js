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

const BACKEND_VALUES = ["local", "claude"];
const ESCALATED_VALUES = ["yes", "no"];
globalThis.BACKEND_VALUES = BACKEND_VALUES;
globalThis.ESCALATED_VALUES = ESCALATED_VALUES;

function defaultFilters() {
  return {
    statuses: [...STATUS_COLUMNS],
    personas: [],
    risks: [],
    backends: [],
    escalated: [],
    sort: "key",
    search: "",
  };
}

globalThis.state = {

const FILTERS_KEY = "pipeline-dashboard-filters";
const state = window.state;

// Re-establishes a fresh logical state on the shared singleton. Node's
// dynamic import() caches ./app/state.js by URL, so this module (and its
// `state` object) is created once per process even when app.js itself is
// cache-busted and re-imported per test — call this from app.js's init
// sequence so each load starts from known-default field values.
function resetState() {
  state.selectedPlan = null;
  state.selectedWorkspace = null;
  state.pollHandle = null;
  state.refreshIndicatorTimer = null;
  state.filters = defaultFilters();
  state.showArchived = false;
  state.showAllRepos = false;
  state.commsActive = true;
  state.configActive = false;
  state.workspaceActive = false;
}

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
  resetState,
  STATUS_COLUMNS,
  SORT_OPTIONS,
  VALID_SORTS,
  BACKEND_VALUES,
  ESCALATED_VALUES,
  FILTERS_KEY,
};
