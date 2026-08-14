const STATUS_COLUMNS = [
  "todo", "in_progress", "tests_passed", "pr_open", "done",
  "changes_requested", "interrupted", "parked", "failed",
];

const SORT_OPTIONS = [
  ["key", "Key"],
  ["risk", "Risk"],
  ["activity", "Activity"],
];

const RISK_RANK = { high: 3, medium: 2, low: 1 };

// Stale threshold (minutes) for the in_progress "aged" indicator on cards.
// Mirrors STALE_IN_PROGRESS_MINUTES in dashboard.py — the dashboard hands
// us `last_activity` and the UI does the math so cards stay accurate
// without re-fetching.
const STALE_IN_PROGRESS_MINUTES = 30;
const NOTIF_SEVERITY_COLOR = { "error": "--c-failed", "warning": "--c-parked", "info": "--c-unknown" };
const FILTERS_KEY = "pipeline-dashboard-filters";
let notifSeverityFilter = "all";
function filterNotifications(records, severity) {
  if (!records) return [];
  if (!severity || severity === "all") return records.slice();
  return records.filter((r) => (r && r.severity) === severity);
}

const DEFAULT_THEME = "dark";
// Parse an ISO-8601 string into a Date. Returns null for any falsy or
// unparseable value — the dashboard never promises strict formatting, and
// a bad row should just hide the age label rather than throw.
function parseIso(ts) {
  if (typeof ts !== "string" || !ts) return null;
  // Date.parse accepts the trailing 'Z' suffix natively, so a direct call
  // handles both "...+00:00" and "...Z" forms. Any garbage falls through
  // to NaN and we return null.
  const d = new Date(ts);
  if (isNaN(d.getTime())) return null;
  return d;
}

