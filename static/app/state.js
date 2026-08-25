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
window.BACKEND_VALUES = BACKEND_VALUES;
window.ESCALATED_VALUES = ESCALATED_VALUES;

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

window.state = {
  selectedPlan: null,
  pollHandle: null,
  refreshIndicatorTimer: null,
  filters: defaultFilters(),
  showArchived: false,
  commsActive: true,
  configActive: false,
};

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
  FILTERS_KEY,
};
