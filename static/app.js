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

const FILTERS_KEY = "pipeline-dashboard-filters";

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

function defaultFilters() {
  return {
    statuses: [...STATUS_COLUMNS], // enabled statuses; default = all
    personas: [], // [] = no persona filter (show all)
    risks: [], // [] = no risk filter (show all)
    sort: "key", // "key" | "risk" | "activity"
  };
}

const state = {
  selectedPlan: null,
  pollHandle: null,
  filters: defaultFilters(),
};

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

function renderPlanList(plans) {
  const nav = document.getElementById("plan-list");
  nav.innerHTML = "";
  for (const plan of plans) {
    const div = document.createElement("div");
    div.className = "plan-item" + (plan.name === state.selectedPlan ? " active" : "");
    const total = plan.story_count;
    const done = plan.status_counts.done || 0;
    div.innerHTML = `
      <div class="plan-name">${escapeHtml(plan.name)}</div>
      <div class="plan-meta">${done}/${total} done${plan.paused ? ' <span class="plan-paused">paused</span>' : ""}</div>
    `;
    div.addEventListener("click", () => selectPlan(plan.name));
    nav.appendChild(div);
  }
}

function riskRank(story) {
  return RISK_RANK[story.risk] || 0;
}

function activityScore(story) {
  return (story.dispatch_attempts || 0)
    + (story.rework_attempts || 0)
    + (story.merge_attempts || 0);
}

// Persona/risk-filter the cards of one status column, then sort per state.filters.sort.
// Status filtering happens at the column level in renderBoard, not here.
function applyFilters(entries) {
  const { personas, risks, sort } = state.filters;
  const filtered = entries.filter(([, s]) =>
    (personas.length === 0 || personas.includes(s.persona))
    && (risks.length === 0 || risks.includes(s.risk)));

  const comparators = {
    key: ([a], [b]) => a.localeCompare(b, undefined, { numeric: true }),
    risk: ([, a], [, b]) => riskRank(b) - riskRank(a),
    activity: ([, a], [, b]) => activityScore(b) - activityScore(a),
  };
  filtered.sort(comparators[sort] || comparators.key);
  return filtered;
}

function renderBoard(stories) {
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
        return `
        <div class="${classes.join(" ")}" style="--badge-color: var(--c-${status})" data-key="${escapeHtml(key)}">
          <div class="card-key">${escapeHtml(key)}</div>
          <div class="card-summary">${escapeHtml(s.summary || "(no summary)")}</div>
          ${ageLabel ? `<div class="card-age${stale ? " stale" : ""}">${escapeHtml(ageLabel)}</div>` : ""}
        </div>
      `;
      }).join("");
      return `
        <div class="column">
          <div class="column-header">
            <span>${status}</span>
            <span class="badge" style="--badge-color: var(--c-${status})">${entries.length}</span>
          </div>
          <div class="column-body">${cards}</div>
        </div>
      `;
    }).join("");

  if (!columns) {
    return '<div class="board"><p class="empty-state">No statuses selected.</p></div>';
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

  const { filters } = state;
  const groups = [
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
  groups.push(`<div class="filter-group"><span class="filter-group-label">Sort</span>${
    SORT_OPTIONS.map(([v, label]) => chip("sort", v, label, filters.sort === v)).join("")
  }</div>`);

  return `<div class="filter-bar">${groups.join("")}`
    + `<button class="filter-reset" data-action="reset">Reset</button></div>`;
}

function renderNotifications(lines) {
  if (!lines.length) return '<p class="empty-state">No notifications yet.</p>';
  return lines.slice().reverse()
    .map((line) => `<div class="log-line">${escapeHtml(line)}</div>`)
    .join("");
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
        <div class="panel-body">${renderNotifications(plan.notifications)}</div>
      </div>
      <div class="panel">
        <h3>Overlord decisions</h3>
        <div class="panel-body">${renderDecisions(plan.decisions)}</div>
      </div>
    </div>
  `;

  section.querySelectorAll(".card").forEach((card) => {
    card.addEventListener("click", () => showStoryModal(plan.stories[card.dataset.key], card.dataset.key));
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
      renderPlanDetail(plan);
    });
  });
  const resetBtn = section.querySelector(".filter-reset");
  if (resetBtn) {
    resetBtn.addEventListener("click", () => {
      state.filters = defaultFilters();
      saveFilters();
      renderPlanDetail(plan);
    });
  }
}

function showStoryModal(story, key) {
  const modal = document.getElementById("story-modal");
  const body = document.getElementById("story-modal-body");
  const fields = [
    ["Key", key],
    ["Status", story.status],
    ["Summary", story.summary],
    ["Persona", story.persona],
    ["Model", story.model],
    ["Risk", story.risk],
    ["Dependencies", (story.dependencies || []).join(", ") || "(none)"],
    ["Worktree", story.worktree],
    ["PID", story.pid],
    ["PR URL", story.pr_url],
    ["Review verdict", story.review_verdict],
    ["Review feedback", story.review_feedback],
    ["Dispatch attempts", story.dispatch_attempts],
    ["Rework attempts", story.rework_attempts],
    ["Merge attempts", story.merge_attempts],
    ["Dispatch error", story.dispatch_error],
    ["Merge error", story.merge_error],
    ["Parked reason", story.parked_reason],
    ["Interrupted at", story.interrupted_at],
    ["Last commit", story.last_commit],
  ].filter(([, v]) => v !== undefined && v !== null && v !== "");

  // Lifecycle section: derived last_activity + client-side age label.
  // Show it only when we actually have a timestamp so we don't render
  // an empty heading for stories with no activity signal.
  const ageLabel = ageLabelFor(story.last_activity);
  const lifecycleRows = story.last_activity
    ? [
        ["Last activity", story.last_activity],
        ["Age", ageLabel || "just now"],
      ]
    : [];
  const lifecycleHtml = lifecycleRows.length
    ? `<h3 class="modal-section">Lifecycle</h3>
       <dl>
         ${lifecycleRows.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}
       </dl>`
    : "";

  body.innerHTML = `
    <h2>${escapeHtml(key)}</h2>
    <dl>
      ${fields.map(([label, value]) => `<dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value)}</dd>`).join("")}
    </dl>
    ${lifecycleHtml}
  `;
  modal.classList.remove("hidden");
}

function hideStoryModal() {
  document.getElementById("story-modal").classList.add("hidden");
}

async function selectPlan(name) {
  state.selectedPlan = name;
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

  const { plans } = await fetchJson("/api/plans");
  renderPlanList(plans);

  if (state.selectedPlan) {
    try {
      const plan = await fetchJson(`/api/plans/${encodeURIComponent(state.selectedPlan)}`);
      renderPlanDetail(plan);
    } catch {
      state.selectedPlan = null;
    }
  }

  document.getElementById("last-updated").textContent =
    `updated ${new Date().toLocaleTimeString()}`;
}

function startPolling() {
  if (state.pollHandle) clearInterval(state.pollHandle);
  state.pollHandle = setInterval(refresh, 4000);
}

function stopPolling() {
  if (state.pollHandle) clearInterval(state.pollHandle);
  state.pollHandle = null;
}

document.getElementById("story-modal-close").addEventListener("click", hideStoryModal);
document.getElementById("story-modal").addEventListener("click", (e) => {
  if (e.target.id === "story-modal") hideStoryModal();
});
document.getElementById("auto-refresh").addEventListener("change", (e) => {
  e.target.checked ? startPolling() : stopPolling();
});

loadFilters();
refresh();
startPolling();