// Format a positive (or zero) age in seconds as a short relative-age label
// like "just now" (<60s), "3m ago" (<1h), "2h ago" (<1d), "4d ago".
// Future timestamps (negative age) are clamped to "just now" — never show
// negative durations.
function relativeAgeLabel(ageSeconds) {
  if (ageSeconds < 60) return "just now";
  const minutes = Math.floor(ageSeconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

// Compute the human-readable age for a story's last_activity ISO timestamp,
// or null if there is no last_activity. Negative ages (future timestamps)
// are clamped to "just now".
function ageLabelFor(lastActivity) {
  const d = parseIso(lastActivity);
  if (!d) return null;
  const ageSec = (Date.now() - d.getTime()) / 1000;
  return relativeAgeLabel(ageSec);
}

// True when an in_progress story has a last_activity older than the stale
// threshold. Stories without last_activity, or with other statuses, are
// never stale here — we only warn about agents that *were* running and
// appear to have stopped.
function isStaleInProgress(story) {
  if (!story || story.status !== "in_progress") return false;
  const d = parseIso(story.last_activity);
  if (!d) return false;
  const ageMin = (Date.now() - d.getTime()) / 60000;
  return ageMin > STALE_IN_PROGRESS_MINUTES;
}

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

// ---------- URL hash deep-linking ----------
//
// The hash encodes the plan + active filters so a view is shareable via URL
// and survives reload / browser back-forward. Defaults are omitted to keep
// URLs short. Unknown values are silently dropped (graceful fallback to
// defaults). localStorage remains a secondary store for when the URL has no
// hash at all (e.g. a fresh tab that previously set filters).

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

// `state` is intentionally attached to `window` so deep-link helpers
// (applyHashToState / updateHash / clearHash) can mutate it from any caller,
// and so test harnesses can inspect it via dom.window.state.
window.state = {
  selectedPlan: null,
  pollHandle: null,
  refreshIndicatorTimer: null,
  filters: defaultFilters(),
  showArchived: false,
};
// Local alias keeps the rest of the file terse.
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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

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

// Plans come back from /api/plans already sorted newest-first by the
// server (by manifest mtime) and, by default, with archived plans excluded
// - state.showArchived controls whether refresh() asks for them too (see
// the include_archived query param built there).
function renderPlanList(plans) {
  const nav = document.getElementById("plan-list");
  nav.innerHTML = "";

  // "Overview" always sits at the top of the plan sidebar so the user has
  // a one-click escape hatch back to the fleet landing view, independent
  // of the currently selected plan.
  const overview = document.createElement("div");
  overview.className = "plan-item overview-item"
    + (state.selectedPlan ? "" : " active");
  overview.setAttribute("data-overview", "true");
  overview.innerHTML = `
    <div class="plan-name">Overview</div>
    <div class="plan-meta">fleet landing</div>
  `;
  overview.addEventListener("click", () => selectOverview());
  nav.appendChild(overview);

  for (const plan of plans) {
    const div = document.createElement("div");
    div.className = "plan-item" + (plan.name === state.selectedPlan ? " active" : "")
      + (plan.archived ? " plan-archived" : "");
    const total = plan.story_count || 0;
    const done = (plan.status_counts && plan.status_counts.done) || 0;
    div.innerHTML = `
      <div class="plan-item-row">
        <div class="plan-item-main">
          <div class="plan-name">${escapeHtml(plan.name)}</div>
          <div class="plan-meta">${done}/${total} done${plan.paused ? ' <span class="plan-paused">paused</span>' : ""}</div>
        </div>
        <button type="button" class="plan-archive-btn" title="${plan.archived ? "Restore" : "Dismiss"}">
          ${plan.archived ? "Restore" : "Dismiss"}
        </button>
      </div>
    `;
    div.addEventListener("click", () => selectPlan(plan.name));
    div.querySelector(".plan-archive-btn").addEventListener("click", (event) => {
      // Don't let the archive/restore click also select the plan.
      event.stopPropagation();
      togglePlanArchived(plan.name, plan.archived);
    });
    nav.appendChild(div);
  }

  // Built with direct DOM calls, not an innerHTML string: a bare
  // <input type="checkbox"> has no closing tag, and this file's lightweight
  // test-only DOM stubs parse innerHTML by matching open/close tag pairs -
  // a self-closing input embedded in an innerHTML string would silently
  // fail to parse into a real, listenable element under test.
  const toggle = document.createElement("div");
  toggle.className = "plan-list-footer";
  const label = document.createElement("label");
  label.className = "show-archived-toggle";
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.checked = state.showArchived;
  checkbox.addEventListener("change", (event) => {
    state.showArchived = event.target.checked;
    refresh();
  });
  label.appendChild(checkbox);
  const labelText = document.createElement("span");
  labelText.textContent = "Show dismissed plans";
  label.appendChild(labelText);
  toggle.appendChild(label);
  nav.appendChild(toggle);
}

// Archive/unarchive is fire-and-forget from the UI's perspective: on
// success, refresh() re-fetches /api/plans so the sidebar reflects the new
// archived set (and re-sorts/re-filters) rather than us hand-patching the
// DOM in place. A failed request leaves the list as-is; the user can retry.
async function togglePlanArchived(planName, currentlyArchived) {
  const action = currentlyArchived ? "unarchive" : "archive";
  try {
    await postJson(`/api/plans/${encodeURIComponent(planName)}/${action}`);
  } catch {
    return;
  }
  await refresh();
}

function riskRank(story) {
  return RISK_RANK[story.risk] || 0;
}

function activityScore(story) {
  return (story.dispatch_attempts || 0)
    + (story.rework_attempts || 0)
    + (story.merge_attempts || 0);
}

// Persona/risk/backend/escalation-filter the cards of one status column,
// then sort per state.filters.sort. Status filtering happens at the column
// level in renderBoard, not here.
function applyFilters(entries) {
  const { personas, risks, backends, escalated, sort, search } = state.filters;
  const term = (search || "").trim().toLowerCase();
  // A story without a `backend` field is treated as "local" (the orchestrator
  // hasn't chosen anything else yet). This must agree with the badge logic
  // in renderBoard so a missing backend never leaks as "undefined".
  const storyBackend = (s) => (s && s.backend) ? s.backend : "local";
  const storyEscalated = (s) => !!(s && s.escalated);
  const filtered = entries.filter(([key, s]) =>
    (personas.length === 0 || personas.includes(s.persona))
    && (risks.length === 0 || risks.includes(s.risk))
    && (backends.length === 0 || backends.includes(storyBackend(s)))
    && (escalated.length === 0
        || (escalated.includes("yes") && storyEscalated(s))
        || (escalated.includes("no") && !storyEscalated(s)))
    && (term === "" || (String(key) + " " + (s.summary || "")).toLowerCase().includes(term)));

  const comparators = {
    key: ([a], [b]) => a.localeCompare(b, undefined, { numeric: true }),
    risk: ([, a], [, b]) => riskRank(b) - riskRank(a),
    activity: ([, a], [, b]) => activityScore(b) - activityScore(a),
  };
  filtered.sort(comparators[sort] || comparators.key);
  return filtered;
}

function renderBoard(stories) {
  // Total story count for the whole plan — used by the done column's
  // completion hint so the user sees "2/5 done" instead of just "2",
  // without changing the filter logic (we still count filtered cards
  // inside each column). Computed once per render to avoid repeating the
  // work in every column map.
  const planTotal = Object.keys(stories).length;

  const columns = state.filters.statuses
    .filter((status) => STATUS_COLUMNS.includes(status))
    .sort((a, b) => STATUS_COLUMNS.indexOf(a) - STATUS_COLUMNS.indexOf(b))
    .map((status) => {
      const entries = applyFilters(
        Object.entries(stories).filter(([, s]) => s.status === status));
      const cards = entries.map(([key, s]) => {
        // Per-card decorations: age label when last_activity exists, and
        // a stale class on aged in_progress stories so the user can see
        // at a glance which agents are wedged. Both are derived from
        // story.last_activity (server-supplied) using client-side time
        // so they stay correct between polls.
        const ageLabel = ageLabelFor(s.last_activity);
        const stale = isStaleInProgress(s);
        const classes = ["card"];
        if (stale) classes.push("stale");
        // Backend / escalation badges. A story without a `backend` field
        // is treated as local and gets no claude badge; a non-escalated
        // story gets no escalated badge. Keeping these conditional means
        // most cards stay visually quiet — the badges surface the
        // *interesting* cases (claude-bound, escalated) rather than
        // repeating what the status stripe already conveys.
        const badges = [];
        if (s.backend === "claude") {
          badges.push(`<span class="card-badge card-badge-claude" title="Backend: claude">claude</span>`);
        }
        if (s.escalated) {
          badges.push(`<span class="card-badge card-badge-escalated" title="Escalated to claude">escalated</span>`);
        }
        const badgesHtml = badges.length
          ? `<div class="card-badges">${badges.join("")}</div>`
          : "";
        // Progress bar for in_progress stories with checklist data
        const pct = (s.status === "in_progress" && s.progress && s.progress.total > 0)
          ? Math.round(s.progress.done / s.progress.total * 100) : 0;
        const progressHtml = (s.status === "in_progress" && s.progress && s.progress.total > 0)
          ? `<div class="card-progress">
               <div class="card-progress-track"><div class="card-progress-fill" style="width: ${pct}%"></div></div>
               <span class="card-progress-label">${s.progress.done}/${s.progress.total}</span>
             </div>`
          : "";
        return `
        <div class="${classes.join(" ")}" style="--badge-color: var(--c-${status})" data-key="${escapeHtml(key)}">
          <div class="card-key">${escapeHtml(key)}</div>
          <div class="card-summary">${escapeHtml(s.summary || "(no summary)")}</div>
          ${badgesHtml}${progressHtml}
          ${ageLabel ? `<div class="card-age${stale ? " stale" : ""}">${escapeHtml(ageLabel)}</div>` : ""}
        </div>
`;
      }).join("");
      // Thin completion hint for the done column: shows done / plan-total
      // so the user sees plan-wide progress at a glance. We use planTotal
      // (not entries.length) so the fraction stays meaningful even when
      // persona/risk filters narrow the visible cards. Hidden when the
      // plan has no stories yet to avoid "0/0".
      const completion = (status === "done" && planTotal > 0)
        ? `<div class="column-completion">${entries.length}/${planTotal}</div>`
        : "";
      return `
        <div class="column">
          <div class="column-header">
            <span>${status}</span>
            <span class="badge" style="--badge-color: var(--c-${status})">${entries.length}</span>
          </div>
          ${completion}
          <div class="column-body">${cards}</div>
        </div>
      `;
    }).join("");

  if (!columns) {
    return '<div class="board"><p class="empty-state">No statuses selected.</p></div>';
  }
  const term = (state.filters.search || "").trim();
  const anyCards = columns.includes('data-key="');
  if (term && !anyCards) {
    return `<div class="board"><div class="board-empty">No matches for "${escapeHtml(term)}"</div></div>`;
  }
  return `<div class="board">${columns}</div>`;
}

function chip(dim, value, label, active, color) {
  const style = color ? ` style="--badge-color: var(--c-${color})"` : "";
  return `<button class="filter-chip${active ? " active" : ""}"`
    + ` data-dim="${escapeHtml(dim)}" data-value="${escapeHtml(value)}"${style}>`
    + `${escapeHtml(label)}</button>`;
}

function renderFilterBar(stories) {
  const all = Object.values(stories);
  const personas = [...new Set(all.map((s) => s.persona).filter(Boolean))].sort();
  const risks = [...new Set(all.map((s) => s.risk).filter(Boolean))]
    .sort((a, b) => (RISK_RANK[b] || 0) - (RISK_RANK[a] || 0));
  // Show the Backend / Escalated groups only when at least one story in
  // this plan has a non-local backend or has been escalated. Most plans
  // will be all-local, all-non-escalated — surfacing "yes" / "claude"
  // chips when they would always return zero results just adds noise.
  // When the relevant field is missing on every story we still render
  // the group with the default values; a later poll that introduces
  // escalation will populate it without a code change.
  const hasClaudeBackend = all.some((s) => s && s.backend === "claude");
  const hasAnyEscalation = all.some((s) => s && s.escalated);

  const { filters } = state;
  const term = (filters.search || "").trim().toLowerCase();
  const matchCount = all.filter((s) =>
    term === "" || (String(s.key || "") + " " + (s.summary || "")).toLowerCase().includes(term)
  ).length;
  const countLabel = term === "" ? "" : `<span class="filter-match-count">${matchCount} match${matchCount === 1 ? "" : "es"}</span>`;
  const groups = [
    `<div class="filter-group filter-search-group"><span class="filter-group-label">Search</span>` +
    `<input class="filter-search" type="search" placeholder="Search key or summary…" value="${escapeHtml(filters.search || "")}" data-action="search"></input>` +
    `${countLabel}</div>`,
    `<div class="filter-group"><span class="filter-group-label">Status</span>${
      STATUS_COLUMNS.map((s) =>
        chip("statuses", s, s, filters.statuses.includes(s), s)).join("")
    }</div>`,
  ];
  if (personas.length) {
    groups.push(`<div class="filter-group"><span class="filter-group-label">Persona</span>${
      personas.map((p) => chip("personas", p, p, filters.personas.includes(p))).join("")
    }</div>`);
  }
  if (risks.length) {
    groups.push(`<div class="filter-group"><span class="filter-group-label">Risk</span>${
      risks.map((r) => chip("risks", r, r, filters.risks.includes(r))).join("")
    }</div>`);
  }
  // Backend group: only render the row when a claude backend actually
  // exists in the plan. We always offer both "local" and "claude" so the
  // user can pin to either side; "local" covers the implicit-default
  // case (no backend field present) plus explicitly-local stories.
  if (hasClaudeBackend) {
    groups.push(`<div class="filter-group"><span class="filter-group-label">Backend</span>${
      BACKEND_VALUES.map((b) =>
        chip("backends", b, b, filters.backends.includes(b), b === "claude" ? "accent" : null)
      ).join("")
    }</div>`);
  }
  if (hasAnyEscalation) {
    groups.push(`<div class="filter-group"><span class="filter-group-label">Escalated</span>${
      ESCALATED_VALUES.map((e) =>
        chip("escalated", e, e, filters.escalated.includes(e), e === "yes" ? "parked" : null)
      ).join("")
    }</div>`);
  }
  groups.push(`<div class="filter-group"><span class="filter-group-label">Sort</span>${
    SORT_OPTIONS.map(([v, label]) => chip("sort", v, label, filters.sort === v)).join("")
  }</div>`);

  return `<div class="filter-bar">${groups.join("")}`
    + `<button class="filter-reset" data-action="reset">Reset</button></div>`;
}

function renderNotifications(records) {
  if (!records || !records.length) return '<p class="empty-state">No notifications yet.</p>';
  return records.slice().reverse().map(function (r) {
    var color = NOTIF_SEVERITY_COLOR[r.severity] || "--c-unknown";
    var sev = escapeHtml(r.severity || "");
    var parts = [];
    parts.push('<div class="log-line">');
    parts.push('<span class="badge" style="--badge-color: var(' + color + ')">' + sev + '</span>');
    if (r.story_key) {
      parts.push('<span class="mono">' + escapeHtml(r.story_key) + '</span>');
    }
    parts.push(escapeHtml(r.message || ""));
    if (r.count && r.count > 1) {
      parts.push('<span class="badge">x' + escapeHtml(String(r.count)) + '</span>');
    }
    if (r.ts) {
      parts.push(escapeHtml(r.ts));
    }
    parts.push('</div>');
    return parts.join(' ');
  }).join('');
}

function renderDecisions(decisions) {
  if (!decisions.length) return '<p class="empty-state">No decisions logged yet.</p>';
  return decisions.slice().reverse().map((d) => `
    <div class="decision">
      <div class="decision-q">${escapeHtml(d.question)}</div>
      <div class="decision-meta">
        ${escapeHtml(d.ruling || "")} — tier=${escapeHtml(d.tier || "?")}, risk=${escapeHtml(d.risk || "?")}
        · ${escapeHtml(d.decided_at || "")}
      </div>
    </div>
  `).join("");
}

function renderPlanDetail(plan) {
  const section = document.getElementById("plan-detail");

  // Snapshot the detail section's state so we can restore it after the full
  // re-render: scroll position, and any focused filter chip. Capturing a
  // stable identity (data-dim + data-value) — rather than the raw DOM node —
  // lets us re-focus the matching chip if it survives the re-render.
  const snapshot = capturePlanDetailState(section);

  section.innerHTML = `
    <div class="plan-header">
      <h2>${escapeHtml(plan.name)}</h2>
      ${plan.paused ? '<span class="badge" style="--badge-color: var(--c-parked)">paused</span>' : ""}
    </div>
    ${renderFilterBar(plan.stories)}
    ${renderBoard(plan.stories)}
    <div class="panels">
      <div class="panel">
        <h3>Notifications</h3>
        <div class="panel-body">${renderNotifications(plan.notification_records)}</div>
      </div>
      <div class="panel">
        <h3>Overlord decisions</h3>
        <div class="panel-body">${renderDecisions(plan.decisions)}</div>
      </div>
    </div>
  `;

  section.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("click", () => showStoryModal(plan.name, plan.stories[card.dataset.key], card.dataset.key));
  });

  section.querySelectorAll(".filter-chip").forEach((el) => {
    el.addEventListener("click", () => {
      const { dim, value } = el.dataset;
      if (dim === "sort") {
        state.filters.sort = value;
        saveFilters();
      } else {
        toggleFilter(dim, value);
      }
      updateHash();
      renderPlanDetail(plan);
    });
  });
  const searchInput = section.querySelector && section.querySelector(".filter-search");
  if (searchInput) {
    searchInput.addEventListener("input", () => {
      state.filters.search = searchInput.value;
      saveFilters();
      updateHash();
      renderPlanDetail(plan);
    });
  }
  const resetBtn = section.querySelector && section.querySelector(".filter-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      state.filters = defaultFilters();
      saveFilters();
      updateHash();
      renderPlanDetail(plan);
    });
  }

  // Restore scroll + focus. Wrapped in try/catch defensively: a missing
  // focused element or a DOM that didn't survive the re-render must NOT throw.
  try {
    restorePlanDetailState(section, snapshot);
  } catch {
    /* snapshot stale or focus target gone; nothing to restore */
  }
}

// Capture the scroll position and the focused element's identity from the
// detail section. The identity is just the data-dim + data-value pair of the
// focused filter chip (or null if nothing meaningful is focused). Plain data
// attributes survive innerHTML replacement, so we can look the chip back up
// in the new DOM.
function capturePlanDetailState(section) {
  if (!section) return { scrollTop: 0, focusKey: null };
  const ae = document.activeElement;
  let focusKey = null;
  if (ae && ae !== document.body && section.contains(ae) && ae.dataset
      && ae.dataset.dim !== undefined && ae.dataset.value !== undefined) {
    focusKey = `${ae.dataset.dim}\u0000${ae.dataset.value}`;
  }
  return { scrollTop: section.scrollTop || 0, focusKey };
}

// Restore scrollTop, and re-focus the matching chip if it still exists.
// Negative/boundary case: focusKey is null (nothing was focused) or the chip
// with that identity was removed by the re-render — in both cases we simply
// skip focusing, never throw.
function restorePlanDetailState(section, snapshot) {
  if (!section || !snapshot) return;
  section.scrollTop = snapshot.scrollTop || 0;
  if (!snapshot.focusKey) return;
  const [dim, value] = snapshot.focusKey.split("\u0000");
  const target = section.querySelector(
    `.filter-chip[data-dim="${CSS.escape(dim)}"][data-value="${CSS.escape(value)}"]`);
  if (target && typeof target.focus === "function") {
    target.focus();
  }
}

// Flash the header refresh indicator. Called once per successful refresh so
// the user sees liveness without staring at the clock. The .flashing class
// drives the dot's pulse animation; we toggle it off after the animation so
// the element returns to its idle (invisible) state.
function flashRefreshIndicator() {
  const el = document.getElementById("refresh-indicator");
  if (!el) return;
  // Re-render contents each time so the animation restarts cleanly even on
  // back-to-back flashes that would otherwise be coalesced by the browser.
  el.innerHTML = '<span class="dot" aria-hidden="true"></span>'
    + '<span class="refresh-indicator-label">updating\u2026</span>';
  // Force a reflow so removing + re-adding the class restarts the keyframes
  // when refreshes happen faster than the animation duration.
  // eslint-disable-next-line no-unused-expressions
  el.offsetWidth;
  el.classList.remove("flashing");
  el.classList.add("flashing");
  // Schedule fade back to idle. setTimeout ID is held so a faster subsequent
  // refresh can cancel and reschedule cleanly.
  if (state.refreshIndicatorTimer) clearTimeout(state.refreshIndicatorTimer);
  state.refreshIndicatorTimer = setTimeout(() => {
    el.classList.remove("flashing");
    state.refreshIndicatorTimer = null;
  }, 750);
}

// True when a value should be rendered in a monospace <pre> block instead
// of an inline <dd>. Long paths, IDs, URLs, commit hashes, and JSON-ish
// blobs benefit from a fixed-width wrap so users can read/copy them.
function isLongValue(value) {
  const s = String(value == null ? "" : value);
  if (s.length > 60) return true;
  return /[\\/\n]/.test(s);
}

// Map of copy-button field name -> rendered value for the currently-open
// story modal. Populated by showStoryModal and consumed by handleCopyClick
// so the click handler can resolve the value without walking detached DOM
// (which the test fixtures simulate) or re-deriving from HTML.
const copyValues = new Map();

// Currently-open story in the modal: { plan, key } or null. Tracked so the
// async journal fetch (which races with the user opening a different story
// or closing the modal) can drop stale responses without racing in late
// DOM writes. Without this guard, a slow fetch from a prior open could
// overwrite the new story's journal section with the previous one.
const openStoryRef = { plan: null, key: null };

// Render a single timeline entry as HTML. Defensive defaults for fields the
// server may or may not have (entry missing `next_hint` must render nothing,
// never the literal string 'undefined'). Returns "" for entries with no
// step AND no summary so corrupt rows leave no visual artifact.
function renderJournalEntry(entry) {
  if (!entry || typeof entry !== "object") return "";
  const step = entry.step ? String(entry.step) : "";
  const summary = entry.summary ? String(entry.summary) : "";
  const nextHint = entry.next_hint ? String(entry.next_hint) : "";
  const ts = entry.ts ? String(entry.ts) : "";
  if (!step && !summary) return "";
  // next_hint is muted/caption-tone because it's metadata about what comes
  // NEXT (process hint), not part of the current entry's narrative. We omit
  // the block entirely when missing rather than emitting an empty <p>.
  const nextHtml = nextHint
    ? `<div class="timeline-next muted">next: ${escapeHtml(nextHint)}</div>`
    : "";
  const tsHtml = ts
    ? `<div class="timeline-ts muted">${escapeHtml(ts)}</div>`
    : "";
  return `
    <li class="timeline-item">
      <div class="timeline-step">${escapeHtml(step || "(no step)")}</div>
      <div class="timeline-summary">${escapeHtml(summary)}</div>
      ${nextHtml}
      ${tsHtml}
    </li>
  `;
}

// Render the entire Journal section for the story modal. `data` matches the
// /api/plans/{plan}/stories/{story}/journal response: {available, entries}.
// The empty state is shown when (a) the file is missing/malformed, or
// (b) the entries array is empty — both are visually indistinguishable in
// the UI, matching the endpoint's contract (test: empty list == unavailable).
function renderJournal(data) {
  if (!data || !data.available || !Array.isArray(data.entries) || data.entries.length === 0) {
    return `
      <h3 class="modal-section">Journal</h3>
      <p class="modal-empty" data-journal-empty>No journal yet.</p>
    `;
  }
  const items = data.entries.map(renderJournalEntry).filter(Boolean).join("");
  return `
    <h3 class="modal-section">Journal</h3>
    <ol class="timeline" data-journal-list>${items}</ol>
  `;
}

// Render the per-story Checklist section (DASHBOARD_STORY_PROGRESS_PLAN.md
// Tier 0): the tech-lead's ordered checklist (.agent_plan.md) and the
// executor's running scratchpad (.agent_scratchpad.md), both fetched from the
// story's worktree. `data` matches the /checklist endpoint response:
//   { plan: {available, text}, scratchpad: {available, text} }
//
// These artifacts only exist for stories run under PIPELINE_DECOMPOSE; every
// other story (the common case) gets the "No checklist" empty state. The
// agent writes both files, so their text is HTML-escaped (never trusted) —
// mirroring renderJournal's escaping contract.
function renderChecklist(data) {
  const empty = `
    <h3 class="modal-section">Checklist</h3>
    <p class="modal-empty" data-checklist-empty>No checklist (story not run with guided decomposition).</p>
  `;
  if (!data) return empty;
  const plan = (data.plan && typeof data.plan === "object") ? data.plan : {};
  const scratch = (data.scratchpad && typeof data.scratchpad === "object") ? data.scratchpad : {};
  if (!plan.available && !scratch.available) return empty;
  const parts = [`<h3 class="modal-section">Checklist</h3>`];
  if (plan.available) {
    parts.push(
      `<pre class="dsh-checklist-plan mono" data-checklist-plan>${escapeHtml(plan.text || "")}</pre>`
    );
  }
  if (scratch.available) {
    parts.push(`<h4 class="modal-subsection">Progress notes</h4>`);
    parts.push(
      `<pre class="dsh-checklist-scratch mono" data-checklist-scratch>${escapeHtml(scratch.text || "")}</pre>`
    );
  }
  return parts.join("");
}

// Fetch the journal for the currently-open story and inject the rendered
// HTML into the modal body. Guards against races with modal close /
// re-open by checking `openStoryRef` before mutating DOM. Failures (network
// down, server 500) are silently swallowed — the empty state shows by
// default. The journal is informational, not critical, so it must never
// block opening the modal or throw an unhandled promise rejection.
async function loadStoryJournal(plan, key, container) {
  if (!plan || !key || !container) return;
  openStoryRef.plan = plan;
  openStoryRef.key = key;
  try {
    const data = await fetchJson(
      `/api/plans/${encodeURIComponent(plan)}/stories/${encodeURIComponent(key)}/journal`
    );
    // Drop the response if the user closed the modal or opened a
    // different story in the meantime — late writes would overwrite
    // the new view with stale data.
    if (openStoryRef.plan !== plan || openStoryRef.key !== key) return;
    const html = renderJournal(data);
    const slot = container.querySelector("[data-journal-slot]");
    if (slot) slot.innerHTML = html;
  } catch {
    // 404 (story/plan gone) or transient network error: keep the empty
    // state placeholder. Don't rethrow — the modal stays usable.
    if (openStoryRef.plan !== plan || openStoryRef.key !== key) return;
    const slot = container.querySelector("[data-journal-slot]");
    if (slot) {
      slot.innerHTML = `
        <h3 class="modal-section">Journal</h3>
        <p class="modal-empty">No journal yet.</p>
      `;
    }
  }
}

// Fetch the checklist + scratchpad for the currently-open story and inject
// the rendered HTML into the modal's checklist slot. Same race-guard
// contract as loadStoryJournal (openStoryRef): a slow fetch that resolves
// after the user opened a different story is dropped, never overwriting the
// new view. Failures (network, 500) fall back to the empty state, never
// throw — the checklist is informational, like the journal.
async function loadStoryChecklist(plan, key, container) {
  if (!plan || !key || !container) return;
  openStoryRef.plan = plan;
  openStoryRef.key = key;
  try {
    const data = await fetchJson(
      `/api/plans/${encodeURIComponent(plan)}/stories/${encodeURIComponent(key)}/checklist`
    );
    if (openStoryRef.plan !== plan || openStoryRef.key !== key) return;
    const slot = container.querySelector("[data-checklist-slot]");
    if (slot) slot.innerHTML = renderChecklist(data);
  } catch {
    if (openStoryRef.plan !== plan || openStoryRef.key !== key) return;
    const slot = container.querySelector("[data-checklist-slot]");
    if (slot) slot.innerHTML = renderChecklist(null);
  }
}


// Build a small "copy" button. The data-copy attribute carries the field
// name; handleCopyClick resolves the value from `copyValues` so the markup
// stays a single tiny token and so copy click events for the currently-open
// story work consistently.
function renderCopyButton(fieldName, value) {
  copyValues.set(fieldName, String(value == null ? "" : value));
  return `<button type="button" class="copy-btn" data-copy="${escapeHtml(fieldName)}" aria-label="Copy ${escapeHtml(fieldName)}">copy</button>`;
}

// Click handler for `.copy-btn` elements within the story modal. Attached
// once at module load via event delegation; we don't rebind on every open.
// Silently swallows clipboard errors and environments where
// navigator.clipboard is undefined, so the button never throws.
function handleCopyClick(e) {
  const btn = e.target.closest && e.target.closest(".copy-btn");
  if (!btn) return;
  const field = btn.getAttribute("data-copy") || "";
  const text = copyValues.get(field) || "";
  if (!text) return;
  // navigator.clipboard requires a secure context; guard both the API
  // presence and the writeText call. In environments where the API is
  // unavailable (older browsers, insecure contexts, Node smoke tests)
  // the click is a silent no-op — never throws.
  if (typeof navigator === "undefined" || !navigator.clipboard
      || typeof navigator.clipboard.writeText !== "function") {
    return;
  }
  navigator.clipboard.writeText(text).then(
    () => {
      const prev = btn.textContent;
      btn.textContent = "copied";
      btn.classList.add("copied");
      setTimeout(() => {
        btn.textContent = prev;
        btn.classList.remove("copied");
      }, 1200);
    },
    () => {
      // Swallow rejection (denied permission, etc.) — feature detection
      // already passed so we treat this as a transient user-side issue.
    }
  );
}

function showStoryModal(planName, story, key) {
  _renderStoryModalBody(planName, story, key);
}

function _renderStoryModalBody(planName, story, key) {
  const modal = document.getElementById("story-modal");
  const body = document.getElementById("story-modal-body");
  const deps = story.dependencies;
  const depsText = Array.isArray(deps) ? deps.join(", ") : "";
  const depsShow = Array.isArray(deps) && deps.length > 0 ? depsText : null;

  // Track which plan/story the modal is currently showing so the async
  // log fetch can resolve into the right slot and so a stale fetch that
  // resolves after the user opened a different story can't write into the
  // wrong modal.
  modal.dataset.plan = state.selectedPlan || "";
  modal.dataset.story = key;

  // Field lists per section. Each entry: [label, value, opts].
  //   - `opts.copy` enables the click-to-copy button on that value, using
  //     `opts.copyField` (or fall back to label) as the data-copy key.
  //   - `opts.mono` forces monospace <pre> rendering for short but
  //     structured values (e.g. a 12-char commit hash).
  //
  // The four sections match the documented field grouping in the story:
  //   - Identity: who/what the story is
  //   - Lifecycle: where the agent is in execution
  //   - Dispatch & review: agent-loop bookkeeping and reviewer notes
  //   - Errors: failure/pause context surfaced to humans
  const identity = [
    ["Key", key, { copy: true, copyField: "key" }],
    ["Summary", story.summary],
    ["Persona", story.persona],
    ["Model", story.dispatched_model || story.model],
    ["Risk", story.risk],
    ["Dependencies", depsShow],
  ];
  const lifecycle = [
    ["Status", story.status],
    ["Backend", story.backend],
    ["Escalated", story.escalated],
    ["Worktree", story.worktree, { copy: true, copyField: "worktree" }],
    ["Branch", story.branch],
    ["PID", story.pid, { copy: true, copyField: "pid", mono: true }],
    ["PR URL", story.pr_url, { copy: true, copyField: "pr_url", mono: true }],
    ["Last commit", story.last_commit, { mono: true }],
    ["Interrupted at", story.interrupted_at],
  ];
  const dispatch = [
    ["Dispatch attempts", story.dispatch_attempts],
    ["Rework attempts", story.rework_attempts],
    ["Merge attempts", story.merge_attempts],
    ["Review verdict", story.review_verdict],
    ["Review feedback", story.review_feedback],
  ];
  const errors = [
    ["Dispatch error", story.dispatch_error],
    ["Merge error", story.merge_error],
    ["Failure reason", story.failure_reason],
    ["Parked reason", story.parked_reason],
  ];

  // Reset any previously-cached copy values before populating this story.
  // Stale entries from a prior open would otherwise let a stale field
  // name resolve to its old value, which would be both confusing and a
  // potential information leak across stories.
  copyValues.clear();

  // Render a field row, applying long/mono/copy semantics.
  function renderRow(label, value, opts) {
    opts = opts || {};
    const v = value;
    if (v === undefined || v === null || v === "") return "";
    const mono = opts.mono || isLongValue(v);
    const inner = mono
      ? `<pre class="mono">${escapeHtml(String(v))}</pre>`
      : escapeHtml(v);
    const btn = opts.copy ? renderCopyButton(opts.copyField || label, v) : "";
    return `<dt>${escapeHtml(label)}</dt><dd>${inner}${btn}</dd>`;
  }

  function renderSection(title, rows) {
    const html = rows.map(([label, value, opts]) => renderRow(label, value, opts))
      .filter(Boolean)
      .join("");
    if (!html) return "";
    // Section titles are hardcoded literal strings, not user input — emit
    // them raw so "&" survives as "&" rather than "&amp;" (which would
    // still render correctly in the browser but makes the markup noisy).
    return `<h3 class="modal-section">${title}</h3><dl>${html}</dl>`;
  }

  const sections = [
    renderSection("Identity", identity),
    renderSection("Lifecycle", lifecycle),
    renderSection("Dispatch & review", dispatch),
    renderSection("Errors", errors),
  ].filter(Boolean).join("");

  body.innerHTML = `
    <h2 class="modal-title">${escapeHtml(key)}</h2>
    ${sections || "<p class=\"modal-empty\">No fields to display.</p>"}
    <div data-checklist-slot>
      <h3 class="modal-section">Checklist</h3>
      <p class="modal-empty" data-checklist-empty>No checklist yet.</p>
    </div>
    <div data-journal-slot>
      <h3 class="modal-section">Journal</h3>
      <p class="modal-empty" data-journal-empty>No journal yet.</p>
    </div>
    <section class="dsh-modal-log-section" aria-labelledby="dsh-log-heading">
      <h3 id="dsh-log-heading" class="modal-section">Log</h3>
      <div class="dsh-modal-log-status" data-state="loading">
        Loading log&hellip;
      </div>
      <pre class="dsh-modal-log mono hidden" tabindex="0"
           aria-label="Story log tail"></pre>
      <p class="dsh-modal-log-empty hidden">No log available.</p>
    </section>
  `;
  modal.classList.remove("hidden");

  // Fire-and-forget journal fetch. The empty-state placeholder is already
  // in the DOM so the modal opens immediately; the slot is updated by
  // loadStoryJournal once the response arrives (or stays as the empty
  // state on error / unavailable). The race guard in loadStoryJournal
  // ensures a slow prior fetch can't clobber a newly-opened story.
  if (planName) loadStoryJournal(planName, key, body);

  // Same fire-and-forget pattern for the worktree checklist + scratchpad
  // (Tier 0 progress view). Most stories have no checklist (not run under
  // PIPELINE_DECOMPOSE), so the slot usually stays at the empty state.
  if (planName) loadStoryChecklist(planName, key, body);

  // Kick off the tail fetch now that the modal is shown. The fetch
  // resolves into the placeholder by class — if the user opens a different
  // story before the fetch lands, we walk away (loadStoryLog guards on
  // modal.dataset.plan/story still matching).
  loadStoryLog();
}

// Asynchronously load /api/plans/{plan}/stories/{key}/log into the Log
// section of the currently-open modal.
//
// Why this is async instead of inlined: the log file can be tens of KB
// and we don't want to block the modal rendering on it. We render a
// "Loading log…" placeholder synchronously, then swap in the tail (or
// the empty state) when the fetch resolves.
//
// Resilience contract (matches the backend): any fetch error or
// non-200 response renders the empty state. The endpoint itself never
// returns 500 for "log gone", so in practice the error path here is
// only reachable if the network fails; we surface "No log available"
// in both cases rather than a misleading error message, because the
// user's question is "is there a log" and the answer is the same.
//
// We swallow JSON/parse failures silently — a partial response from a
// flaky proxy should not blank the modal.
async function loadStoryLog() {
  const modal = document.getElementById("story-modal");
  const plan = modal.dataset.plan;
  const key = modal.dataset.story;
  if (!plan || !key) return;

  const section = modal.querySelector(".dsh-modal-log-section");
  const statusEl = modal.querySelector(".dsh-modal-log-status");
  const tailEl = modal.querySelector(".dsh-modal-log");
  const emptyEl = modal.querySelector(".dsh-modal-log-empty");
  if (!section || !statusEl || !tailEl || !emptyEl) return;

  const url = `/api/plans/${encodeURIComponent(plan)}`
    + `/stories/${encodeURIComponent(key)}/log?lines=200`;

  let body;
  try {
    const res = await fetch(url);
    if (!res.ok) {
      // Endpoint gone or the plan/story was renamed between modal open
      // and fetch: render empty state, don't raise.
      body = { available: false, lines: [] };
    } else {
      body = await res.json();
    }
  } catch {
    body = { available: false, lines: [] };
  }

  // Stale response guard: if the user closed or navigated the modal
  // (or opened another story) before this fetch resolved, do nothing.
  if (modal.dataset.plan !== plan || modal.dataset.story !== key) return;
  if (modal.classList.contains("hidden")) return;

  const isAvailable = !!(body && body.available);
  const lines = (body && Array.isArray(body.lines)) ? body.lines : [];

  if (!isAvailable || lines.length === 0) {
    // available:false OR available:true but the file was zero bytes:
    // both render the same "No log available" empty state. The user
    // asked "is there a log" and the answer is no.
    statusEl.classList.add("hidden");
    tailEl.classList.add("hidden");
    emptyEl.classList.remove("hidden");
    section.dataset.state = "empty";
    return;
  }

  // Escape per-line so binary garbage / &<> / replacement chars render
  // as text instead of breaking the page. Newline preserved by the
  // surrounding <pre>.
  const text = lines.map((l) => escapeHtml(String(l))).join("\n");
  tailEl.textContent = text;
  statusEl.classList.add("hidden");
  emptyEl.classList.add("hidden");
  tailEl.classList.remove("hidden");
  section.dataset.state = "ready";
}

function hideStoryModal() {
  const modal = document.getElementById("story-modal");
  // Invalidate any in-flight log fetch so it can't write into a hidden
  // modal that the user just closed.
  delete modal.dataset.plan;
  delete modal.dataset.story;
  modal.classList.add("hidden");
}

// Format a fractional rate (0..1, or 0.0 from the backend when dispatched
// count is zero) as a percentage string. We never want to render "NaN%" or
// "undefined%" — a denominator of 0 must still produce "0%".
function fmtPct(rate) {
  const v = Number(rate);
  if (!Number.isFinite(v)) return "0%";
  return `${Math.round(v * 100)}%`;
}

// Render the fleet Overview landing view into #plan-detail. This is the
// default view whenever no plan is selected and is also what selectOverview
// navigates back to from a selected plan.
//
// `plansPayload` is the body of /api/plans ({plans: [...]}) and
// `healthPayload` is the body of /api/dispatch_health ({totals, per_plan}).
// Both are passed in (rather than re-fetched) so renderOverview can be
// unit-tested by callers that already have the JSON in hand.
//
// Empty/zero payloads must render cleanly — no NaN, no throws — so the
// dashboard looks sensible before any plan has shipped a story.
function renderOverview(plansPayload, healthPayload) {
  const section = document.getElementById("plan-detail");
  if (!section) return;

  const plans = (plansPayload && Array.isArray(plansPayload.plans))
    ? plansPayload.plans
    : [];
  const health = healthPayload || {};
  const withAcc = health.with_acceptance
    || { dispatched: 0, done: 0, escalated: 0, stories: 0,
         escalation_rate: 0.0, success_rate: 0.0 };
  const withoutAcc = health.without_acceptance
    || { dispatched: 0, done: 0, escalated: 0, stories: 0,
         escalation_rate: 0.0, success_rate: 0.0 };

  // Fleet totals. Every count is total-stories-derived (the story_count
  // field), so a plan with no stories simply contributes 0 to totals and
  // an empty status_counts object — the breakdown bar handles missing
  // keys by treating them as zero.
  let totalPlans = plans.length;
  let totalStories = 0;
  let totalDone = 0;
  const statusTotals = Object.create(null);
  for (const plan of plans) {
    const sc = plan.status_counts || {};
    totalStories += plan.story_count || 0;
    totalDone += sc.done || 0;
    for (const [k, v] of Object.entries(sc)) {
      statusTotals[k] = (statusTotals[k] || 0) + (v || 0);
    }
  }

  // Per-status breakdown bar — a single horizontal bar split into the
  // STATUS_COLUMNS, each segment proportional to its share of totalStories.
  // Zero totalStories -> an empty-state placeholder rather than a 0-width
  // bar with bogus percentages.
  let breakdownBarHtml;
  if (totalStories === 0) {
    breakdownBarHtml =
      `<div class="overview-empty">No stories yet.</div>`;
  } else {
    const segments = STATUS_COLUMNS
      .map((s) => {
        const n = statusTotals[s] || 0;
        if (!n) return "";
        const pct = (n / totalStories) * 100;
        return `<div class="overview-bar-seg" data-status="${s}"
            style="width:${pct.toFixed(2)}%;background:var(--c-${s})"
            title="${s}: ${n}"></div>`;
      })
      .join("");
    const legend = STATUS_COLUMNS
      .filter((s) => statusTotals[s])
      .map((s) => `<span class="overview-legend-item">
          <span class="overview-legend-dot" style="background:var(--c-${s})"></span>
          ${escapeHtml(s)} ${statusTotals[s]}
        </span>`)
      .join("");
    breakdownBarHtml = `
      <div class="overview-bar">${segments}</div>
      <div class="overview-legend">${legend}</div>
    `;
  }

  // Compact plan list — every plan with done/total and a paused flag.
  const planListHtml = plans.length
    ? plans.map((plan) => {
        const total = plan.story_count || 0;
        const done = (plan.status_counts && plan.status_counts.done) || 0;
        const pausedTag = plan.paused
          ? ` <span class="plan-paused">paused</span>`
          : "";
        return `
        <li class="overview-plan-row" data-plan="${escapeHtml(plan.name)}">
          <span class="overview-plan-name">${escapeHtml(plan.name)}</span>
          <span class="overview-plan-meta">${done}/${total}${pausedTag}</span>
        </li>`;
      }).join("")
    : `<li class="overview-empty">No plans yet.</li>`;

  section.innerHTML = `
    <div class="overview">
      <h2 class="overview-title">Fleet Overview</h2>

      <section class="overview-section overview-totals">
        <div class="overview-stat">
          <div class="overview-stat-value">${totalPlans}</div>
          <div class="overview-stat-label">plans</div>
        </div>
        <div class="overview-stat">
          <div class="overview-stat-value">${totalStories}</div>
          <div class="overview-stat-label">stories</div>
        </div>
        <div class="overview-stat">
          <div class="overview-stat-value">${totalDone}</div>
          <div class="overview-stat-label">done</div>
        </div>
      </section>

      <section class="overview-section">
        <h3 class="overview-section-title">Status breakdown</h3>
        ${breakdownBarHtml}
      </section>

      <section class="overview-section overview-health">
        <h3 class="overview-section-title">Dispatch health by acceptance</h3>
        <p class="overview-section-help">
          Stories are split by whether their plan carried an
          <code>acceptance</code> block at dispatch time. Watch the
          <em>with_acceptance</em> rate trend down relative to
          <em>without_acceptance</em> &mdash; that's the Fix #1 oracle paying off.
        </p>
        <div class="overview-stat-cards">
          <div class="overview-stat-card">
            <div class="overview-stat-card-title">with acceptance</div>
            <div class="overview-stat-card-metric">
              <span class="overview-stat-card-num">${fmtPct(withAcc.escalation_rate)}</span>
              <span class="overview-stat-card-kind">escalation</span>
            </div>
            <div class="overview-stat-card-metric">
              <span class="overview-stat-card-num">${fmtPct(withAcc.success_rate)}</span>
              <span class="overview-stat-card-kind">success</span>
            </div>
            <div class="overview-stat-card-foot">
              ${withAcc.dispatched || 0} dispatched &middot;
              ${withAcc.escalated || 0} escalated &middot;
              ${withAcc.done || 0} done
            </div>
          </div>
          <div class="overview-stat-card">
            <div class="overview-stat-card-title">without acceptance</div>
            <div class="overview-stat-card-metric">
              <span class="overview-stat-card-num">${fmtPct(withoutAcc.escalation_rate)}</span>
              <span class="overview-stat-card-kind">escalation</span>
            </div>
            <div class="overview-stat-card-metric">
              <span class="overview-stat-card-num">${fmtPct(withoutAcc.success_rate)}</span>
              <span class="overview-stat-card-kind">success</span>
            </div>
            <div class="overview-stat-card-foot">
              ${withoutAcc.dispatched || 0} dispatched &middot;
              ${withoutAcc.escalated || 0} escalated &middot;
              ${withoutAcc.done || 0} done
            </div>
          </div>
        </div>
      </section>

      <section class="overview-section">
        <h3 class="overview-section-title">Plans</h3>
        <ul class="overview-plan-list">${planListHtml}</ul>
      </section>
    </div>
  `;
}

// Navigate back to the fleet Overview landing view. Clears the selected
// plan and re-renders the sidebar so the Overview item shows as active.
function selectOverview() {
  state.selectedPlan = null;
  updateHash();
  const nav = document.getElementById("plan-list");
  if (nav) {
    for (const child of nav.children) {
      const isOverview = child.dataset && child.dataset.overview === "true";
      child.classList.toggle("active", isOverview);
    }
  }
  // Render with whatever data we already have in state so the click is
  // instant. The next refresh() will swap in fresh numbers.
  renderOverview(
    state.lastPlans || { plans: [] },
    state.lastHealth || null,
  );
}

async function selectPlan(name) {
  state.selectedPlan = name;
  updateHash();
  await refresh();
}

function renderUsage(usage) {
  const banner = document.getElementById("usage-banner");
  if (!usage || !usage.available) {
    banner.classList.add("hidden");
    return;
  }
  banner.classList.remove("hidden");
  if (usage.gate_blind) {
    // Failing open: spend is unguarded. Make it impossible to miss.
    banner.classList.add("blind");
    const fails = usage.consecutive_parse_failures || 0;
    banner.innerHTML =
      `&#9888; <strong>Claude usage gate is BLIND</strong> — the usage probe has ` +
      `been unparseable (${fails} consecutive failures) since ` +
      `${escapeHtml(usage.blind_since || "?")}, so the gate is failing OPEN and ` +
      `spend is unguarded. Check the <code>claude -p /cost</code> output / poller.`;
  } else {
    banner.classList.remove("blind");
    const s = usage.session_pct, w = usage.week_pct;
    const paused = usage.paused ? " &middot; <strong>PAUSED</strong>" : "";
    banner.innerHTML =
      `Usage gate: session ${s}% &middot; week ${w}%${paused} ` +
      `<span class="muted">(measured ${escapeHtml(usage.measured_at || "?")})</span>`;
  }
}

async function refresh() {
  try {
    renderUsage(await fetchJson("/api/usage"));
  } catch {
    /* usage state unavailable; leave the banner as-is */
  }

  const plansUrl = state.showArchived ? "/api/plans?include_archived=true" : "/api/plans";
  const { plans } = await fetchJson(plansUrl);
  state.lastPlans = { plans };
  renderPlanList(plans);

  // Fleet dispatch health is consumed by the Overview landing view. Fetch
  // it every refresh so the headline rates stay live alongside the plan
  // list. Failure here must not block the rest of the refresh — we
  // gracefully fall back to all-zeros on the Overview.
  let health = null;
  try {
    health = await fetchJson("/api/dispatch_health");
  } catch {
    health = null;
  }
  state.lastHealth = health;

  if (state.selectedPlan) {
    try {
      const plan = await fetchJson(`/api/plans/${encodeURIComponent(state.selectedPlan)}`);
      renderPlanDetail(plan);
    } catch {
      state.selectedPlan = null;
    }
  } else {
    // No plan selected -> render the fleet Overview into #plan-detail.
    renderOverview({ plans }, health);
  }

  document.getElementById("last-updated").textContent =
    `updated ${new Date().toLocaleTimeString()}`;

  // Visible liveness: a brief indicator flash on each successful refresh,
  // gated by the auto-refresh checkbox so manual, indicator-free refreshes
  // remain possible while polling is disabled.
  if (state.pollHandle) flashRefreshIndicator();
}

function startPolling() {
  if (state.pollHandle) clearInterval(state.pollHandle);
  state.pollHandle = setInterval(refresh, 4000);
}

function stopPolling() {
  if (state.pollHandle) clearInterval(state.pollHandle);
  state.pollHandle = null;
}

// Pause polling while the tab is hidden so we don't burn requests / re-render
// DOM that nobody is looking at. Resume on visibilitychange, but only if the
// user has auto-refresh enabled — visibility never overrides the checkbox.
function syncPollingWithVisibility() {
  const auto = document.getElementById("auto-refresh");
  if (!auto || !auto.checked) return;
  if (document.hidden) {
    stopPolling();
  } else if (!state.pollHandle) {
    startPolling();
    // Catch up on one immediate refresh so the user sees fresh data the
    // moment they return to the tab instead of waiting up to 4s for the
    // next tick.
    refresh();
  }
}

document.getElementById("story-modal-close").addEventListener("click", hideStoryModal);
document.getElementById("story-modal-body").addEventListener("click", handleCopyClick);
document.getElementById("story-modal").addEventListener("click", (e) => {
  // Click on the backdrop (outside the modal-content) closes the modal.
  if (e.target.id === "story-modal") hideStoryModal();
});
document.getElementById("auto-refresh").addEventListener("change", (e) => {
  if (e.target.checked) {
    // Respect the tab-hidden state on initial enable: don't start polling
    // into a hidden tab just because the user toggled the checkbox.
    if (document.hidden) return;
    startPolling();
  } else {
    stopPolling();
  }
});

// Pause / resume the polling loop around tab visibility. visibilitychange
// fires on tab switch, minimize, and on some browsers when the window
// loses focus to the OS — exactly the moments we want to stop polling.
document.addEventListener("visibilitychange", syncPollingWithVisibility);

// === Theme toggle =========================================================
// Persists choice in localStorage under THEME_KEY. Defaults to "dark" when
// unset/empty. Wrapped in try/catch so a locked-down browser (or any
// document without Storage permission) doesn't break the page.
const THEME_KEY = "pipeline-dashboard-theme";
const DEFAULT_THEME = "dark";
const VALID_THEMES = new Set(["dark", "light"]);

function readStoredTheme() {
  try {
    const raw = localStorage.getItem(THEME_KEY);
    if (!raw) return DEFAULT_THEME;
    const value = String(raw).trim().toLowerCase();
    return VALID_THEMES.has(value) ? value : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

function writeStoredTheme(theme) {
  try {
    localStorage.setItem(THEME_KEY, theme);
  } catch {
    /* localStorage unavailable; theme simply won't persist */
  }
}

function applyTheme(theme) {
  const t = VALID_THEMES.has(theme) ? theme : DEFAULT_THEME;
  document.documentElement.dataset.theme = t;
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    btn.setAttribute("aria-pressed", t === "light" ? "true" : "false");
    btn.title = t === "dark" ? "Switch to light theme" : "Switch to dark theme";
  }
}

function initTheme() {
  applyTheme(readStoredTheme());
}

function toggleTheme() {
  const current = document.documentElement.dataset.theme === "light" ? "light" : "dark";
  const next = current === "light" ? "dark" : "light";
  applyTheme(next);
  writeStoredTheme(next);
}

(function wireThemeToggle() {
  const btn = document.getElementById("theme-toggle");
  if (btn) btn.addEventListener("click", toggleTheme);
})();

initTheme();
loadFilters();
// Apply the URL hash (if any) before the first refresh — hash wins over
// localStorage. Also wire the browser's hashchange event so back/forward
// re-selects plans / re-applies filters without a full reload.
applyHashToState();
window.addEventListener("hashchange", applyHashToState);
refresh();
startPolling();

// Expose helpers for node-based smoke tests. Guarded so the file still works
// as a plain browser <script>.
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    capturePlanDetailState, restorePlanDetailState, flashRefreshIndicator,
    startPolling, stopPolling, syncPollingWithVisibility, renderPlanDetail,
    showStoryModal, _renderStoryModalBody, handleCopyClick,
    renderOverview, selectOverview, refresh, state,
    renderNotifications,
    renderChecklist,
  };
}
